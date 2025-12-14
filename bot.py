import re
import asyncio
import os
from datetime import datetime
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

# Compiled regex patterns
DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
DOI_URL_REGEX = re.compile(r"https?://(dx\.)?doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
CLEANUP_REGEX = re.compile(r"\bdoi\s*:\s*|[^\w]", re.IGNORECASE)

# IEEE and other direct article link patterns
DIRECT_LINK_REGEX = re.compile(
    r"https?://(ieeexplore\.ieee\.org|dl\.acm\.org|link\.springer\.com|sciencedirect\.com|arxiv\.org)/\S+",
    re.IGNORECASE
)

# Persian/Arabic character range
PERSIAN_REGEX = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+')

# English letter detection
ENGLISH_REGEX = re.compile(r'[a-zA-Z]+')

# Bot state
bot_active = True
request_count = 0

def extract_dois(text: str) -> list[str]:
    """Extract unique DOIs from text."""
    if not text:
        return []
    
    url_dois = [m[1] for m in DOI_URL_REGEX.findall(text)]
    plain_dois = DOI_REGEX.findall(text)
    
    all_dois = url_dois + plain_dois
    seen = set()
    unique = []
    for doi in all_dois:
        doi_lower = doi.lower()
        if doi_lower not in seen:
            seen.add(doi_lower)
            unique.append(doi)
    
    return unique

def has_direct_link_without_doi(text: str) -> bool:
    """Check if message contains direct article links WITHOUT a separate DOI."""
    if not text:
        return False
    
    direct_links = DIRECT_LINK_REGEX.findall(text)
    if not direct_links:
        return False
    
    has_doi_org = DOI_URL_REGEX.search(text) is not None
    
    text_without_direct_links = DIRECT_LINK_REGEX.sub("", text)
    has_plain_doi = DOI_REGEX.search(text_without_direct_links) is not None
    
    return not (has_doi_org or has_plain_doi)

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

    user_name = user.first_name or "Unknown"
    
    # RULE 0: Check for direct links WITHOUT separate DOI (highest priority)
    if has_direct_link_without_doi(msg.text):
        log_status("REJECTED", user_name, user.id, "Direct Link (No DOI)", "Missing DOI")
        asyncio.create_task(delete_and_warn(
            context, msg, chat.id, user.id, user_name,
            "please include the DOI when sharing article links (IEEE, ACM, Springer, etc.)."
        ))
        return

    dois = extract_dois(msg.text)
    if not dois:
        return

    # RULE 1: Check for Persian-only text (before checking if DOI-only)
    if has_only_persian_text(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "Persian text only")
        asyncio.create_task(delete_and_warn(
            context, msg, chat.id, user.id, user_name,
            "please add the English article title to your request."
        ))
        return

    # RULE 2: Missing title entirely
    if is_doi_only_message(msg.text, dois):
        log_status("REJECTED", user_name, user.id, dois[0], "No title")
        asyncio.create_task(delete_and_warn(
            context, msg, chat.id, user.id, user_name,
            "please add the article title to your request."
        ))
        return

    # RULE 3: Multiple DOIs
    unique_dois = len(set(d.lower() for d in dois))
    if unique_dois > 1:
        log_status("REJECTED", user_name, user.id, ", ".join(dois[:2]), f"{unique_dois} DOIs")
        asyncio.create_task(delete_and_warn(
            context, msg, chat.id, user.id, user_name,
            "please send only one article per message."
        ))
        return

    doi = dois[0]

    # Valid request
    request_count += 1
    log_status("VALID", user_name, user.id, doi, f"Request #{request_count}")

async def delete_and_warn(context, message, chat_id, user_id, user_name, warning_text):
    """Delete and warn asynchronously."""
    try:
        await message.delete()
        
        mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 {mention}, {warning_text}",
            parse_mode="HTML"
        )
        
        await asyncio.sleep(WARNING_TTL)
        await msg.delete()
    except Exception as e:
        print(f"Error: {e}")

def main() -> None:
    """Run bot."""
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN environment variable not set!")
        return
    
    if not TARGET_GROUP_IDS:
        print("❌ ERROR: GROUP_IDS environment variable not set!")
        return
    
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
    print(f"   Warning auto-delete: {WARNING_TTL} seconds")
    print(f"   Target group IDs: {', '.join(map(str, TARGET_GROUP_IDS))}")
    print(f"   Direct links: Allowed only WITH DOI")
    print(f"   Language: Any English text required")
    print(f"   Bot status: {'ACTIVE' if bot_active else 'INACTIVE'}")
    print(f"   Admin commands: /start, /stop")
    print("="*70)
    print()
    
    app.run_polling()

if __name__ == "__main__":
    main()
