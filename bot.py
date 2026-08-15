import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from supabase import Client, create_client
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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-backup-catalog")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not ADMIN_ID_RAW:
    raise RuntimeError("BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, dan ADMIN_ID wajib diisi.")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID harus berupa angka Telegram user ID.") from exc

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

REG_TID, REG_PHOTOS, REG_DESC = range(3)
EDIT_TID, EDIT_CHOICE, REPLACE_PHOTOS, ADD_PHOTOS, EDIT_DESC = range(3, 8)
TID_PATTERN = re.compile(r"^\d{1,30}$")
SETTINGS_ID = 1
DEFAULT_SETTINGS = {
    "maintenance_mode": False,
    "registration_enabled": True,
    "edit_enabled": True,
}


def normalize_tid(value: str) -> Optional[str]:
    value = (value or "").strip()
    return value if TID_PATTERN.fullmatch(value) else None


def user_id(update: Update) -> int:
    return update.effective_user.id if update.effective_user else 0


def is_admin(update: Update) -> bool:
    return user_id(update) == ADMIN_ID


async def db_call(fn: Callable[[], Any]) -> Any:
    return await asyncio.to_thread(fn)


async def get_settings() -> dict:
    response = await db_call(
        lambda: supabase.table("bot_settings").select(
            "id,maintenance_mode,registration_enabled,edit_enabled,updated_at"
        ).eq("id", SETTINGS_ID).maybe_single().execute()
    )
    if response.data:
        return {**DEFAULT_SETTINGS, **response.data}
    await db_call(lambda: supabase.table("bot_settings").insert({"id": SETTINGS_ID, **DEFAULT_SETTINGS}).execute())
    return {"id": SETTINGS_ID, **DEFAULT_SETTINGS}


async def ensure_settings() -> None:
    await get_settings()
    logger.info("Bot settings siap digunakan.")


async def update_setting(field: str, value: bool) -> dict:
    if field not in DEFAULT_SETTINGS:
        raise ValueError(f"Setting tidak dikenal: {field}")
    await db_call(
        lambda: supabase.table("bot_settings")
        .update({field: value, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", SETTINGS_ID)
        .execute()
    )
    return await get_settings()


async def is_maintenance() -> bool:
    return bool((await get_settings()).get("maintenance_mode", False))


async def feature_enabled(field: str) -> bool:
    return bool((await get_settings()).get(field, False))


async def deny_if_maintenance(update: Update) -> bool:
    if not is_admin(update) and await is_maintenance():
        if update.effective_message:
            await update.effective_message.reply_text(
                "Bot sedang dalam maintenance, silakan coba lagi nanti."
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "Bot sedang dalam maintenance, silakan coba lagi nanti.", show_alert=True
            )
        return True
    return False


async def deny_if_disabled(update: Update, field: str, message: str) -> bool:
    if await deny_if_maintenance(update):
        return True
    if not is_admin(update) and not await feature_enabled(field):
        if update.effective_message:
            await update.effective_message.reply_text(message)
        elif update.callback_query:
            await update.callback_query.answer(message, show_alert=True)
        return True
    return False


async def get_tid(tid: str) -> Optional[dict]:
    response = await db_call(
        lambda: supabase.table("tid_records")
        .select("tid,description,created_by,updated_by,created_at,updated_at")
        .eq("tid", tid).maybe_single().execute()
    )
    return response.data


async def get_photos(tid: str) -> list[dict]:
    response = await db_call(
        lambda: supabase.table("tid_photos")
        .select("id,file_id,photo_order,uploaded_by,created_at")
        .eq("tid", tid).order("photo_order").execute()
    )
    return response.data or []


async def create_tid(tid: str, description: str, uid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db_call(lambda: supabase.table("tid_records").insert({
        "tid": tid, "description": description, "created_by": uid,
        "updated_by": uid, "created_at": now, "updated_at": now,
    }).execute())


async def update_description(tid: str, description: str, uid: int) -> None:
    await db_call(lambda: supabase.table("tid_records").update({
        "description": description, "updated_by": uid,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("tid", tid).execute())


async def add_photos(tid: str, file_ids: list[str], uid: int, replace: bool = False) -> None:
    if replace:
        await db_call(lambda: supabase.table("tid_photos").delete().eq("tid", tid).execute())
    existing = await get_photos(tid)
    start_order = 1 if replace else len(existing) + 1
    rows = [{
        "tid": tid, "file_id": file_id, "photo_order": start_order + index,
        "uploaded_by": uid,
    } for index, file_id in enumerate(file_ids)]
    if rows:
        await db_call(lambda: supabase.table("tid_photos").insert(rows).execute())


async def get_statistics() -> tuple[int, int]:
    tid_response = await db_call(
        lambda: supabase.table("tid_records").select("tid", count="exact", head=True).execute()
    )
    photo_response = await db_call(
        lambda: supabase.table("tid_photos").select("id", count="exact", head=True).execute()
    )
    return int(tid_response.count or 0), int(photo_response.count or 0)


async def send_tid_data(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: str) -> None:
    record = await get_tid(tid)
    if not record:
        await update.effective_message.reply_text(
            f"TID <b>{tid}</b> belum terdaftar.\n\nApakah ingin mendaftarkannya?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Daftarkan TID ini", callback_data=f"register:{tid}")]]
            ),
        )
        return
    photos = await get_photos(tid)
    description = record.get("description") or "(Belum ada keterangan)"
    await update.effective_message.reply_text(
        f"<b>Data TID</b>\nTID: <code>{tid}</code>\n"
        f"Jumlah foto: {len(photos)}\nKeterangan: {description}",
        parse_mode=ParseMode.HTML,
    )
    if not photos:
        await update.effective_message.reply_text("Belum ada foto untuk TID ini.")
        return
    for offset in range(0, len(photos), 10):
        batch = photos[offset:offset + 10]
        media = [InputMediaPhoto(photo["file_id"]) for photo in batch]
        try:
            await update.effective_message.reply_media_group(media=media)
        except Exception:
            logger.exception("Gagal mengirim media group untuk TID %s", tid)
            for photo in batch:
                await update.effective_message.reply_photo(photo=photo["file_id"])


# -----------------------------------------------------------------------------
# Admin
# -----------------------------------------------------------------------------
def admin_menu_markup(settings: dict) -> InlineKeyboardMarkup:
    maintenance = "Nonaktifkan maintenance" if settings["maintenance_mode"] else "Aktifkan maintenance"
    registration = "Nonaktifkan pendaftaran" if settings["registration_enabled"] else "Aktifkan pendaftaran"
    editing = "Nonaktifkan edit" if settings["edit_enabled"] else "Aktifkan edit"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(maintenance, callback_data="admin:maintenance")],
        [InlineKeyboardButton(registration, callback_data="admin:registration")],
        [InlineKeyboardButton(editing, callback_data="admin:edit")],
        [InlineKeyboardButton("Lihat statistik", callback_data="admin:stats")],
        [InlineKeyboardButton("Refresh menu", callback_data="admin:refresh")],
    ])


def admin_status_text(settings: dict) -> str:
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
        settings = await get_settings()
        await update.effective_message.reply_text(
            admin_status_text(settings), parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_markup(settings),
        )
    except Exception:
        logger.exception("Gagal membuka menu admin")
        await update.effective_message.reply_text("Menu admin gagal dibuka. Periksa tabel bot_settings di Supabase.")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Akses ditolak.", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1]
    try:
        if action == "maintenance":
            settings = await get_settings()
            settings = await update_setting("maintenance_mode", not settings["maintenance_mode"])
        elif action == "registration":
            settings = await get_settings()
            settings = await update_setting("registration_enabled", not settings["registration_enabled"])
        elif action == "edit":
            settings = await get_settings()
            settings = await update_setting("edit_enabled", not settings["edit_enabled"])
        elif action in {"refresh", "stats"}:
            settings = await get_settings()
        else:
            return
        if action == "stats":
            tid_count, photo_count = await get_statistics()
            await query.message.reply_text(
                f"<b>Statistik katalog</b>\n\nJumlah TID terdaftar: <b>{tid_count}</b>\n"
                f"Jumlah foto total: <b>{photo_count}</b>", parse_mode=ParseMode.HTML,
            )
        await query.message.edit_text(
            admin_status_text(settings), parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_markup(settings),
        )
    except Exception:
        logger.exception("Gagal memproses aksi admin %s", action)
        await query.message.reply_text("Aksi admin gagal diproses. Periksa log Railway dan tabel Supabase.")


# -----------------------------------------------------------------------------
# Command dan pencarian
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_maintenance(update):
        return
    await update.effective_message.reply_text(
        "<b>Katalog Backup Transaksi</b>\n\nKirim TID berupa angka untuk melihat data.\n"
        "Gunakan <b>Daftar</b> untuk menambah TID, <b>Edit</b> untuk mengubah data, "
        "atau <b>Batal</b> untuk membatalkan proses.\n\nContoh: <code>123456</code>",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Proses dibatalkan. Kirim TID atau ketik Daftar/Edit kapan saja.")
    return ConversationHandler.END


async def search_tid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_maintenance(update):
        return
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("Format belum dikenali. Kirim TID berupa angka, atau ketik Daftar/Edit/Batal.")
        return
    try:
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mencari TID %s", tid)
        await update.effective_message.reply_text("Maaf, data belum dapat diambil. Silakan coba lagi.")


# -----------------------------------------------------------------------------
# Pendaftaran TID
# -----------------------------------------------------------------------------
async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Silakan kirim TID baru berupa angka.")
    return REG_TID


async def register_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        return ConversationHandler.END
    await query.answer()
    tid = query.data.split(":", 1)[1]
    if await get_tid(tid):
        await query.message.reply_text("TID tersebut baru saja didaftarkan oleh pengguna lain.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["tid"] = tid
    context.user_data["photos"] = []
    await query.message.reply_text(f"Pendaftaran TID <code>{tid}</code> dimulai.\n\nKirim foto disk/CD, lalu ketik <b>Selesai</b>.", parse_mode=ParseMode.HTML)
    return REG_PHOTOS


async def register_tid_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa angka. Silakan kirim ulang.")
        return REG_TID
    if await get_tid(tid):
        await update.effective_message.reply_text("TID sudah terdaftar. Kirim TID lain, atau ketik Batal.")
        return REG_TID
    context.user_data["tid"] = tid
    context.user_data["photos"] = []
    await update.effective_message.reply_text(f"TID <code>{tid}</code> diterima. Kirim foto, lalu ketik <b>Selesai</b>.", parse_mode=ParseMode.HTML)
    return REG_PHOTOS


async def register_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.setdefault("photos", []).append(update.effective_message.photo[-1].file_id)
    await update.effective_message.reply_text(f"Foto ke-{len(context.user_data['photos'])} diterima. Kirim lagi atau ketik Selesai.")
    return REG_PHOTOS


async def register_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    if not context.user_data.get("photos"):
        await update.effective_message.reply_text("Minimal kirim satu foto sebelum mengetik Selesai.")
        return REG_PHOTOS
    await update.effective_message.reply_text("Foto selesai. Sekarang kirim keterangan untuk TID tersebut.")
    return REG_DESC


async def register_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "registration_enabled", "Fitur pendaftaran TID sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong. Silakan kirim keterangan.")
        return REG_DESC
    tid = context.user_data["tid"]
    try:
        await create_tid(tid, description, user_id(update))
        await add_photos(tid, context.user_data["photos"], user_id(update))
        await update.effective_message.reply_text(f"Pendaftaran TID <code>{tid}</code> berhasil disimpan.", parse_mode=ParseMode.HTML)
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mendaftarkan TID %s", tid)
        await update.effective_message.reply_text("Pendaftaran gagal disimpan. Periksa konfigurasi Supabase.")
    context.user_data.clear()
    return ConversationHandler.END


# -----------------------------------------------------------------------------
# Pengeditan TID
# -----------------------------------------------------------------------------
async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Kirim TID yang ingin diedit berupa angka.")
    return EDIT_TID


async def edit_tid_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        return ConversationHandler.END
    tid = normalize_tid(update.effective_message.text or "")
    if not tid:
        await update.effective_message.reply_text("TID harus berupa angka. Silakan kirim ulang.")
        return EDIT_TID
    if not await get_tid(tid):
        await update.effective_message.reply_text("TID tidak ditemukan. Kirim TID lain atau ketik Batal.")
        return EDIT_TID
    context.user_data["tid"] = tid
    await update.effective_message.reply_text(
        f"Pilih perubahan untuk TID <code>{tid}</code>:", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Ubah semua gambar", callback_data="edit:replace")],
            [InlineKeyboardButton("Tambah gambar", callback_data="edit:add")],
            [InlineKeyboardButton("Ubah keterangan", callback_data="edit:description")],
        ]),
    )
    return EDIT_CHOICE


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
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
    await query.message.reply_text(f"Kirim foto yang ingin {instruction}. Setelah selesai, ketik Selesai.")
    return REPLACE_PHOTOS if action == "replace" else ADD_PHOTOS


async def edit_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.setdefault("photos", []).append(update.effective_message.photo[-1].file_id)
    await update.effective_message.reply_text(f"Foto ke-{len(context.user_data['photos'])} diterima. Kirim lagi atau ketik Selesai.")
    return REPLACE_PHOTOS if context.user_data.get("edit_action") == "replace" else ADD_PHOTOS


async def edit_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.effective_message.reply_text("Minimal kirim satu foto sebelum mengetik Selesai.")
        return REPLACE_PHOTOS if context.user_data.get("edit_action") == "replace" else ADD_PHOTOS
    try:
        await add_photos(context.user_data["tid"], photos, user_id(update), replace=context.user_data.get("edit_action") == "replace")
        tid = context.user_data["tid"]
        await update.effective_message.reply_text("Perubahan gambar berhasil disimpan.")
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mengedit foto")
        await update.effective_message.reply_text("Perubahan gambar gagal disimpan. Silakan coba lagi.")
    context.user_data.clear()
    return ConversationHandler.END


async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_disabled(update, "edit_enabled", "Fitur edit sedang dinonaktifkan admin."):
        context.user_data.clear()
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    if not description:
        await update.effective_message.reply_text("Keterangan tidak boleh kosong. Silakan kirim ulang.")
        return EDIT_DESC
    tid = context.user_data["tid"]
    try:
        await update_description(tid, description, user_id(update))
        await update.effective_message.reply_text("Keterangan berhasil diperbarui.")
        await send_tid_data(update, context, tid)
    except Exception:
        logger.exception("Error saat mengedit keterangan TID %s", tid)
        await update.effective_message.reply_text("Keterangan gagal diperbarui. Silakan coba lagi.")
    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Terjadi kesalahan teknis. Silakan coba lagi.")


async def post_init(application: Application) -> None:
    await ensure_settings()


def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(False).post_init(post_init).build()
    common_fallbacks = [CommandHandler("batal", cancel), MessageHandler(filters.Regex(r"(?i)^batal$"), cancel)]
    register_conversation = ConversationHandler(
        entry_points=[CommandHandler("daftar", register_command), MessageHandler(filters.Regex(r"(?i)^daftar$"), register_command), CallbackQueryHandler(register_from_callback, pattern=r"^register:\d+$")],
        states={
            REG_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_tid_received)],
            REG_PHOTOS: [MessageHandler(filters.PHOTO, register_photo_received), MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), register_photos_done)],
            REG_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_description_received)],
        }, fallbacks=common_fallbacks, allow_reentry=True,
    )
    edit_conversation = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_command), MessageHandler(filters.Regex(r"(?i)^edit$"), edit_command)],
        states={
            EDIT_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_tid_received)],
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice, pattern=r"^edit:(replace|add|description)$")],
            REPLACE_PHOTOS: [MessageHandler(filters.PHOTO, edit_photo_received), MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done)],
            ADD_PHOTOS: [MessageHandler(filters.PHOTO, edit_photo_received), MessageHandler(filters.Regex(r"(?i)^(selesai|done|cukup)$"), edit_photos_done)],
            EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_description_received)],
        }, fallbacks=common_fallbacks, allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:(maintenance|registration|edit|stats|refresh)$"))
    app.add_handler(register_conversation)
    app.add_handler(edit_conversation)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_tid))
    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    application = build_application()
    logger.info("Bot berjalan dengan mode polling Telegram.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
