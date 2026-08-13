import asyncio
import os
import sys
import time
import threading
import subprocess
import urllib3
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# تعطيل تحذيرات SSL لضمان الاتصال السلس
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERSION = "v9.7 Stealth"

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ خطأ: يرجى إدخال API_ID و API_HASH و BOT_TOKEN في إعدادات Render!")
    sys.exit(1)

# --- سيرفر إبقاء الخدمة خضراء في Render ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active")
    def log_message(self, format, *args): pass

def run_health_server():
    try:
        httpd = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        httpd.serve_forever()
    except Exception: pass

threading.Thread(target=run_health_server, daemon=True).start()

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

def format_bytes(size):
    if not size: return "0 MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

def make_progress_bar(percentage, length=12):
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return bar

async def safe_edit(msg, text):
    try:
        await msg.edit(text, parse_mode="markdown")
    except Exception:
        pass

# --- محرك التنزيل المتخفي المانع لخطأ 429 ---
def download_stream(url, filename, status_msg, loop):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

    session = requests.Session()
    max_retries = 3
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, headers=headers, stream=True, timeout=60, verify=False)
            
            # إذا ظهر خطأ 429 ننتظر ثم نعيد المحاولة تلقائياً
            if r.status_code == 429:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # مضاعفة زمن الانتظار
                    continue
            
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            start_time = time.time()
            last_update = time.time()

            with open(filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024): # 4MB Chunk
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        
                        if now - last_update > 4 or downloaded == total_size:
                            last_update = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            percentage = (downloaded / total_size * 100) if total_size > 0 else 0
                            bar = make_progress_bar(percentage)

                            prog = f"📥 **جاري التحميل المباشر ({VERSION})...**\n\n"
                            prog += f"📦 الملف: `{os.path.basename(filename)}`\n"
                            prog += f"📊 `[{bar}]` `{percentage:.1f}%`\n"
                            prog += f"🚀 السرعة: `{format_bytes(speed)}/s`\n"
                            prog += f"⚡ التنزيل: `{format_bytes(downloaded)}`"
                            if total_size > 0:
                                prog += f" / `{format_bytes(total_size)}`"
                                
                            asyncio.run_coroutine_threadsafe(safe_edit(status_msg, prog), loop)
            return True

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429 and attempt < max_retries:
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise e
    return False

# --- إدارة الرفع إلى تليجرام ---
class StealthUploader:
    def __init__(self, filename, status_msg, loop):
        self.filename = filename
        self.status_msg = status_msg
        self.loop = loop
        self.start_time = time.time()
        self.last_update = time.time()

    def callback(self, current, total):
        now = time.time()
        if now - self.last_update > 4.5 or current == total:
            self.last_update = now
            elapsed = now - self.start_time
            speed = current / elapsed if elapsed > 0 else 0
            percentage = (current / total) * 100 if total > 0 else 0
            bar = make_progress_bar(percentage)

            remaining_bytes = total - current
            eta = remaining_bytes / speed if speed > 0 else 0
            eta_str = f"{int(eta)} ثانية" if eta < 60 else f"{int(eta // 60)} دقيقة"

            text = f"📤 **جاري الرفع إلى تليجرام ({VERSION})...**\n\n"
            text += f"📦 **الملف:** `{os.path.basename(self.filename)}`\n"
            text += f"📊 `[{bar}]` **{percentage:.1f}%**\n"
            text += f"🚀 **السرعة:** `{format_bytes(speed)}/s`\n"
            text += f"📦 **المرفوع:** `{format_bytes(current)}` / `{format_bytes(total)}`\n"
            text += f"⏱ **الوقت المتبقي:** `{eta_str}`"

            asyncio.run_coroutine_threadsafe(safe_edit(self.status_msg, text), self.loop)

def get_video_meta(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip().split("\n")
        return int(out[0]), int(out[1]), int(float(out[2]))
    except Exception: return 1280, 720, 0

def make_thumb(path):
    thumb = f"{path}_thumb.jpg"
    try:
        cmd = ["ffmpeg", "-y", "-ss", "00:00:02", "-i", path, "-vframes", "1", "-vf", "scale=320:-1", thumb]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumb if os.path.exists(thumb) else None
    except Exception: return None

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(f"🚀 **بوت التحميل المحسن {VERSION} جاهز!**\nأرسل رابط أي ملف للتحميل الفوري.")

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handle_url(event):
    url = event.text.strip()
    status_msg = await event.respond("⏳ **جاري الاتصال بالسيرفر ومعالجة الرابط...**")
    loop = asyncio.get_event_loop()

    clean_url = url.split("?")[0].split("#")[0]
    filename = os.path.basename(clean_url)
    if not filename or not os.path.splitext(filename)[1]:
        filename = f"video_{int(time.time())}.mp4"

    try:
        # 1. التنزيل مع تخفي وتخطي 429
        await loop.run_in_executor(None, download_stream, url, filename, status_msg, loop)

        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            file_size = os.path.getsize(filename)
            await safe_edit(status_msg, f"📤 **جاري بدء عملية الرفع...**\n📦 الحجم النهائي: `{format_bytes(file_size)}`")

            is_video = filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
            thumb = None
            attributes = None

            if is_video:
                w, h, dur = get_video_meta(filename)
                thumb = make_thumb(filename)
                attributes = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)]

            # 2. إنشاء العداد
            uploader = StealthUploader(filename, status_msg, loop)

            # 3. الرفع إلى تليجرام
            await bot.send_file(
                event.chat_id,
                filename,
                caption=f"✅ **تم التحميل والرفع بنجاح!**\n📄 **اسم الملف:** `{filename}`\n📦 **الحجم:** `{format_bytes(file_size)}`",
                thumb=thumb,
                attributes=attributes,
                supports_streaming=is_video,
                progress_callback=uploader.callback,
                reply_to=event.id
            )
            await status_msg.delete()
        else:
            await safe_edit(status_msg, "❌ فشل التنزيل: الملف فارغ.")

    except Exception as e:
        await safe_edit(status_msg, f"❌ حدث خطأ أثناء العملية:\n`{str(e)}`")
    finally:
        if os.path.exists(filename): os.remove(filename)
        if 'thumb' in locals() and thumb and os.path.exists(thumb): os.remove(thumb)

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    print(f"✅ Bot {VERSION} connected!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
