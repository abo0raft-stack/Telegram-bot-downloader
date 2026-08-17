import asyncio
import os
import shutil
import threading
import time
import gc
import re
import base64
import requests
import yt_dlp
import subprocess
import json
import mimetypes
from PIL import Image
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# --- التحديث التلقائي للمكتبات ---
def update_libraries():
    try:
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ تم تحديث مكتبات التنزيل ومعالجة الصور بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_libraries()

# --- الإعدادات ---
VERSION = "v72.0-Final-X-SmartFetch-Cookies-Fix"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

# --- التعامل مع ملفات الكوكيز تلقائياً ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
            print("✅ تم تجهيز ملف كوكيز X بنجاح.")
        except Exception as e:
            print(f"⚠️ خطأ في قراءة X_COOKIES_BASE64: {e}")

    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
        except Exception: pass

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive'
}

def clean_download_folder():
    folder = "downloads"
    if os.path.exists(folder):
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception: pass
    else:
        os.makedirs(folder, exist_ok=True)
    gc.collect()

clean_download_folder()

# --- Health Check للسيرفر ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

bot = TelegramClient('bot_session', API_ID, API_HASH)

def clean_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

def is_instagram_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['instagram.com', 'instagr.am'])

def is_tiktok_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['tiktok.com', 'vt.tiktok.com'])

def get_video_metadata_and_thumb(file_path):
    duration, width, height = 0, 1280, 720
    thumb_path = f"{file_path}_thumb.jpg"
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration:stream=width,height,rotation',
            '-of', 'json', file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        
        if 'format' in data and 'duration' in data['format']:
            duration = int(float(data['format']['duration']))
            
        if 'streams' in data and len(data['streams']) > 0:
            for stream in data['streams']:
                if 'width' in stream and 'height' in stream:
                    width, height = int(stream['width']), int(stream['height'])
                    for side in stream.get('side_data_list', []):
                        if abs(side.get('rotation', 0)) in [90, 270]:
                            width, height = height, width
                    break

        subprocess.run(['ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path, '-vframes', '1', '-q:v', '2', thumb_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            thumb_path = None
    except Exception:
        thumb_path = None
    return duration, width, height, thumb_path

def deep_sanitize_image(file_path):
    try:
        base_dir = os.path.dirname(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        cleaned_path = os.path.join(base_dir, f"{base_name}_clean.jpg")

        with Image.open(file_path) as img:
            rgb_img = img.convert('RGB')
            w, h = rgb_img.size
            if w % 2 != 0: w -= 1
            if h % 2 != 0: h -= 1
            if w > 0 and h > 0:
                rgb_img = rgb_img.resize((w, h), Image.Resampling.LANCZOS)
            rgb_img.save(cleaned_path, 'JPEG', quality=95)

        ffmpeg_path = os.path.join(base_dir, f"{base_name}_final.jpg")
        cmd = ['ffmpeg', '-y', '-i', cleaned_path, '-map_metadata', '-1', '-vf', 'format=yuv420p', '-q:v', '2', ffmpeg_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

        target_file = ffmpeg_path if (os.path.exists(ffmpeg_path) and os.path.getsize(ffmpeg_path) > 100) else cleaned_path
        if os.path.exists(file_path) and file_path != target_file:
            try: os.remove(file_path)
            except: pass
        if os.path.exists(cleaned_path) and cleaned_path != target_file:
            try: os.remove(cleaned_path)
            except: pass
        return target_file
    except Exception as e:
        print(f"Image sanitize error: {e}")
        return file_path

# --- المحرك الذكي المزدوج لتنفيذ التحميل لمنصة X ---
def fetch_x_media_sync(url, task_dir):
    """تحميل الوسائط من X بالاعتماد الشامل على الكوكيز"""
    # 1. التجربة عبر gallery-dl أولاً (ممتاز للصور المتعددة والمنشورات المركبة)
    try:
        cmd = ["gallery-dl", "--directory", task_dir, "--filename", "x_{id}_{num}.{ext}"]
        if os.path.exists(X_COOKIES_FILE):
            cmd.extend(["--cookies", X_COOKIES_FILE])
        cmd.append(url)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
    except Exception as e:
        print(f"gallery-dl X error: {e}")

    # 2. إذا لم يتم جلب ملفات أو كان هناك فيديو لم يستخرجه gallery-dl، نستخدم yt-dlp
    downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
    if not downloaded:
        try:
            ydl_opts = {
                'outtmpl': os.path.join(task_dir, 'x_media_%(id)s_%(autonumber)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'format': 'best',
                'headers': BROWSER_HEADERS,
                'ignoreerrors': True,
            }
            if os.path.exists(X_COOKIES_FILE):
                ydl_opts['cookiefile'] = X_COOKIES_FILE
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            print(f"yt-dlp X error: {e}")

async def start_direct_execution(chat_id, url, status_msg=None):
    if not status_msg:
        status_msg = await bot.send_message(chat_id, "🔄 **جاري جلب جميع الوسائط (فيديو + صور) بأعلى جودة...**")
    else:
        await status_msg.edit("🔄 **جاري جلب جميع الوسائط (فيديو + صور) بأعلى جودة...**")
        
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        loop = asyncio.get_event_loop()
        clean_target_url = clean_url(url)
        
        # تنفذ عملية التحميل دون تجميد الأوامر عبر Thread Executor
        await loop.run_in_executor(None, fetch_x_media_sync, clean_target_url, task_dir)

        downloaded_files = []
        for root, _, files in os.walk(task_dir):
            for file in files:
                fpath = os.path.join(root, file)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 500 and not file.endswith('_thumb.jpg') and not file.endswith('_clean.jpg') and not file.endswith('_final.jpg'):
                    ext = os.path.splitext(file)[1].lower()
                    if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                        fpath = await loop.run_in_executor(None, deep_sanitize_image, fpath)
                    downloaded_files.append(fpath)

        if not downloaded_files:
            raise Exception("تعذر الوصول إلى الصور أو الفيديوهات. تأكد أن المنشور في حساب عام أو قم بتحديث متغيرة X_COOKIES_BASE64.")

        await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر)...**")

        photos, videos, other_files = [], [], []
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm')
        image_extensions = ('.jpg', '.jpeg', '.png')

        for fpath in sorted(downloaded_files):
            ext = os.path.splitext(fpath)[1].lower()
            if ext in image_extensions:
                photos.append(fpath)
            elif ext in video_extensions:
                videos.append(fpath)
            else:
                other_files.append(fpath)

        # 1. إرسال الفيديوهات
        for vid in videos:
            duration, width, height, thumb_path = get_video_metadata_and_thumb(vid)
            attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
            await bot.send_file(
                chat_id, vid,
                caption=f"🎥 **تم تنزيل الفيديو بنجاح!**",
                thumb=thumb_path,
                attributes=attr
            )
            if thumb_path and os.path.exists(thumb_path):
                try: os.remove(thumb_path)
                except: pass

        # 2. إرسال الصور (ألبوم أو فردي تلقائياً)
        if photos:
            sent_as_album = False
            if len(photos) > 1:
                try:
                    uploaded_handles = []
                    for p in photos[:10]:
                        uploaded_file = await bot.upload_file(p)
                        uploaded_handles.append(uploaded_file)
                    await bot.send_file(chat_id, uploaded_handles, caption=f"📸 **تم تنزيل ألبوم الصور ({len(photos)} صورة):**")
                    sent_as_album = True
                except Exception as album_err:
                    print(f"Album upload fallback: {album_err}")

            if not sent_as_album:
                for idx, p in enumerate(photos, start=1):
                    uploaded_single = await bot.upload_file(p)
                    cap = f"📸 **صورة ({idx}/{len(photos)}):**" if len(photos) > 1 else "📸 **تم تنزيل الصورة بنجاح!**"
                    await bot.send_file(chat_id, uploaded_single, caption=cap)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(task_dir):
            try: shutil.rmtree(task_dir)
            except: pass

# --- المعالج الرئيسي ---
@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    for u in urls:
        asyncio.create_task(start_direct_execution(chat_id, u))

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بأعلى كفاءة بوضع (Smart Fetch) المدعوم بالكوكيز لمنافسة منصة X!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
