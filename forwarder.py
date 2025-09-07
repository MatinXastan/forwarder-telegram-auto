import os
import asyncio
import json
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
import logging
from datetime import datetime, timedelta, timezone
import sys

# --- ۱. خواندن تنظیمات از متغیرهای محیطی ---
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
TELETHON_SESSION = os.environ.get('TELETHON_SESSION')
STATE_REPO_PATH = 'state-repo'
STATE_FILE_PATH = os.path.join(STATE_REPO_PATH, "forwarder_state.json")
HOURS_OF_INACTIVITY = 4 # ساعت عدم فعالیت برای ارسال پست

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

async def clean_and_validate_message(message, source_channel_username):
    """یک پیام را برای ارسال بررسی و پاک‌سازی می‌کند."""
    # --- رفع باگ ---
    # یک بررسی ایمنی اضافه شده تا از خطا در صورت None بودن متغیر جلوگیری شود
    if not source_channel_username:
        logging.error("تابع clean_and_validate_message با یک نام کاربری کانال مبدأ نامعتبر فراخوانی شد.")
        return None

    text = message.text
    if not text:
        return None  # پیام‌های بدون متن را نادیده می‌گیریم

    # ۱. نام کاربری کانال مبدأ را برای مقایسه تمیز می‌کنیم
    source_username_clean = source_channel_username.lstrip('@')

    # ۲. بررسی وجود لینک در متن
    url_pattern = r'https?://\S+|www\.\S+|t\.me/\S+'
    if re.search(url_pattern, text):
        logging.warning(f"پست {message.id} حاوی لینک بود. از آن صرف‌نظر می‌شود.")
        return None

    # ۳. بررسی منشن‌های خارجی
    mention_pattern = r'@(\w+)'
    mentions = re.findall(mention_pattern, text)
    for mention in mentions:
        if mention.lower() != source_username_clean.lower():
            logging.warning(f"پست {message.id} حاوی منشن خارجی @{mention} بود. از آن صرف‌نظر می‌شود.")
            return None

    # ۴. پاک‌سازی متن نهایی: حذف آیدی کانال مبدأ
    # از re.sub برای جایگزینی بدون توجه به حروف بزرگ و کوچک استفاده می‌کنیم
    cleaned_text = re.sub(r'@' + re.escape(source_username_clean), '', text, flags=re.IGNORECASE).strip()
    return cleaned_text

async def main():
    """منطق اصلی ربات."""
    state_data = read_json_file(STATE_FILE_PATH, default_content={"last_processed_index": -1})

    source_channels = [ch.strip() for ch in os.environ.get('SOURCE_CHANNELS_LIST', '').split(',') if ch.strip()]
    dest_channels = [ch.strip() for ch in os.environ.get('DESTINATION_CHANNELS_LIST', '').split(',') if ch.strip()]

    if not source_channels or not dest_channels or len(source_channels) != len(dest_channels):
        logging.error("لیست کانال‌های مبدأ یا مقصد به درستی تنظیم نشده‌اند.")
        sys.exit(1)

    # تعیین اینکه کدام جفت کانال باید پردازش شود
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

        # بررسی آخرین پیام در کانال مقصد
        last_message = await client.get_messages(destination_channel, limit=1)
        if last_message:
            last_post_time = last_message[0].date
            time_since_last_post = datetime.now(timezone.utc) - last_post_time
            if time_since_last_post < timedelta(hours=HOURS_OF_INACTIVITY):
                logging.info(f"کانال {destination_channel} به تازگی فعال بوده است. نیازی به ارسال پست نیست.")
                return  # خروج از برنامه

        logging.info(f"در {HOURS_OF_INACTIVITY} ساعت گذشته فعالیتی در {destination_channel} نبوده. در حال بررسی کانال مبدأ...")

        # جستجو برای یک پست معتبر در کانال مبدأ
        async for message in client.iter_messages(source_channel, limit=10):
            if not message:
                continue

            cleaned_text = await clean_and_validate_message(message, source_channel)
            if cleaned_text:
                final_text = f"{cleaned_text}\n\n{destination_channel}"
                await client.send_message(destination_channel, final_text)
                logging.info(f"پست {message.id} با موفقیت به {destination_channel} ارسال شد.")
                break  # پس از یافتن اولین پست معتبر، از حلقه خارج شو
        else:
            logging.warning(f"هیچ پست معتبری در ۱۰ پیام اخیر {source_channel} یافت نشد.")

    except Exception as e:
        logging.error(f"یک خطای غیرمنتظره رخ داد: {e}", exc_info=True)
    finally:
        if client.is_connected():
            await client.disconnect()
            logging.info("کلاینت تلگرام قطع شد.")
        # به‌روزرسانی وضعیت برای اجرای بعدی
        state_data['last_processed_index'] = next_index
        write_json_file(STATE_FILE_PATH, state_data)

if __name__ == "__main__":
    asyncio.run(main())

