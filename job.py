import logging
import asyncio
import sqlite3
import random
import os
import sys
import shutil
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from html import escape
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest
import feedparser
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from flask import Flask

# --- CONFIGURATION ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"Startup error: {name} is not set. "
            "Copy .env.example to .env, set the value, and start the bot again."
        )
    return value


BOT_TOKEN = _require_env("BOT_TOKEN")
try:
    ADMIN_ID = int(_require_env("ADMIN_ID"))
except ValueError:
    sys.exit(
        "Startup error: ADMIN_ID must be a numeric Telegram user ID. "
        "Update ADMIN_ID in .env and start the bot again."
    )

XML_FEED_URL = "https://www.myjobmag.co.ke/jobsxml.xml"
CHECK_INTERVAL = 300  # 5 minutes
DB_PATH = BASE_DIR / "myjobkenya.db"
BACKUPS_DIR = BASE_DIR / "backups"
EAT = timezone(timedelta(hours=3), name="EAT")
RESTORE_FILENAME = "myjobkenya.db"
RESTORE_TIMEOUT_SECONDS = 300
MAX_RESTORE_BYTES = 10 * 1024 * 1024
REQUIRED_SCHEMA = {
    "users": ("chat_id", "username", "joined_at", "is_verified"),
    "sent_jobs": ("link",),
    "pending_quiz": ("chat_id", "answer"),
    "user_delivery_stats": ("chat_id", "jobs_sent"),
    "sent_test_jobs": ("link",),
}
# Held across database reads/writes and automatic broadcasts so a restore
# cannot replace the file while those operations are in progress.
db_activity_lock = asyncio.Lock()

# --- AZURE WEB SERVER HACK ---
app = Flask(__name__)


@app.route('/')
def health_check():
    return "Bot is awake and running!", 200


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


# Start the dummy web server in the background
threading.Thread(target=run_web_server, daemon=True).start()
# -----------------------------

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)


def get_db_connection():
    return sqlite3.connect(DB_PATH)


def now_eat():
    return datetime.now(timezone.utc).astimezone(EAT)


def format_eat(dt_obj):
    return dt_obj.strftime("%d %b %Y %H:%M EAT")


def normalize_joined_at_to_eat(value):
    if pd.isna(value):
        return "N/A"

    if isinstance(value, datetime):
        dt_obj = value
    else:
        raw = str(value).strip()
        if not raw:
            return "N/A"
        try:
            # Supports legacy SQLite forms like "YYYY-MM-DD HH:MM:SS" and ISO forms.
            dt_obj = datetime.fromisoformat(raw)
        except ValueError:
            parsed = pd.to_datetime(raw, errors="coerce")
            if pd.isna(parsed):
                return "N/A"
            dt_obj = parsed.to_pydatetime()

    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=EAT)
    else:
        dt_obj = dt_obj.astimezone(EAT)
    return format_eat(dt_obj)


def format_entry_date_eat(entry):
    parsed = getattr(entry, "published_parsed", None)
    if parsed:
        dt_utc = datetime(*parsed[:6], tzinfo=timezone.utc)
        return format_eat(dt_utc.astimezone(EAT))

    published_text = getattr(entry, "published", None)
    if isinstance(published_text, str) and published_text.strip():
        return escape(published_text.strip())
    return format_eat(now_eat())

# --- DATABASE SETUP ---
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, joined_at DATETIME, is_verified INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sent_jobs (link TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_quiz (chat_id INTEGER PRIMARY KEY, answer INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_delivery_stats
                 (chat_id INTEGER PRIMARY KEY, jobs_sent INTEGER NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sent_test_jobs (link TEXT PRIMARY KEY)''')
    conn.commit()
    conn.close()

# --- JOB SCRAPER LOGIC ---
def format_job_message(entry, test_mode=False):
    title = escape(str(getattr(entry, "title", "New Job")))
    link = escape(str(getattr(entry, "link", "#")), quote=True)
    published = format_entry_date_eat(entry)
    header = "<b>Test Job Broadcast</b>" if test_mode else "<b>New Job Vacancy in Kenya</b>"
    return (
        f"{header}\n"
        f"📅 {published}\n\n"
        f"» <a href='{link}'>{title}</a>"
    )


async def send_message_with_retry(bot, user_id, msg):
    try:
        await bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        return True
    except RetryAfter as exc:
        wait_seconds = int(getattr(exc, "retry_after", 2)) + 1
        logging.warning("Rate limited for user %s. Waiting %s seconds.", user_id, wait_seconds)
        await asyncio.sleep(wait_seconds)
        try:
            await bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            return True
        except Exception:
            logging.exception("Retry send failed for user %s", user_id)
            return False
    except (TimedOut, NetworkError):
        logging.warning("Temporary network issue when sending to user %s", user_id)
        return False
    except Forbidden:
        # User blocked the bot or deleted their account. Expected; skip quietly.
        logging.info("Skipping user %s: bot was blocked or chat is unavailable.", user_id)
        return False
    except BadRequest as exc:
        # e.g. "chat not found" for a deactivated/never-started chat. Expected; skip quietly.
        logging.info("Skipping user %s: %s", user_id, exc.message)
        return False
    except Exception:
        logging.exception("Unexpected error when sending to user %s", user_id)
        return False


def get_verified_user_ids():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT chat_id FROM users WHERE is_verified=1')
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users


def increment_jobs_sent(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO user_delivery_stats (chat_id, jobs_sent)
           VALUES (?, 1)
           ON CONFLICT(chat_id) DO UPDATE SET jobs_sent = jobs_sent + 1''',
        (chat_id,)
    )
    conn.commit()
    conn.close()


async def send_db_backup(bot, target_chat_id, reason):
    async with db_activity_lock:
        payload = DB_PATH.read_bytes() if DB_PATH.exists() else None

    if payload is None:
        await bot.send_message(chat_id=target_chat_id, text="Database file not found.")
        return False

    caption = f"Database backup ({reason}) - {format_eat(now_eat())}"
    try:
        await bot.send_document(
            chat_id=target_chat_id,
            document=InputFile(payload, filename=RESTORE_FILENAME),
            caption=caption,
        )
        return True
    except Exception:
        logging.error("Failed to send database backup")
        return False


class RestoreValidationError(Exception):
    """The uploaded file is not a safe database to restore."""


def clear_restore_state(user_data):
    user_data.pop("restore_deadline", None)


def validate_restore_database(path):
    if not path.is_file():
        raise RestoreValidationError()
    if path.stat().st_size > MAX_RESTORE_BYTES:
        raise RestoreValidationError()

    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RestoreValidationError() from exc

    try:
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error as exc:
            raise RestoreValidationError() from exc
        if integrity != [("ok",)]:
            raise RestoreValidationError()

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table, columns in REQUIRED_SCHEMA.items():
            if table not in tables:
                raise RestoreValidationError()
            found = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not set(columns).issubset(found):
                raise RestoreValidationError()
    finally:
        conn.close()


def _unique_backup_path():
    stamp = now_eat().strftime("%Y%m%d-%H%M%S")
    candidate = BACKUPS_DIR / f"myjobkenya-{stamp}.db"
    suffix = 1
    while candidate.exists():
        candidate = BACKUPS_DIR / f"myjobkenya-{stamp}-{suffix}.db"
        suffix += 1
    return candidate


def install_validated_database(source):
    """Replace the live database. Returns True when the previous file was backed up."""
    validate_restore_database(source)
    staging = DB_PATH.with_name(DB_PATH.name + ".restore-staging")
    backed_up = False
    try:
        if DB_PATH.exists():
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DB_PATH, _unique_backup_path())
            backed_up = True
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staging)
        os.replace(staging, DB_PATH)
    except RestoreValidationError:
        raise
    except Exception:
        if staging.exists():
            staging.unlink()
        raise
    return backed_up


def build_report_keyboard():
    keyboard = [
        [InlineKeyboardButton("3 Months", callback_data='rep_3'),
         InlineKeyboardButton("6 Months", callback_data='rep_6'),
         InlineKeyboardButton("12 Months", callback_data='rep_12')]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("Database Backup", callback_data="admin_database"),
        ],
        [
            InlineKeyboardButton("Reports", callback_data="admin_report"),
            InlineKeyboardButton("Test Job", callback_data="admin_testjob"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_broadcast(bot, message_text):
    users = get_verified_user_ids()
    if not users:
        return 0

    sent = 0
    for user_id in users:
        delivered = await send_message_with_retry(bot, user_id, message_text)
        if delivered:
            sent += 1
        await asyncio.sleep(0.05)
    return sent


def next_weekly_backup_time():
    now = now_eat()
    target_date = (now + timedelta(days=7)).date()
    return datetime.combine(target_date, datetime.min.time(), tzinfo=EAT)


async def broadcast_jobs(bot, jobs, test_mode=False):
    users = get_verified_user_ids()
    if not users:
        return 0

    if not test_mode:
        logging.info(
            "Starting automatic broadcast: %s new job(s) to %s recipient(s).",
            len(jobs),
            len(users),
        )

    sent_count = 0
    for job in jobs:
        msg = format_job_message(job, test_mode=test_mode)
        for user_id in users:
            delivered = await send_message_with_retry(bot, user_id, msg)
            if delivered:
                sent_count += 1
                increment_jobs_sent(user_id)
            # Conservative pacing to stay under Telegram rate limits.
            # RetryAfter handling still applies inside send_message_with_retry.
            await asyncio.sleep(0.1)
    return sent_count


async def check_jobs(bot):
    try:
        feed = feedparser.parse(XML_FEED_URL)
    except Exception:
        logging.exception("Feed fetch crashed; retrying on next interval")
        return

    if getattr(feed, "bozo", False):
        logging.warning("Feed parser warning: %s", getattr(feed, "bozo_exception", "unknown"))

    entries = getattr(feed, "entries", [])
    if not entries:
        logging.info("Feed check completed: no entries available.")
        return

    async with db_activity_lock:
        conn = get_db_connection()
        c = conn.cursor()

        # Baseline guard: on first startup (or an empty sent_jobs table) treat every
        # current feed link as already-seen so we never mass-broadcast the backlog.
        c.execute('SELECT COUNT(*) FROM sent_jobs')
        is_baseline = c.fetchone()[0] == 0

        if is_baseline:
            baseline_count = 0
            for entry in entries:
                link = getattr(entry, "link", None)
                if not link:
                    continue
                c.execute('INSERT OR IGNORE INTO sent_jobs VALUES (?)', (link,))
                baseline_count += 1
            conn.commit()
            conn.close()
            logging.info(
                "Feed baseline initialized: %s existing jobs saved; no messages sent.",
                baseline_count,
            )
            return

        new_jobs = []
        for entry in entries:
            link = getattr(entry, "link", None)
            if not link:
                continue
            c.execute('SELECT link FROM sent_jobs WHERE link=?', (link,))
            if not c.fetchone():
                new_jobs.append(entry)
                c.execute('INSERT OR IGNORE INTO sent_jobs VALUES (?)', (link,))

        conn.commit()
        conn.close()

        if new_jobs:
            delivered = await broadcast_jobs(bot, new_jobs, test_mode=False)
            logging.info("Broadcast complete: %s new jobs, %s messages delivered.", len(new_jobs), delivered)


def get_latest_feed_entry():
    feed = feedparser.parse(XML_FEED_URL)
    entries = getattr(feed, "entries", [])
    if not entries:
        return None
    return entries[0]


def get_latest_untested_feed_entry():
    feed = feedparser.parse(XML_FEED_URL)
    entries = getattr(feed, "entries", [])
    if not entries:
        return None

    latest = entries[0]
    latest_link = getattr(latest, "link", None)
    if not latest_link:
        return None

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT 1 FROM sent_test_jobs WHERE link=?', (latest_link,))
    already_sent = c.fetchone()
    conn.close()
    if already_sent:
        return None
    return latest


def mark_test_job_sent(link):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO sent_test_jobs VALUES (?)', (link,))
    conn.commit()
    conn.close()

async def job_loop(bot):
    while True:
        await check_jobs(bot)
        await asyncio.sleep(CHECK_INTERVAL)

# --- COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    c = conn.cursor()

    # Existing verified users should not be re-challenged after restarts.
    c.execute('SELECT is_verified FROM users WHERE chat_id=?', (user.id,))
    existing = c.fetchone()
    username = user.username or ""

    if existing and int(existing[0]) == 1:
        c.execute('UPDATE users SET username=? WHERE chat_id=?', (username, user.id))
        conn.commit()
        conn.close()
        await update.message.reply_text("You are already verified. You will continue receiving job alerts.")
        return

    # Generate Math Quiz
    num1, num2 = random.randint(1, 20), random.randint(1, 20)
    answer = num1 + num2

    c.execute('INSERT OR REPLACE INTO pending_quiz VALUES (?, ?)', (user.id, answer))
    if existing:
        c.execute('UPDATE users SET username=? WHERE chat_id=?', (username, user.id))
    else:
        joined_at = now_eat().isoformat(timespec="seconds")
        c.execute(
            'INSERT INTO users (chat_id, username, joined_at, is_verified) VALUES (?, ?, ?, 0)',
            (user.id, username, joined_at)
        )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Welcome {user.first_name}! To prevent bots, solve this: \n\n<b>{num1} + {num2} = ?</b>", parse_mode=ParseMode.HTML)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id == ADMIN_ID and context.user_data.get("awaiting_broadcast"):
        context.user_data["awaiting_broadcast"] = False
        sent = await send_broadcast(context.bot, text)
        if sent == 0:
            await update.message.reply_text("No verified users available.")
            return

        await update.message.reply_text(f"Broadcast complete. Messages delivered: {sent}")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT answer FROM pending_quiz WHERE chat_id=?', (user_id,))
    row = c.fetchone()
    admin_message = None
    outcome = None

    if row:
        if text.isdigit() and int(text) == row[0]:
            c.execute('SELECT is_verified FROM users WHERE chat_id=?', (user_id,))
            existing = c.fetchone()
            was_verified = bool(existing and int(existing[0]) == 1)

            c.execute('UPDATE users SET is_verified=1 WHERE chat_id=?', (user_id,))
            c.execute('DELETE FROM pending_quiz WHERE chat_id=?', (user_id,))
            conn.commit()
            outcome = "correct"

            if not was_verified and user_id != ADMIN_ID:
                user = update.effective_user
                display_name = escape(user.full_name or user.first_name or "Unknown")
                username = f"@{user.username}" if user.username else "(no username)"
                verified_at = format_eat(now_eat())
                admin_message = (
                    "New user verified\n"
                    f"Name: {display_name}\n"
                    f"Username: {escape(username)}\n"
                    f"Chat ID: {user_id}\n"
                    f"Verified at: {verified_at}"
                )
        else:
            outcome = "wrong"
    conn.close()

    if outcome == "correct":
        await update.message.reply_text("✅ Correct! You will now receive instant job alerts.")
        if admin_message:
            await send_message_with_retry(context.bot, ADMIN_ID, admin_message)
    elif outcome == "wrong":
        await update.message.reply_text("❌ Wrong answer. Try again or type /start.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        text = (
            "Available commands:\n"
            "/start - Verify and receive job alerts\n"
            "/stop - Opt out of job alerts\n"
            "/admin - Admin menu\n"
            "/broadcast - Send message to all verified users\n"
            "/database - Send database backup\n"
            "/restore - Restore a validated database backup\n"
            "/report - Generate 3/6/12 month reports\n"
            "/testjob - Send latest job test"
        )
    else:
        text = (
            "Available commands:\n"
            "/start - Verify and receive job alerts\n"
            "/stop - Opt out of job alerts"
        )

    await update.message.reply_text(text)


async def opt_out_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""

    conn = get_db_connection()
    c = conn.cursor()
    joined_at = now_eat().isoformat(timespec="seconds")
    c.execute(
        '''INSERT INTO users (chat_id, username, joined_at, is_verified)
           VALUES (?, ?, ?, 0)
           ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username, is_verified = 0''',
        (user.id, username, joined_at)
    )
    c.execute('DELETE FROM pending_quiz WHERE chat_id=?', (user.id,))
    conn.commit()
    conn.close()

    await update.message.reply_text("You have opted out. Use /start to re-enable job alerts.")

# --- ADMIN & REPORTING ---
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Select report duration:", reply_markup=build_report_keyboard())


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    context.user_data["restore_deadline"] = time.monotonic() + RESTORE_TIMEOUT_SECONDS
    await update.message.reply_text(
        "Send or forward the backup as a document named myjobkenya.db within 5 minutes."
    )


async def handle_restore_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document if message is not None else None
    if document is None or update.effective_user is None:
        return

    file_name = document.file_name or ""
    is_admin = update.effective_user.id == ADMIN_ID
    if not is_admin:
        if file_name == RESTORE_FILENAME:
            await message.reply_text("Unauthorized.")
        return

    deadline = context.user_data.get("restore_deadline")
    if not deadline:
        return

    if time.monotonic() > deadline:
        clear_restore_state(context.user_data)
        await message.reply_text("The restore request timed out. Send /restore to start again.")
        return

    if file_name != RESTORE_FILENAME:
        clear_restore_state(context.user_data)
        await message.reply_text("That file was rejected. Send a document named myjobkenya.db.")
        return

    file_size = getattr(document, "file_size", None)
    if file_size is not None and file_size > MAX_RESTORE_BYTES:
        clear_restore_state(context.user_data)
        await message.reply_text("That file was rejected. The database must be 10 MB or smaller.")
        return

    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix="mjm-restore-", suffix=".db")
        os.close(fd)
        temp_path = Path(temp_name)
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(temp_path))
        if temp_path.stat().st_size > MAX_RESTORE_BYTES:
            clear_restore_state(context.user_data)
            await message.reply_text("That file was rejected. The database must be 10 MB or smaller.")
            return

        async with db_activity_lock:
            backed_up = install_validated_database(temp_path)
    except RestoreValidationError:
        clear_restore_state(context.user_data)
        logging.error("Database restore failed validation")
        await message.reply_text(
            "Restore failed. The uploaded file did not pass validation, so the live database was not changed."
        )
        return
    except Exception:
        clear_restore_state(context.user_data)
        logging.error("Database restore failed")
        await message.reply_text("Restore failed. The live database was not changed.")
        return
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    clear_restore_state(context.user_data)
    logging.info("Database restore completed")
    if backed_up:
        await message.reply_text(
            "Restore complete. The previous live database was backed up locally."
        )
    else:
        await message.reply_text(
            "Restore complete. There was no previous live database to back up."
        )


async def database_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    delivered = await send_db_backup(context.bot, ADMIN_ID, "manual export")
    if not delivered:
        await update.message.reply_text("Database export failed. Check logs.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    message_text = ""
    if update.message.reply_to_message:
        message_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not message_text:
        message_text = " ".join(context.args).strip()

    if not message_text:
        await update.message.reply_text("Usage: /broadcast <message> or reply to a message with /broadcast")
        return

    sent = await send_broadcast(context.bot, message_text)
    if sent == 0:
        await update.message.reply_text("No verified users available.")
        return

    await update.message.reply_text(f"Broadcast complete. Messages delivered: {sent}")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Admin menu:", reply_markup=build_admin_keyboard())


async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()
    with suppress(Exception):
        await query.message.edit_reply_markup(reply_markup=None)
    action = query.data

    if action == "admin_broadcast":
        context.user_data["awaiting_broadcast"] = True
        await query.message.reply_text("Send the broadcast message now.")
        return

    if action == "admin_database":
        delivered = await send_db_backup(context.bot, ADMIN_ID, "manual export")
        if not delivered:
            await query.message.reply_text("Database export failed. Check logs.")
        return

    if action == "admin_report":
        await query.message.reply_text("Select report duration:", reply_markup=build_report_keyboard())
        return

    if action == "admin_testjob":
        latest = get_latest_untested_feed_entry()
        if latest is None:
            await query.message.reply_text("No other new job available at the moment homie")
            return

        latest_link = getattr(latest, "link", None)
        msg = format_job_message(latest, test_mode=True)
        delivered = await send_message_with_retry(context.bot, ADMIN_ID, msg)
        if delivered and latest_link:
            mark_test_job_sent(latest_link)
        delivered_count = 1 if delivered else 0
        await query.message.reply_text(f"Test broadcast complete. Messages delivered: {delivered_count}")
        return


async def testjob_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    latest = get_latest_untested_feed_entry()
    if latest is None:
        await update.message.reply_text("No other new job available at the moment homie")
        return

    latest_link = getattr(latest, "link", None)
    msg = format_job_message(latest, test_mode=True)
    delivered = await send_message_with_retry(context.bot, ADMIN_ID, msg)
    if delivered and latest_link:
        mark_test_job_sent(latest_link)
    delivered_count = 1 if delivered else 0
    await update.message.reply_text(f"Test broadcast complete. Messages delivered: {delivered_count}")

async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()
    with suppress(Exception):
        await query.message.edit_reply_markup(reply_markup=None)
    months = int(query.data.split('_')[1])

    conn = get_db_connection()
    # Filter by date
    cutoff_iso = (now_eat() - timedelta(days=months * 30)).isoformat(timespec="seconds")
    df = pd.read_sql_query(
        '''SELECT
               u.chat_id,
               u.username,
               u.joined_at,
               u.is_verified,
               COALESCE(s.jobs_sent, 0) AS jobs_sent
           FROM users u
           LEFT JOIN user_delivery_stats s ON s.chat_id = u.chat_id
           WHERE u.joined_at > ?''',
        conn,
        params=(cutoff_iso,)
    )
    conn.close()

    if not df.empty:
        sort_key = pd.to_datetime(df["joined_at"], errors="coerce")
        df = df.assign(_sort_key=sort_key).sort_values(by="_sort_key", ascending=False).drop(columns=["_sort_key"])
        df["joined_at"] = df["joined_at"].apply(normalize_joined_at_to_eat)
        df["username"] = df["username"].fillna("").replace("", "(no username)")
        df["jobs_sent"] = pd.to_numeric(df["jobs_sent"], errors="coerce").fillna(0).astype(int)
    else:
        df = pd.DataFrame(columns=["chat_id", "username", "joined_at", "is_verified", "jobs_sent"])

    # Create Excel in statement-style format
    excel_file = BASE_DIR / f"report_{months}m.xlsx"
    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="User Statement")
        ws = writer.book["User Statement"]

        header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(fill_type="solid", fgColor="1E293B")
        header_alignment = Alignment(horizontal="center", vertical="center")
        thin_border = Border(
            left=Side(style="thin", color="D1D5DB"),
            right=Side(style="thin", color="D1D5DB"),
            top=Side(style="thin", color="D1D5DB"),
            bottom=Side(style="thin", color="D1D5DB"),
        )

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 24

        for col_idx in range(1, ws.max_column + 1):
            header_cell = ws.cell(row=1, column=col_idx)
            header_cell.font = header_font
            header_cell.fill = header_fill
            header_cell.alignment = header_alignment
            header_cell.border = thin_border

            max_len = len(str(header_cell.value or ""))
            for row_idx in range(2, ws.max_row + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                value_len = len(str(cell.value or ""))
                if value_len > max_len:
                    max_len = value_len
                cell.border = thin_border
                if col_idx == 2:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center")

            adjusted_width = min(max(max_len + 2, 14), 34)
            ws.column_dimensions[get_column_letter(col_idx)].width = adjusted_width

        # Stripe alternating rows for readability.
        stripe_fill = PatternFill(fill_type="solid", fgColor="F8FAFC")
        for row_idx in range(2, ws.max_row + 1):
            if row_idx % 2 == 0:
                for col_idx in range(1, ws.max_column + 1):
                    ws.cell(row=row_idx, column=col_idx).fill = stripe_fill

    # Create PDF in statement-style layout
    pdf_file = BASE_DIR / f"report_{months}m.pdf"
    can = canvas.Canvas(str(pdf_file), pagesize=letter)

    page_width, page_height = letter
    margin = 40
    report_title = "MYJOB KENYA USER STATEMENT"
    generated_at = format_eat(now_eat())
    period_start = format_eat(now_eat() - timedelta(days=months * 30))
    period_end = format_eat(now_eat())

    can.setFillColor(colors.HexColor("#0F172A"))
    can.rect(0, page_height - 90, page_width, 90, fill=1, stroke=0)
    can.setFillColor(colors.white)
    can.setFont("Helvetica-Bold", 18)
    can.drawString(margin, page_height - 42, report_title)
    can.setFont("Helvetica", 10)
    can.drawString(margin, page_height - 62, f"Generated: {generated_at}")

    can.setFillColor(colors.black)
    can.setFont("Helvetica-Bold", 11)
    can.drawString(margin, page_height - 112, "Statement Summary")
    can.setFont("Helvetica", 10)
    total_users = len(df)
    verified_users = int(df["is_verified"].sum()) if total_users else 0
    pending_users = total_users - verified_users
    total_jobs_sent = int(df["jobs_sent"].sum()) if total_users else 0
    can.drawString(margin, page_height - 128, f"Period: {period_start} to {period_end}")
    can.drawString(margin, page_height - 144, f"Total Users: {total_users}")
    can.drawString(margin + 180, page_height - 144, f"Verified: {verified_users}")
    can.drawString(margin + 300, page_height - 144, f"Pending: {pending_users}")
    can.drawString(margin + 420, page_height - 144, f"Jobs Sent: {total_jobs_sent}")

    table_top = page_height - 180
    row_h = 20
    col_x = [margin, 118, 238, 378, 468]
    headers = ["CHAT ID", "USERNAME", "JOINED AT (EAT)", "STATUS", "JOBS SENT"]

    can.setFillColor(colors.HexColor("#1E293B"))
    can.rect(margin, table_top, page_width - (2 * margin), row_h, fill=1, stroke=0)
    can.setFillColor(colors.white)
    can.setFont("Helvetica-Bold", 9)
    for i, header in enumerate(headers):
        can.drawString(col_x[i] + 4, table_top + 6, header)

    y = table_top - row_h
    can.setFont("Helvetica", 9)
    max_rows = 22
    preview = df.head(max_rows)
    for display_idx, (_, row) in enumerate(preview.iterrows()):
        if (display_idx % 2) == 0:
            can.setFillColor(colors.HexColor("#F8FAFC"))
            can.rect(margin, y, page_width - (2 * margin), row_h, fill=1, stroke=0)
        can.setFillColor(colors.black)
        status = "VERIFIED" if int(row["is_verified"]) == 1 else "PENDING"
        can.drawString(col_x[0] + 4, y + 6, str(row["chat_id"]))
        can.drawString(col_x[1] + 4, y + 6, str(row["username"])[:18])
        can.drawString(col_x[2] + 4, y + 6, str(row["joined_at"]))
        can.drawString(col_x[3] + 4, y + 6, status)
        can.drawString(col_x[4] + 4, y + 6, str(row["jobs_sent"]))
        y -= row_h

    can.setStrokeColor(colors.HexColor("#CBD5E1"))
    can.rect(margin, y + row_h, page_width - (2 * margin), (table_top - y), fill=0, stroke=1)

    can.setFont("Helvetica-Oblique", 8)
    can.setFillColor(colors.HexColor("#475569"))
    can.drawString(margin, 24, "Confidential: Internal administrative statement")
    can.save()

    with open(excel_file, 'rb') as excel_handle:
        await query.message.reply_document(document=excel_handle)
    with open(pdf_file, 'rb') as pdf_handle:
        await query.message.reply_document(document=pdf_handle)

# --- MAIN EXECUTION ---
def main():
    init_db()

    # Start periodic job checker without relying on PTB JobQueue extras.
    async def start_background_tasks(application):
        application.bot_data["job_loop_task"] = asyncio.create_task(job_loop(application.bot))

        async def weekly_db_task():
            next_run = next_weekly_backup_time()
            while True:
                sleep_seconds = max((next_run - now_eat()).total_seconds(), 0)
                await asyncio.sleep(sleep_seconds)
                await send_db_backup(application.bot, ADMIN_ID, "weekly backup")
                next_run = next_run + timedelta(days=7)

        application.bot_data["weekly_db_task"] = asyncio.create_task(weekly_db_task())

    async def stop_background_tasks(application):
        for key in ("job_loop_task", "weekly_db_task"):
            task = application.bot_data.pop(key, None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_background_tasks)
        .post_shutdown(stop_background_tasks)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("commands", help_command))
    app.add_handler(CommandHandler("stop", opt_out_command))
    app.add_handler(CommandHandler("cancel", opt_out_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("testjob", testjob_command))
    app.add_handler(CommandHandler("database", database_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_restore_document))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(generate_report, pattern='^rep_'))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern='^admin_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running locally...")
    app.run_polling()

if __name__ == '__main__':
    main()