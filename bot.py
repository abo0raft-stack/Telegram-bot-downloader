import asyncio
import os
import sys
import time
import threading
import subprocess
import urllib3
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# تعطيل تحذيرات SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERSION = "v10.1 Thumbnail Fix"

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ خطأ: يرجى إدخال البيانات في إعدادات المنصة!")
    sys.exit(1)

# --- سيرفر إبقاء الخدمة خضراء ---
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

# تتبع العمليات
CANCELLED_TASKS = {}

def format_bytes(size):
    if not size: return "0 MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

def make_progress_bar(percentage, length=12):
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return bar

async def safe_edit(msg, text, buttons=None):
    try:
        await msg.edit(text, parse_mode="markdown", buttons=buttons)
    except Exception: pass

# --- محرك التنزيل ---
def download_stream(url, filename, status_msg, loop, task_id):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"}
    session = requests.Session()
    
    try:
        r = session.get(url, headers=headers, stream=True, timeout=60, verify=False)
        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()
        last_update = time.time()

        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if CANCELLED_TASKS.get(task_id): raise Exception("CANCELLED_BY_USER")
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_update > 4 or downloaded == total_size:
                        last_update = now
                        percentage = (downloaded / total_size * 100) if total_size > 0 else 0
                        prog = f"📥 **جاري التحميل...**\n📦 `{os.path.basename(filename)}`\n📊 `[{make_progress_bar(percentage)}]` `{percentage:.1f}%`"
                        asyncio.run_coroutine_threadsafe(safe_edit(status_msg, prog, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")]), loop)
        return True
    except Exception as e:
        raise e

# --- إدارة الرفع ---
class StealthUploader:
    def __init__(self, filename, status_msg, loop, task_id):
        self.filename, self.status_msg, self.loop, self.task_id = filename, status_msg, loop, task_id
        self.start_time = time.time()
        self.last_update = time.time()

    def callback(self, current, total):
        if CANCELLED_TASKS.get(self.task_id): raise Exception("CANCELLED_BY_USER")
        now = time.time()
        if now - self.last_update > 4.5 or current == total:
            self.last_update = now
            percentage = (current / total) * 100 if total > 0 else 0
            text = f"📤 **جاري الرفع إلى تليجرام...**\n📦 `{os.path.basename(self.filename)}`\n📊 `[{make_progress_bar(percentage)}]` **{percentage:.1f}%**"
            asyncio.run_coroutine_threadsafe(safe_edit(self.status_msg, text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{self.task_id}")]), self.loop)

def get_video_meta(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip().split("\n")
        return int(out[0]), int(out[1]), int(float(out[2]))
    except: return 1280, 720, 0

def make_thumb(path):
    thumb = f"{path}_thumb.jpg"
    try:
        # أخذ لقطة من الثانية 5 لضمان صورة واضحة
        cmd = ["ffmpeg", "-y", "-ss", "00:00:05", "-i", path, "-vframes", "1", "-vf", "scale=320:-1", thumb]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumb if os.path.exists(thumb) else None
    except: return None

@bot.on(events.CallbackQuery(pattern=r"^cancel_"))
async def cancel_handler(event):
    task_id = event.data.decode().split("_")[1]
    CANCELLED_TASKS[task_id] = True
    await event.answer("🚫 جاري الإلغاء...", alert=True)

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handle_url(event):
    url = event.text.strip()
    task_id = str(event.id)
    CANCELLED_TASKS[task_id] = False
    status_msg = await event.respond("⏳ **جاري البدء...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")])
    loop = asyncio.get_event_loop()
    filename = f"video_{int(time.time())}.mp4"
    thumb = None

    try:
        await loop.run_in_executor(None, download_stream, url, filename, status_msg, loop, task_id)
        if os.path.exists(filename):
            w, h, dur = get_video_meta(filename)
            thumb = make_thumb(filename)
            
            await bot.send_file(
                event.chat_id,
                filename,
                caption=f"✅ **تم الرفع بنجاح!**\n📄 `{os.path.basename(filename)}`",
                thumb=thumb,
                attributes=[DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)],
                progress_callback=StealthUploader(filename, status_msg, loop, task_id).callback,
                reply_to=event.id
            )
            await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ خطأ: `{str(e)}`")
    finally:
        if os.path.exists(filename): os.remove(filename)
        if thumb and os.path.exists(thumb): os.remove(thumb)
        if task_id in CANCELLED_TASKS: del CANCELLED_TASKS[task_id]

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
