import re
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes
from flask import Flask, request, Response
import asyncio

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g., "https://yourdomain.com"
# Ensure HTTPS prefix and remove trailing slash
if WEBHOOK_URL:
    WEBHOOK_URL = WEBHOOK_URL.rstrip('/')
    if not WEBHOOK_URL.startswith("http"):
        WEBHOOK_URL = f"https://{WEBHOOK_URL}"
PORT = int(os.getenv("PORT", "8443"))
WARNING_TTL = 60*5

# Parse group IDs from comma-separated string
TARGET_GROUP_IDS = []
if GROUP_IDS_STR:
    TARGET_GROUP_IDS = [int(gid.strip()) for gid in GROUP_IDS_STR.split(",") if gid.strip()]

# Daily limit configuration
DB_PATH = os.getenv("DB_PATH", "requests.db")
MAX_REQUESTS_PER_DAY = 2
IST = timezone(timedelta(hours=5, minutes=30))

# Compiled regex patterns
DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
DOI_URL_REGEX = re.compile(r"https?://(dx\.)?doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
CLEANUP_REGEX = re.compile(r"\bdoi\s*:\s*|[^\w]", re.IGNORECASE)
DOI_IN_URL_REGEX = re.compile(r"https?://[^\s/]+/[^\s]*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
DIRECT_LINK_REGEX = re.compile(
    r"https?://(www\.)?("
    r"ieeexplore\.ieee\.org/(abstract/)?document/\d+|"
    r"sciencedirect\.com|"
    r"linkinghub\.elsevier\.com|"
    r"link\.springer\.com|"
    r"springer\.com|"
    r"connect\.springerpub\.com|"
    r"(pubmed|pmc|ncbi)\.ncbi\.nlm\.nih\.gov|"
    r"nature\.com|"
    r"researchgate\.net|"
    r"semanticscholar\.org|"
    r"emerald\.com|"
    r"ascelibrary\.org|"
    r"share\.google"
    r")(/\S*)?",
    re.IGNORECASE
)

PERSIAN_REGEX = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+')
ENGLISH_REGEX = re.compile(r'[a-zA-Z]+')

# Bot state
bot_active = True
request_count = 0

# Flask app
app = Flask(__name__)

# Global bot application
bot_app = None
webhook_setup_done = False

def init_db():
    """Initialize SQLite database with message_id column."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            user_id INTEGER,
            doi TEXT,
            ts INTEGER,
            message_id INTEGER
        )
    """)
    
    c.execute("PRAGMA table_info(requests)")
    columns = [row[1] for row in c.fetchall()]
    
    if 'message_id' not in columns:
        print("📊 Adding message_id column to existing database...")
        c.execute("ALTER TABLE requests ADD COLUMN message_id INTEGER")
        print("✅ message_id column added successfully")
    
    conn.commit()
    conn.close()

def today_4am_ist_timestamp():
    """Get timestamp of today's 4 AM IST (or yesterday's if it's before 4 AM now)."""
    now = datetime.now(IST)
    four_am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now < four_am:
        four_am -= timedelta(days=1)
    return int(four_am.timestamp())

def user_request_count(user_id: int) -> int:
    """Count user's valid requests since 4 AM IST today."""
    since = today_4am_ist_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM requests WHERE user_id = ? AND ts >= ?", (user_id, since))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_duplicate_doi_message_id(user_id: int, doi: str) -> int | None:
    """Get the message_id of a duplicate DOI request from today, if it exists."""
    since = today_4am_ist_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT message_id FROM requests WHERE user_id = ? AND doi = ? AND ts >= ? LIMIT 1", 
              (user_id, doi.lower(), since))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def delete_request_by_message_id(message_id: int):
    """Delete a request entry from the database by message_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM requests WHERE message_id = ?", (message_id,))
    conn.commit()
    conn.close()

def get_user_request_by_message_id(message_id: int) -> tuple[int, str] | None:
    """Get user_id and DOI for a specific message_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, doi FROM requests WHERE message_id = ?", (message_id,))
    result = c.fetchone()
    conn.close()
    return result if result else None

def update_request_doi(message_id: int, new_doi: str):
    """Update the DOI for an existing request by message_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE requests SET doi = ? WHERE message_id = ?", (new_doi.lower(), message_id))
    conn.commit()
    conn.close()

def log_user_request(user_id: int, doi: str, message_id: int):
    """Log a valid user request to the database with message_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO requests (user_id, doi, ts, message_id) VALUES (?, ?, ?, ?)",
              (user_id, doi.lower(), int(time.time()), message_id))
    conn.commit()
    conn.close()

def extract_dois(text: str) -> list[str]:
    """Extract unique DOIs from text, including from any links."""
    if not text:
        return []
    
    url_dois = [m[1] for m in DOI_URL_REGEX.findall(text)]
    link_dois = [m[1] for m in DOI_IN_URL_REGEX.findall(text)]
    plain_dois = DOI_REGEX.findall(text)
    
    seen = set()
    unique = []
    for doi in url_dois + link_dois + plain_dois:
        doi_normalized = doi.lower().rstrip('/')
        if re.match(r'^10\.\d{4,9}/.+', doi_normalized) and doi_normalized not in seen:
            seen.add(doi_normalized)
            unique.append(doi)
    
    return unique

def has_direct_link_without_doi(text: str) -> bool:
    """Check if message contains article links WITHOUT any DOI."""
    if not text:
        return False
    return bool(DIRECT_LINK_REGEX.search(text)) and not extract_dois(text)

def has_only_persian_text(text: str, dois: list[str]) -> bool:
    """Check if message contains only DOI and Persian text (no English at all)."""
    if not text or not dois:
        return False
    
    cleaned = DOI_URL_REGEX.sub("", text)
    cleaned = DIRECT_LINK_REGEX.sub("", cleaned)
    
    for doi in dois:
        cleaned = cleaned.replace(doi, "").replace(doi.lower(), "")
    
    cleaned = re.sub(r"\bdoi\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://\S+', '', cleaned)
    cleaned = re.sub(r'\d+', '', cleaned)
    cleaned = re.sub(r'[^\w\s\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', '', cleaned)
    cleaned = cleaned.strip()
    
    if not cleaned:
        return False
    
    return PERSIAN_REGEX.search(cleaned) is not None and ENGLISH_REGEX.search(cleaned) is None

def is_doi_only_message(text: str, dois: list[str]) -> bool:
    """Check if message contains only DOI(s) without article title."""
    if not text or not dois:
        return False

    cleaned = DOI_URL_REGEX.sub("", text)
    cleaned = DIRECT_LINK_REGEX.sub("", cleaned)
    
    for doi in dois:
        cleaned = cleaned.replace(doi, "").replace(doi.lower(), "")
    
    cleaned = CLEANUP_REGEX.sub("", cleaned)
    return len(cleaned.strip()) == 0

def log_status(status: str, user_name: str, user_id: int, doi: str, reason: str = "") -> None:
    """Fast terminal logging."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    symbol = "✅" if status == "VALID" else ("🔄" if status == "UPDATED" else ("⏪" if status == "REVERTED" else "❌"))
    doi_display = doi[:30] + ('...' if len(doi) > 30 else '')
    print(f"{symbol} [{timestamp}] {status} | {user_name} ({user_id}) | {doi_display} | {reason}")

async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Check if user is an admin in the group."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start bot moderation (admin only)."""
    global bot_active
    
    chat = update.effective_chat
    user = update.effective_user
    
    if not chat or not user or chat.id not in TARGET_GROUP_IDS:
        return
    
    if not await is_admin(context, chat.id, user.id):
        print(f"⚠️ Non-admin {user.first_name} ({user.id}) tried to use /start")
        try:
            await update.message.delete()
        except:
            pass
        return
    
    bot_active = True
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"▶️ BOT STARTED by {user.first_name} ({user.id})")
    print(f"   Timestamp: {timestamp}")
    print(f"{'='*70}\n")
    
    try:
        await update.message.delete()
    except:
        pass
    
    msg = await context.bot.send_message(
        chat_id=chat.id,
        text="✅ Bot moderation activated"
    )
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except:
        pass

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop bot moderation (admin only)."""
    global bot_active
    
    chat = update.effective_chat
    user = update.effective_user
    
    if not chat or not user or chat.id not in TARGET_GROUP_IDS:
        return
    
    if not await is_admin(context, chat.id, user.id):
        print(f"⚠️ Non-admin {user.first_name} ({user.id}) tried to use /stop")
        try:
            await update.message.delete()
        except:
            pass
        return
    
    bot_active = False
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"⏸️ BOT STOPPED by {user.first_name} ({user.id})")
    print(f"   Timestamp: {timestamp}")
    print(f"{'='*70}\n")
    
    try:
        await update.message.delete()
    except:
        pass
    
    msg = await context.bot.send_message(
        chat_id=chat.id,
        text="⏸️ Bot moderation deactivated"
    )
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except:
        pass

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process messages."""
    global request_count
    
    if not bot_active:
        return
    
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    
    is_edit = update.edited_message is not None

    if not (msg and chat and user and msg.text) or chat.id not in TARGET_GROUP_IDS:
        return

    message_id = msg.message_id
    user_name = user.first_name or "Unknown"
    
    if not is_edit:
        await asyncio.sleep(1.5)

    if has_direct_link_without_doi(msg.text):
        log_status("REJECTED", user_name, user.id, "Direct Link (No DOI)", "Missing DOI")
        if not is_edit:
            deleted = await try_delete_message(context, msg)
            if deleted:
                asyncio.create_task(send_warning(
                    context, chat.id, user.id, user_name,
                    "لطفاً عنوان مقاله و doi مقاله را در درخواست خود اضافه کنید",
                    None
                ))
        return

    dois = extract_dois(msg.text)
    
    if not dois:
        return

    if has_only_persian_text(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "Persian text only")
        if not is_edit:
            deleted = await try_delete_message(context, msg)
            if deleted:
                asyncio.create_task(send_warning(
                    context, chat.id, user.id, user_name,
                    "لطفاً عنوان مقاله را به درخواست خود اضافه کنید",
                    None
                ))
        return

    if is_doi_only_message(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "No title")
        if not is_edit:
            deleted = await try_delete_message(context, msg)
            if deleted:
                asyncio.create_task(send_warning(
                    context, chat.id, user.id, user_name,
                    "لطفاً عنوان مقاله را به درخواست خود اضافه کنید",
                    None
                ))
        return

    unique_dois = len(set(d.lower() for d in dois))
    if unique_dois > 1:
        log_status("REJECTED", user_name, user.id, ", ".join(dois[:2]), f"{unique_dois} DOIs")
        
        deleted = await try_delete_message(context, msg)
        
        if is_edit and deleted:
            previous_request = get_user_request_by_message_id(message_id)
            if previous_request:
                delete_request_by_message_id(message_id)
                log_status("INFO", user_name, user.id, ", ".join(dois[:2]), f"Deleted database entry for msg_id:{message_id} (edited to multiple DOIs)")
        
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "لطفاً درخواست خود را به دو پیام جداگانه ارسال کنید. شما می‌توانید حداکثر دو مقاله در روز درخواست کنید",
                None
            ))
        return

    doi = dois[0]
    is_user_admin = await is_admin(context, chat.id, user.id)

    if is_edit:
        previous_request = get_user_request_by_message_id(message_id)
        
        if previous_request:
            prev_user_id, prev_doi = previous_request
            
            if prev_doi.lower() != doi.lower():
                log_status("INFO", user_name, user.id, doi, f"DOI change detected: {prev_doi} → {doi}")
            else:
                log_status("VALID", user_name, user.id, doi, f"[EDITED - No DOI change] msg_id:{message_id}")
                return

    if not is_user_admin:
        duplicate_message_id = get_duplicate_doi_message_id(user.id, doi)
        
        if duplicate_message_id is not None and duplicate_message_id != message_id:
            try:
                mention = f'<a href="tg://user?id={user.id}">{user_name}</a>'
                warning_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"🚫 {mention}, درخواست تکراری نفرستید",
                    parse_mode="HTML",
                    reply_to_message_id=duplicate_message_id
                )
                
                log_status("REJECTED", user_name, user.id, doi, f"Duplicate DOI (original msg_id:{duplicate_message_id})")
                
                if not is_edit:
                    await try_delete_message(context, msg)
                else:
                    previous_request = get_user_request_by_message_id(message_id)
                    if previous_request:
                        prev_user_id, prev_doi = previous_request
                        log_status("REVERTED", user_name, user.id, prev_doi, f"Edit created duplicate, kept original {prev_doi} | msg_id:{message_id}")
                
                asyncio.create_task(auto_delete_warning(context, chat.id, warning_msg.message_id))
                return
                
            except Exception as e:
                error_str = str(e).lower()
                if any(err in error_str for err in ["reply message not found", "message not found", "message to be replied not found"]):
                    print(f"🔄 Original message {duplicate_message_id} was deleted - allowing new request and cleaning database")
                    delete_request_by_message_id(duplicate_message_id)
                    log_status("INFO", user_name, user.id, doi, f"Cleaned deleted msg_id:{duplicate_message_id} from database")
                else:
                    print(f"Error sending duplicate warning: {e}")
                    return

    if is_edit:
        previous_request = get_user_request_by_message_id(message_id)
        if previous_request:
            prev_user_id, prev_doi = previous_request
            if prev_doi.lower() != doi.lower():
                update_request_doi(message_id, doi)
                log_status("UPDATED", user_name, user.id, doi, f"DOI updated from {prev_doi} | msg_id:{message_id}")
                return

    if not is_user_admin and not is_edit and user_request_count(user.id) >= MAX_REQUESTS_PER_DAY:
        log_status("REJECTED", user_name, user.id, doi, "Daily limit reached")
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "روزانه فقط دو مقاله میتونید درخواست بدین",
                None
            ))
        return

    if not is_edit:
        log_user_request(user.id, doi, message_id)
        request_count += 1
        admin_badge = " [ADMIN]" if is_user_admin else ""
        log_status("VALID", user_name, user.id, doi, f"Request #{request_count}{admin_badge} | msg_id:{message_id}")
    elif not get_user_request_by_message_id(message_id):
        log_user_request(user.id, doi, message_id)
        request_count += 1
        admin_badge = " [ADMIN]" if is_user_admin else ""
        log_status("VALID", user_name, user.id, doi, f"[EDITED - Now Valid] Request #{request_count}{admin_badge} | msg_id:{message_id}")

async def try_delete_message(context, message) -> bool:
    """Try to delete a message. Returns True if successful, False if already deleted."""
    try:
        await message.delete()
        return True
    except Exception as e:
        if any(err in str(e).lower() for err in ["message to delete not found", "message not found"]):
            return False
        print(f"Error deleting message: {e}")
        return False

async def send_warning(context, chat_id, user_id, user_name, warning_text, reply_to_message_id):
    """Send a warning message that auto-deletes."""
    try:
        mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 {mention}, {warning_text}",
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id
        )
        await asyncio.sleep(WARNING_TTL)
        await msg.delete()
    except Exception as e:
        print(f"Error sending warning: {e}")

async def auto_delete_warning(context, chat_id, message_id):
    """Auto-delete a warning message after TTL."""
    try:
        await asyncio.sleep(WARNING_TTL)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Error auto-deleting warning: {e}")

async def handle_update(update_data):
    """Process incoming update asynchronously."""
    global webhook_setup_done
    
    # Lazy webhook setup on first update
    if not webhook_setup_done:
        asyncio.create_task(setup_webhook_async())
        webhook_setup_done = True
    
    try:
        update = Update.de_json(update_data, bot_app.bot)
        
        if update.message and update.message.text:
            if update.message.text.startswith('/start'):
                await start_command(update, bot_app)
                return
            elif update.message.text.startswith('/stop'):
                await stop_command(update, bot_app)
                return
        
        await process_message(update, bot_app)
    except Exception as e:
        print(f"Error handling update: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming webhook requests."""
    if request.method == 'POST':
        update_data = request.get_json(force=True)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(handle_update(update_data))
        finally:
            loop.close()
        
        return Response(status=200)
    
    return Response(status=405)

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return {'status': 'ok', 'bot_active': bot_active, 'webhook_setup': webhook_setup_done}, 200

@app.route('/setup_webhook', methods=['GET', 'POST'])
def manual_webhook_setup():
    """Manual webhook setup endpoint."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(setup_webhook_async())
        loop.close()
        return {'status': 'success', 'message': result}, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/')
def index():
    """Root endpoint."""
    return {
        'message': 'DOI Moderation Bot Webhook Service',
        'webhook_setup': webhook_setup_done,
        'bot_active': bot_active
    }, 200

async def setup_webhook_async():
    """Set up the webhook with Telegram (async, with retries)."""
    global webhook_setup_done
    
    if webhook_setup_done:
        return "Webhook already set up"
    
    webhook_url = f"{WEBHOOK_URL}/webhook"
    
    for attempt in range(3):
        try:
            print(f"🔄 Setting up webhook (attempt {attempt + 1}/3)...")
            await bot_app.bot.set_webhook(webhook_url, drop_pending_updates=True)
            print(f"✅ Webhook set to: {webhook_url}")
            webhook_setup_done = True
            return f"Webhook successfully set to {webhook_url}"
        except Exception as e:
            print(f"⚠️  Webhook setup attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(5)
            else:
                print("⚠️  Webhook setup will be retried on first incoming update")
                return f"Webhook setup delayed: {str(e)}"

def init_bot_app():
    """Initialize bot application without setting webhook."""
    from telegram.request import HTTPXRequest
    
    request_obj = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    
    global bot_app
    bot_app = ApplicationBuilder() \
        .token(BOT_TOKEN) \
        .request(request_obj) \
        .build()

def main():
    """Initialize the application."""
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN environment variable not set!")
        return
    
    if not TARGET_GROUP_IDS:
        print("❌ ERROR: GROUP_IDS environment variable not set!")
        return
    
    if not WEBHOOK_URL:
        print("❌ ERROR: WEBHOOK_URL environment variable not set!")
        return
    
    init_db()
    init_bot_app()
    
    print("="*70)
    print("🤖 DOI MODERATION BOT WEBHOOK SERVICE STARTED")
    print("="*70)
    print(f"   Message processing delay: 1.5 seconds")
    print(f"   Warning auto-delete: {WARNING_TTL} seconds")
    print(f"   Target group IDs: {', '.join(map(str, TARGET_GROUP_IDS))}")
    print(f"   Webhook URL: {WEBHOOK_URL}/webhook")
    print(f"   Port: {PORT}")
    print(f"   Webhook setup: Will be done on first update or manually via /setup_webhook")
    print(f"   Direct link check: IEEE, ScienceDirect, Springer")
    print(f"   Language: Any English text required")
    print(f"   Daily limit: {MAX_REQUESTS_PER_DAY} requests per user")
    print(f"   Duplicate DOI: Blocked per user per day")
    print(f"   Duplicate handling: Reply to original message")
    print(f"   Edit handling: Smart DOI change detection")
    print(f"   Reset time: 4:00 AM IST")
    print(f"   Bot status: {'ACTIVE' if bot_active else 'INACTIVE'}")
    print(f"   Admin bypass: Enabled (no limits for admins)")
    print(f"   Admin commands: /start, /stop")
    print(f"   Database: Tracking message_id for all valid requests")
    print("="*70)
    print(f"\n💡 To manually setup webhook, visit: {WEBHOOK_URL}/setup_webhook")
    print()

# Initialize when module is imported (for gunicorn)
main()

if __name__ == "__main__":
    # Only run Flask dev server if executed directly
    app.run(host='0.0.0.0', port=PORT, debug=False)
