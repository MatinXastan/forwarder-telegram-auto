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
from typing import List, Optional, Dict

# --- ۱. خواندن تنظیمات از متغیرهای محیطی ---
# دریافت متغیرها با مقادیر پیش‌فرض برای جلوگیری از خطا
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
TELETHON_SESSION = os.environ.get('TELETHON_SESSION')
STATE_REPO_PATH = 'state-repo'
STATE_FILE_PATH = os.path.join(STATE_REPO_PATH, "forwarder_state.json")
HOURS_OF_INACTIVITY = int(os.environ.get('HOURS_OF_INACTIVITY', 4))

# بررسی وجود متغیرهای اصلی
if not all([API_ID, API_HASH, TELETHON_SESSION]):
    logging.critical("متغیرهای API_ID, API_HASH, or TELETHON_SESSION تنظیم نشده‌اند. برنامه خاتمه می‌یابد.")
    sys.exit(1)

API_ID = int(API_ID)

# تنظیمات لاگ‌گیری برای نمایش بهتر اطلاعات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ۲. توابع کمکی برای مدیریت فایل و متن ---

def read_json_file(file_path: str, default_content: Dict = None) -> Dict:
    """یک فایل JSON را می‌خواند. اگر وجود نداشته باشد یا خالی باشد، محتوای پیش‌فرض را ایجاد می‌کند."""
    if default_content is None:
        default_content = {"last_processed_index": -1, "last_sent_ids": {}}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = json.load(f)
            # اطمینان از وجود کلیدهای لازم در فایل state
            if "last_processed_index" not in content or "last_sent_ids" not in content:
                return default_content
            return content
    except (FileNotFoundError, json.JSONDecodeError):
        logging.info(f"فایل وضعیت در {file_path} یافت نشد. یک فایل جدید ایجاد می‌شود.")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        write_json_file(file_path, default_content)
        return default_content

def write_json_file(file_path: str, data: Dict):
    """داده را در یک فایل JSON می‌نویسد."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def is_caption_valid(text: Optional[str], source_channel_username: str) -> bool:
    """بررسی می‌کند که آیا متن حاوی لینک یا منشن به کانال‌های دیگر است یا خیر."""
    if not text:
        return True  # متن خالی همیشه معتبر است

    source_username_clean = source_channel_username.lstrip('@')
    # الگوی بهبودیافته برای شناسایی لینک‌ها
    url_pattern = r'https?://(?!t\.me/' + re.escape(source_username_clean) + r')\S+|www\.\S+'
    if re.search(url_pattern, text, re.IGNORECASE):
        logging.warning(f"متن حاوی لینک خارجی بود. رد می‌شود.")
        return False

    # الگوی بهبودیافته برای شناسایی منشن‌ها
    mention_pattern = r'@(\w+)'
    mentions = re.findall(mention_pattern, text)
    for mention in mentions:
        if mention.lower() != source_username_clean.lower():
            logging.warning(f"متن حاوی منشن خارجی @{mention} بود. رد می‌شود.")
            return False
    return True

def clean_caption(text: Optional[str], source_channel_username: str) -> str:
    """آیدی کانال مبدأ را از متن پاک می‌کند."""
    if not text:
        return ""
    source_username_clean = source_channel_username.lstrip('@')
    # حذف تمام لینک‌های تلگرامی و منشن‌های کانال مبدأ
    text = re.sub(r'https?://t\.me/' + re.escape(source_username_clean) + r'/\d+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'@' + re.escape(source_username_clean), '', text, flags=re.IGNORECASE)
    return text.strip()

async def get_reply_quote(client: TelegramClient, message: Message, source_channel: str) -> str:
    """در صورت ریپلای بودن، متن پیام اصلی را به صورت نقل‌قول برمی‌گرداند."""
    if not message.reply_to_msg_id:
        return ""
    try:
        replied_to_msg = await client.get_messages(source_channel, ids=message.reply_to_msg_id)
        if replied_to_msg and replied_to_msg.text:
            snippet = replied_to_msg.text.split('\n')[0]
            if len(snippet) > 70:
                snippet = snippet[:70] + "..."
            return f"> {snippet}\n\n"
    except Exception as e:
        logging.warning(f"امکان دریافت پیام ریپلای شده وجود نداشت: {e}")
    return ""

# --- ۳. منطق اصلی برنامه ---

async def main():
    """منطق اصلی ربات برای بررسی و ارسال پیام."""
    state_data = read_json_file(STATE_FILE_PATH)

    source_channels = [ch.strip() for ch in os.environ.get('SOURCE_CHANNELS_LIST', '').split(',') if ch.strip()]
    dest_channels = [ch.strip() for ch in os.environ.get('DESTINATION_CHANNELS_LIST', '').split(',') if ch.strip()]

    if not source_channels or len(source_channels) != len(dest_channels):
        logging.error("لیست کانال‌های مبدأ یا مقصد به درستی تنظیم نشده‌اند. تعداد باید برابر باشد.")
        sys.exit(1)

    # انتخاب جفت کانال بعدی برای پردازش
    num_channels = len(source_channels)
    current_index = state_data.get('last_processed_index', -1)
    next_index = (current_index + 1) % num_channels

    source_channel = source_channels[next_index]
    destination_channel = dest_channels[next_index]
    logging.info(f"شروع پردازش: {source_channel} -> {destination_channel}")

    client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
    
    try:
        await client.start()
        logging.info("کلاینت تلگرام با موفقیت متصل شد.")

        # به طور پیش‌فرض فرض می‌کنیم که باید پستی ارسال شود
        should_forward = True

        # ۱. بررسی فعالیت کانال مقصد
        last_message_list = await client.get_messages(destination_channel, limit=1)
        if last_message_list:
            last_message = last_message_list[0]
            last_post_time = last_message.date
            inactive_threshold = datetime.now(timezone.utc) - timedelta(hours=HOURS_OF_INACTIVITY)
            if last_post_time > inactive_threshold:
                logging.info(f"کانال {destination_channel} به تازگی فعال بوده است. نیازی به ارسال پست نیست.")
                should_forward = False
        
        if should_forward:
            logging.info(f"در {HOURS_OF_INACTIVITY} ساعت گذشته فعالیتی در {destination_channel} نبوده. در حال یافتن پست جدید از {source_channel}...")

            # ۲. یافتن آخرین پست معتبر و ارسال نشده از کانال مبدأ
            last_sent_id = state_data.get("last_sent_ids", {}).get(source_channel, 0)
            logging.info(f"آخرین آیدی ارسال شده برای {source_channel} برابر است با: {last_sent_id}. در حال جستجوی پیام‌های جدیدتر...")
            
            messages: List[Message] = await client.get_messages(source_channel, limit=50, min_id=last_sent_id)
            
            post_to_forward = None
            # از بین پیام‌های جدید، جدیدترین پیام معتبر را پیدا می‌کنیم
            for message in messages:
                if not message or message.id <= last_sent_id:
                    continue

                caption_text = message.text if message.text else ""
                if is_caption_valid(caption_text, source_channel):
                    post_to_forward = message
                    break
            
            if post_to_forward:
                # ۳. ارسال پست یافت‌شده
                logging.info(f"پست معتبر با آیدی {post_to_forward.id} از {source_channel} یافت شد. در حال ارسال...")
                
                cleaned_caption = clean_caption(post_to_forward.text, source_channel)
                reply_quote = await get_reply_quote(client, post_to_forward, source_channel)
                final_text = f"{reply_quote}{cleaned_caption}\n\n{destination_channel}".strip()

                # مدیریت آلبوم‌ها
                if post_to_forward.grouped_id:
                    album_messages_iter = client.iter_messages(source_channel, limit=20)
                    album_messages = [m async for m in album_messages_iter if m.grouped_id == post_to_forward.grouped_id]
                    album_media = [m.media for m in sorted(album_messages, key=lambda m: m.id) if m.media]
                    await client.send_file(destination_channel, album_media, caption=final_text)
                    logging.info(f"آلبوم {post_to_forward.grouped_id} با موفقیت به {destination_channel} ارسال شد.")
                else: # پست‌های تکی
                    await client.send_message(destination_channel, final_text, file=post_to_forward.media)
                    logging.info(f"پست {post_to_forward.id} با موفقیت به {destination_channel} ارسال شد.")
                
                # ۴. به‌روزرسانی state با آیدی پست ارسال‌شده
                state_data["last_sent_ids"][source_channel] = post_to_forward.id
            else:
                logging.warning(f"هیچ پست معتبر و جدیدی در {source_channel} برای ارسال یافت نشد.")

    except Exception as e:
        logging.error(f"یک خطای غیرمنتظره رخ داد: {e}", exc_info=True)
    finally:
        # ۵. به‌روزرسانی ایندکس و ذخیره state در هر صورت
        logging.info("فرایند برای این اجرا به پایان رسید.")
        if client.is_connected():
            await client.disconnect()
            logging.info("کلاینت تلگرام قطع شد.")
        
        state_data['last_processed_index'] = next_index
        write_json_file(STATE_FILE_PATH, state_data)
        logging.info(f"فایل وضعیت به‌روز شد. ایندکس بعدی: {(next_index + 1) % num_channels}")

if __name__ == "__main__":
    asyncio.run(main())

