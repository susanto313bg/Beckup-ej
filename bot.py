import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -----------------------------------------------------------------------------
# Konfigurasi
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-backup-catalog")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
DB_PATH = "data.db"

if not BOT_TOKEN or not ADMIN_ID_RAW:
    raise RuntimeError("BOT_TOKEN dan ADMIN_ID wajib diisi sebagai environment variable.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID harus berupa angka Telegram user ID.") from exc

# State ConversationHandler.
REG_TID, REG_PHOTOS, REG_DESC = range(3)
EDIT_TID, EDIT_CHOICE, REPLACE_PHOTOS, ADD_PHOTOS, EDIT_DESC = range(3, 8)
DELETE_TID, DELETE_CONFIRM = range(8, 10)

TID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,30}$")
SETTINGS_ID = 1

DEFAULT_SETTINGS = {
    "maintenance_mode": False,
    "registration_enabled": True,
    "edit_enabled": True,
}


# -----------------------------------------------------------------------------
# SQLite
# -----------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Membuka koneksi singkat per operasi agar aman untuk service polling."""
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tid_records (
                tid TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                updated_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (length(tid) BETWEEN 1 AND 30),
                CHECK (length(trim(description)) > 0)
            );

            CREATE TABLE IF NOT EXISTS tid_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tid TEXT NOT NULL,
                file_id TEXT NOT NULL,
                photo_order INTEGER NOT NULL,
                uploaded_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (tid) REFERENCES tid_records(tid) ON DELETE CASCADE,
                UNIQUE (tid, photo_order)
            );

            CREATE INDEX IF NOT EXISTS idx_tid_photos_order
                ON tid_photos (tid, photo_order, id);

            CREATE TABLE IF NOT EXISTS bot_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                maintenance_mode INTEGER NOT NULL DEFAULT 0 CHECK (maintenance_mode IN (0, 1)),
                registration_enabled INTEGER NOT NULL DEFAULT 1 CHECK (registration_enabled IN (0, 1)),
                edit_enabled INTEGER NOT NULL DEFAULT 1 CHECK (edit_enabled IN (0, 1)),
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO bot_settings
                (id, maintenance_mode, registration_enabled, edit_enabled, updated_at)
            VALUES (1, 0, 1, 1, datetime('now'));
            """
        )


def get_settings() -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT maintenance_mode, registration_enabled, edit_enabled, updated_at "
            "FROM bot_settings WHERE id = ?",
            (SETTINGS_ID,),
        ).fetchone()
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {
        "maintenance_mode": bool(row["maintenance_mode"]),
        "registration_enabled": bool(row["registration_enabled"]),
        "edit_enabled": bool(row["edit_enabled"]),
        "updated_at": row["updated_at"],
    }


def set_setting(name: str, value: bool) -> dict:
    if name not in DEFAULT_SETTINGS:
        raise ValueError(f"Setting tidak dikenal: {name}")
    with get_db() as db:
        db.execute(
            f"UPDATE bot_settings SET {name} = ?, updated_at = ? WHERE id = ?",
            (1 if value else 0, utc_now(), SETTINGS_ID),
        )
    return get_settings()


def find_tid(tid: str) -> Optional[sqlite3.Row]:
    with get_db() as db:
        return db.execute(
            "SELECT tid, description, created_by, updated_by, created_at, updated_at "
            "FROM tid_records WHERE LOWER(tid) = LOWER(?)",
            (tid,),
        ).fetchone()


def get_photos(tid: str) -> list[sqlite3.Row]:
    with get_db() as db:
        return db.execute(
            "SELECT id, file_id, photo_order, uploaded_by, created_at "
            "FROM tid_photos WHERE LOWER(tid) = LOWER(?) ORDER BY photo_order, id",
            (tid,),
        ).fetchall()


def create_tid(tid: str, description: str, file_ids: list[str], uid: int) -> None:
    now = utc_now()
    tid_upper = tid.upper()
    with get_db() as db:
        db.execute(
            "INSERT INTO tid_records "
            "(tid, description, created_by, updated_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid_upper, description, uid, uid, now, now),
        )
        db.executemany(
            "INSERT INTO tid_photos "
            "(tid, file_id, photo_order, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?)",
            [(tid_upper, file_id, index, uid, now) for index, file_id in enumerate(file_ids, start=1)],
        )


def replace_photos(tid: str, file_ids: list[str], uid: int) -> None:
    now = utc_now()
    record = find_tid(tid)
    actual_tid = record["tid"] if record else tid.upper()
    with get_db() as db:
        db.execute("DELETE FROM tid_photos WHERE LOWER(tid) = LOWER(?)", (actual_tid,))
        db.executemany(
            "INSERT INTO tid_photos "
            "(tid, file_id, photo_order, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?)",
            [(actual_tid, file_id, index, uid, now) for index, file_id in enumerate(file_ids, start=1)],
        )
        db.execute(
            "UPDATE tid_records SET updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (uid, now, actual_tid),
        )


def append_photos(tid: str, file_ids: list[str], uid: int) -> None:
    existing_count = len(get_photos(tid))
    now = utc_now()
    record = find_tid(tid)
    actual_tid = record["tid"] if record else tid.upper()
    with get_db() as db:
        db.executemany(
            "INSERT INTO tid_photos "
            "(tid, file_id, photo_order, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (actual_tid, file_id, existing_count + index, uid, now)
                for index, file_id in enumerate(file_ids, start=1)
            ],
        )
        db.execute(
            "UPDATE tid_records SET updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (uid, now, actual_tid),
        )


def update_description(tid: str, description: str, uid: int) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE tid_records SET description = ?, updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (description, uid, utc_now(), tid),
        )


def delete_tid(tid: str) -> bool:
    with get_db() as db:
        cursor = db.execute("DELETE FROM tid_records WHERE LOWER(tid) = LOWER(?)", (tid,))
        return cursor.rowcount > 0


def get_statistics() -> tuple[int, int]:
    with get_db() as db:
        tid_count = db.execute("SELECT COUNT(*) AS total FROM tid_records").fetchone()["total"]
        photo_count = db.execute("SELECT COUNT(*) AS total FROM tid_photos").fetchone()["total"]
    return int(tid_count), int(photo_count)


# -----------------------------------------------------------------------------
# Utilitas Telegram dan guard akses
# -----------------------------------------------------------------------------
def normalize_tid(value: str) -> Optional[str]:
    value = (value or "").strip().upper()
    return value if TID_PATTERN.fullmatch(value) else None


def get_user_id(update: Update) -> int:
    return update.effective_user.id if update.effective_user else 0


def is_admin(update: Update) -> bool:
    return get_user_id(update) == ADMIN_ID


async def block_for_maintenance(update: Update) -> bool:
    if is_admin(update) or not get_settings()["maintenance_mode"]:
        return False
    text = "Bot sedang dalam maintenance, silakan coba lagi nanti."
    if update.effective_message:
        await update.effective_message.reply_text(text)
    elif update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    return True


async def block_if_disabled(update: Update, setting: str, message: str) -> bool:
    if await block_for_maintenance(update):
        return True
    if is_admin(update) or get_settings()[setting]:
        return False
    if update.effective_message:
        await update.effective_message.reply_text(message)
    elif update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    return True


async def send_tid_data(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: str) -> None:
    record = find_tid(tid)
    if record is None:
        await update.effective_message.reply_text(
            f"TID <b>{tid}</b> belum terdaftar.\n\nApakah ingin mendaftarkannya?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Daftarkan TID ini", callback_data=f"register:{tid}")]]
            ),
        )
        return

    actual_tid = record["tid"]
    photos = get_photos(actual_tid)
    description = record["description"] or "(Belum ada keterangan)"
    await update.effective_message.reply_text(
        f"<b>Data TID</b>\n"
        f"TID: <code>{actual_tid}</code>\n"
        f"Jumlah foto: {len(photos)}\n"
        f"Keterangan: {description}",
        parse_mode=ParseMode.HTML,
    )

    if not photos:
        await update.effective_message.reply_text("Belum ada foto untuk TID ini.")
        return

    for offset in range(0, len(photos), 10):
        batch = photos[offset:offset + 10]
        media = [InputMediaPhoto(row["file_id"]) for row in batch]
        try:
            await update.effective_message.reply_media_group(media=media)
        except Exception:
            logger.exception("Gagal mengirim media group untuk TID %s", actual_tid)
            for row in batch:
                await update.effective_message.reply_photo(photo=row["file_id"])


# -----------------------------------------------------------------------------
# Panel admin
# -----------------------------------------------------------------------------
def admin_keyboard(settings: dict) -> InlineKeyboardMarkup:
    maintenance_text = (
        "Nonaktifkan maintenance" if settings["maintenance_mode"] else "Aktifkan maintenance"
    )
    registration_text = (
        "Nonaktifkan pendaftaran"
        if settings["registration_enabled"]
        else "Aktifkan pendaftaran"
    )
    edit_text = "Nonaktifkan edit" if settings["edit_enabled"] else "Aktifkan edit"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(maintenance_text, callback_data="admin:maintenance")],
        [InlineKeyboardButton(registration_text, callback_data="admin:registration")],
        [InlineKeyboardButton(edit_text, callback_data="admin:edit")],
        [InlineKeyboardButton("Lihat statistik", callback_data="admin:stats")],
        [InlineKeyboardButton("Refresh menu", callback_data="admin:refresh")],
    ])


def admin_status(settings: dict) -> str:
    return (
        "<b>Menu Kontrol Admin</b>\n\n"
        f"Mode maintenance: <b>{'AKTIF' if settings['maintenance_mode'] else 'NONAKTIF'}</b>\n"
        f"Pendaftaran TID: <b>{'AKTIF' if settings['registration_enabled'] else 'NONAKTIF'}</b>\n"
        f"Fitur edit: <b>{'AKTIF' if settings['edit_enabled'] else 'NONAKTIF'}</b>"
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak. Perintah ini khusus admin.")
        return
    try:
        settings = get_settings()
        await update.effective_message.reply_text(
            admin_status(settings),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(settings),
        )
    except Exception:
        logger.exception("Gagal membuka panel admin")
        await update.effective_message.reply_text("Panel admin gagal dibuka. Periksa file database.")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Akses ditolak.", show_alert=True)
        return

    await query.answer()
    action = query.data.split(":", 1)[1]
    try:
        settings = get_settings()
        if action == "maintenance":
            settings = set_setting("maintenance_mode", not settings["maintenance_mode"])
        elif action == "registration":
            settings = set_setting("registration_enabled", not settings["registration_enabled"])
        elif action == "edit":
            settings = set_setting("edit_enabled", not settings["edit_enabled"])
        elif action == "stats":
            tid_count, photo_count = get_statistics()
            await query.message.reply_text(
                f"<b>Statistik katalog</b>\n\n"
                f"Jumlah TID terdaftar: <b>{tid_count}</b>\n"
                f"Jumlah foto total: <b>{photo_count}</b>",
                parse_mode=ParseMode.HTML,
            )
        elif action == "refresh":
            settings = get_settings()
        else:
            return

        await query.message.edit_text(
            admin_status(settings),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(settings),
        )
    except Exception:
        logger.exception("Gagal memproses aksi admin %s", action)
        await query.message.reply_text("Aksi admin gagal diproses. Periksa log Railway dan data.db.")


# -----------------------------------------------------------------------------
# Handler umum
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await block_for_maintenance(update):
        return
    await update.effective_message.reply_text(
        "<b>Katalog Backup Transaksi</b>\n\n"
        "Kirim TID (huruf/angka) untuk melihat data.\n"
        "Ketik <b>Daftar</b> untuk menambah, <b>Edit</b> untuk mengubah, "
        "<b>Hapus</b> untuk menghapus data TID, atau <b>Batal</b> untuk membatalkan proses.\n\n"
        "Contoh: <code>B469</code> atau <code>123456</code>",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Proses dibatalkan. Kirim TID atau ketik Daftar/Edit/Hapus kapan saja."
    )
    return ConversationHandler.END


async def search_tid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await block_for_maintenance(update):
        return
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text(
            "Format belum dikenali. Kirim TID (huruf/angka), atau ketik Daftar/Edit/Hapus/Batal."
        )
        return
    try:
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mencari TID %s", tid)
        await update.effective_message.reply_text(
            "Maaf, data belum dapat diambil. Silakan coba lagi."
        )


# -----------------------------------------------------------------------------
# Pendaftaran
# -----------------------------------------------------------------------------
async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Silakan kirim TID baru (huruf/angka).")
    return REG_TID


async def register_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    tid = normalize_tid(query.data.split(":", 1)[1])
    if find_tid(tid) is not None:
        await query.message.reply_text("TID tersebut baru saja didaftarkan oleh pengguna lain.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["tid"] = tid
    context.user_data["photos"] = []
    await query.message.reply_text(
        f"Pendaftaran TID <code>{tid}</code> dimulai.\n\n"
        "Kirim foto disk/CD satu per satu, lalu ketik <b>Selesai</b>.",
        parse_mode=ParseMode.HTML,
    )
    return REG_PHOTOS


async def register_tid_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        context.user_data.clear()
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi. Silakan kirim ulang.")
        return REG_TID
    if find_tid(tid) is not None:
        await update.effective_message.reply_text(
            "TID sudah terdaftar. Kirim TID lain, atau ketik Batal."
        )
        return REG_TID
    context.user_data["tid"] = tid
    context.user_data["photos"] = []
    await update.effective_message.reply_text(
        f"TID <code>{tid}</code> diterima. Kirim foto, lalu ketik <b>Selesai</b>.",
        parse_mode=ParseMode.HTML,
    )
    return REG_PHOTOS


async def register_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.setdefault("photos", []).append(update.effective_message.photo[-1].file_id)
    await update.effective_message.reply_text(
        f"Foto ke-{len(context.user_data['photos'])} diterima. "
        "Kirim lagi atau ketik Selesai."
    )
    return REG_PHOTOS


async def register_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        context.user_data.clear()
        return ConversationHandler.END
    if not context.user_data.get("photos"):
        await update.effective_message.reply_text("Minimal kirim satu foto sebelum mengetik Selesai.")
        return REG_PHOTOS
    await update.effective_message.reply_text(
        "Foto selesai. Sekarang kirim keterangan untuk TID tersebut."
    )
    return REG_DESC


async def register_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update,
        "registration_enabled",
        "Fitur pendaftaran TID sedang dinonaktifkan admin.",
    ):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong. Silakan kirim keterangan.")
        return REG_DESC
    tid = context.user_data.get("tid")
    photos = context.user_data.get("photos", [])
    if not tid or not photos:
        context.user_data.clear()
        await update.effective_message.reply_text("Data pendaftaran tidak lengkap. Silakan mulai lagi dengan Daftar.")
        return ConversationHandler.END
    try:
        create_tid(tid, description, photos, get_user_id(update))
        await update.effective_message.reply_text(
            f"Pendaftaran TID <code>{tid}</code> berhasil disimpan.",
            parse_mode=ParseMode.HTML,
        )
        await send_tid_data(update, context, tid)
    except sqlite3.IntegrityError:
        logger.exception("TID duplikat atau konflik saat mendaftarkan %s", tid)
        await update.effective_message.reply_text(
            "TID tersebut sudah didaftarkan oleh pengguna lain. Silakan cari TID tersebut atau gunakan TID lain."
        )
    except Exception:
        logger.exception("Error saat mendaftarkan TID %s", tid)
        await update.effective_message.reply_text("Pendaftaran gagal disimpan. Silakan coba lagi.")
    context.user_data.clear()
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Pengeditan
# -----------------------------------------------------------------------------
async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Kirim TID yang ingin diedit (huruf/angka).")
    return EDIT_TID


async def edit_tid_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi. Silakan kirim ulang.")
        return EDIT_TID
    record = find_tid(tid)
    if record is None:
        await update.effective_message.reply_text("TID tidak ditemukan. Kirim TID lain atau ketik Batal.")
        return EDIT_TID
    actual_tid = record["tid"]
    context.user_data["tid"] = actual_tid
    await update.effective_message.reply_text(
        f"Pilih perubahan untuk TID <code>{actual_tid}</code>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Ubah semua gambar", callback_data="edit:replace")],
            [InlineKeyboardButton("Tambah gambar", callback_data="edit:add")],
            [InlineKeyboardButton("Ubah keterangan", callback_data="edit:description")],
        ]),
    )
    return EDIT_CHOICE


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "description":
        await query.message.reply_text("Kirim keterangan baru untuk TID tersebut.")
        return EDIT_DESC
    context.user_data["photos"] = []
    context.user_data["edit_action"] = action
    instruction = "menggantikan semua gambar lama" if action == "replace" else "menambahkan gambar baru"
    await query.message.reply_text(
        f"Kirim foto yang ingin {instruction}. Setelah selesai, ketik Selesai."
    )
    return REPLACE_PHOTOS if action == "replace" else ADD_PHOTOS


async def edit_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.setdefault("photos", []).append(update.effective_message.photo[-1].file_id)
    await update.effective_message.reply_text(
        f"Foto ke-{len(context.user_data['photos'])} diterima. Kirim lagi atau ketik Selesai."
    )
    return REPLACE_PHOTOS if context.user_data.get("edit_action") == "replace" else ADD_PHOTOS


async def edit_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    photos = context.user_data.get("photos", [])
    tid = context.user_data.get("tid")
    if not photos or not tid:
        await update.effective_message.reply_text("Minimal kirim satu foto sebelum mengetik Selesai.")
        return REPLACE_PHOTOS if context.user_data.get("edit_action") == "replace" else ADD_PHOTOS
    try:
        if find_tid(tid) is None:
            await update.effective_message.reply_text("TID sudah tidak ditemukan. Silakan mulai edit kembali.")
        elif context.user_data.get("edit_action") == "replace":
            replace_photos(tid, photos, get_user_id(update))
            await update.effective_message.reply_text("Semua gambar lama berhasil diganti.")
            await send_tid_data(update, context, tid)
        else:
            append_photos(tid, photos, get_user_id(update))
            await update.effective_message.reply_text("Gambar baru berhasil ditambahkan.")
            await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mengedit foto TID %s", tid)
        await update.effective_message.reply_text("Perubahan gambar gagal disimpan. Silakan coba lagi.")
    context.user_data.clear()
    return ConversationHandler.END


async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    tid = context.user_data.get("tid")
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong. Silakan kirim ulang.")
        return EDIT_DESC
    if not tid or find_tid(tid) is None:
        context.user_data.clear()
        await update.effective_message.reply_text("TID sudah tidak ditemukan. Silakan mulai edit kembali.")
        return ConversationHandler.END
    try:
        update_description(tid, description, get_user_id(update))
        await update.effective_message.reply_text("Keterangan berhasil diperbarui.")
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mengedit keterangan TID %s", tid)
        await update.effective_message.reply_text("Keterangan gagal diperbarui. Silakan coba lagi.")
    context.user_data.clear()
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Penghapusan
# -----------------------------------------------------------------------------
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_for_maintenance(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Kirim TID yang ingin dihapus (huruf/angka).")
    return DELETE_TID


async def delete_tid_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_for_maintenance(update):
        context.user_data.clear()
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi. Silakan kirim ulang.")
        return DELETE_TID

    record = find_tid(tid)
    if record is None:
        await update.effective_message.reply_text("TID tidak ditemukan. Kirim TID lain atau ketik Batal.")
        return DELETE_TID

    actual_tid = record["tid"]
    context.user_data["tid"] = actual_tid
    await update.effective_message.reply_text(
        f"Apakah Anda yakin ingin menghapus TID <code>{actual_tid}</code> beserta semua fotonya?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Ya, Hapus", callback_data="delete:confirm"),
                InlineKeyboardButton("Batal", callback_data="delete:cancel"),
            ]
        ]),
    )
    return DELETE_CONFIRM


async def delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tid = context.user_data.get("tid")

    if action == "confirm" and tid:
        try:
            if delete_tid(tid):
                await query.message.reply_text(f"TID <code>{tid}</code> berhasil dihapus.", parse_mode=ParseMode.HTML)
            else:
                await query.message.reply_text("TID sudah tidak ditemukan atau telah dihapus.")
        except Exception:
            logger.exception("Error saat menghapus TID %s", tid)
            await query.message.reply_text("Gagal menghapus TID. Silakan coba lagi.")
    else:
        await query.message.reply_text("Penghapusan TID dibatalkan.")

    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Terjadi kesalahan teknis. Silakan coba lagi.")


# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------
def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(False).build()
    common_fallbacks = [
        CommandHandler("batal", cancel),
        MessageHandler(filters.Regex(r"(?i)^batal$"), cancel),
    ]

    register_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("daftar", register_command),
            MessageHandler(filters.Regex(r"(?i)^daftar$"), register_command),
            CallbackQueryHandler(register_from_callback, pattern=r"^register:[A-Za-z0-9]+$"),
        ],
        states={
            REG_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_tid_received)],
            REG_PHOTOS: [
                MessageHandler(filters.PHOTO, register_photo_received),
                MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), register_photos_done),
            ],
            REG_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_description_received)],
        },
        fallbacks=common_fallbacks,
        allow_reentry=True,
    )

    edit_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_command),
            MessageHandler(filters.Regex(r"(?i)^edit$"), edit_command),
        ],
        states={
            EDIT_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_tid_received)],
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice, pattern=r"^edit:(replace|add|description)$")],
            REPLACE_PHOTOS: [
                MessageHandler(filters.PHOTO, edit_photo_received),
                MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done),
            ],
            ADD_PHOTOS: [
                MessageHandler(filters.PHOTO, edit_photo_received),
                MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done),
            ],
            EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_description_received)],
        },
        fallbacks=common_fallbacks,
        allow_reentry=True,
    )

    delete_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("hapus", delete_command),
            MessageHandler(filters.Regex(r"(?i)^hapus$"), delete_command),
        ],
        states={
            DELETE_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_tid_received)],
            DELETE_CONFIRM: [CallbackQueryHandler(delete_confirm_callback, pattern=r"^delete:(confirm|cancel)$")],
        },
        fallbacks=common_fallbacks,
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:(maintenance|registration|edit|stats|refresh)$",
        )
    )
    application.add_handler(register_conversation)
    application.add_handler(edit_conversation)
    application.add_handler(delete_conversation)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_tid))
    application.add_error_handler(error_handler)
    return application


if __name__ == "__main__":
    init_db()
    application = build_application()
    logger.info("Bot berjalan dengan SQLite lokal: %s", DB_PATH)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)