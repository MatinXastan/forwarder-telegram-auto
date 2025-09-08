import os
import asyncio
import json
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message
import logging
from datetime import datetime, timedelta, timezone
import sys
from typing import List, Optional

# --- ۱. خواندن تنظیمات ---
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
TELETHON_SESSION = os.environ.get('TELETHON_SESSION')
STATE_REPO_PATH = 'state-repo'
STATE_FILE_PATH = os.path.join(STATE_REPO_PATH, "forwarder_state.json")
# اگر متغیر در گیت‌هاب تنظیم نشده باشد، از مقدار پیش‌فرض ۴ استفاده کن
HOURS_OF_INACTIVITY = int(os.environ.get('HOURS_OF_INACTIVITY', 4))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def read_json_file(file_path, default_content=None):
    """یک فایل JSON را می‌خواند یا در صورت عدم وجود، محتوای پیش‌فرض را ایجاد می‌کند."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        if default_content is not None:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            write_json_file(file_path, default_content)
            return default_content
        return None

def write_json_file(file_path, data):
    """داده را در یک فایل JSON می‌نویسد."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def is_caption_valid(text: Optional[str], source_channel_username: str) -> bool:
    """بررسی می‌کند که آیا متن حاوی لینک یا منشن خارجی است یا نه."""
    if not text:
        return True # متن خالی همیشه معتبر است

    source_username_clean = source_channel_username.lstrip('@')
    url_pattern = r'https?://\S+|www\.\S+|t\.me/\S+'
    if re.search(url_pattern, text):
        logging.warning(f"متن حاوی لینک بود. از آن صرف‌نظر می‌شود.")
        return False

    mention_pattern = r'@(\w+)'
    mentions = re.findall(mention_pattern, text)
    for mention in mentions:
        if mention.lower() != source_username_clean.lower():
            logging.warning(f"متن حاوی منشن خارجی @{mention} بود. از آن صرف‌نظر می‌شود.")
            return False
    return True

def clean_caption(text: Optional[str], source_channel_username: str) -> str:
    """آیدی کانال مبدأ را از متن پاک می‌کند."""
    if not text:
        return ""
    source_username_clean = source_channel_username.lstrip('@')
    return re.sub(r'@' + re.escape(source_username_clean), '', text, flags=re.IGNORECASE).strip()

async def get_reply_quote(client: TelegramClient, message: Message, source_channel: str) -> str:
    """در صورت ریپلای بودن، متن پیام اصلی را به صورت نقل‌قول برمی‌گرداند."""
    if not message.reply_to_msg_id:
        return ""
    
    try:
        replied_to_msg = await client.get_messages(source_channel, ids=message.reply_to_msg_id)
        if replied_to_msg and replied_to_msg.text:
            # خلاصه‌ای از پیام اصلی را به عنوان نقل‌قول آماده کن
            snippet = replied_to_msg.text.split('\n')[0]
            if len(snippet) > 70:
                snippet = snippet[:70] + "..."
            return f"> {snippet}\n\n"
    except Exception as e:
        logging.warning(f"امکان دریافت پیام ریپلای شده وجود نداشت: {e}")
    
    return ""

async def main():
    """منطق اصلی ربات."""
    state_data = read_json_file(STATE_FILE_PATH, default_content={"last_processed_index": -1})

    source_channels = [ch.strip() for ch in os.environ.get('SOURCE_CHANNELS_LIST', '').split(',') if ch.strip()]
    dest_channels = [ch.strip() for ch in os.environ.get('DESTINATION_CHANNELS_LIST', '').split(',') if ch.strip()]

    if not source_channels or not dest_channels or len(source_channels) != len(dest_channels):
        logging.error("لیست کانال‌های مبدأ یا مقصد به درستی تنظیم نشده‌اند.")
        sys.exit(1)

    num_channels = len(source_channels)
    current_index = state_data.get('last_processed_index', -1)
    next_index = (current_index + 1) % num_channels

    source_channel = source_channels[next_index]
    destination_channel = dest_channels[next_index]

    logging.info(f"پردازش جفت کانال: {source_channel} -> {destination_channel}")

    client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)

    try:
        await client.connect()
        logging.info("کلاینت تلگرام با موفقیت متصل شد.")

        last_messages = await client.get_messages(destination_channel, limit=1)
        if last_messages:
            last_post_time = last_messages[0].date
            if (datetime.now(timezone.utc) - last_post_time) < timedelta(hours=HOURS_OF_INACTIVITY):
                logging.info(f"کانال {destination_channel} به تازگی فعال بوده است. نیازی به ارسال پست نیست.")
                return

        logging.info(f"در {HOURS_OF_INACTIVITY} ساعت گذشته فعالیتی در {destination_channel} نبوده. در حال بررسی کانال مبدأ...")

        messages: List[Message] = await client.get_messages(source_channel, limit=20)
        processed_group_ids = set()
        
        for message in reversed(messages): # از پیام‌های قدیمی‌تر شروع می‌کنیم
            if not message: continue

            # --- مدیریت آلبوم (پست‌های چندرسانه‌ای) ---
            if message.grouped_id and message.grouped_id not in processed_group_ids:
                album_messages = [m for m in messages if m.grouped_id == message.grouped_id]
                album_media = [m.media for m in album_messages if m.media]
                
                # پیدا کردن کپشن در آلبوم
                caption_message = next((m for m in album_messages if m.text), None)
                caption_text = caption_message.text if caption_message else ""

                if not is_caption_valid(caption_text, source_channel):
                    processed_group_ids.add(message.grouped_id)
                    continue

                cleaned_caption = clean_caption(caption_text, source_channel)
                reply_quote = await get_reply_quote(client, caption_message or message, source_channel)
                final_text = f"{reply_quote}{cleaned_caption}\n\n{destination_channel}".strip()

                await client.send_file(destination_channel, album_media, caption=final_text)
                logging.info(f"آلبوم {message.grouped_id} با {len(album_media)} رسانه با موفقیت به {destination_channel} ارسال شد.")
                processed_group_ids.add(message.grouped_id)
                break # پس از ارسال موفق، از حلقه خارج شو
            
            # --- مدیریت پست‌های تکی ---
            elif not message.grouped_id:
                caption_text = message.text
                if not is_caption_valid(caption_text, source_channel):
                    continue

                if not message.media and (not caption_text or not caption_text.strip()):
                    continue # اگر نه رسانه بود و نه متن، رد شو

                cleaned_caption = clean_caption(caption_text, source_channel)
                reply_quote = await get_reply_quote(client, message, source_channel)
                final_text = f"{reply_quote}{cleaned_caption}\n\n{destination_channel}".strip()
                
                await client.send_message(destination_channel, final_text, file=message.media)
                logging.info(f"پست {message.id} با موفقیت به {destination_channel} ارسال شد.")
                break
        else: # اگر حلقه به طور طبیعی تمام شود (و break نشود)
            logging.warning(f"هیچ پست معتبری در ۲۰ پیام اخیر {source_channel} یافت نشد.")

    except Exception as e:
        logging.error(f"یک خطای غیرمنتظره رخ داد: {e}", exc_info=True)
    finally:
        if client.is_connected():
            await client.disconnect()
            logging.info("کلاینت تلگرام قطع شد.")
        state_data['last_processed_index'] = next_index
        write_json_file(STATE_FILE_PATH, state_data)

if __name__ == "__main__":
    asyncio.run(main())

