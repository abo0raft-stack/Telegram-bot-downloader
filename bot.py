import asyncio
import os
import sys
import time
import threading
import subprocess
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

VERSION = "v9.3 Stable"

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
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args): pass

def run_health_server():
    try:
        httpd = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        httpd.serve_forever()
    except Exception: pass

threading.Thread(target=run_health_server, daemon=True).start()

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

def format_bytes(size):
    if not size: return "غير معروف"
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

async def safe_edit(msg, text):
    try: await msg.edit(text, parse_mode="markdown")
    except Exception: pass

# --- محرك التحميل المباشر المضمون ---
def download_stream(url, filename, status_msg, loop):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }
    
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        last_update = 0

        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=2 * 1024 * 1024): # 2MB Chunk
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_update > 3:
                        last_update = now
                        prog = f"📥 **جاري التحميل المباشر ({VERSION})...**\n\n"
                        prog += f"📦 الملف: `{os.path.basename(filename)}`\n"
                        prog += f"🚀 تم تنزيل: `{format_bytes(downloaded)}`"
                        if total_size > 0:
                            p = (downloaded / total_size) * 100
                            prog += f" / `{format_bytes(total_size)}` (`{p:.1f}%`)"
                        asyncio.run_coroutine_threadsafe(safe_edit(status_msg, prog), loop)
                        
    return True

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
    await event.respond(f"🚀 **بوت التحميل السريع {VERSION} جاهز!**\nأرسل رابط الملف ليتم تحميله فوراً.")

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handle_url(event):
    url = event.text.strip()
    status_msg = await event.respond("⏳ **جاري الاتصال بالسيرفر والبدء بالتحميل...**")
    loop = asyncio.get_event_loop()

    # استخراج اسم الملف المباشر من الرابط
    clean_url = url.split("?")[0].split("#")[0]
    filename = os.path.basename(clean_url)
    if not filename or not re.search(r'\.[a-zA-Z0-9]+$', filename):
        filename = f"file_{int(time.time())}.mp4"

    try:
        # تشغيل التحميل في Thread منفصل لعدم تجميد البوت
        await loop.run_in_executor(None, download_stream, url, filename, status_msg, loop)

        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            file_size = os.path.getsize(filename)
            await safe_edit(status_msg, f"📤 **جاري الرفع إلى تليجرام...**\n📦 الحجم: `{format_bytes(file_size)}`")

            is_video = filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
            thumb = None
            attributes = None

            if is_video:
                w, h, dur = get_video_meta(filename)
                thumb = make_thumb(filename)
                attributes = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)]

            await bot.send_file(
                event.chat_id,
                filename,
                caption=f"✅ **تم التحميل والرفع بنجاح!**\n📄 **الملف:** `{filename}`\n📦 **الحجم:** `{format_bytes(file_size)}`",
                thumb=thumb,
                attributes=attributes,
                supports_streaming=is_video,
                reply_to=event.id
            )
            await status_msg.delete()
        else:
            await safe_edit(status_msg, "❌ فشل التنزيل: الملف الناتج فارغ.")

    except Exception as e:
        await safe_edit(status_msg, f"❌ حدث خطأ أثناء التحميل:\n`{str(e)}`")
    finally:
        if os.path.exists(filename): os.remove(filename)
        if 'thumb' in locals() and thumb and os.path.exists(thumb): os.remove(thumb)

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    print(f"✅ Bot {VERSION} connected!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
