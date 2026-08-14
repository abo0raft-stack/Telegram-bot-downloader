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

VERSION = "v10.6 Debugged-Screens"

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ خطأ: يرجى إدخال البيانات في المتغيرات!")
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

CANCELLED_TASKS = {}

def format_bytes(size):
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

async def safe_edit(msg, text, buttons=None):
    try: await msg.edit(text, parse_mode="markdown", buttons=buttons)
    except: pass

def download_stream(url, filename, status_msg, loop, task_id):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"}
    session = requests.Session()
    try:
        r = session.get(url, headers=headers, stream=True, timeout=60, verify=False)
        r.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if CANCELLED_TASKS.get(task_id): raise Exception("CANCELLED_BY_USER")
                if chunk: f.write(chunk)
        return True
    except Exception as e: raise e

def generate_9_screenshots(path, duration):
    screenshots = []
    if duration < 10: return screenshots
    # أخذ 9 لقطات (أرقام صحيحة لضمان نجاح ffmpeg)
    intervals = [int(duration * i / 10) for i in range(1, 10)]
    for i, sec in enumerate(intervals):
        out_jpg = f"ss_{i}.jpg"
        try:
            cmd = ["ffmpeg", "-y", "-ss", str(sec), "-i", path, "-vframes", "1", "-vf", "scale=640:-1", out_jpg]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_jpg): screenshots.append(out_jpg)
        except: pass
    return screenshots

@bot.on(events.CallbackQuery(pattern=r"^cancel_"))
async def cancel_handler(event):
    task_id = event.data.decode().split("_")[1]
    CANCELLED_TASKS[task_id] = True
    await event.answer("🚫 جاري إلغاء العملية...", alert=True)

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handle_url(event):
    url = event.text.strip()
    task_id = str(event.id)
    CANCELLED_TASKS[task_id] = False

    status_msg = await event.respond("⏳ **جاري الاتصال بالسيرفر...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")])
    loop = asyncio.get_event_loop()
    filename = f"video_{int(time.time())}.mp4"
    screenshot_list = []

    try:
        await loop.run_in_executor(None, download_stream, url, filename, status_msg, loop, task_id)
        
        if os.path.exists(filename):
            await safe_edit(status_msg, "⚙️ **جاري استخراج اللقطات...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")])
            
            # استخراج اللقطات
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filename]
            dur = int(float(subprocess.check_output(cmd).decode()))
            screenshot_list = await loop.run_in_executor(None, generate_9_screenshots, filename, dur)

            # إرسال الصور كألبوم
            if screenshot_list:
                try:
                    await bot.send_file(event.chat_id, file=screenshot_list, as_album=True, caption="🖼 **لقطات الشاشة المستخرجة:**", reply_to=event.id)
                except Exception as e:
                    print(f"Error sending screenshots: {e}")

            # الرفع
            await bot.send_file(event.chat_id, filename, caption=f"✅ **تم التحميل!**", reply_to=event.id)
            await status_msg.delete()

    except Exception as e:
        await safe_edit(status_msg, f"❌ خطأ:\n`{str(e)}`")
    finally:
        if os.path.exists(filename): os.remove(filename)
        for ss in screenshot_list:
            if os.path.exists(ss): os.remove(ss)

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
