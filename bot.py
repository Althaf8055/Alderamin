import re
import asyncio
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "")
WARNING_TTL = 60*10

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

# DOI embedded in any URL path (generalized for all publishers)
DOI_IN_URL_REGEX = re.compile(
    r"https?://[^\s/]+/[^\s]*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE
)

# Direct links pattern (IEEE, ScienceDirect, Springer, PubMed, etc.)
DIRECT_LINK_REGEX = re.compile(
    r"https?://(www\.)?(ieeexplore\.ieee\.org|sciencedirect\.com|link\.springer\.com|springer\.com|"
    r"pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/\S+",
    re.IGNORECASE
)

# Persian/Arabic character range
PERSIAN_REGEX = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+')

# English letter detection
ENGLISH_REGEX = re.compile(r'[a-zA-Z]+')

# Bot state
bot_active = True
request_count = 0

def init_db():
    """Initialize SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            user_id INTEGER,
            doi TEXT,
            ts INTEGER
        )
    """)
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
    c.execute(
        "SELECT COUNT(*) FROM requests WHERE user_id = ? AND ts >= ?",
        (user_id, since)
    )
    count = c.fetchone()[0]
    conn.close()
    return count

def has_duplicate_doi_today(user_id: int, doi: str) -> bool:
    """Check if user has already requested this DOI since 4 AM IST today."""
    since = today_4am_ist_timestamp()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM requests WHERE user_id = ? AND doi = ? AND ts >= ? LIMIT 1",
        (user_id, doi.lower(), since)
    )
    exists = c.fetchone() is not None
    conn.close()
    return exists

def log_user_request(user_id: int, doi: str):
    """Log a valid user request to the database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO requests (user_id, doi, ts) VALUES (?, ?, ?)",
        (user_id, doi.lower(), int(time.time()))
    )
    conn.commit()
    conn.close()

def extract_dois(text: str) -> list[str]:
    """Extract unique DOIs from text, including from any links."""
    if not text:
        return []
    
    # Extract DOIs from doi.org URLs
    url_dois = [m[1] for m in DOI_URL_REGEX.findall(text)]
    
    # Extract DOIs from ANY URL (IEEE, Springer, ScienceDirect, etc.)
    link_dois = [m[1] for m in DOI_IN_URL_REGEX.findall(text)]
    
    # Extract plain DOIs from text
    plain_dois = DOI_REGEX.findall(text)
    
    # Combine all DOIs
    all_dois = url_dois + link_dois + plain_dois
    
    # Deduplicate and validate
    seen = set()
    unique = []
    for doi in all_dois:
        # Normalize: lowercase and remove trailing slash
        doi_normalized = doi.lower().rstrip('/')
        
        # Validate: DOI must have format 10.XXXX/something (at least 4 digits after 10.)
        if not re.match(r'^10\.\d{4,9}/.+', doi_normalized):
            continue
            
        if doi_normalized not in seen:
            seen.add(doi_normalized)
            unique.append(doi)
    
    return unique

def has_direct_link_without_doi(text: str) -> bool:
    """Check if message contains article links WITHOUT any DOI."""
    if not text:
        return False
    
    # Check if there are any direct links to publishers
    direct_links = DIRECT_LINK_REGEX.findall(text)
    if not direct_links:
        return False
    
    # Check if there are any DOIs anywhere in the message
    dois = extract_dois(text)
    
    # If there are direct links but NO DOI anywhere, return True (violation)
    return len(dois) == 0

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
    
    has_persian = PERSIAN_REGEX.search(cleaned) is not None
    has_english = ENGLISH_REGEX.search(cleaned) is not None
    
    return has_persian and not has_english

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
    symbol = "✅" if status == "VALID" else "❌"
    print(f"{symbol} [{timestamp}] {status} | {user_name} ({user_id}) | {doi[:30]}{'...' if len(doi) > 30 else ''} | {reason}")

async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Check if user is an admin in the group."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

async def message_still_exists(context, chat_id: int, message_id: int) -> bool:
    """Check if a message still exists by trying to copy it."""
    try:
        # Try to copy the message - most reliable way to check existence
        copied = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=message_id
        )
        # If copy succeeds, delete the copy immediately
        await context.bot.delete_message(chat_id=chat_id, message_id=copied.message_id)
        return True
    except Exception as e:
        # Message doesn't exist
        error_str = str(e).lower()
        if "message to copy not found" in error_str or "message not found" in error_str:
            return False
        # Other errors - assume message doesn't exist to be safe
        return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start bot moderation (admin only)."""
    global bot_active
    
    chat = update.effective_chat
    user = update.effective_user
    
    if not chat or not user:
        return
    
    if chat.id not in TARGET_GROUP_IDS:
        return
    
    if not await is_admin(context, chat.id, user.id):
        print(f"⚠️ Non-admin {user.first_name} ({user.id}) tried to use /start")
        return
    
    bot_active = True
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"▶️ BOT STARTED by {user.first_name} ({user.id})")
    print(f"   Timestamp: {timestamp}")
    print(f"{'='*70}\n")
    
    msg = await update.message.reply_text("✅ Bot moderation activated")
    await asyncio.sleep(5)
    try:
        await msg.delete()
        await update.message.delete()
    except:
        pass

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop bot moderation (admin only)."""
    global bot_active
    
    chat = update.effective_chat
    user = update.effective_user
    
    if not chat or not user:
        return
    
    if chat.id not in TARGET_GROUP_IDS:
        return
    
    if not await is_admin(context, chat.id, user.id):
        print(f"⚠️ Non-admin {user.first_name} ({user.id}) tried to use /stop")
        return
    
    bot_active = False
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"⏸️ BOT STOPPED by {user.first_name} ({user.id})")
    print(f"   Timestamp: {timestamp}")
    print(f"{'='*70}\n")
    
    msg = await update.message.reply_text("⏸️ Bot moderation deactivated")
    await asyncio.sleep(5)
    try:
        await msg.delete()
        await update.message.delete()
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

    if not (msg and chat and user and msg.text) or chat.id not in TARGET_GROUP_IDS:
        return

    message_id = msg.message_id
    user_name = user.first_name or "Unknown"
    
    # Wait 3 seconds before processing to let anti-spam bots act first
    await asyncio.sleep(3)
    
    # Check if message still exists (verification bot may have deleted it)
    exists = await message_still_exists(context, chat.id, message_id)
    print(f"🔍 Message {message_id} existence check: {exists}")
    
    if not exists:
        print(f"⚠️ Message {message_id} from {user_name} ({user.id}) was deleted by verification bot - NOT PROCESSING")
        return

    # RULE 0: Check for article links WITHOUT any DOI
    if has_direct_link_without_doi(msg.text):
        log_status("REJECTED", user_name, user.id, "Direct Link (No DOI)", "Missing DOI")
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "لطفاً عنوان مقاله و doi مقاله را در درخواست خود اضافه کنید"
            ))
        return

    dois = extract_dois(msg.text)
    
    # Debug: print what we found
    if dois:
        print(f"🔍 DEBUG: Found {len(dois)} DOI(s): {dois}")
        print(f"🔍 DEBUG: Message text: {msg.text[:100]}")
    
    if not dois:
        return

    # RULE 1: Check for Persian-only text (before checking if DOI-only)
    if has_only_persian_text(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "Persian text only")
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "لطفاً عنوان مقاله را به درخواست خود اضافه کنید"
            ))
        return

    # RULE 2: Missing title entirely
    if is_doi_only_message(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "No title")
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "لطفاً عنوان مقاله را به درخواست خود اضافه کنید"
            ))
        return

    # RULE 3: Multiple DOIs
    unique_dois = len(set(d.lower() for d in dois))
    if unique_dois > 1:
        log_status("REJECTED", user_name, user.id, ", ".join(dois[:2]), f"{unique_dois} DOIs")
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "لطفاً درخواست خود را به دو پیام جداگانه ارسال کنید. شما می‌توانید حداکثر دو مقاله در روز درخواست کنید"
            ))
        return

    doi = dois[0]

    # Check if user is admin (admins bypass all rate limits)
    is_user_admin = await is_admin(context, chat.id, user.id)

    # RULE 4: Duplicate DOI check (same user, same day) - skip for admins
    if not is_user_admin and has_duplicate_doi_today(user.id, doi):
        log_status("REJECTED", user_name, user.id, doi, "Duplicate DOI")
        # Try to delete - if it fails, the verification bot already deleted it
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "شما قبلاً این مقاله را امروز درخواست کرده‌اید"
            ))
        else:
            print(f"⚠️ Duplicate message {message_id} was already deleted by verification bot")
        return

    # RULE 5: Daily limit check (only for valid requests) - skip for admins
    if not is_user_admin and user_request_count(user.id) >= MAX_REQUESTS_PER_DAY:
        log_status("REJECTED", user_name, user.id, doi, "Daily limit reached")
        # Try to delete - if it fails, the verification bot already deleted it
        deleted = await try_delete_message(context, msg)
        if deleted:
            asyncio.create_task(send_warning(
                context, chat.id, user.id, user_name,
                "روزانه فقط دو مقاله میتونید درخواست بدین"
            ))
        else:
            print(f"⚠️ Limit-exceeded message {message_id} was already deleted by verification bot")
        return

    # VALID request - Check if message still exists before logging
    # This is the critical check - only log if message wasn't deleted by verification bot
    exists = await message_still_exists(context, chat.id, message_id)
    if not exists:
        print(f"⚠️ Valid message {message_id} from {user_name} ({user.id}) was deleted by verification bot - NOT LOGGING")
        return
    
    # Message exists and is valid - log it
    log_user_request(user.id, doi)
    request_count += 1
    admin_badge = " [ADMIN]" if is_user_admin else ""
    log_status("VALID", user_name, user.id, doi, f"Request #{request_count}{admin_badge}")

async def try_delete_message(context, message) -> bool:
    """Try to delete a message. Returns True if successful, False if already deleted."""
    try:
        await message.delete()
        return True
    except Exception as e:
        error_str = str(e).lower()
        if "message to delete not found" in error_str or "message not found" in error_str:
            return False
        print(f"Error deleting message: {e}")
        return False

async def send_warning(context, chat_id, user_id, user_name, warning_text):
    """Send a warning message that auto-deletes."""
    try:
        mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 {mention}, {warning_text}",
            parse_mode="HTML"
        )
        await asyncio.sleep(WARNING_TTL)
        await msg.delete()
    except Exception as e:
        print(f"Error sending warning: {e}")

def main() -> None:
    """Run bot."""
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN environment variable not set!")
        return
    
    if not TARGET_GROUP_IDS:
        print("❌ ERROR: GROUP_IDS environment variable not set!")
        return
    
    # Initialize database
    init_db()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.UpdateType.EDITED_MESSAGE,
        process_message
    ))

    print("="*70)
    print("🤖 DOI MODERATION BOT STARTED")
    print("="*70)
    print(f"   Message processing delay: 3 seconds")
    print(f"   Warning auto-delete: {WARNING_TTL} seconds")
    print(f"   Target group IDs: {', '.join(map(str, TARGET_GROUP_IDS))}")
    print(f"   Direct link check: IEEE, ScienceDirect, Springer")
    print(f"   Language: Any English text required")
    print(f"   Daily limit: {MAX_REQUESTS_PER_DAY} requests per user")
    print(f"   Duplicate DOI: Blocked per user per day")
    print(f"   Reset time: 4:00 AM IST")
    print(f"   Bot status: {'ACTIVE' if bot_active else 'INACTIVE'}")
    print(f"   Admin bypass: Enabled (no limits for admins)")
    print(f"   Admin commands: /start, /stop")
    print("="*70)
    print()
    
    app.run_polling()

if __name__ == "__main__":
    main()
