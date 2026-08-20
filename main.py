# main.py
import os
import sys
import shutil
import psutil
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import json
import io
import base64
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False
from time import time
from datetime import datetime, timedelta

# ============ SINGLE INSTANCE CHECK ============
load_dotenv()
PID_FILE = os.path.join(os.getenv("DATA_DIR", "."), "bot.pid")

def check_single_instance():
    """Prevent multiple bot instances with a portable atomic PID file."""
    try:
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as existing:
                    old_pid = int(existing.read().strip())
                if old_pid != os.getpid() and psutil.pid_exists(old_pid):
                    print(f"Another instance is already running (PID {old_pid}).")
                    sys.exit(1)
            except (OSError, TypeError, ValueError):
                pass
            try:
                os.unlink(PID_FILE)
            except FileNotFoundError:
                pass

        descriptor = os.open(PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        pid_file = os.fdopen(descriptor, "w", encoding="utf-8")
        pid_file.write(str(os.getpid()))
        pid_file.flush()
        print(f"Bot started with PID: {os.getpid()}")
        return pid_file
    except FileExistsError:
        print("Another bot instance started at the same time.")
        sys.exit(1)
    except Exception as e:
        print(f"Could not create PID file: {e}")
        return None

# ============ SETUP LOGGING ============
os.makedirs("logs", exist_ok=True)

log_filename = f"logs/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(log_filename, maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(sys.stdout)
    ]
)

# ============ PATCH PYROGRAM SESSION ============

def patch_pyrogram_session():
    """Patch Pyrogram's session.send method to catch FloodWait."""
    try:
        from pyrogram.session import Session
        from pyrogram.errors import FloodWait
        import asyncio
        import re
        
        original_send = Session.send
        
        async def patched_send(self, data, timeout=Session.START_TIMEOUT, wait_response=True):
            try:
                return await original_send(self, data, timeout, wait_response)
            except FloodWait as e:
                wait_time = e.value
                print(f"⏳ FLOOD WAIT: {wait_time}s in session.send, waiting...")
                await asyncio.sleep(wait_time + 2)
                return await original_send(self, data, timeout, wait_response)
            except Exception as e:
                error_msg = str(e)
                if "FLOOD_WAIT" in error_msg:
                    match = re.search(r'wait of (\d+)', error_msg)
                    if match:
                        wait_time = int(match.group(1))
                        print(f"⏳ FLOOD WAIT: {wait_time}s in session.send, waiting...")
                        await asyncio.sleep(wait_time + 2)
                        return await original_send(self, data, timeout, wait_response)
                raise
        
        Session.send = patched_send
        print("✅ Pyrogram session.send patched!")
        return True
        
    except Exception as e:
        print(f"⚠️ Error patching Pyrogram session: {e}")
        import traceback
        traceback.print_exc()
        return False

# Pyrofork's built-in sleep_threshold handles short flood waits. Monkeypatching
# the transport layer can stall update acknowledgements and is intentionally
# disabled in production.

# ============ NOW IMPORT EVERYTHING ELSE ============

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid, BadRequest
from pyrogram.errors import SessionPasswordNeeded
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand, LoginToken

from config import Config
from logger import get_logger
from database import Database
from helpers.downloader import Downloader
from helpers.forward import check_forward_permission, resolve_forward_chat_id
from helpers.files import (
    get_readable_file_size, get_readable_time,
    cleanup_downloads_root, cleanup_old_downloads
)
from helpers.msg import (
    is_story_link, 
    getStoryChatMsgID, 
    getChatMsgID,
    parse_chat_identifier,
    format_message_link,
    debug_url,
    is_valid_telegram_url
)
from helpers.chats import resolve_chat, preferred_chat_reference, chat_title as get_chat_title
from helpers.files import get_readable_file_size
from i18n import tr
from security import secret_box
from session_manager import UserSessionManager

logger = get_logger(__name__)
db = Database()

# ============ INITIALIZE BOT CLIENTS ============

bot = Client(
    "media_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    # Bot authorization is fully represented by BOT_TOKEN. Keeping an MTProto
    # session on disk can preserve stale update state after deployments or an
    # unclean shutdown, leaving a connected bot that receives no new messages.
    in_memory=True,
    workers=4,
    parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=1,
    sleep_threshold=30,
)

# Initialize user client - will prompt for credentials if no session exists
user = Client(
    "user_session", api_id=Config.API_ID, api_hash=Config.API_HASH,
    session_string=Config.SESSION_STRING, workers=2,
    max_concurrent_transmissions=1, sleep_threshold=30,
) if Config.SESSION_STRING else None

# ============ GLOBAL VARIABLES ============

RUNNING_TASKS = set()
USER_TASKS = {}
download_semaphore = None
forward_chat_id = None
downloader = None
session_manager = UserSessionManager(db)
user_downloaders = {}
FORWARD_SETUP_COMPLETE = False
SHUTDOWN_IN_PROGRESS = False
pid_lock = check_single_instance()

# ============ TASK MANAGEMENT ============

def track_task(coro, user_id=None):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    if user_id is not None:
        USER_TASKS.setdefault(user_id, set()).add(task)
    def _remove(_):
        if not SHUTDOWN_IN_PROGRESS:
            RUNNING_TASKS.discard(task)
            if user_id is not None:
                USER_TASKS.get(user_id, set()).discard(task)
    task.add_done_callback(_remove)
    return task


async def cancel_user_tasks(user_id):
    tasks = list(USER_TASKS.get(user_id, set()))
    active = []
    for task in tasks:
        if not task.done():
            task.cancel()
            active.append(task)
    if active:
        await asyncio.gather(*active, return_exceptions=True)
    USER_TASKS.pop(user_id, None)
    return len(active)


def user_language(user_id):
    return db.get_user_profile(user_id).language


def is_admin(user_id):
    return bool(Config.ADMIN_USER_ID and user_id == Config.ADMIN_USER_ID)


async def get_user_downloader(user_id):
    account = await session_manager.get(user_id)
    if not account:
        return None
    existing = user_downloaders.get(user_id)
    if existing and existing.user is account.client:
        return existing
    instance = Downloader(
        account.client, bot, download_semaphore,
        owner_user_id=user_id, admin_chat_id=Config.ADMIN_USER_ID,
    )
    user_downloaders[user_id] = instance
    return instance


async def connected_account(user_id):
    """Return the user's client, including the Linux-managed admin account."""
    return await session_manager.get(user_id)


def _state_payload(data):
    return secret_box.encrypt(json.dumps(data)) if data else None


def _read_state_payload(value):
    if not value:
        return {}
    return json.loads(secret_box.decrypt(value))

async def cancel_all_tasks():
    global SHUTDOWN_IN_PROGRESS
    SHUTDOWN_IN_PROGRESS = True
    cancelled = 0
    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    return cancelled

# ============ FORWARD CHAT SETUP ============

async def setup_forward_chat():
    global forward_chat_id, FORWARD_SETUP_COMPLETE
    
    if FORWARD_SETUP_COMPLETE:
        return
    
    if not Config.FORWARD_CHAT_ID:
        logger.info("ℹ️ No FORWARD_CHAT_ID configured in .env")
        FORWARD_SETUP_COMPLETE = True
        return
    
    try:
        forward_chat_id = await resolve_forward_chat_id(Config.FORWARD_CHAT_ID)
        logger.info(f"Attempting to setup forward chat: {forward_chat_id}")
        
        try:
            chat = await bot.get_chat(forward_chat_id)
            logger.info(f"✅ Forward chat found: {chat.title} (ID: {forward_chat_id})")
            
            try:
                await bot.send_message(
                    chat_id=forward_chat_id,
                    text="🔍 Permission test - bot is active"
                )
                logger.info(f"✅ Auto-forward enabled to: {forward_chat_id}")
                FORWARD_SETUP_COMPLETE = True
                
            except FloodWait as e:
                wait_time = e.value if hasattr(e, 'value') else 30
                logger.warning(f"⏳ FloodWait on setup: {wait_time}s")
                await asyncio.sleep(wait_time + 2)
                # Retry
                await bot.send_message(
                    chat_id=forward_chat_id,
                    text="🔍 Permission test - bot is active"
                )
                logger.info(f"✅ Auto-forward enabled to: {forward_chat_id}")
                FORWARD_SETUP_COMPLETE = True
                
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"⚠️ Could not send test message: {error_msg}")
                
                if "not enough rights" in error_msg.lower() or "not admin" in error_msg.lower():
                    logger.warning("⚠️ Bot doesn't have permission to send messages")
                    forward_chat_id = None
                    FORWARD_SETUP_COMPLETE = True
                    logger.info("Make the bot an admin in the forward chat")
                elif "Client has not been started" in error_msg:
                    logger.info("⏳ Bot client not started yet. Will retry...")
                    FORWARD_SETUP_COMPLETE = False
                else:
                    forward_chat_id = None
                    FORWARD_SETUP_COMPLETE = True
                    
        except FloodWait as e:
            wait_time = e.value if hasattr(e, 'value') else 30
            logger.warning(f"⏳ FloodWait on setup: {wait_time}s")
            await asyncio.sleep(wait_time + 2)
            chat = await bot.get_chat(forward_chat_id)
            logger.info(f"✅ Forward chat found after retry: {chat.title}")
            FORWARD_SETUP_COMPLETE = True
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Failed to access forward chat: {error_msg}")
            
            if "Client has not been started" in error_msg:
                logger.info("⏳ Bot client not started yet. Will retry on first download...")
            else:
                logger.info("Make sure the bot is a member of the forward chat")
                forward_chat_id = None
                FORWARD_SETUP_COMPLETE = True
                
    except Exception as e:
        logger.error(f"❌ Error setting up forward chat: {e}")
        forward_chat_id = None
        FORWARD_SETUP_COMPLETE = True

async def ensure_forward_chat_setup():
    global FORWARD_SETUP_COMPLETE
    if not FORWARD_SETUP_COMPLETE and Config.FORWARD_CHAT_ID:
        await setup_forward_chat()

# ============ STARTUP CLIENTS ============

async def initialize_clients():
    """Start both clients on the same event loop."""
    try:
        if user:
            await user.start()
            user_me = await user.get_me()
            logger.info(f"👤 Legacy server account started: @{user_me.username} (ID: {user_me.id})")
            await session_manager.attach_external(Config.ADMIN_USER_ID, user)

        await bot.start()
        bot_me = await bot.get_me()
        logger.info(f"🤖 Bot started: @{bot_me.username} (ID: {bot_me.id})")
        handler_count = sum(len(group) for group in bot.dispatcher.groups.values())
        logger.info(f"📨 Dispatcher ready with {handler_count} registered handlers")
        await bot.set_bot_commands([
            BotCommand("start", "Start or choose language"),
            BotCommand("menu", "Open the interactive menu"),
            BotCommand("help", "Open the step-by-step guide"),
            BotCommand("dl", "Open download tools"),
            BotCommand("list", "Browse accessible chats"),
            BotCommand("settings", "Forwarding and language"),
            BotCommand("stats", "Download statistics and recovery"),
            BotCommand("whoami", "Connected Telegram account"),
        ])
        await bot.set_bot_commands([
            BotCommand("start", "شروع یا انتخاب زبان"),
            BotCommand("menu", "باز کردن منوی تعاملی"),
            BotCommand("help", "نمایش راهنمای گام‌به‌گام"),
            BotCommand("dl", "باز کردن ابزار دانلود"),
            BotCommand("list", "مرور گفتگوهای قابل دسترس"),
            BotCommand("settings", "مقصد ارسال و زبان"),
            BotCommand("stats", "آمار دانلود و بازیابی"),
            BotCommand("whoami", "حساب تلگرام متصل"),
        ], language_code="fa")
        
        return True
    except Exception as e:
        logger.error(f"❌ Failed to start Telegram clients: {e}")
        if getattr(bot, "is_connected", False):
            await bot.stop()
        if user and getattr(user, "is_connected", False):
            await user.stop()
        logger.info("Check BOT_TOKEN, API credentials, and SESSION_STRING in .env")
        return False

# ============ COMMAND HANDLERS ============

def main_keyboard(language, admin=False):
    rows = [
        [InlineKeyboardButton(tr(language, "btn_download"), callback_data="ui:download"),
         InlineKeyboardButton(tr(language, "btn_account"), callback_data="ui:account")],
        [InlineKeyboardButton(tr(language, "btn_browse"), callback_data="ui:browse")],
        [InlineKeyboardButton(tr(language, "btn_settings"), callback_data="ui:settings"),
         InlineKeyboardButton(tr(language, "btn_tools"), callback_data="ui:tools")],
        [InlineKeyboardButton(tr(language, "btn_help"), callback_data="ui:help")],
    ]
    if admin:
        rows.insert(-1, [InlineKeyboardButton(tr(language, "btn_admin"), callback_data="ui:admin")])
    return InlineKeyboardMarkup(rows)


def download_keyboard(language):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_single"), callback_data="ui:single"),
         InlineKeyboardButton(tr(language, "btn_story"), callback_data="ui:story")],
        [InlineKeyboardButton(tr(language, "btn_bulk"), callback_data="ui:bulk"),
         InlineKeyboardButton(tr(language, "btn_batch"), callback_data="ui:range")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ])


def account_keyboard(language, connected=False, server_managed=False):
    rows = [
        [InlineKeyboardButton(tr(language, "btn_status"), callback_data="ui:account_status")],
        [InlineKeyboardButton(tr(language, "btn_connect"), callback_data="ui:connect")],
    ]
    if connected and not server_managed:
        rows.append([InlineKeyboardButton(tr(language, "btn_disconnect"), callback_data="ui:disconnect")])
    rows.append([InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")])
    return InlineKeyboardMarkup(rows)


def tools_keyboard(language):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_clear_state"), callback_data="ui:clear_state")],
        [InlineKeyboardButton(tr(language, "btn_cancel"), callback_data="ui:cancel_jobs")],
        [InlineKeyboardButton(tr(language, "btn_stats"), callback_data="ui:stats")],
        [InlineKeyboardButton(tr(language, "btn_getid"), callback_data="ui:getid")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ])


def browse_keyboard(language):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_recent"), callback_data="ui:browse_recent"),
         InlineKeyboardButton(tr(language, "btn_latest"), callback_data="ui:browse_latest")],
        [InlineKeyboardButton(tr(language, "btn_membership"), callback_data="ui:browse_membership")],
        [InlineKeyboardButton(tr(language, "btn_test_url"), callback_data="ui:test_url")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ])


def settings_keyboard(language):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_forward"), callback_data="ui:set_forward")],
        [InlineKeyboardButton(tr(language, "btn_show_forward"), callback_data="ui:show_forward"),
         InlineKeyboardButton(tr(language, "btn_test_forward"), callback_data="ui:test_forward")],
        [InlineKeyboardButton(tr(language, "btn_clear_forward"), callback_data="ui:clear_forward")],
        [InlineKeyboardButton(tr(language, "btn_language"), callback_data="ui:language")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ])


def admin_keyboard(language):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_botstats"), callback_data="ui:admin_stats")],
        [InlineKeyboardButton(tr(language, "btn_logs"), callback_data="ui:admin_logs"),
         InlineKeyboardButton(tr(language, "btn_cleanup"), callback_data="ui:admin_cleanup")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ])


def code_keyboard(language):
    rows = []
    for start in (1, 4, 7):
        rows.append([InlineKeyboardButton(str(number), callback_data=f"ui:code:{number}")
                     for number in range(start, start + 3)])
    rows.append([
        InlineKeyboardButton(tr(language, "btn_erase"), callback_data="ui:code:back"),
        InlineKeyboardButton("0", callback_data="ui:code:0"),
        InlineKeyboardButton(tr(language, "btn_submit"), callback_data="ui:code:submit"),
    ])
    rows.append([InlineKeyboardButton(tr(language, "btn_auth_cancel"), callback_data="ui:auth_cancel")])
    return InlineKeyboardMarkup(rows)


def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="ui:lang:en"),
         InlineKeyboardButton("فارسی", callback_data="ui:lang:fa")],
    ])


async def render_panel(target, text, keyboard):
    try:
        await target.edit(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        await target.reply(text, reply_markup=keyboard, disable_web_page_preview=True)


async def show_main(target, user_id, welcome=False):
    language = user_language(user_id)
    text = tr(language, "welcome") + "\n\n" + tr(language, "main_title") if welcome else tr(language, "main_title")
    await render_panel(target, text, main_keyboard(language, is_admin(user_id)))


async def send_qr_panel(message, language, login_token, waiting=False):
    login_url = "tg://login?token=" + base64.urlsafe_b64encode(login_token.token).decode().rstrip("=")
    import qrcode
    image = qrcode.make(login_url)
    buffer = io.BytesIO()
    buffer.name = "restdl-login.png"
    image.save(buffer, format="PNG")
    buffer.seek(0)
    caption = (tr(language, "qr_waiting") + "\n\n" if waiting else "") + tr(language, "scan_qr")
    await message.reply_photo(
        buffer, caption=caption,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(tr(language, "btn_scanned"), callback_data="ui:qr_complete")],
            [InlineKeyboardButton(tr(language, "btn_auth_cancel"), callback_data="ui:auth_cancel")],
        ]),
    )


async def handle_ui_callback(callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    language = user_language(user_id)
    action = data[3:]
    await callback_query.answer()

    if action.startswith("lang:"):
        language = action.split(":", 1)[1]
        db.update_user_profile(user_id, language=language, onboarding_complete=True)
        await show_main(callback_query.message, user_id, welcome=True)
        return
    if action == "main":
        await show_main(callback_query.message, user_id)
    elif action == "download":
        await render_panel(callback_query.message, tr(language, "download_title"), download_keyboard(language))
    elif action == "browse":
        await render_panel(callback_query.message, tr(language, "browse_title"), browse_keyboard(language))
    elif action == "account":
        connected = bool(await connected_account(user_id))
        await render_panel(
            callback_query.message, tr(language, "account_title"),
            account_keyboard(language, connected, is_admin(user_id) and bool(user)),
        )
    elif action == "settings":
        await render_panel(callback_query.message, tr(language, "settings_title"), settings_keyboard(language))
    elif action == "tools":
        await render_panel(callback_query.message, tr(language, "tools_title"), tools_keyboard(language))
    elif action == "help":
        await render_panel(callback_query.message, tr(language, "help_title"), InlineKeyboardMarkup([
            [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")]
        ]))
    elif action == "language":
        await render_panel(callback_query.message, tr(language, "choose_language"), language_keyboard())
    elif action == "connect":
        if is_admin(user_id) and user:
            account = await connected_account(user_id)
            await callback_query.message.reply(tr(
                language, "admin_server_session", name=account.display_name,
                username=account.username or "-",
            ))
            return
        if not secret_box.available:
            await callback_query.message.reply(tr(language, "invalid_setup", error="SESSION_ENCRYPTION_KEY is missing"))
            return
        db.set_conversation_state(user_id, "setup_api_id", expires_at=datetime.utcnow() + timedelta(minutes=15))
        await callback_query.message.reply(tr(language, "setup_intro") + "\n\n" + tr(language, "ask_api_id"))
    elif action in {"setup_session", "setup_qr", "setup_phone"}:
        state = db.get_conversation_state(user_id)
        if not state or state.state != "setup_method":
            await callback_query.message.reply(tr(language, "cancelled"))
            return
        if action == "setup_session":
            db.set_conversation_state(
                user_id, "setup_session", state.payload,
                datetime.utcnow() + timedelta(minutes=15),
            )
            await callback_query.message.reply(tr(language, "ask_session"))
        elif action == "setup_phone":
            db.set_conversation_state(
                user_id, "setup_phone_number", state.payload,
                datetime.utcnow() + timedelta(minutes=15),
            )
            await callback_query.message.reply(tr(language, "ask_phone"))
        else:
            payload = _read_state_payload(state.payload)
            try:
                login_token = await session_manager.begin_qr(
                    user_id, int(payload["api_id"]), payload["api_hash"]
                )
                await send_qr_panel(callback_query.message, language, login_token)
            except SessionPasswordNeeded:
                db.set_conversation_state(user_id, "setup_qr_password", expires_at=datetime.utcnow() + timedelta(minutes=5))
                await callback_query.message.reply(tr(language, "qr_2fa"))
            except Exception as exc:
                logger.warning("QR setup failed for user %s: %s", user_id, exc)
                await callback_query.message.reply(tr(language, "qr_error", error=str(exc)[:120]))
    elif action == "qr_complete":
        try:
            result = await session_manager.complete_qr(user_id)
            if isinstance(result, LoginToken):
                await send_qr_panel(callback_query.message, language, result, waiting=True)
                return
            account = result
            db.clear_conversation_state(user_id)
            user_downloaders.pop(user_id, None)
            await callback_query.message.reply(tr(
                language, "connected", name=account.display_name,
                username=account.username or "-",
            ), reply_markup=main_keyboard(language))
        except SessionPasswordNeeded:
            db.set_conversation_state(user_id, "setup_qr_password", expires_at=datetime.utcnow() + timedelta(minutes=5))
            await callback_query.message.reply(tr(language, "qr_2fa"))
        except Exception as exc:
            logger.warning("QR completion failed for user %s: %s", user_id, exc)
            await callback_query.message.reply(tr(language, "qr_error", error=str(exc)[:120]))
    elif action.startswith("code:"):
        state = db.get_conversation_state(user_id)
        if not state or state.state != "setup_phone_code":
            await callback_query.message.reply(tr(language, "auth_cancelled"))
            return
        payload = _read_state_payload(state.payload)
        code = payload.get("code", "")
        key = action.split(":", 1)[1]
        if key == "back":
            code = code[:-1]
        elif key == "submit":
            if not code:
                await callback_query.message.reply(tr(language, "code_empty"), reply_markup=code_keyboard(language))
                return
            try:
                account = await session_manager.submit_phone_code(user_id, code)
                db.clear_conversation_state(user_id)
                user_downloaders.pop(user_id, None)
                await callback_query.message.reply(tr(
                    language, "connected", name=account.display_name,
                    username=account.username or "-",
                ), reply_markup=main_keyboard(language, is_admin(user_id)))
            except SessionPasswordNeeded:
                db.set_conversation_state(user_id, "setup_phone_password", expires_at=datetime.utcnow() + timedelta(minutes=5))
                await callback_query.message.reply(tr(language, "ask_password"))
            except Exception as exc:
                payload["code"] = ""
                db.set_conversation_state(user_id, "setup_phone_code", _state_payload(payload), datetime.utcnow() + timedelta(minutes=10))
                await callback_query.message.reply(tr(language, "invalid_setup", error=str(exc)[:120]), reply_markup=code_keyboard(language))
            return
        elif key.isdigit() and len(code) < 8:
            code += key
        payload["code"] = code
        db.set_conversation_state(user_id, "setup_phone_code", _state_payload(payload), datetime.utcnow() + timedelta(minutes=10))
        masked = "•" * len(code) if code else "—"
        await render_panel(callback_query.message, tr(language, "code_sent", code=masked), code_keyboard(language))
    elif action == "auth_cancel":
        await session_manager.cancel_pending(user_id)
        db.clear_conversation_state(user_id)
        await callback_query.message.reply(tr(language, "auth_cancelled"), reply_markup=main_keyboard(language, is_admin(user_id)))
    elif action == "account_status":
        account = await connected_account(user_id)
        if account:
            key = "admin_server_session" if is_admin(user_id) and user else "status_connected"
            text = tr(language, key, name=account.display_name, username=account.username or "-")
        else:
            text = tr(language, "status_disconnected")
        await callback_query.message.reply(text)
    elif action == "disconnect":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(tr(language, "btn_confirm"), callback_data="ui:disconnect_yes"),
            InlineKeyboardButton(tr(language, "btn_no"), callback_data="ui:account"),
        ]])
        await render_panel(callback_query.message, tr(language, "btn_disconnect") + "?", keyboard)
    elif action == "disconnect_yes":
        if is_admin(user_id) and user:
            await callback_query.message.reply(tr(language, "admin_disconnect_blocked"))
            return
        await cancel_user_tasks(user_id)
        await session_manager.disconnect(user_id, erase=True)
        user_downloaders.pop(user_id, None)
        await callback_query.message.reply(tr(language, "session_removed"))
        await show_main(callback_query.message, user_id)
    elif action in {"single", "story", "bulk", "range", "set_forward"}:
        if action != "set_forward" and not await connected_account(user_id):
            await callback_query.message.reply(tr(language, "not_connected"))
            return
        state_map = {"single": "download_single", "story": "download_single", "bulk": "download_bulk", "range": "download_range", "set_forward": "set_forward"}
        db.set_conversation_state(user_id, state_map[action], expires_at=datetime.utcnow() + timedelta(minutes=15))
        prompts = {
            "single": tr(language, "ask_link"), "story": tr(language, "ask_link"), "bulk": tr(language, "ask_bulk"),
            "range": tr(language, "ask_range"),
            "set_forward": tr(language, "ask_forward"),
        }
        await callback_query.message.reply(prompts[action])
    elif action == "clear_state":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(tr(language, "btn_confirm"), callback_data="ui:clear_state_yes"),
            InlineKeyboardButton(tr(language, "btn_no"), callback_data="ui:tools"),
        ]])
        await render_panel(callback_query.message, tr(language, "btn_clear_state") + "?", keyboard)
    elif action == "clear_state_yes":
        await cancel_user_tasks(user_id)
        instance = user_downloaders.get(user_id)
        if instance:
            instance.clear_state()
        db.clear_user_download_state(user_id)
        db.clear_conversation_state(user_id)
        await callback_query.message.reply(tr(language, "state_cleared"))
        await show_main(callback_query.message, user_id)
    elif action == "cancel_jobs":
        count = await cancel_user_tasks(user_id)
        await callback_query.message.reply(f"{tr(language, 'cancelled')} ({count})")
    elif action == "stats":
        stats = db.get_stats(user_id)
        await callback_query.message.reply(tr(
            language, "stats", total=stats["total"], successful=stats["successful"],
            failed=stats["failed"], size=get_readable_file_size(stats["total_size"]),
        ))
    elif action == "getid":
        await callback_query.message.reply(f"🆔 `{user_id}`")
    elif action in {"browse_recent", "browse_latest", "browse_membership", "test_url"}:
        if action != "test_url" and not await connected_account(user_id):
            await callback_query.message.reply(tr(language, "not_connected"))
            return
        state_name = {
            "browse_recent": "browse_recent", "browse_latest": "browse_latest",
            "browse_membership": "browse_membership", "test_url": "test_url",
        }[action]
        db.set_conversation_state(user_id, state_name, expires_at=datetime.utcnow() + timedelta(minutes=15))
        prompt = tr(language, "ask_test_url") if action == "test_url" else tr(language, "ask_browse")
        await callback_query.message.reply(prompt)
    elif action == "show_forward":
        settings = db.get_user_settings(user_id)
        status = (f"{settings.forward_chat_title or settings.forward_chat_id} (`{settings.forward_chat_id}`)"
                  if settings and settings.forward_chat_id else tr(language, "forward_not_set"))
        await callback_query.message.reply(tr(language, "settings_forward", status=status), reply_markup=settings_keyboard(language))
    elif action == "clear_forward":
        db.update_user_settings(user_id, forward_chat_id=None, forward_chat_title=None)
        await callback_query.message.reply(tr(language, "forward_cleared"), reply_markup=settings_keyboard(language))
    elif action == "test_forward":
        settings = db.get_user_settings(user_id)
        if not settings or not settings.forward_chat_id:
            await callback_query.message.reply(tr(language, "forward_not_set"))
            return
        probe = await bot.send_message(settings.forward_chat_id, tr(language, "forward_probe"))
        await probe.delete()
        await callback_query.message.reply(tr(language, "forward_test_ok"))
    elif action == "admin":
        if not is_admin(user_id):
            return
        await render_panel(callback_query.message, tr(language, "admin_title"), admin_keyboard(language))
    elif action == "admin_stats":
        if not is_admin(user_id):
            return
        stats = db.get_stats()
        active_tasks = sum(1 for tasks in USER_TASKS.values() for task in tasks if not task.done())
        await callback_query.message.reply(tr(
            language, "bot_stats_short", uptime=get_readable_time(time() - Config.BOT_START_TIME),
            total=stats["total"], successful=stats["successful"], failed=stats["failed"],
            tasks=active_tasks,
        ))
    elif action == "admin_logs":
        if not is_admin(user_id):
            return
        log_files = sorted([f for f in os.listdir("logs") if f.endswith(".log")], reverse=True)
        if log_files:
            await callback_query.message.reply_document(os.path.join("logs", log_files[0]))
    elif action == "admin_cleanup":
        if not is_admin(user_id):
            return
        files_removed, bytes_freed = cleanup_old_downloads(7)
        await callback_query.message.reply(tr(
            language, "cleanup_done", files=files_removed,
            size=get_readable_file_size(bytes_freed),
        ))


@bot.on_message(filters.private, group=-90)
async def handle_conversation_input(_, message: Message):
    if not message.from_user or not message.text or message.text.startswith("/"):
        return
    user_id = message.from_user.id
    state = db.get_conversation_state(user_id)
    if not state:
        return
    if state.expires_at and state.expires_at < datetime.utcnow():
        db.clear_conversation_state(user_id)
        return
    message.stop_propagation()
    language = user_language(user_id)
    value = message.text.strip()
    try:
        if state.state == "setup_api_id":
            if not value.isdigit() or int(value) <= 0:
                await message.reply(tr(language, "invalid_api_id"))
                return
            db.set_conversation_state(
                user_id, "setup_api_hash", _state_payload({"api_id": int(value)}),
                datetime.utcnow() + timedelta(minutes=15),
            )
            await message.reply(tr(language, "ask_api_hash"))
        elif state.state == "setup_api_hash":
            payload = _read_state_payload(state.payload)
            if len(value) < 16:
                raise ValueError("API hash is too short")
            payload["api_hash"] = value
            try:
                await message.delete()
            except Exception:
                pass
            db.set_conversation_state(
                user_id, "setup_method", _state_payload(payload),
                datetime.utcnow() + timedelta(minutes=15),
            )
            await bot.send_message(user_id, tr(language, "choose_login"), reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(tr(language, "btn_qr"), callback_data="ui:setup_qr"),
                InlineKeyboardButton(tr(language, "btn_phone"), callback_data="ui:setup_phone"),
            ], [
                InlineKeyboardButton(tr(language, "btn_import"), callback_data="ui:setup_session"),
            ]]))
        elif state.state == "setup_phone_number":
            payload = _read_state_payload(state.payload)
            try:
                await message.delete()
            except Exception:
                pass
            await session_manager.begin_phone(
                user_id, int(payload["api_id"]), payload["api_hash"], value
            )
            db.set_conversation_state(
                user_id, "setup_phone_code", _state_payload({"code": ""}),
                datetime.utcnow() + timedelta(minutes=10),
            )
            await bot.send_message(
                user_id,
                tr(language, "code_sent", code="—"),
                reply_markup=code_keyboard(language),
            )
        elif state.state == "setup_phone_code":
            # Telegram invalidates login codes posted as chat messages. Keep the
            # flow alive, remove the accidental message, and require callbacks.
            try:
                await message.delete()
            except Exception:
                pass
            await bot.send_message(
                user_id,
                tr(language, "code_sent", code="—"),
                reply_markup=code_keyboard(language),
            )
        elif state.state == "setup_session":
            payload = _read_state_payload(state.payload)
            try:
                await message.delete()
            except Exception:
                pass
            account = await session_manager.import_session(
                user_id, int(payload["api_id"]), payload["api_hash"], value
            )
            db.clear_conversation_state(user_id)
            user_downloaders.pop(user_id, None)
            await bot.send_message(user_id, tr(
                language, "connected", name=account.display_name,
                username=account.username or "-",
            ), reply_markup=main_keyboard(language, is_admin(user_id)))
        elif state.state in {"setup_phone_password", "setup_qr_password"}:
            try:
                await message.delete()
            except Exception:
                pass
            if state.state == "setup_phone_password":
                account = await session_manager.submit_password(user_id, value)
            else:
                account = await session_manager.submit_qr_password(user_id, value)
            db.clear_conversation_state(user_id)
            user_downloaders.pop(user_id, None)
            await bot.send_message(user_id, tr(
                language, "connected", name=account.display_name,
                username=account.username or "-",
            ), reply_markup=main_keyboard(language, is_admin(user_id)))
        elif state.state in {"download_single", "download_bulk"}:
            instance = await get_user_downloader(user_id)
            if not instance:
                await message.reply(tr(language, "not_connected"))
                return
            db.clear_conversation_state(user_id)
            settings = db.get_user_settings(user_id)
            destination = settings.forward_chat_id if settings else None
            if state.state == "download_single":
                track_task(instance.download_media(message, value, destination), user_id)
            else:
                track_task(instance.download_all_channel_media_optimized(message, value, destination), user_id)
            await message.reply(tr(language, "queued"), reply_markup=main_keyboard(language, is_admin(user_id)))
        elif state.state == "set_forward":
            parsed = parse_chat_identifier(value)
            chat = await bot.get_chat(parsed)
            test = await bot.send_message(chat.id, tr(language, "forward_probe"))
            await test.delete()
            db.update_user_settings(
                user_id, forward_chat_id=str(chat.id),
                forward_chat_title=getattr(chat, "title", None) or str(chat.id),
            )
            db.clear_conversation_state(user_id)
            await message.reply(
                tr(language, "forward_verified", destination=getattr(chat, "title", None) or chat.id),
                reply_markup=main_keyboard(language, is_admin(user_id)),
            )
        elif state.state == "download_range":
            links = value.split()
            if len(links) != 2:
                raise ValueError(tr(language, "range_two_required"))
            start_chat, start_id = getChatMsgID(links[0])
            end_chat, end_id = getChatMsgID(links[1])
            if start_chat != end_chat or start_id > end_id or end_id - start_id > 500:
                raise ValueError(tr(language, "range_invalid"))
            instance = await get_user_downloader(user_id)
            settings = db.get_user_settings(user_id)
            destination = settings.forward_chat_id if settings else None
            prefix = links[0].rsplit("/", 1)[0]
            db.clear_conversation_state(user_id)
            async def run_range():
                for message_id in range(start_id, end_id + 1):
                    await instance.download_media(message, f"{prefix}/{message_id}", destination)
            track_task(run_range(), user_id)
            await message.reply(tr(language, "queued"), reply_markup=main_keyboard(language, is_admin(user_id)))
        elif state.state in {"browse_recent", "browse_latest", "browse_membership"}:
            account = await connected_account(user_id)
            if not account:
                await message.reply(tr(language, "not_connected"))
                return
            parsed = parse_chat_identifier(value)
            chat = await resolve_chat(account.client, parsed)
            reference = preferred_chat_reference(chat)
            title = get_chat_title(chat)
            if state.state == "browse_membership":
                try:
                    await account.client.get_chat_member(reference, "me")
                    response = tr(language, "membership_yes", chat=title, chat_id=chat.id)
                except Exception as exc:
                    response = tr(language, "membership_no", error=str(exc)[:140])
            elif state.state == "browse_latest":
                latest = None
                async for item in account.client.get_chat_history(reference, limit=1):
                    latest = item
                    break
                if not latest:
                    raise ValueError("No messages found")
                link_ref = f"@{chat.username}" if getattr(chat, "username", None) else chat.id
                response = tr(
                    language, "latest_result", chat=title, message_id=latest.id,
                    date=latest.date, media="✅" if latest.media else "❌",
                    link=format_message_link(link_ref, latest.id),
                )
            else:
                entries = []
                link_ref = f"@{chat.username}" if getattr(chat, "username", None) else chat.id
                async for item in account.client.get_chat_history(reference, limit=10):
                    if item.media or item.text or item.caption:
                        preview = (item.text or item.caption or "")[:80].replace("\n", " ")
                        entries.append(f"`{item.id}` · {preview or 'media'}\n{format_message_link(link_ref, item.id)}")
                    if len(entries) == 5:
                        break
                response = tr(language, "recent_title", chat=title) + "\n\n" + "\n\n".join(entries)
            db.clear_conversation_state(user_id)
            await message.reply(response, reply_markup=browse_keyboard(language), disable_web_page_preview=True)
        elif state.state == "test_url":
            result = debug_url(value)
            db.clear_conversation_state(user_id)
            await message.reply(tr(
                language, "url_result", valid=result["is_valid"], type=result["type"],
                chat=result["chat_id"], message=result["message_id"],
                error=result["error"] or "-",
            ), reply_markup=browse_keyboard(language))
    except Exception as exc:
        logger.exception("Interactive flow failed for user %s", user_id)
        await message.reply(tr(language, "invalid_setup", error=str(exc)[:160]))


LEGACY_MENU_COMMANDS = [
    "dl", "dls", "bdl", "bdls", "downloadall", "list", "latest",
    "settings", "setforward", "clearforward", "myforward", "testforwarding",
    "stats", "stopdownload", "killall", "cleanup", "cleandb", "logs",
    "botstats", "getid", "testurl", "setupforward", "whoami", "joincheck",
]


@bot.on_message(filters.command(LEGACY_MENU_COMMANDS) & filters.private, group=-95)
async def route_legacy_commands_to_menu(_, message: Message):
    """Keep old links/bookmarks working without exposing a second, stale UI."""
    language = user_language(message.from_user.id)
    command = (message.command[0] if message.command else "").lower()
    if command in {"dl", "dls", "bdl", "bdls", "downloadall"}:
        await message.reply(tr(language, "download_title"), reply_markup=download_keyboard(language))
    elif command in {"list", "latest", "joincheck", "testurl"}:
        await message.reply(tr(language, "browse_title"), reply_markup=browse_keyboard(language))
    elif command in {"settings", "setforward", "clearforward", "myforward", "testforwarding"}:
        await message.reply(tr(language, "settings_title"), reply_markup=settings_keyboard(language))
    elif command == "whoami":
        connected = bool(await connected_account(message.from_user.id))
        await message.reply(tr(language, "account_title"), reply_markup=account_keyboard(
            language, connected, is_admin(message.from_user.id) and bool(user)
        ))
    elif command in {"cleanup", "cleandb", "logs", "botstats"} and is_admin(message.from_user.id):
        await message.reply(tr(language, "admin_title"), reply_markup=admin_keyboard(language))
    else:
        await message.reply(tr(language, "tools_title"), reply_markup=tools_keyboard(language))
    message.stop_propagation()

@bot.on_message(group=-100)
async def log_incoming_message(_, message: Message):
    """Lightweight dispatch heartbeat; command handlers run in group 0."""
    sender_id = message.from_user.id if message.from_user else "unknown"
    # Never log message bodies: setup messages can contain API hashes or
    # session strings. Metadata is sufficient for dispatcher diagnostics.
    logger.info(f"📩 Update received from user {sender_id} (message {message.id})")

@bot.on_message(filters.command(["start", "menu"]) & filters.private)
async def start_command(_, message: Message):
    """Handle /start command"""
    logger.info(f"User {message.from_user.id} started the bot")
    profile = db.get_user_profile(message.from_user.id)
    if not profile.onboarding_complete:
        await message.reply(tr(profile.language, "choose_language"), reply_markup=language_keyboard())
    else:
        await show_main(message, message.from_user.id, welcome=True)
    return
    await ensure_forward_chat_setup()
    
    welcome_text = (
        "🚀 **Enhanced Media Downloader Bot**\n\n"
        "I can download media from any Telegram channel, even restricted ones!\n\n"
        "**📥 Features:**\n"
        "• Download posts, stories, and media groups\n"
        "• Batch download multiple posts\n"
        "• Download entire channels with `/downloadall`\n"
        "• Download from restricted channels\n"
        "• Auto-forward to another chat\n"
        "• Custom forward channel per user\n"
        "• Download history tracking\n"
        "• Support for all media types\n\n"
        "**📚 Commands:**\n"
        "• `/dl <url>` - Download single post\n"
        "• `/dls <url>` - Download story\n"
        "• `/bdl <start> <end>` - Batch download posts\n"
        "• `/bdls <start> <end>` - Batch download stories\n"
        "• `/downloadall <chat_id>` - Download ALL media from a channel\n"
        "• `/stopdownload` - Stop all downloads\n"
        "• `/list <chat_id>` - List recent messages\n"
        "• `/latest <chat_id>` - Get latest message\n"
        "• `/testurl <url>` - Test URL parsing\n"
        "• `/whoami` - Check current user account\n"
        "• `/joincheck <chat_id>` - Check channel membership\n"
        "• `/stats` - View your statistics\n"
        "• `/settings` - Configure your preferences\n"
        "• `/cleanup` - Clean temporary files\n"
        "• `/cleandb` - Clean database and session files\n"
        "• `/killall` - Cancel all downloads\n"
        "• `/logs` - Download bot logs\n"
        "• `/testforwarding` - Test forward chat\n"
        "• `/help` - Show this help\n\n"
        "**📤 Forward Channel Commands:**\n"
        "• `/setforward <chat_id>` - Set custom forward channel\n"
        "• `/clearforward` - Clear custom forward channel\n"
        "• `/myforward` - Check your current forward channel\n\n"
        "**🔑 Requirements:**\n"
        "• Your account must be a member of the channel\n"
        "• For private channels, accept the invite first\n\n"
        "Ready to download! Send me a Telegram link or chat ID."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
    ])
    
    await message.reply(welcome_text, reply_markup=keyboard, disable_web_page_preview=True)

@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    """Handle /help command"""
    language = user_language(message.from_user.id)
    await message.reply(
        tr(language, "help_title"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")
        ]]), disable_web_page_preview=True,
    )
    return
    help_text = (
        "📖 **Help & Commands Guide**\n\n"
        "**📥 Download Commands:**\n"
        "• `/dl <post_url>` - Download media from a post\n"
        "  Example: `/dl https://t.me/channel/123`\n"
        "  Example: `/dl https://t.me/c/1719871015/123`\n\n"
        "• `/dls <story_url>` - Download a story\n"
        "  Example: `/dls https://t.me/username/s/12`\n\n"
        "• `/bdl <start> <end>` - Batch download posts\n"
        "  Example: `/bdl https://t.me/channel/100 https://t.me/channel/120`\n\n"
        "• `/bdls <start> <end>` - Batch download stories\n"
        "  Example: `/bdls https://t.me/user/s/10 https://t.me/user/s/25`\n\n"
        "• `/downloadall <chat_id>` - Download ALL media from a channel\n"
        "  Example: `/downloadall -1001719871015`\n"
        "  ⚠️ This will download every media in the channel!\n\n"
        "• `/stopdownload` - Stop all running downloads\n\n"
        "**📤 Forward Channel Commands:**\n"
        "• `/setforward <chat_id>` - Set custom forward channel\n"
        "  Example: `/setforward -100123456789`\n"
        "• `/clearforward` - Clear custom forward channel\n"
        "• `/myforward` - Check your current forward channel\n"
        "• `/testforwarding` - Test if forwarding is working\n\n"
        "**🔍 Chat ID Commands:**\n"
        "• `/list <chat_id>` - List recent messages\n"
        "  Example: `/list -1001719871015`\n"
        "• `/latest <chat_id>` - Get latest message\n"
        "  Example: `/latest @channel_name`\n"
        "• `/testurl <url>` - Test URL parsing\n"
        "  Example: `/testurl https://t.me/c/1719871015/123`\n"
        "• `/whoami` - Check current user account\n"
        "• `/joincheck <chat_id>` - Check channel membership\n\n"
        "**⚙️ Utility Commands:**\n"
        "• `/stats` - View your download statistics\n"
        "• `/settings` - Configure your preferences\n"
        "• `/cleanup` - Clean temporary files\n"
        "• `/cleandb` - Clean database and session files\n"
        "• `/killall` - Cancel all running downloads\n"
        "• `/logs` - Download bot logs\n"
        "• `/getid` - Get current chat ID\n\n"
        "**💡 Tips:**\n"
        "• Just paste a Telegram link without any command\n"
        "• You can use chat IDs instead of full URLs\n"
        "• Media groups are downloaded automatically\n"
        "• Check /settings to configure your preferences"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
    ])
    
    await message.reply(help_text, reply_markup=keyboard, disable_web_page_preview=True)

# ============ TEST FORWARDING COMMAND ============

@bot.on_message(filters.command("testforwarding") & filters.private)
async def test_forwarding_command(_, message: Message):
    """Test if forwarding is working"""
    global forward_chat_id
    
    await ensure_forward_chat_setup()
    
    # Check user's custom forward first
    settings = db.get_user_settings(message.from_user.id)
    user_forward_chat_id = settings.forward_chat_id if settings else None
    
    if user_forward_chat_id:
        test_forward_id = user_forward_chat_id
        forward_type = "your custom forward channel"
    elif forward_chat_id:
        test_forward_id = forward_chat_id
        forward_type = "global forward channel"
    else:
        await message.reply(
            "❌ **No forward chat configured.**\n\n"
            "You can:\n"
            "1. Set `FORWARD_CHAT_ID` in `.env` for global forwarding\n"
            "2. Use `/setforward <chat_id>` to set your own custom forward channel\n"
            "3. Use `/clearforward` to clear your custom forward channel"
        )
        return
    
    try:
        # Try to send a test message
        test_msg = await bot.send_message(
            chat_id=test_forward_id,
            text=f"🧪 **Test Message**\n\nForwarding is working! ✅\n\n"
                 f"Using: {forward_type}\n"
                 f"Chat ID: `{test_forward_id}`"
        )
        
        await message.reply(
            f"✅ **Forwarding test successful!**\n\n"
            f"**Forward Chat ID:** `{test_forward_id}`\n"
            f"**Type:** {forward_type}\n"
            f"**Test Message ID:** `{test_msg.id}`\n\n"
            f"All your downloads will be forwarded here."
        )
        logger.info(f"Forward test successful for user {message.from_user.id} to {test_forward_id}")
        
    except FloodWait as e:
        wait_time = e.value if hasattr(e, 'value') else 30
        await message.reply(
            f"⏳ **Rate limited while testing forward.**\n\n"
            f"Waiting {wait_time} seconds before retry...\n\n"
            f"Please wait and try `/testforwarding` again."
        )
        await asyncio.sleep(wait_time + 2)
        # Retry
        try:
            test_msg = await bot.send_message(
                chat_id=test_forward_id,
                text=f"🧪 **Test Message**\n\nForwarding is working! ✅\n\n"
                     f"Using: {forward_type}\n"
                     f"Chat ID: `{test_forward_id}`"
            )
            await message.reply(
                f"✅ **Forwarding test successful!**\n\n"
                f"**Forward Chat ID:** `{test_forward_id}`\n"
                f"**Type:** {forward_type}\n"
                f"**Test Message ID:** `{test_msg.id}`"
            )
        except Exception as e2:
            await message.reply(f"❌ **Forwarding test failed after retry:** `{str(e2)}`")
            
    except Exception as e:
        await message.reply(
            f"❌ **Forwarding test failed:**\n\n"
            f"**Error:** `{str(e)}`\n\n"
            f"**Make sure:**\n"
            f"1. The chat ID is correct\n"
            f"2. The bot is a member of the chat\n"
            f"3. The bot has permission to send messages\n"
            f"4. For channels, the bot needs 'Post Messages' permission"
        )

# ============ FORWARD CHANNEL COMMANDS ============

@bot.on_message(filters.command("setforward") & filters.private)
async def set_forward_command(_, message: Message):
    """Set a custom forward channel per user"""
    if len(message.command) < 2:
        await message.reply(
            "📤 **Set Forward Channel**\n\n"
            "Usage: `/setforward <chat_id_or_username>`\n\n"
            "Examples:\n"
            "• `/setforward -100123456789`\n"
            "• `/setforward @my_channel`\n"
            "• `/setforward my_channel`\n\n"
            "⚠️ The bot must be a member of the channel with send permissions!\n\n"
            "Use `/clearforward` to remove your custom forward channel."
        )
        return
    
    chat_input = message.command[1]
    user_id = message.from_user.id
    
    try:
        from helpers.msg import parse_chat_identifier
        parsed_chat_id = parse_chat_identifier(chat_input)
        
        try:
            # Check if bot can access this chat
            chat = await bot.get_chat(parsed_chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else parsed_chat_id
            
            # Test send permission
            test_msg = await bot.send_message(
                chat_id=parsed_chat_id,
                text="🔍 **Permission Test**\n\n"
                     f"This is a test message for user {user_id}.\n"
                     f"Bot has permission to send to this channel."
            )
            await test_msg.delete()
            
            # Save to database
            db.update_user_settings(
                user_id,
                forward_chat_id=str(parsed_chat_id),
                forward_chat_title=chat_title
            )
            
            await message.reply(
                f"✅ **Custom Forward Channel Set!**\n\n"
                f"**Channel:** {chat_title}\n"
                f"**ID:** `{parsed_chat_id}`\n\n"
                f"All your downloads will now be forwarded to this channel.\n"
                f"Use `/clearforward` to disable.\n"
                f"Use `/testforwarding` to test if it's working."
            )
            logger.info(f"User {user_id} set custom forward to {parsed_chat_id}")
            
        except FloodWait as e:
            wait_time = e.value if hasattr(e, 'value') else 30
            await message.reply(
                f"⏳ **Rate limited.**\n\n"
                f"Please wait {wait_time} seconds and try again."
            )
            await asyncio.sleep(wait_time + 2)
            # Retry
            chat = await bot.get_chat(parsed_chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else parsed_chat_id
            test_msg = await bot.send_message(
                chat_id=parsed_chat_id,
                text="🔍 **Permission Test**\n\nBot has permission to send to this channel."
            )
            await test_msg.delete()
            db.update_user_settings(
                user_id,
                forward_chat_id=str(parsed_chat_id),
                forward_chat_title=chat_title
            )
            await message.reply(
                f"✅ **Custom Forward Channel Set!**\n\n"
                f"**Channel:** {chat_title}\n"
                f"**ID:** `{parsed_chat_id}`"
            )
            
        except Exception as e:
            error_msg = str(e)
            if "not enough rights" in error_msg.lower() or "not admin" in error_msg.lower():
                await message.reply(
                    f"❌ **Permission Denied**\n\n"
                    f"The bot doesn't have permission to send messages to this chat.\n\n"
                    f"**Solutions:**\n"
                    f"1. Make the bot an admin in the chat\n"
                    f"2. Make sure the bot has 'Send Messages' permission\n"
                    f"3. For channels, add the bot as an admin with 'Post Messages' permission"
                )
            elif "bot is not a member" in error_msg.lower():
                await message.reply(
                    f"❌ **Bot Not a Member**\n\n"
                    f"The bot is not a member of this channel.\n\n"
                    f"**Solutions:**\n"
                    f"1. Add the bot to the channel\n"
                    f"2. For private channels, invite the bot first"
                )
            else:
                await message.reply(
                    f"❌ **Failed to Set Forward Channel**\n\n"
                    f"Error: `{error_msg}`\n\n"
                    f"Make sure:\n"
                    f"1. The chat ID/username is correct\n"
                    f"2. The bot is a member of the chat\n"
                    f"3. The bot has permission to send messages"
                )
            
    except Exception as e:
        await message.reply(f"❌ **Error:** `{str(e)}`")

@bot.on_message(filters.command("clearforward") & filters.private)
async def clear_forward_command(_, message: Message):
    """Clear the user's custom forward channel"""
    try:
        db.update_user_settings(message.from_user.id, forward_chat_id=None, forward_chat_title=None)
        await message.reply(
            "🗑️ **Custom Forward Channel Cleared!**\n\n"
            "Your downloads will no longer be forwarded to your custom channel.\n"
            "The global `FORWARD_CHAT_ID` from `.env` will be used instead (if configured).\n\n"
            "Use `/setforward` to set a new custom channel."
        )
        logger.info(f"User {message.from_user.id} cleared forward channel")
    except Exception as e:
        await message.reply(f"❌ **Error:** `{str(e)}`")

@bot.on_message(filters.command("myforward") & filters.private)
async def my_forward_command(_, message: Message):
    """Check your current forward channel"""
    global forward_chat_id
    
    settings = db.get_user_settings(message.from_user.id)
    
    response = "📤 **Your Forward Channel Settings**\n\n"
    
    # Check custom forward
    if settings and settings.forward_chat_id:
        response += f"**Custom Forward Channel:**\n"
        response += f"• **Channel:** {settings.forward_chat_title or settings.forward_chat_id}\n"
        response += f"• **ID:** `{settings.forward_chat_id}`\n"
        response += f"• **Status:** ✅ Active (custom)\n\n"
    else:
        response += f"**Custom Forward Channel:** ❌ Not set\n\n"
    
    # Check global forward
    if forward_chat_id:
        response += f"**Global Forward Channel:**\n"
        response += f"• **ID:** `{forward_chat_id}`\n"
        response += f"• **Status:** ✅ Active (global)\n\n"
    else:
        response += f"**Global Forward Channel:** ❌ Not configured\n\n"
    
    response += "**Priority:** Custom channel > Global channel\n\n"
    response += "Use `/setforward <chat_id>` to set a custom channel.\n"
    response += "Use `/clearforward` to clear your custom channel.\n"
    response += "Use `/testforwarding` to test if forwarding works."
    
    await message.reply(response)

@bot.on_message(filters.command("settings") & filters.private)
async def settings_command(_, message: Message):
    """Handle /settings command with forward chat options"""
    language = user_language(message.from_user.id)
    await message.reply(tr(language, "settings_title"), reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(language, "btn_language"), callback_data="ui:language")],
        [InlineKeyboardButton(tr(language, "btn_forward"), callback_data="ui:set_forward")],
        [InlineKeyboardButton(tr(language, "btn_back"), callback_data="ui:main")],
    ]))
    return
    settings = db.get_user_settings(message.from_user.id)
    global forward_chat_id
    
    # Check if user has custom forward
    custom_forward = "❌ Not set"
    if settings.forward_chat_id:
        custom_forward = f"✅ {settings.forward_chat_title or settings.forward_chat_id}"
    
    # Check global forward
    global_forward = "❌ Not configured"
    if forward_chat_id:
        global_forward = f"✅ {forward_chat_id}"
    
    settings_text = (
        "⚙️ **Your Settings**\n\n"
        f"**Auto Download:** {'✅' if settings.auto_download else '❌'}\n"
        f"**Max File Size:** {get_readable_file_size(settings.max_file_size)}\n"
        f"**Preferred Quality:** {settings.preferred_quality}\n\n"
        f"**📤 Forward Channels:**\n"
        f"• **Custom:** {custom_forward}\n"
        f"• **Global:** {global_forward}\n\n"
        "Use the buttons below to modify settings:"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Toggle Auto Download", callback_data="toggle_auto")],
        [InlineKeyboardButton("Change Max Size", callback_data="change_size")],
        [InlineKeyboardButton("Change Quality", callback_data="change_quality")],
        [InlineKeyboardButton("📤 Set Custom Forward", callback_data="set_forward")],
        [InlineKeyboardButton("🗑️ Clear Custom Forward", callback_data="clear_forward")],
        [InlineKeyboardButton("🧪 Test Forwarding", callback_data="test_forward")],
        [InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])
    
    await message.reply(settings_text, reply_markup=keyboard)

@bot.on_callback_query()
async def handle_callback(bot: Client, callback_query: CallbackQuery):
    """Handle callback queries from inline keyboards"""
    data = callback_query.data
    user_id = callback_query.from_user.id

    if data.startswith("ui:"):
        await handle_ui_callback(callback_query)
        return
    
    if data == "stats":
        stats = db.get_stats(user_id)
        await callback_query.message.reply(
            "📊 **Your Statistics**\n\n"
            f"**Total Downloads:** {stats['total']}\n"
            f"**Successful:** {stats['successful']}\n"
            f"**Failed:** {stats['failed']}\n"
            f"**Total Size:** {get_readable_file_size(stats['total_size'])}"
        )
    
    elif data == "settings":
        await settings_command(bot, callback_query.message)
    
    elif data == "toggle_auto":
        settings = db.get_user_settings(user_id)
        db.update_user_settings(user_id, auto_download=not settings.auto_download)
        await callback_query.answer(f"Auto download {'enabled' if not settings.auto_download else 'disabled'}")
        await settings_command(bot, callback_query.message)
    
    elif data == "change_size":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("100 MB", callback_data="size_100")],
            [InlineKeyboardButton("500 MB", callback_data="size_500")],
            [InlineKeyboardButton("1 GB", callback_data="size_1000")],
            [InlineKeyboardButton("2 GB", callback_data="size_2000")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings")]
        ])
        await callback_query.message.reply("Select max file size:", reply_markup=keyboard)
    
    elif data.startswith("size_"):
        size_mb = int(data.split("_")[1])
        size_bytes = size_mb * 1024 * 1024
        db.update_user_settings(user_id, max_file_size=size_bytes)
        await callback_query.answer(f"Max size set to {size_mb} MB")
        await settings_command(bot, callback_query.message)
    
    elif data == "set_forward":
        await callback_query.message.reply(
            "📤 **Set Custom Forward Channel**\n\n"
            "Please send the channel ID or username using the command:\n"
            "`/setforward <chat_id_or_username>`\n\n"
            "Examples:\n"
            "• `/setforward -100123456789`\n"
            "• `/setforward @my_channel`\n\n"
            "⚠️ The bot must be a member of the channel!"
        )
        await callback_query.answer()
    
    elif data == "clear_forward":
        try:
            db.update_user_settings(user_id, forward_chat_id=None, forward_chat_title=None)
            await callback_query.answer("Custom forward channel cleared!")
            await settings_command(bot, callback_query.message)
        except Exception as e:
            await callback_query.answer(f"Error: {str(e)[:50]}")
    
    elif data == "test_forward":
        await callback_query.answer("Testing forward...")
        # Create a fake message to use with test_forwarding_command
        class FakeMessage:
            def __init__(self, user_id):
                self.from_user = type('obj', (object,), {'id': user_id})()
                self.chat = type('obj', (object,), {'id': user_id})()
                self.reply = callback_query.message.reply
        fake_msg = FakeMessage(user_id)
        await test_forwarding_command(bot, fake_msg)
    
    elif data.startswith("confirm_all_"):
        chat_id = data.replace("confirm_all_", "")
        await callback_query.answer("Starting download all...")
        await callback_query.message.edit("📥 **Starting download all content...**")
        instance = await get_user_downloader(user_id)
        if not instance:
            return await callback_query.message.reply(tr(user_language(user_id), "not_connected"))
        
        # Check if user has custom forward
        settings = db.get_user_settings(user_id)
        effective_forward = settings.forward_chat_id if settings else None
        
        track_task(instance.download_all_channel_media_optimized(
            callback_query.message, 
            chat_id, 
            effective_forward
        ), user_id)
    
    elif data == "cancel_all":
        await callback_query.answer("Download cancelled")
        await callback_query.message.edit("❌ **Download cancelled**")
    
    elif data == "back":
        await start_command(bot, callback_query.message)
    
    await callback_query.answer()

# ============ DOWNLOAD COMMANDS ============

@bot.on_message(filters.command("dl") & filters.private)
async def download_command(_, message: Message):
    """Handle /dl command"""
    logger.info(f"User {message.from_user.id} requested download: {message.text[:100]}...")
    instance = await get_user_downloader(message.from_user.id)
    if not instance:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"), reply_markup=main_keyboard(user_language(message.from_user.id)))
    
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing URL or Chat ID**\n\n"
            "Usage: `/dl <post_url_or_chat_id>`\n"
            "Examples:\n"
            "• `/dl https://t.me/channel/123`\n"
            "• `/dl https://t.me/c/1719871015/123`\n"
            "• `/dl -1001719871015` (gets latest message)"
        )
        return
    
    # Check if user has custom forward
    settings = db.get_user_settings(message.from_user.id)
    effective_forward = settings.forward_chat_id if settings else None
    
    urls = message.command[1:]
    for url in urls:
        track_task(instance.download_media(message, url, effective_forward), message.from_user.id)

@bot.on_message(filters.command("dls") & filters.private)
async def download_story_command(_, message: Message):
    """Handle /dls command"""
    instance = await get_user_downloader(message.from_user.id)
    if not instance:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"))
    
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing URL**\n\n"
            "Usage: `/dls <story_url>`\n"
            "Example: `/dls https://t.me/username/s/12`"
        )
        return
    
    # Check if user has custom forward
    settings = db.get_user_settings(message.from_user.id)
    effective_forward = settings.forward_chat_id if settings else None
    
    story_url = message.command[1]
    if not is_story_link(story_url):
        await message.reply(
            "❌ **Invalid story URL**\n\n"
            "Expected format: `https://t.me/<username>/s/<story_id>`"
        )
        return
    
    track_task(instance.download_media(message, story_url, effective_forward), message.from_user.id)

@bot.on_message(filters.command("bdl") & filters.private)
async def batch_download_command(_, message: Message):
    """Handle /bdl command"""
    language = user_language(message.from_user.id)
    db.set_conversation_state(message.from_user.id, "download_range", expires_at=datetime.utcnow() + timedelta(minutes=15))
    await message.reply("Send two post links separated by a space:", reply_markup=download_keyboard(language))
    return
    await ensure_forward_chat_setup()
    
    # Check if user has custom forward
    settings = db.get_user_settings(message.from_user.id)
    effective_forward = settings.forward_chat_id if settings and settings.forward_chat_id else forward_chat_id
    
    args = message.text.split()
    
    if len(args) != 3:
        await message.reply(
            "🚀 **Batch Download**\n\n"
            "Usage: `/bdl start_link end_link`\n\n"
            "**Example:**\n"
            "`/bdl https://t.me/channel/100 https://t.me/channel/120`\n\n"
            "This will download all posts from ID 100 to 120."
        )
        return
    
    try:
        start_chat, start_id = getChatMsgID(args[1])
        end_chat, end_id = getChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"❌ **Error parsing links:** `{e}`")
    
    if start_chat != end_chat:
        return await message.reply("❌ **Both links must be from the same channel.**")
    if start_id > end_id:
        return await message.reply("❌ **Invalid range:** start ID cannot exceed end ID.")
    
    prefix = args[1].rsplit("/", 1)[0]
    loading = await message.reply(f"📥 **Downloading posts {start_id}–{end_id}...**")

    try:
        source_chat = await resolve_chat(user, start_chat)
        start_chat = preferred_chat_reference(source_chat)
    except Exception as e:
        await loading.delete()
        return await message.reply(f"❌ **Cannot access chat:** `{str(e)[:150]}`")
    
    downloaded = skipped = failed = 0
    processed_media_groups = set()
    batch_tasks = []
    
    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            chat_msg = await user.get_messages(chat_id=start_chat, message_ids=msg_id)
            if not chat_msg:
                skipped += 1
                continue
            
            if chat_msg.media_group_id:
                if chat_msg.media_group_id in processed_media_groups:
                    skipped += 1
                    continue
                processed_media_groups.add(chat_msg.media_group_id)
            
            has_content = bool(chat_msg.media or chat_msg.text or chat_msg.caption)
            if not has_content:
                skipped += 1
                continue
            
            task = track_task(downloader.download_media(message, url, effective_forward))
            batch_tasks.append(task)
            
            if len(batch_tasks) >= Config.BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        failed += 1
                    else:
                        downloaded += 1
                batch_tasks.clear()
                await asyncio.sleep(Config.FLOOD_WAIT_DELAY)
                
        except Exception as e:
            failed += 1
            logger.error(f"Error at {url}: {e}")
    
    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1
    
    await loading.delete()
    await message.reply(
        "✅ **Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded:** `{downloaded}`\n"
        f"⏭️ **Skipped:** `{skipped}`\n"
        f"❌ **Failed:** `{failed}`"
    )

@bot.on_message(filters.command("bdls") & filters.private)
async def batch_story_download_command(_, message: Message):
    """Handle /bdls command"""
    await message.reply("Story ranges are not enabled in production mode. Use the single-story menu to avoid Telegram authorization limits.")
    return
    await ensure_forward_chat_setup()
    
    # Check if user has custom forward
    settings = db.get_user_settings(message.from_user.id)
    effective_forward = settings.forward_chat_id if settings and settings.forward_chat_id else forward_chat_id
    
    args = message.text.split()
    
    if len(args) != 3 or not all(is_story_link(arg) for arg in args[1:]):
        await message.reply(
            "🚀 **Batch Story Download**\n\n"
            "Usage: `/bdls start_link end_link`\n\n"
            "**Example:**\n"
            "`/bdls https://t.me/username/s/10 https://t.me/username/s/25`"
        )
        return
    
    try:
        start_chat, start_id = getStoryChatMsgID(args[1])
        end_chat, end_id = getStoryChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"❌ **Error parsing links:** `{e}`")
    
    if start_chat.lower() != end_chat.lower():
        return await message.reply("❌ **Both links must be from the same user/channel.**")
    if start_id > end_id:
        return await message.reply("❌ **Invalid range:** start ID cannot exceed end ID.")
    
    prefix = f"https://t.me/{start_chat}/s"
    loading = await message.reply(f"📥 **Downloading stories {start_id}–{end_id}...**")
    
    downloaded = failed = 0
    batch_tasks = []
    
    for sid in range(start_id, end_id + 1):
        url = f"{prefix}/{sid}"
        task = track_task(downloader.download_media(message, url, effective_forward))
        batch_tasks.append(task)
        
        if len(batch_tasks) >= Config.BATCH_SIZE:
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    failed += 1
                else:
                    downloaded += 1
            batch_tasks.clear()
            await asyncio.sleep(Config.FLOOD_WAIT_DELAY)
    
    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1
    
    await loading.delete()
    await message.reply(
        "✅ **Batch Story Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded:** `{downloaded}`\n"
        f"❌ **Failed:** `{failed}`"
    )

# ============ DOWNLOAD ALL COMMANDS ============

@bot.on_message(filters.command("downloadall") & filters.private)
async def download_all_command(_, message: Message):
    """Download ALL media from a channel"""
    language = user_language(message.from_user.id)
    db.set_conversation_state(message.from_user.id, "download_bulk", expires_at=datetime.utcnow() + timedelta(minutes=15))
    await message.reply(tr(language, "ask_bulk"), reply_markup=download_keyboard(language))
    return
    logger.info(f"User {message.from_user.id} requested download all: {message.text[:100]}...")
    instance = await get_user_downloader(message.from_user.id)
    if not instance:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"))
    
    if len(message.command) < 2:
        await message.reply(
            "📥 **Download All Channel Content**\n\n"
            "Usage: `/downloadall <chat_id>`\n"
            "Example: `/downloadall -1001719871015`\n\n"
            "⚠️ This will download ALL media from the channel!\n"
            "• May take a long time for large channels\n"
            "• You'll receive each media as it downloads\n"
            "• Use `/stopdownload` to cancel\n\n"
            "📊 **Progress will be shown in real-time**"
        )
        return
    
    chat_id_input = message.command[1]
    
    try:
        parsed_chat_id = parse_chat_identifier(chat_id_input)
        chat = await resolve_chat(instance.user, parsed_chat_id)
        parsed_chat_id = preferred_chat_reference(chat)
        resolved_title = get_chat_title(chat)
        
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Download All", callback_data=f"confirm_all_{parsed_chat_id}"),
                InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_all")
            ]
        ])
        
        await message.reply(
            f"⚠️ **Confirm Download All**\n\n"
            f"**Channel:** {resolved_title}\n"
            f"**Chat ID:** `{parsed_chat_id}`\n\n"
            f"This will download ALL media from this channel.\n"
            f"Are you sure you want to continue?",
            reply_markup=confirm_keyboard
        )
        
    except Exception as e:
        await message.reply(f"❌ Error: `{str(e)}`")

@bot.on_message(filters.command("stopdownload") & filters.private)
async def stop_download_command(_, message: Message):
    """Stop all downloads"""
    logger.info(f"User {message.from_user.id} stopped downloads")
    cancelled = await cancel_user_tasks(message.from_user.id)
    await message.reply(f"🛑 **Stopped {cancelled} download task(s).**")

# ============ STATS COMMAND ============

@bot.on_message(filters.command("stats") & filters.private)
async def stats_command(_, message: Message):
    """Handle /stats command"""
    stats = db.get_stats(message.from_user.id)
    await message.reply(
        "📊 **Your Download Statistics**\n\n"
        f"**Total Downloads:** {stats['total']}\n"
        f"**Successful:** {stats['successful']}\n"
        f"**Failed:** {stats['failed']}\n"
        f"**Total Size:** {get_readable_file_size(stats['total_size'])}"
    )

# ============ CLEANUP COMMANDS ============

@bot.on_message(filters.command("cleanup") & filters.private)
async def cleanup_command(_, message: Message):
    """Handle /cleanup command"""
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Admin only.")
    try:
        files_removed, bytes_freed = cleanup_old_downloads(7)
        if files_removed == 0:
            return await message.reply("🧹 **Cleanup complete:** no old files found.")
        return await message.reply(
            f"🧹 **Cleanup complete:**\n"
            f"Removed `{files_removed}` file(s)\n"
            f"Freed `{get_readable_file_size(bytes_freed)}`"
        )
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return await message.reply("❌ **Cleanup failed.** Check logs for details.")

@bot.on_message(filters.command("cleandb") & filters.private)
async def clean_db_command(_, message: Message):
    """Clean database and session files to fix lock issues"""
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Admin only.")
    try:
        try:
            db.Session.remove()
        except:
            pass
        
        try:
            db.engine.dispose()
        except:
            pass
        
        files_deleted = []
        
        for file in os.listdir("."):
            if file.endswith(".session") or file.endswith(".session-journal"):
                try:
                    os.remove(file)
                    files_deleted.append(file)
                except Exception as e:
                    logger.error(f"Failed to delete {file}: {e}")
        
        if os.path.exists("database"):
            for file in os.listdir("database"):
                if file.endswith(".db") or file.endswith(".db-journal"):
                    try:
                        os.remove(os.path.join("database", file))
                        files_deleted.append(f"database/{file}")
                    except Exception as e:
                        logger.error(f"Failed to delete database/{file}: {e}")
        
        if files_deleted:
            await message.reply(
                f"🧹 **Database Cleanup Complete!**\n\n"
                f"Deleted:\n" + "\n".join(f"• `{f}`" for f in files_deleted) + "\n\n"
                f"✅ Please restart the bot now."
            )
        else:
            await message.reply("✅ No database files to clean.")
            
    except Exception as e:
        await message.reply(f"❌ **Cleanup failed:** `{str(e)}`")

# ============ KILLALL COMMAND ============

@bot.on_message(filters.command("killall") & filters.private)
async def killall_command(_, message: Message):
    """Handle /killall command"""
    cancelled = await cancel_user_tasks(message.from_user.id)
    await message.reply(f"🛑 **Cancelled {cancelled} running task(s).**")

# ============ LOGS COMMAND ============

@bot.on_message(filters.command("logs") & filters.private)
async def logs_command(_, message: Message):
    """Handle /logs command - send the log file"""
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Admin only.")
    try:
        log_files = sorted([f for f in os.listdir("logs") if f.endswith(".log")], reverse=True)
        if log_files:
            log_path = os.path.join("logs", log_files[0])
            await message.reply_document(document=log_path, caption="📋 **Bot Logs**")
            logger.info(f"Sent logs to user {message.from_user.id}")
        else:
            await message.reply("❌ No logs file found")
    except Exception as e:
        logger.error(f"Failed to send logs: {e}")
        await message.reply(f"❌ Failed to send logs: {str(e)[:100]}")

# ============ GETID COMMAND ============

@bot.on_message(filters.command("getid") & filters.private)
async def get_chat_id(_, message: Message):
    """Handle /getid command"""
    chat_id = message.chat.id
    chat_type = message.chat.type
    
    await message.reply(
        f"**Chat ID Information**\n\n"
        f"**Chat ID:** `{chat_id}`\n"
        f"**Type:** `{chat_type}`\n"
        f"**Title:** `{message.chat.title if hasattr(message.chat, 'title') else 'Private'}`\n\n"
        f"For FORWARD_CHAT_ID use:\n"
        f"`{chat_id}`"
    )

# ============ WHOAMI COMMAND ============

@bot.on_message(filters.command("whoami") & filters.private)
async def whoami_command(_, message: Message):
    """Handle /whoami command"""
    try:
        account = await session_manager.get(message.from_user.id)
        language = user_language(message.from_user.id)
        if not account:
            return await message.reply(tr(language, "not_connected"))
        await message.reply(tr(
            language, "status_connected", name=account.display_name,
            username=account.username or "-",
        ), reply_markup=account_keyboard(language, True))
    except Exception as e:
        await message.reply(f"❌ Error getting user info: `{str(e)}`")

# ============ JOINCHECK COMMAND ============

@bot.on_message(filters.command("joincheck") & filters.private)
async def join_check_command(_, message: Message):
    """Handle /joincheck command"""
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing Chat ID**\n\n"
            "Usage: `/joincheck <chat_id>`\n"
            "Example: `/joincheck -1001719871015`"
        )
        return
    
    chat_id = message.command[1]
    account = await session_manager.get(message.from_user.id)
    if not account:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"))
    source_client = account.client
    
    try:
        parsed_chat_id = parse_chat_identifier(chat_id)
        chat = await resolve_chat(source_client, parsed_chat_id)
        parsed_chat_id = preferred_chat_reference(chat)
        
        try:
            member = await source_client.get_chat_member(parsed_chat_id, "me")
            is_member = True
            status = member.status
            me = await source_client.get_me()
            
            await message.reply(
                f"📋 **Channel Membership Check**\n\n"
                f"**Chat ID:** `{parsed_chat_id}`\n"
            f"**Title:** {get_chat_title(chat)}\n"
                f"**Type:** {chat.type}\n"
                f"**Is Member:** {'✅ YES' if is_member else '❌ NO'}\n"
                f"**Status:** `{status}`\n\n"
                f"**Checking with account:**\n"
                f"• Username: @{me.username or 'None'}\n"
                f"• ID: `{me.id}`\n\n"
                f"{'✅ You can download from this channel!' if is_member else '❌ You need to join this channel first!'}"
            )
            
        except Exception as e:
            me = await source_client.get_me()
            await message.reply(
                f"📋 **Channel Membership Check**\n\n"
                f"**Chat ID:** `{parsed_chat_id}`\n"
                f"**Title:** {get_chat_title(chat)}\n"
                f"**Is Member:** ❌ NO\n"
                f"**Error:** `{str(e)}`\n\n"
                f"**Checking with account:**\n"
                f"• Username: @{me.username or 'None'}\n"
                f"• ID: `{me.id}`\n\n"
                f"⚠️ This account is NOT a member of this channel!"
            )
        
    except Exception as e:
        await message.reply(
            f"❌ **Error checking channel**\n\n"
            f"Chat ID: `{chat_id}`\n"
            f"Error: `{str(e)}`\n\n"
            f"This could mean:\n"
            f"1. The channel doesn't exist\n"
            f"2. You're not a member\n"
            f"3. The chat ID is incorrect"
        )

# ============ TESTURL COMMAND ============

@bot.on_message(filters.command("testurl") & filters.private)
async def test_url_command(_, message: Message):
    """Handle /testurl command"""
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing URL**\n\n"
            "Usage: `/testurl <telegram_url>`\n"
            "Example: `/testurl https://t.me/c/1719871015/123`"
        )
        return
    
    url = message.command[1]
    result = debug_url(url)
    
    response = (
        f"**🔍 URL Debug Info**\n\n"
        f"**URL:** `{url}`\n"
        f"**Valid:** {result['is_valid']}\n"
        f"**Type:** {result['type']}\n"
        f"**Chat ID:** `{result['chat_id']}`\n"
        f"**Message ID:** `{result['message_id']}`\n"
    )
    
    if result['error']:
        response += f"\n**Error:** `{result['error']}`"
    else:
        response += f"\n\n✅ **This URL is valid and can be used with /dl**"
    
    await message.reply(response)

# ============ LIST COMMAND ============

@bot.on_message(filters.command("list") & filters.private)
async def list_messages_command(_, message: Message):
    """Handle /list command"""
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing Chat ID**\n\n"
            "Usage: `/list <chat_id>`\n"
            "Example: `/list -1001719871015`\n"
            "Example: `/list @channel_name`"
        )
        return
    
    chat_id_input = message.command[1]
    account = await session_manager.get(message.from_user.id)
    if not account:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"))
    source_client = account.client
    
    try:
        parsed_chat_id = parse_chat_identifier(chat_id_input)
        chat = await resolve_chat(source_client, parsed_chat_id)
        parsed_chat_id = preferred_chat_reference(chat)
        resolved_title = get_chat_title(chat)
        
        loading = await message.reply(f"📋 **Fetching messages from {resolved_title}...**")
        
        messages = []
        async for msg in source_client.get_chat_history(parsed_chat_id, limit=10):
            if msg.media or msg.text or msg.caption:
                msg_type = "📷 Photo" if msg.photo else \
                          "🎬 Video" if msg.video else \
                          "🎵 Audio" if msg.audio else \
                          "📄 Document" if msg.document else \
                          "🗣️ Voice" if msg.voice else \
                          "📝 Text" if msg.text else "📦 Other"
                
                msg_text = msg.text or msg.caption or ""
                msg_preview = msg_text[:50] + "..." if len(msg_text) > 50 else msg_text
                
                link_reference = f"@{chat.username}" if getattr(chat, "username", None) else parsed_chat_id
                link = format_message_link(link_reference, msg.id)
                
                messages.append(
                    f"**ID:** `{msg.id}`\n"
                    f"**Type:** {msg_type}\n"
                    f"**Preview:** {msg_preview if msg_preview else 'No text'}\n"
                    f"**Link:** `{link}`\n"
                )
                
                if len(messages) >= 5:
                    break
        
        await loading.delete()
        
        if not messages:
            await message.reply(f"❌ No messages found in {resolved_title}")
            return
        
        response = f"📋 **Recent Messages from {resolved_title}**\n\n"
        response += "\n---\n".join(messages)
        response += f"\n\n💡 Use `/dl <link>` to download any message"
        
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for part in parts:
                await message.reply(part)
        else:
            await message.reply(response)
        
    except Exception as e:
        await message.reply(
            f"❌ **Error accessing chat**\n\n"
            f"Chat ID: `{chat_id_input}`\n"
            f"Error: `{str(e)}`\n\n"
            f"Make sure:\n"
            f"1. The chat ID is correct\n"
            f"2. Your account is a member of the chat\n"
            f"3. The chat is accessible"
        )

# ============ LATEST COMMAND ============

@bot.on_message(filters.command("latest") & filters.private)
async def get_latest_command(_, message: Message):
    """Handle /latest command"""
    if len(message.command) < 2:
        await message.reply(
            "❌ **Missing Chat ID**\n\n"
            "Usage: `/latest <chat_id>`\n"
            "Example: `/latest -1001719871015`"
        )
        return
    
    chat_id_input = message.command[1]
    account = await session_manager.get(message.from_user.id)
    if not account:
        return await message.reply(tr(user_language(message.from_user.id), "not_connected"))
    source_client = account.client
    
    try:
        parsed_chat_id = parse_chat_identifier(chat_id_input)
        chat = await resolve_chat(source_client, parsed_chat_id)
        parsed_chat_id = preferred_chat_reference(chat)
        resolved_title = get_chat_title(chat)
        
        async for msg in source_client.get_chat_history(parsed_chat_id, limit=1):
            if msg:
                link_reference = f"@{chat.username}" if getattr(chat, "username", None) else parsed_chat_id
                link = format_message_link(link_reference, msg.id)
                
                response = (
                    f"📌 **Latest Message from {resolved_title}**\n\n"
                    f"**Message ID:** `{msg.id}`\n"
                    f"**Date:** `{msg.date}`\n"
                    f"**Has Media:** {'✅' if msg.media else '❌'}\n"
                    f"**Link:** {link}\n\n"
                )
                
                if msg.text or msg.caption:
                    text = msg.text or msg.caption
                    preview = text[:200] + "..." if len(text) > 200 else text
                    response += f"**Preview:**\n{preview}\n\n"
                
                response += f"💡 Use `/dl {link}` to download this message"
                
                await message.reply(response)
                return
        
        await message.reply(f"❌ No messages found in {resolved_title}")
        
    except Exception as e:
        await message.reply(
            f"❌ **Error accessing chat**\n\n"
            f"Error: `{str(e)}`"
        )

# ============ BOT STATS ============

@bot.on_message(filters.command("botstats") & filters.private)
async def bot_stats_command(_, message: Message):
    """Handle /botstats command"""
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Admin only.")
    currentTime = get_readable_time(time() - Config.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    total = get_readable_file_size(total)
    used = get_readable_file_size(used)
    free = get_readable_file_size(free)
    sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
    recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
    cpuUsage = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    process = psutil.Process(os.getpid())
    
    db_stats = db.get_stats()
    
    stats_text = (
        "🤖 **Bot Statistics**\n\n"
        f"**Uptime:** `{currentTime}`\n"
        f"**Total Downloads:** `{db_stats['total']}`\n"
        f"**Successful:** `{db_stats['successful']}`\n"
        f"**Failed:** `{db_stats['failed']}`\n\n"
        f"**Disk Space:**\n"
        f"  Total: `{total}`\n"
        f"  Used: `{used}`\n"
        f"  Free: `{free}`\n\n"
        f"**Memory Usage:** `{round(process.memory_info()[0] / 1024**2)} MiB`\n"
        f"**CPU:** `{cpuUsage}%`\n"
        f"**RAM:** `{memory}%`\n"
        f"**DISK:** `{disk}%`\n\n"
        f"**Network:**\n"
        f"  Upload: `{sent}`\n"
        f"  Download: `{recv}`"
    )
    
    await message.reply(stats_text)

# ============ HANDLE ANY MESSAGE ============

@bot.on_message(filters.private & ~filters.command([
    "start", "help", "dl", "dls", "bdl", "bdls", 
    "stats", "settings", "logs", "killall", "cleanup", 
    "getid", "testforwarding", "setupforward", "testurl", 
    "list", "latest", "botstats", "whoami", "joincheck",
    "downloadall", "stopdownload", "cleandb",
    "setforward", "clearforward", "myforward"
]))
async def handle_any_message(_, message: Message):
    """Handle any non-command message (treat as link or chat ID)"""
    language = user_language(message.from_user.id)
    await message.reply(tr(language, "unknown"), reply_markup=main_keyboard(language))

# ============ INITIALIZATION ============

async def initialize():
    """Initialize the bot"""
    global download_semaphore, forward_chat_id, downloader
    
    logger.info("="*60)
    configuration_errors = Config.validate()
    if configuration_errors:
        for error in configuration_errors:
            logger.error("Configuration: %s", error)
        return False
    logger.info("🚀 Initializing bot...")
    logger.info("="*60)
    
    if not await initialize_clients():
        logger.error("❌ Failed to initialize clients. Exiting.")
        return False
    
    download_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_DOWNLOADS)
    downloader = Downloader(
        user, bot, download_semaphore, owner_user_id=Config.ADMIN_USER_ID,
        admin_chat_id=Config.ADMIN_USER_ID,
    ) if user else None
    
    # Setup forward chat after clients are ready
    await setup_forward_chat()
    
    logger.info("="*60)
    logger.info("✅ Initialization complete!")
    logger.info(f"📁 Log file: {log_filename}")
    logger.info("="*60)
    return True


async def run_app():
    """Run both Telegram clients in one asyncio lifecycle."""
    if not await initialize():
        raise RuntimeError("Telegram clients could not be initialized")
    logger.info("🤖 Bot is now running. Press Ctrl+C to stop.")
    try:
        await idle()
    finally:
        await session_manager.close_all()
        if getattr(bot, "is_connected", False):
            await bot.stop()
        if user and getattr(user, "is_connected", False):
            await user.stop()

# ============ MAIN ============

if __name__ == "__main__":
    try:
        logger.info("="*50)
        logger.info("🚀 Enhanced Media Downloader Bot Starting...")
        logger.info("="*50)
        
        # Let Pyrogram drive the loop used by its sessions and dispatcher.
        # Passing a coroutine does not auto-start the client; run_app owns the
        # complete start/idle/stop lifecycle for both clients.
        bot.run(run_app())
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as err:
        logger.error(f"❌ Fatal error: {err}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        if pid_lock:
            try:
                pid_lock.close()
                os.unlink(PID_FILE)
            except OSError:
                pass
        logger.info("Bot stopped")
