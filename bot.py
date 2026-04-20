import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

# --- CONFIGURATION ---
def load_env_file(path: str = ".env") -> None:
    """Simple .env loader without external dependency."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env_file()

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable must be set with your bot token.")

def parse_id_list(raw: str | None, default: str = "") -> list[int]:
    """Parse comma-separated numeric IDs into a list of ints."""
    ids: list[int] = []
    data = raw if raw not in (None, "") else default
    for part in data.replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids

SUPERADMIN_IDS = parse_id_list(os.getenv("SUPERADMIN_ID"), "7706048424")
if not SUPERADMIN_IDS:
    raise RuntimeError("SUPERADMIN_ID must contain at least one numeric ID (comma-separated for multiple).")
SUPERADMIN_ID = SUPERADMIN_IDS[0]  # backward-compat alias
DATABASE = os.getenv("DATABASE_PATH", "kino_bot.db")
MOVIE_CHANNEL_ID = os.getenv("MOVIE_CHANNEL_ID", "-1003736304208")  # Bu yerga kinolar yuklangan kanal ID sini yozing

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- STATES ---
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel = State()
    waiting_for_new_admin = State()
    waiting_for_movie_channel = State()
    waiting_for_invite_link = State()

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, joined_date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS join_requests (
        user_id INTEGER,
        channel_id TEXT,
        requested_at TEXT,
        PRIMARY KEY (user_id, channel_id)
    )''')
    
    # Schema migration: add request_required column if missing
    cursor.execute("PRAGMA table_info(channels)")
    cols = [row[1] for row in cursor.fetchall()]
    if "request_required" not in cols:
        cursor.execute("ALTER TABLE channels ADD COLUMN request_required INTEGER DEFAULT 0")
    if "invite_link" not in cols:
        cursor.execute("ALTER TABLE channels ADD COLUMN invite_link TEXT")
    
    # Default settings
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('mandatory_enabled', '1'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('movie_channel', MOVIE_CHANNEL_ID))
    
    # Add superadmin(s) to admins table
    for sa_id in SUPERADMIN_IDS:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (sa_id, 0))
    
    conn.commit()
    conn.close()

# DB Helper Functions
def db_query(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    res = None
    if fetchone: res = cursor.fetchone()
    elif fetchall: res = cursor.fetchall()
    if not query.lstrip().upper().startswith("SELECT"):
        conn.commit()
    conn.close()
    return res

def prune_join_requests(days: int = 7):
    """Cleanup old join request records to prevent table growth."""
    db_query(
        "DELETE FROM join_requests WHERE requested_at < datetime('now', ?)",
        (f'-{days} day',)
    )

def ensure_user(user_id: int, username: str | None, full_name: str | None):
    """Insert user if missing; keep first join date."""
    db_query(
        "INSERT OR IGNORE INTO users (user_id, username, joined_date) VALUES (?, ?, ?)",
        (user_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

def is_admin(user_id):
    res = db_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return res is not None

# --- BOT INITIALIZATION ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- UTILS ---
async def check_subscriptions(user_id):
    enabled = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    if enabled == '0': return []
    
    prune_join_requests()  # avoid table swelling; drop entries older than 7 days

    channels = db_query("SELECT channel_id, COALESCE(request_required,0) FROM channels", fetchall=True)
    not_subscribed = []
    for channel, req in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                # If user has already sent a join request, treat as temporarily allowed
                jr = db_query(
                    "SELECT 1 FROM join_requests WHERE user_id = ? AND channel_id = ?",
                    (user_id, str(channel)),
                    fetchone=True
                )
                if jr:
                    continue
                not_subscribed.append((channel, req))
            else:
                # User is fully in; clean up stale join_request record
                db_query("DELETE FROM join_requests WHERE user_id = ? AND channel_id = ?", (user_id, str(channel)))
        except Exception as exc:
            logger.warning("Subscription check failed for user %s in %s: %s", user_id, channel, exc)
            continue
    return not_subscribed

async def build_join_button(channel_id: str, request_required: bool = False) -> InlineKeyboardButton | None:
    """
    Build a join button that works for both @username channels and numeric -100 IDs.
    If invite link creation fails, returns None so the caller can skip it.
    """
    channel_id = channel_id.strip()
    url = None
    if request_required:
        # Use admin-supplied join-request link if available (most reliable for private/zayavka kanallar)
        stored = db_query(
            "SELECT invite_link FROM channels WHERE channel_id = ?",
            (channel_id,),
            fetchone=True
        )
        if stored and stored[0]:
            url = stored[0]
        else:
            # If admin link yo'q, try to create fresh join-request link (requires bot to be admin with invite rights)
            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=channel_id,
                    creates_join_request=True
                )
                url = invite.invite_link
                db_query(
                    "UPDATE channels SET invite_link = ? WHERE channel_id = ?",
                    (url, channel_id)
                )
            except Exception as exc:
                logger.warning("Join-request invite generation failed for %s: %s", channel_id, exc)
                return None
    else:
        if channel_id.startswith("@"):
            url = f"https://t.me/{channel_id[1:]}"
        else:
            try:
                invite = await bot.create_chat_invite_link(chat_id=channel_id, creates_join_request=False)
                url = invite.invite_link
            except Exception as exc:
                logger.warning("Invite link generation failed for %s: %s", channel_id, exc)
                return None
    return InlineKeyboardButton(text="A'zo bo'lish", url=url)

# --- HANDLERS ---

@dp.chat_join_request()
async def on_chat_join_request(request: ChatJoinRequest) -> None:
    """
    Record join-requests for mandatory channels so bot knows user bosgan (clicked) join.
    No auto-approve – approval remains channel admins' responsibility.
    """
    channel_row = db_query(
        "SELECT request_required FROM channels WHERE channel_id = ?",
        (str(request.chat.id),),
        fetchone=True
    )
    # Fallback: match by @username if admin saved channel that way
    if not channel_row and request.chat.username:
        channel_row = db_query(
            "SELECT request_required FROM channels WHERE channel_id = ?",
            (f"@{request.chat.username}",),
            fetchone=True
        )
    if not channel_row:
        return  # Bot only manages known mandatory channels

    user = request.from_user
    if not user:
        return

    ensure_user(user.id, user.username, user.full_name)
    # Mark that user sent join request (can be used to suppress repeated prompts)
    db_query(
        "INSERT OR REPLACE INTO join_requests (user_id, channel_id, requested_at) VALUES (?, ?, ?)",
        (user.id, str(request.chat.id), datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

    try:
        await request.bot.send_message(
            user.id,
            "✅ Zayavka yuborildi. Admin tasdiqlagach botdan foydalanishingiz mumkin."
        )
    except Exception as exc:
        logger.error("Join request approval failed for %s in %s: %s", user.id, request.chat.id, exc)
        # quietly ignore so handler doesn't crash
        return

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    db_query("INSERT OR IGNORE INTO users (user_id, username, joined_date) VALUES (?, ?, ?)", 
             (message.from_user.id, message.from_user.username, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    
    not_subscribed = await check_subscriptions(message.from_user.id)
    if not_subscribed:
        builder = InlineKeyboardBuilder()
        for ch, req in not_subscribed:
            btn = await build_join_button(ch, bool(req))
            if btn:
                builder.row(btn)
        builder.row(InlineKeyboardButton(text="Tekshirish", callback_data="check_sub"))
        await message.answer(
            "Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling. "
            "A'zo bo'lgach «Tekshirish» tugmasini bosing.",
            reply_markup=builder.as_markup()
        )
        return

    # Check if user sent a movie code
    args = message.text.split()
    if len(args) > 1:
        code = args[1]
        await send_movie(message, code)
    else:
        await message.answer("Assalomu alaykum! Kino kodini yuboring.")

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(callback: CallbackQuery):
    not_subscribed = await check_subscriptions(callback.from_user.id)
    if not not_subscribed:
        await callback.message.edit_text(
            "Rahmat! Endi kino kodini yuborishingiz mumkin.\n"
            "Kod misoli: 123"
        )
    else:
        await callback.answer("Hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)

async def send_movie(message, code):
    movie_channel = db_query("SELECT value FROM settings WHERE key = 'movie_channel'", fetchone=True)[0]
    try:
        # Copy the message from the channel using the ID (code)
        await bot.copy_message(chat_id=message.chat.id, from_chat_id=movie_channel, message_id=int(code))
    except Exception as e:
        logger.error("Failed to send movie code %s from channel %s: %s", code, movie_channel, e)
        await message.answer(
            "Kino topilmadi yoki kod xato.\n"
            "Kod to'g'riligini tekshirib, qayta yuboring."
        )

@dp.message(F.text.isdigit())
async def handle_movie_code(message: types.Message):
    not_subscribed = await check_subscriptions(message.from_user.id)
    if not_subscribed:
        await start_cmd(message)
        return
    await send_movie(message, message.text)

# --- ADMIN PANEL ---

@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if not is_admin(message.from_user.id): return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Statistika", callback_data="adm_stats"))
    builder.row(InlineKeyboardButton(text="Kanallar (majburiy)", callback_data="adm_channels"))
    builder.row(InlineKeyboardButton(text="Reklama (broadcast)", callback_data="adm_broadcast"))
    builder.row(InlineKeyboardButton(text="Adminlar", callback_data="adm_admins"))
    builder.row(InlineKeyboardButton(text="Sozlamalar", callback_data="adm_settings"))
    
    await message.answer(
        "Admin panel:\n"
        "Statistika, majburiy kanallar, reklama va boshqa sozlamalarni shu yerda boshqaring.",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "adm_stats")
async def adm_stats_cb(callback: CallbackQuery):
    u_count = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    a_count = db_query("SELECT COUNT(*) FROM admins", fetchone=True)[0]
    c_count = db_query("SELECT COUNT(*) FROM channels", fetchone=True)[0]
    
    text = (
        "📊 Bot Statistikasi:\n\n"
        f"👥 Foydalanuvchilar: {u_count}\n"
        f"👑 Adminlar: {a_count}\n"
        f"📢 Majburiy kanallar: {c_count}"
    )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "adm_channels")
async def adm_channels_cb(callback: CallbackQuery):
    channels = db_query("SELECT channel_id, COALESCE(request_required,0) FROM channels", fetchall=True)
    builder = InlineKeyboardBuilder()
    for ch, req in channels:
        label = f"O'chirish: {ch} ({'zayavka' if req else 'oddiy'})"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"del_ch|{ch}"))
    builder.row(InlineKeyboardButton(text="Kanal qo'shish (oddiy)", callback_data="add_ch|0"))
    builder.row(InlineKeyboardButton(text="Kanal qo'shish (zayavka)", callback_data="add_ch|1"))
    
    enabled = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    status_text = "Yoqilgan" if enabled == '1' else "O'chirilgan"
    builder.row(InlineKeyboardButton(text=f"Holat: {status_text}", callback_data="toggle_mandatory"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    
    await callback.message.edit_text(
        "Majburiy kanallarni boshqarish:\n"
        "• Oddiy: darhol qo'shadi\n"
        "• Zayavka: join-request yuboradi",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "toggle_mandatory")
async def toggle_mandatory_cb(callback: CallbackQuery):
    current = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    new_val = '0' if current == '1' else '1'
    db_query("UPDATE settings SET value = ? WHERE key = 'mandatory_enabled'", (new_val,))
    await adm_channels_cb(callback)

@dp.callback_query(F.data.startswith("add_ch|"))
async def add_ch_cb(callback: CallbackQuery, state: FSMContext):
    req_flag = callback.data.split("|")[1]
    await state.update_data(request_required=int(req_flag))
    await callback.message.answer(
        "Kanal username yoki ID sini yuboring (masalan: @kanal_nomi yoki -100...), "
        "yoki kanaldan bir dona xabarni forward qiling."
    )
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_ch|"))
async def del_ch_cb(callback: CallbackQuery):
    ch_id = callback.data.split("|")[1]
    db_query("DELETE FROM channels WHERE channel_id = ?", (ch_id,))
    await adm_channels_cb(callback)

@dp.callback_query(F.data == "adm_admins")
async def adm_admins_cb(callback: CallbackQuery):
    if callback.from_user.id not in SUPERADMIN_IDS:
        await callback.answer("Faqat Superadmin uchun!", show_alert=True)
        return
    
    admins = db_query("SELECT user_id FROM admins", fetchall=True)
    builder = InlineKeyboardBuilder()
    for (a_id,) in admins:
        if a_id in SUPERADMIN_IDS: continue
        builder.row(InlineKeyboardButton(text=f"O'chirish: {a_id}", callback_data=f"del_adm|{a_id}"))
    builder.row(InlineKeyboardButton(text="Admin qo'shish (@username)", callback_data="add_adm"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    await callback.message.edit_text("Adminlarni boshqarish:", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "add_adm")
async def add_adm_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Yangi admin username'ini yuboring (masalan: @username):")
    await state.set_state(AdminStates.waiting_for_new_admin)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_adm|"))
async def del_adm_cb(callback: CallbackQuery):
    a_id = callback.data.split("|")[1]
    db_query("DELETE FROM admins WHERE user_id = ?", (a_id,))
    await adm_admins_cb(callback)

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Reklama xabarini yuboring (Forward, Rasm, Video, Text hammasi o'tadi):")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.callback_query(F.data == "adm_settings")
async def adm_settings_cb(callback: CallbackQuery):
    current = db_query("SELECT value FROM settings WHERE key = 'movie_channel'", fetchone=True)[0]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Kino kanalini o'zgartirish", callback_data="set_movie_ch"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    await callback.message.edit_text(
        f"Sozlamalar:\\n\\nKino kanali: {current}",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "set_movie_ch")
async def set_movie_ch_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Kino kanali ID yoki username'ini yuboring:")
    await state.set_state(AdminStates.waiting_for_movie_channel)
    await callback.answer()

@dp.callback_query(F.data == "adm_back")
async def adm_back_cb(callback: CallbackQuery):
    await admin_cmd(callback.message)
    await callback.message.delete()

# --- FSM PROCESSORS ---

@dp.message(AdminStates.waiting_for_channel)
async def proc_add_ch(message: types.Message, state: FSMContext):
    data = await state.get_data()
    req_flag = int(data.get("request_required", 0))

    # Try to extract channel from forwarded message first
    source_chat = None
    if getattr(message, "forward_from_chat", None):
        source_chat = message.forward_from_chat
    elif getattr(message, "forward_origin", None):
        origin = message.forward_origin
        chat = getattr(origin, "chat", None)
        if chat:
            source_chat = chat

    channel_id = None
    if source_chat:
        # Prefer numeric ID for reliability
        channel_id = str(source_chat.id)
    else:
        text_val = (message.text or "").strip()
        if not text_val:
            await message.answer("Kanal qo'shish uchun username/ID yuboring yoki kanal xabarini forward qiling.")
            return
        channel_id = text_val

    await state.update_data(channel_id=channel_id)

    if req_flag == 1:
        await message.answer(
            "Zayavka kanali uchun join link yuboring (masalan: https://t.me/+ilUQlM-PNQQxZDli)."
        )
        await state.set_state(AdminStates.waiting_for_invite_link)
        return

    db_query(
        "INSERT OR IGNORE INTO channels (channel_id, request_required, invite_link) VALUES (?, ?, ?)",
        (channel_id, req_flag, None)
    )
    await message.answer(f"{channel_id} qo'shildi. Tur: {'zayavka' if req_flag else 'oddiy'}.")
    await state.clear()

@dp.message(AdminStates.waiting_for_invite_link)
async def proc_add_invite_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("channel_id")
    req_flag = int(data.get("request_required", 1))

    invite_link = (message.text or "").strip()
    if not invite_link:
        await message.answer("Join link yuboring (https://t.me/+...).")
        return
    # Basic validation for zayavka links
    if not (
        invite_link.startswith("https://t.me/+")
        or invite_link.startswith("https://t.me/joinchat/")
        or "join_request=1" in invite_link
    ):
        await message.answer("Zayavka uchun t.me/+ yoki joinchat link yuboring.")
        return

    db_query(
        "INSERT OR REPLACE INTO channels (channel_id, request_required, invite_link) VALUES (?, ?, ?)",
        (channel_id, req_flag, invite_link)
    )
    await message.answer(f"{channel_id} qo'shildi. Join link saqlandi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_new_admin)
async def proc_add_adm(message: types.Message, state: FSMContext):
    text = message.text.strip()
    user_id = None

    if text.startswith("@"):
        try:
            chat = await bot.get_chat(text)
            user_id = chat.id
        except Exception as exc:
            logger.error("Username lookup failed for %s: %s", text, exc)
            await message.answer("Username topilmadi. To'g'ri @username yuboring.")
            return
    elif text.isdigit():
        user_id = int(text)
    else:
        await message.answer("Username (@user) yoki raqamli ID yuboring.")
        return

    db_query("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (user_id, message.from_user.id))
    await message.answer(f"{text} admin qilindi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_movie_channel)
async def proc_set_movie_ch(message: types.Message, state: FSMContext):
    db_query("UPDATE settings SET value = ? WHERE key = 'movie_channel'", (message.text,))
    await message.answer(f"Kino kanali {message.text} ga o'zgartirildi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_broadcast)
async def proc_broadcast(message: types.Message, state: FSMContext):
    users = [row[0] for row in db_query("SELECT user_id FROM users", fetchall=True)]
    count = 0
    msg = await message.answer(f"Yuborilmoqda: 0/{len(users)}")
    for i, u_id in enumerate(users):
        try:
            await message.copy_to(u_id)
            count += 1
        except TelegramRetryAfter as exc:
            logger.warning("Flood wait %.2fs when sending to %s", exc.retry_after, u_id)
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await message.copy_to(u_id)
                count += 1
            except Exception as retry_exc:
                logger.error("Second attempt failed for %s: %s", u_id, retry_exc)
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot; skipping.", u_id)
        except Exception as exc:
            logger.error("Broadcast failed for %s: %s", u_id, exc)
        if count % 20 == 0:
            await msg.edit_text(f"Yuborilmoqda: {count}/{len(users)}")
        await asyncio.sleep(0.05)
    await msg.edit_text(f"Tugatildi. {count} ta foydalanuvchiga yuborildi.")
    await state.clear()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
