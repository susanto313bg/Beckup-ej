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
# Konfigurasi Log & Environment
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
    SUPER_ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID harus berupa angka Telegram user ID.") from exc

# State ConversationHandler
REG_TID, REG_PHOTOS, REG_DESC = range(3)
(
    EDIT_TID,
    EDIT_CHOICE,
    REPLACE_PHOTOS,
    ADD_PHOTOS,
    EDIT_DESC,
    EDIT_SELECT_PHOTO,
    EDIT_CHANGE_PHOTO,
    EDIT_DELETE_PHOTO,
) = range(3, 11)
DELETE_TID, DELETE_CONFIRM = range(11, 13)
ADD_ADMIN_ID, REMOVE_ADMIN_ID = range(13, 15)

TID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,30}$")
SETTINGS_ID = 1

DEFAULT_SETTINGS = {
    "maintenance_mode": False,
    "registration_enabled": True,
    "edit_enabled": True,
}


# -----------------------------------------------------------------------------
# Operasi Database SQLite & Manajemen Admin
# -----------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
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
                FOREIGN KEY (tid) REFERENCES tid_records(tid) ON DELETE CASCADE
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

            CREATE TABLE IF NOT EXISTS admin_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO bot_settings
                (id, maintenance_mode, registration_enabled, edit_enabled, updated_at)
            VALUES (1, 0, 1, 1, datetime('now'));
            """
        )


def add_allowed_admin(user_id: int, added_by: int) -> bool:
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO admin_users (user_id, added_by, added_at) VALUES (?, ?, ?)",
                (user_id, added_by, utc_now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_allowed_admin(user_id: int) -> bool:
    with get_db() as db:
        cursor = db.execute("DELETE FROM admin_users WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0


def get_allowed_admins() -> list[int]:
    with get_db() as db:
        rows = db.execute("SELECT user_id FROM admin_users").fetchall()
        return [row["user_id"] for row in rows]


def get_all_tids() -> list[tuple[str, int]]:
    with get_db() as db:
        query = """
            SELECT r.tid, COUNT(p.id) AS photo_count
            FROM tid_records r
            LEFT JOIN tid_photos p ON LOWER(r.tid) = LOWER(p.tid)
            GROUP BY r.tid
            ORDER BY r.tid ASC
        """
        rows = db.execute(query).fetchall()
        return [(row["tid"], row["photo_count"]) for row in rows]


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


def reorder_photos(db: sqlite3.Connection, tid: str) -> None:
    photos = db.execute(
        "SELECT id FROM tid_photos WHERE LOWER(tid) = LOWER(?) ORDER BY photo_order, id",
        (tid,),
    ).fetchall()
    for idx, photo in enumerate(photos, start=1):
        db.execute("UPDATE tid_photos SET photo_order = ? WHERE id = ?", (idx, photo["id"]))


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


def update_single_photo(photo_id: int, new_file_id: str, uid: int, tid: str) -> None:
    now = utc_now()
    with get_db() as db:
        db.execute(
            "UPDATE tid_photos SET file_id = ?, uploaded_by = ?, created_at = ? WHERE id = ?",
            (new_file_id, uid, now, photo_id),
        )
        db.execute(
            "UPDATE tid_records SET updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (uid, now, tid),
        )


def delete_single_photo(photo_id: int, tid: str, uid: int) -> None:
    now = utc_now()
    with get_db() as db:
        db.execute("DELETE FROM tid_photos WHERE id = ?", (photo_id,))
        reorder_photos(db, tid)
        db.execute(
            "UPDATE tid_records SET updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (uid, now, tid),
        )


def delete_all_photos(tid: str, uid: int) -> None:
    now = utc_now()
    with get_db() as db:
        db.execute("DELETE FROM tid_photos WHERE LOWER(tid) = LOWER(?)", (tid,))
        db.execute(
            "UPDATE tid_records SET updated_by = ?, updated_at = ? WHERE LOWER(tid) = LOWER(?)",
            (uid, now, tid),
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
# Utilitas Telegram & Validasi Otorisasi
# -----------------------------------------------------------------------------
def normalize_tid(value: str) -> Optional[str]:
    value = (value or "").strip().upper()
    return value if TID_PATTERN.fullmatch(value) else None


def get_user_id(update: Update) -> int:
    return update.effective_user.id if update.effective_user else 0


def is_super_admin(update: Update) -> bool:
    return get_user_id(update) == SUPER_ADMIN_ID


def is_admin(update: Update) -> bool:
    uid = get_user_id(update)
    if uid == SUPER_ADMIN_ID:
        return True
    return uid in get_allowed_admins()


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
        if not is_admin(update):
            await update.effective_message.reply_text(
                f"TID <b>{tid}</b> belum terdaftar.",
                parse_mode=ParseMode.HTML,
            )
            return

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
# Panel Admin & Perizinan Akses ID Telegram
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
        [InlineKeyboardButton("Cek TID Terdaftar", callback_data="admin:list_tids")],
        [InlineKeyboardButton(maintenance_text, callback_data="admin:maintenance")],
        [InlineKeyboardButton(registration_text, callback_data="admin:registration")],
        [InlineKeyboardButton(edit_text, callback_data="admin:edit")],
        [InlineKeyboardButton("Tambah Izin Admin", callback_data="admin:add_access")],
        [InlineKeyboardButton("Hapus Izin Admin", callback_data="admin:remove_access")],
        [InlineKeyboardButton("Daftar Admin Diizinkan", callback_data="admin:list_access")],
        [InlineKeyboardButton("Lihat statistik", callback_data="admin:stats")],
        [InlineKeyboardButton("Refresh menu", callback_data="admin:refresh")],
    ])


def admin_status(settings: dict) -> str:
    allowed_count = len(get_allowed_admins())
    return (
        "<b>Menu Kontrol Admin</b>\n\n"
        f"Mode maintenance: <b>{'AKTIF' if settings['maintenance_mode'] else 'NONAKTIF'}</b>\n"
        f"Pendaftaran TID: <b>{'AKTIF' if settings['registration_enabled'] else 'NONAKTIF'}</b>\n"
        f"Fitur edit: <b>{'AKTIF' if settings['edit_enabled'] else 'NONAKTIF'}</b>\n"
        f"Admin tambahan diizinkan: <b>{allowed_count} akun</b>"
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
        await update.effective_message.reply_text("Panel admin gagal dibuka.")


async def list_tids_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak. Fitur ini khusus Admin.")
        return

    tids = get_all_tids()
    if not tids:
        await update.effective_message.reply_text("Belum ada TID yang terdaftar di database.")
        return

    lines = [f"<b>Daftar TID Terdaftar ({len(tids)} total):</b>\n"]
    for idx, (tid, photo_count) in enumerate(tids, start=1):
        lines.append(f"{idx}. <code>{tid}</code> — <i>({photo_count} foto)</i>")

    full_text = "\n".join(lines)

    # Kirim secara bertahap jika teks melebihi batas 4000 karakter Telegram
    if len(full_text) <= 4000:
        await update.effective_message.reply_text(full_text, parse_mode=ParseMode.HTML)
    else:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Akses ditolak.", show_alert=True)
        return None

    await query.answer()
    action = query.data.split(":", 1)[1]
    try:
        settings = get_settings()
        if action == "list_tids":
            await list_tids_command(update, context)
            return None
        elif action == "maintenance":
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
        elif action == "add_access":
            await query.message.reply_text("Kirim ID Telegram (angka) user yang ingin diberi akses Admin.")
            return ADD_ADMIN_ID
        elif action == "remove_access":
            await query.message.reply_text("Kirim ID Telegram (angka) user yang ingin dicabut akses Adminnya.")
            return REMOVE_ADMIN_ID
        elif action == "list_access":
            admins = get_allowed_admins()
            if not admins:
                text = "Belum ada admin tambahan yang terdaftar."
            else:
                list_str = "\n".join([f"• <code>{aid}</code>" for aid in admins])
                text = f"<b>Daftar User ID Admin Tambahan:</b>\n{list_str}"
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)
        elif action == "refresh":
            settings = get_settings()
        else:
            return None

        await query.message.edit_text(
            admin_status(settings),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(settings),
        )
    except Exception:
        logger.exception("Gagal memproses aksi admin %s", action)
        await query.message.reply_text("Aksi admin gagal diproses.")
    return None


async def add_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak.")
        return ConversationHandler.END

    raw_text = (update.effective_message.text or "").strip()
    try:
        target_id = int(raw_text)
    except ValueError:
        await update.effective_message.reply_text("ID Telegram harus berupa angka. Silakan coba lagi atau ketik Batal.")
        return ADD_ADMIN_ID

    if target_id == SUPER_ADMIN_ID:
        await update.effective_message.reply_text("User ID ini adalah Super Admin utama.")
        return ConversationHandler.END

    if add_allowed_admin(target_id, get_user_id(update)):
        await update.effective_message.reply_text(
            f"Berhasil menambahkan Telegram ID <code>{target_id}</code> sebagai Admin.", parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            f"Telegram ID <code>{target_id}</code> sudah terdaftar sebagai Admin sebelumnya.", parse_mode=ParseMode.HTML
        )
    return ConversationHandler.END


async def remove_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak.")
        return ConversationHandler.END

    raw_text = (update.effective_message.text or "").strip()
    try:
        target_id = int(raw_text)
    except ValueError:
        await update.effective_message.reply_text("ID Telegram harus berupa angka. Silakan coba lagi atau ketik Batal.")
        return REMOVE_ADMIN_ID

    if remove_allowed_admin(target_id):
        await update.effective_message.reply_text(
            f"Akses Admin untuk Telegram ID <code>{target_id}</code> berhasil dicabut.", parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            f"Telegram ID <code>{target_id}</code> tidak ditemukan dalam daftar Admin.", parse_mode=ParseMode.HTML
        )
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Handler Umum & Pencarian Data
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await block_for_maintenance(update):
        return
    await update.effective_message.reply_text(
        "<b>Katalog Backup Transaksi</b>\n\n"
        "Kirim TID (huruf/angka) untuk melihat data.\n"
        "Ketik <b>Daftar</b> untuk menambah, <b>Edit</b> untuk mengubah, "
        "<b>Hapus</b> untuk menghapus data TID, atau <b>Batal</b> untuk membatalkan proses.\n"
        "Khusus Admin: ketik <b>/cek</b> untuk melihat semua TID terdaftar.\n\n"
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
        await update.effective_message.reply_text("Maaf, data belum dapat diambil.")


# -----------------------------------------------------------------------------
# Fitur Pendaftaran TID Baru
# -----------------------------------------------------------------------------
async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak. Hanya Admin yang dapat menambah data baru.")
        return ConversationHandler.END

    if await block_if_disabled(
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Silakan kirim TID baru (huruf/angka).")
    return REG_TID


async def register_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Akses ditolak. Hanya Admin yang dapat mendaftarkan TID.", show_alert=True)
        return ConversationHandler.END

    if await block_if_disabled(
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        return ConversationHandler.END

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
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        context.user_data.clear()
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi.")
        return REG_TID
    if find_tid(tid) is not None:
        await update.effective_message.reply_text("TID sudah terdaftar. Kirim TID lain atau Batal.")
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
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.setdefault("photos", []).append(update.effective_message.photo[-1].file_id)
    await update.effective_message.reply_text(
        f"Foto ke-{len(context.user_data['photos'])} diterima. Kirim lagi atau ketik Selesai."
    )
    return REG_PHOTOS


async def register_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        context.user_data.clear()
        return ConversationHandler.END
    if not context.user_data.get("photos"):
        await update.effective_message.reply_text("Minimal kirim satu foto sebelum ketik Selesai.")
        return REG_PHOTOS
    await update.effective_message.reply_text("Foto selesai. Sekarang kirim keterangan TID tersebut.")
    return REG_DESC


async def register_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(
        update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."
    ):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong.")
        return REG_DESC
    tid = context.user_data.get("tid")
    photos = context.user_data.get("photos", [])
    if not tid or not photos:
        context.user_data.clear()
        await update.effective_message.reply_text("Data tidak lengkap. Silakan pendaftaran ulang.")
        return ConversationHandler.END
    try:
        create_tid(tid, description, photos, get_user_id(update))
        await update.effective_message.reply_text(
            f"Pendaftaran TID <code>{tid}</code> berhasil disimpan.",
            parse_mode=ParseMode.HTML,
        )
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mendaftarkan TID %s", tid)
        await update.effective_message.reply_text("Pendaftaran gagal disimpan.")
    context.user_data.clear()
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Fitur Pengeditan Data
# -----------------------------------------------------------------------------
async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak. Hanya Admin yang dapat mengedit data TID.")
        return ConversationHandler.END

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
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi.")
        return EDIT_TID
    record = find_tid(tid)
    if record is None:
        await update.effective_message.reply_text("TID tidak ditemukan. Kirim TID lain/Batal.")
        return EDIT_TID
    actual_tid = record["tid"]
    context.user_data["tid"] = actual_tid
    await update.effective_message.reply_text(
        f"Pilih tindakan pengeditan untuk TID <code>{actual_tid}</code>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Ubah gambar tertentu", callback_data="edit:change_single")],
            [InlineKeyboardButton("Tambah gambar baru", callback_data="edit:add")],
            [InlineKeyboardButton("Ganti semua gambar", callback_data="edit:replace")],
            [InlineKeyboardButton("Hapus gambar tertentu", callback_data="edit:delete_single")],
            [InlineKeyboardButton("Hapus semua gambar", callback_data="edit:delete_all")],
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
    tid = context.user_data.get("tid")
    photos = get_photos(tid)

    if action == "description":
        await query.message.reply_text("Kirim keterangan baru untuk TID tersebut.")
        return EDIT_DESC

    elif action in ("change_single", "delete_single"):
        if not photos:
            await query.message.reply_text("TID ini belum memiliki gambar untuk diubah/dihapus.")
            return EDIT_CHOICE

        buttons = []
        for idx, p in enumerate(photos, start=1):
            buttons.append([
                InlineKeyboardButton(
                    f"Gambar Ke-{idx}", callback_data=f"pic_{action}:{p['id']}:{idx}"
                )
            ])
        buttons.append([InlineKeyboardButton("Batal", callback_data="edit:cancel_sub")])

        action_label = "mengubah" if action == "change_single" else "menghapus"
        await query.message.reply_text(
            f"Pilih nomor gambar yang ingin Anda {action_label}:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return EDIT_SELECT_PHOTO

    elif action == "delete_all":
        if not photos:
            await query.message.reply_text("TID ini belum memiliki gambar untuk dihapus.")
            return EDIT_CHOICE
        await query.message.reply_text(
            f"Apakah Anda yakin ingin menghapus <b>SEMUA</b> gambar ({len(photos)} foto) pada TID <code>{tid}</code>?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Ya, Hapus Semua Foto", callback_data="edit_delall:confirm"),
                    InlineKeyboardButton("Batal", callback_data="edit_delall:cancel"),
                ]
            ]),
        )
        return EDIT_DELETE_PHOTO

    elif action in ("replace", "add"):
        context.user_data["photos"] = []
        context.user_data["edit_action"] = action
        instruction = "menggantikan semua gambar lama" if action == "replace" else "menambahkan gambar baru"
        await query.message.reply_text(
            f"Kirim foto yang ingin {instruction}. Setelah selesai, ketik <b>Selesai</b>.",
            parse_mode=ParseMode.HTML,
        )
        return REPLACE_PHOTOS if action == "replace" else ADD_PHOTOS

    return EDIT_CHOICE


async def edit_select_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "edit:cancel_sub":
        await query.message.reply_text("Aksi dibatalkan. Kirim TID atau ketik Batal.")
        return ConversationHandler.END

    data = query.data.split(":")
    mode = data[0]
    photo_id = int(data[1])
    photo_idx = data[2]

    context.user_data["selected_photo_id"] = photo_id
    tid = context.user_data.get("tid")

    if mode == "pic_change_single":
        await query.message.reply_text(
            f"Silakan kirimkan 1 foto baru untuk menggantikan <b>Gambar Ke-{photo_idx}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_CHANGE_PHOTO

    elif mode == "pic_delete_single":
        try:
            delete_single_photo(photo_id, tid, get_user_id(update))
            await query.message.reply_text(
                f"Gambar Ke-{photo_idx} berhasil dihapus.",
                parse_mode=ParseMode.HTML,
            )
            await send_tid_data(update, context, tid)
        except Exception:
            logger.exception("Error menghapus foto tunggal")
            await query.message.reply_text("Gagal menghapus foto tersebut.")
        context.user_data.clear()
        return ConversationHandler.END

    return EDIT_CHOICE


async def edit_change_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END

    photo_id = context.user_data.get("selected_photo_id")
    tid = context.user_data.get("tid")
    new_file_id = update.effective_message.photo[-1].file_id

    if not photo_id or not tid:
        await update.effective_message.reply_text("Sesi kedaluwarsa. Silakan ulang proses edit.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        update_single_photo(photo_id, new_file_id, get_user_id(update), tid)
        await update.effective_message.reply_text("Gambar berhasil diperbarui.")
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error update foto tunggal")
        await update.effective_message.reply_text("Gagal memperbarui gambar.")

    context.user_data.clear()
    return ConversationHandler.END


async def edit_delete_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tid = context.user_data.get("tid")

    if action == "confirm" and tid:
        try:
            delete_all_photos(tid, get_user_id(update))
            await query.message.reply_text(
                f"Semua gambar untuk TID <code>{tid}</code> berhasil dihapus.",
                parse_mode=ParseMode.HTML,
            )
            await send_tid_data(update, context, tid)
        except Exception:
            logger.exception("Error hapus semua gambar")
            await query.message.reply_text("Gagal menghapus gambar.")
    else:
        await query.message.reply_text("Penghapusan semua gambar dibatalkan.")

    context.user_data.clear()
    return ConversationHandler.END


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
            await update.effective_message.reply_text("TID tidak ditemukan. Silakan mulai edit kembali.")
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
        await update.effective_message.reply_text("Perubahan gambar gagal disimpan.")
    context.user_data.clear()
    return ConversationHandler.END


async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    tid = context.user_data.get("tid")
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong.")
        return EDIT_DESC
    if not tid or find_tid(tid) is None:
        context.user_data.clear()
        await update.effective_message.reply_text("TID tidak ditemukan. Silakan mulai edit kembali.")
        return ConversationHandler.END
    try:
        update_description(tid, description, get_user_id(update))
        await update.effective_message.reply_text("Keterangan berhasil diperbarui.")
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mengedit keterangan TID %s", tid)
        await update.effective_message.reply_text("Keterangan gagal diperbarui.")
    context.user_data.clear()
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Fitur Penghapusan TID
# -----------------------------------------------------------------------------
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.effective_message.reply_text("Akses ditolak. Hanya Admin yang dapat menghapus data TID.")
        return ConversationHandler.END

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
        await update.effective_message.reply_text("TID harus berupa huruf/angka tanpa spasi.")
        return DELETE_TID

    record = find_tid(tid)
    if record is None:
        await update.effective_message.reply_text("TID tidak ditemukan. Kirim TID lain/Batal.")
        return DELETE_TID

    actual_tid = record["tid"]
    context.user_data["tid"] = actual_tid
    await update.effective_message.reply_text(
        f"Apakah Anda yakin ingin menghapus TID <code>{actual_tid}</code> beserta seluruh datanya?",
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
            await query.message.reply_text("Gagal menghapus TID.")
    else:
        await query.message.reply_text("Penghapusan TID dibatalkan.")

    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Terjadi kesalahan teknis. Silakan coba lagi.")


# -----------------------------------------------------------------------------
# Aplikasi Utama & Inisialisasi Handler
# -----------------------------------------------------------------------------
def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(False).build()
    common_fallbacks = [
        CommandHandler("batal", cancel),
        MessageHandler(filters.Regex(r"(?i)^batal$"), cancel),
    ]

    admin_management_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern=r"^admin:(add_access|remove_access)$")
        ],
        states={
            ADD_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_id_received)],
            REMOVE_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_id_received)],
        },
        fallbacks=common_fallbacks,
        allow_reentry=True,
    )

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
            EDIT_CHOICE: [
                CallbackQueryHandler(
                    edit_choice,
                    pattern=r"^edit:(replace|add|description|change_single|delete_single|delete_all)$",
                )
            ],
            EDIT_SELECT_PHOTO: [
                CallbackQueryHandler(
                    edit_select_photo_callback,
                    pattern=r"^(pic_change_single|pic_delete_single):\d+:\d+$|^edit:cancel_sub$",
                )
            ],
            EDIT_CHANGE_PHOTO: [
                MessageHandler(filters.PHOTO, edit_change_photo_received)
            ],
            EDIT_DELETE_PHOTO: [
                CallbackQueryHandler(
                    edit_delete_all_callback, pattern=r"^edit_delall:(confirm|cancel)$"
                )
            ],
            REPLACE_PHOTOS: [
                MessageHandler(filters.PHOTO, edit_photo_received),
                MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done),
            ],
            ADD_PHOTOS: [
                MessageHandler(filters.PHOTO, edit_photo_received),
                MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done),
            ],
            EDIT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_description_received)
            ],
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
            DELETE_CONFIRM: [
                CallbackQueryHandler(
                    delete_confirm_callback, pattern=r"^delete:(confirm|cancel)$"
                )
            ],
        },
        fallbacks=common_fallbacks,
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("cek", list_tids_command))
    application.add_handler(admin_management_conv)
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:(list_tids|maintenance|registration|edit|stats|list_access|refresh)$",
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