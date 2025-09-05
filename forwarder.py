import os
import asyncio
import re
import sys
import logging
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# --- خواندن تنظیمات از متغیرهای محیطی ---
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
TELETHON_SESSION = os.environ.get('TELETHON_SESSION')

# این متغیرها توسط GitHub Action برای هر اجرا تنظیم می‌شوند
SOURCE_CHANNEL = os.environ.get('SOURCE_CHANNEL')
DESTINATION_CHANNEL = os.environ.get('DESTINATION_CHANNEL')

# --- تنظیمات محلی ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
CHECK_INTERVAL_HOURS = 4 # فاصله زمانی برای بررسی (۴ ساعت)
MESSAGES_TO_CHECK = 5 # تعداد آخرین پیام‌ها برای بررسی در کانال مبدأ

def clean_and_validate_message(message, source_channel_username):
    """
    پست را برای لینک‌ها و منشن‌های خارجی بررسی می‌کند.
    در صورت معتبر بودن، متن پاک‌سازی شده را برمی‌گرداند.
    در غیر این صورت None را برمی‌گرداند.
    """
    if not message:
        return None

    text = message.text or ""
    
    # ۱. بررسی وجود لینک
    url_pattern = r'https?://\S+|www\.\S+|t\.me/\S+'
    if re.search(url_pattern, text):
        logging.warning(f"Skipping post {message.id} from {source_channel_username}: Contains a link.")
        return None

    # ۲. بررسی منشن‌های خارجی
    mention_pattern = r'@(\w+)'
    mentions = re.findall(mention_pattern, text)
    source_username_clean = source_channel_username.lstrip('@')
    for mention in mentions:
        if mention.lower() != source_username_clean.lower():
            logging.warning(f"Skipping post {message.id} from {source_channel_username}: Contains an external mention @{mention}.")
            return None
            
    # ۳. پاک‌سازی متن از آیدی کانال مبدأ (اگر وجود داشته باشد)
    cleaned_text = re.sub(fr'@{source_username_clean}', '', text, flags=re.IGNORECASE).strip()
    return cleaned_text


async def main():
    if not all([API_ID, API_HASH, TELETHON_SESSION, SOURCE_CHANNEL, DESTINATION_CHANNEL]):
        logging.error("One or more required environment variables are not set.")
        sys.exit(1)

    client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)

    try:
        await client.connect()
        logging.info(f"Telegram client connected. Processing: {SOURCE_CHANNEL} -> {DESTINATION_CHANNEL}")

        # ۱. بررسی کانال مقصد
        dest_entity = await client.get_entity(DESTINATION_CHANNEL)
        last_messages = await client.get_messages(dest_entity, limit=1)
        
        if last_messages:
            last_message_date = last_messages[0].date
            time_since_last_post = datetime.now(timezone.utc) - last_message_date
            if time_since_last_post < timedelta(hours=CHECK_INTERVAL_HOURS):
                logging.info(f"Last post in {DESTINATION_CHANNEL} was less than {CHECK_INTERVAL_HOURS} hours ago. No action needed.")
                return

        logging.info(f"No activity in {DESTINATION_CHANNEL} for the last {CHECK_INTERVAL_HOURS} hours. Checking source channel...")

        # ۲. پیدا کردن پست معتبر از کانال مبدأ
        source_entity = await client.get_entity(SOURCE_CHANNEL)
        source_username = getattr(source_entity, 'username', SOURCE_CHANNEL)
        
        messages = await client.get_messages(source_entity, limit=MESSAGES_TO_CHECK)

        post_to_forward = None
        cleaned_caption = ""

        for message in messages:
            cleaned_text = clean_and_validate_message(message, source_username)
            if cleaned_text is not None:
                post_to_forward = message
                cleaned_caption = cleaned_text
                logging.info(f"Found a valid post to forward: ID {message.id} from {SOURCE_CHANNEL}.")
                break
        
        if not post_to_forward:
            logging.warning(f"No valid posts found in the last {MESSAGES_TO_CHECK} messages of {SOURCE_CHANNEL}. Exiting.")
            return

        # ۳. آماده‌سازی و ارسال پست
        # آیدی کانال مقصد به صورت خودکار به انتهای پست اضافه می‌شود
        final_caption = f"{cleaned_caption}\n\n{DESTINATION_CHANNEL}".strip()

        await client.send_file(
            dest_entity,
            file=post_to_forward.media,
            caption=final_caption
        )
        logging.info(f"Successfully forwarded post {post_to_forward.id} to {DESTINATION_CHANNEL}.")

    except FloodWaitError as e:
        logging.error(f"Flood wait error: sleeping for {e.seconds} seconds.")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        if client.is_connected():
            await client.disconnect()
            logging.info("Telegram client disconnected.")

if __name__ == "__main__":
    asyncio.run(main())

