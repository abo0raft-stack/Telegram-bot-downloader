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

VERSION = "v11.0 Screenshots-Fixed"

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ خطأ: يرجى إدخال البيانات في المتغيرات البيئية!")
    sys.exit(1)

# --- سيرفر إبقاء الخدمة نشطة ---
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
    if not size: return "0 MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

def make_progress_bar(percentage, length=12):
    filled_length = int(length * percentage // 100)
    return '█' * filled_length + '░' * (length - filled_length)

async def safe_edit(msg, text, buttons=None):
    try: await msg.edit(text, parse_mode="markdown", buttons=buttons)
    except Exception: pass

# --- محرك التنزيل ---
def download_stream(url, filename, status_msg, loop, task_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }

    session = requests.Session()
    r = session.get(url, headers=headers, stream=True, timeout=60, verify=False)
    r.raise_for_status()
    total_size = int(r.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update = time.time()

    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

    with open(filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            if CANCELLED_TASKS.get(task_id):
                raise Exception("CANCELLED_BY_USER")
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
                    prog += f"⚡ التنزيل: `{format_bytes(downloaded)}` / `{format_bytes(total_size)}`"
                    asyncio.run_coroutine_threadsafe(safe_edit(status_msg, prog, buttons=cancel_btn), loop)
    return True

# --- معرفة مدة الفيديو ---
def get_video_duration(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
        return float(output)
    except Exception as e:
        print(f"⚠️ FFprobe Error: {e}")
        return 0

# --- استخراج 9 لقطات موثوقة ---
def extract_9_screenshots(video_path, duration):
    screenshots = []
    if duration < 5:
        print("⚠️ الفيديو قصير جداً لاستخراج 9 لقطات")
        return screenshots

    step = duration / 10.0
    for i in range(1, 10):
        seek_time = i * step
        out_jpg = f"ss_frame_{i}.jpg"
        
        # أمر ffmpeg محسن لالتقاط الإطار بدقة
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(round(seek_time, 2)),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            out_jpg
        ]
        
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0:
                screenshots.append(out_jpg)
            else:
                print(f"⚠️ فشل استخراج اللقطة {i}: {res.stderr.decode()[:100]}")
        except Exception as e:
            print(f"❌ خطأ أثناء تشغيل FFmpeg للقطة {i}: {e}")

    return screenshots

# --- إدارة الرفع ---
class StealthUploader:
    def __init__(self, filename, status_msg, loop, task_id):
        self.filename = filename
        self.status_msg = status_msg
        self.loop = loop
        self.task_id = task_id
        self.start_time = time.time()
        self.last_update = time.time()

    def callback(self, current, total):
        if CANCELLED_TASKS.get(self.task_id):
            raise Exception("CANCELLED_BY_USER")

        now = time.time()
        if now - self.last_update > 4.5 or current == total:
            self.last_update = now
            elapsed = now - self.start_time
            speed = current / elapsed if elapsed > 0 else 0
            percentage = (current / total) * 100 if total > 0 else 0
            bar = make_progress_bar(percentage)

            cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{self.task_id}")]

            text = f"📤 **جاري الرفع إلى تليجرام ({VERSION})...**\n\n"
            text += f"📦 **الملف:** `{os.path.basename(self.filename)}`\n"
            text += f"📊 `[{bar}]` **{percentage:.1f}%**\n"
            text += f"🚀 **السرعة:** `{format_bytes(speed)}/s`\n"
            text += f"📦 **المرفوع:** `{format_bytes(current)}` / `{format_bytes(total)}`"

            asyncio.run_coroutine_threadsafe(safe_edit(self.status_msg, text, buttons=cancel_btn), self.loop)

# --- الأحداث ---
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(f"🚀 **بوت التحميل المحسن {VERSION} جاهز!**")

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

    status_msg = await event.respond(
        "⏳ **جاري الاتصال بالسيرفر ومعالجة الرابط...**",
        buttons=[Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]
    )
    loop = asyncio.get_event_loop()

    clean_url = url.split("?")[0].split("#")[0]
    filename = os.path.basename(clean_url)
    if not filename or not os.path.splitext(filename)[1]:
        filename = f"video_{int(time.time())}.mp4"

    screenshot_list = []

    try:
        # 1. التنزيل
        await loop.run_in_executor(None, download_stream, url, filename, status_msg, loop, task_id)

        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            await safe_edit(status_msg, "⚙️ **جاري التقاط 9 لقطات شاشة معتمدة...**", buttons=[Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")])

            # 2. قياس الفيديو واستخراج 9 صور
            dur = await loop.run_in_executor(None, get_video_duration, filename)
            if dur > 0:
                screenshot_list = await loop.run_in_executor(None, extract_9_screenshots, filename, dur)

            # 3. إرسال اللقطات في حال نجاح الاستخراج
            if screenshot_list and len(screenshot_list) > 0:
                try:
                    await safe_edit(status_msg, "🖼 **جاري إرسال ألبوم اللقطات...**", buttons=[Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")])
                    await bot.send_file(
                        event.chat_id,
                        file=screenshot_list,
                        caption=f"🖼 **لقطات الشاشة المستخرجة (9 Screenshots):**\n📄 `{filename}`",
                        reply_to=event.id
                    )
                except Exception as send_err:
                    print(f"❌ خطأ أثناء إرسال ألبوم الصور: {send_err}")

            if CANCELLED_TASKS.get(task_id):
                raise Exception("CANCELLED_BY_USER")

            # 4. الرفع الفعلي
            uploader = StealthUploader(filename, status_msg, loop, task_id)
            await bot.send_file(
                event.chat_id,
                filename,
                caption=f"✅ **تم التحميل والرفع بنجاح!**\n📄 **اسم الملف:** `{filename}`\n📦 **الحجم:** `{format_bytes(os.path.getsize(filename))}`",
                progress_callback=uploader.callback,
                reply_to=event.id
            )
            await status_msg.delete()

    except Exception as e:
        if str(e) == "CANCELLED_BY_USER" or CANCELLED_TASKS.get(task_id):
            await safe_edit(status_msg, "🛑 **تم إلغاء العملية بنجاح.**")
        else:
            await safe_edit(status_msg, f"❌ حدث خطأ أثناء العملية:\n`{str(e)}`")
    finally:
        # تنظيف الملفات
        if task_id in CANCELLED_TASKS: del CANCELLED_TASKS[task_id]
        if os.path.exists(filename): os.remove(filename)
        for img in screenshot_list:
            if os.path.exists(img): os.remove(img)

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    print(f"✅ Bot {VERSION} Connected!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
