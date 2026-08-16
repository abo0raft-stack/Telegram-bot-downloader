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
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v53.0-Direct-Download-NoQueueMsg"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}

# --- التعامل مع ملفات الكوكيز ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
            print("✅ تم تجهيز كوكيز منصة X بنجاح.")
        except Exception as e:
            print(f"❌ خطأ في كوكيز X: {e}")

    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
            print("✅ تم تجهيز كوكيز إنستغرام بنجاح.")
        except Exception as e:
            print(f"❌ خطأ في كوكيز إنستغرام: {e}")

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive'
}

def get_user_settings(user_id):
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {'streaming_mode': True}
    return USER_SETTINGS[user_id]

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

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com'])

def get_clean_filename(url):
    if is_complex_url(url): return "media_download"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'): return filename.replace('.m3u8', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv', '.jpg', '.jpeg', '.png', '.webp']): 
        return "downloaded_media"
    return filename

def get_video_metadata_and_thumb(file_path):
    duration, width, height = 0, 0, 0
    thumb_path = f"{file_path}.jpg"
    try:
        parser = createParser(file_path)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    if metadata.has('duration'): duration = int(metadata.get('duration').seconds)
                    if metadata.has('width'): width = metadata.get('width')
                    if metadata.has('height'): height = metadata.get('height')
    except Exception: pass
    subprocess.run(['ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path, '-vframes', '1', '-q:v', '2', thumb_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0: thumb_path = None
    return duration, width, height, thumb_path

def download_with_ytdlp(url, task_dir, fmt='mp4', quality='best'):
    out_template = os.path.join(task_dir, '%(title).30s_%(id)s_%(autonumber)s.%(ext)s')
    
    ydl_opts = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'headers': BROWSER_HEADERS,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'writethumbnails': False,
    }
    url_lower = url.lower()

    if any(x in url_lower for x in ['twitter.com', 'x.com']) and os.path.exists(X_COOKIES_FILE):
        ydl_opts['cookiefile'] = X_COOKIES_FILE
    elif any(x in url_lower for x in ['instagram.com', 'instagr.am']) and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE

    if 'tiktok.com' in url_lower:
        ydl_opts['format'] = 'best'
    elif fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
    elif quality != 'best':
        ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        ydl_opts['format'] = 'best'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def download_direct_file(url, filepath, cancel_event):
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=4*1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            if chunk: f.write(chunk)

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4'):
    # رسالة المعالجة المباشرة فوراً بدون رسائل تمهيدية
    status_msg = await bot.send_message(chat_id, "⏳ **جاري تنزيل المحتوى...**")
    cancel_event = threading.Event()
    
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        loop = asyncio.get_event_loop()
        
        if url and is_complex_url(url):
            await loop.run_in_executor(None, download_with_ytdlp, url, task_dir, target_fmt, quality)
        elif url:
            filepath = os.path.join(task_dir, filename)
            await loop.run_in_executor(None, download_direct_file, url, filepath, cancel_event)
        elif media_msg:
            filepath = os.path.join(task_dir, filename)
            await bot.download_media(media_msg, file=filepath)

        downloaded_files = [
            os.path.join(task_dir, f) for f in os.listdir(task_dir)
            if os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 0
        ]

        if not downloaded_files:
            raise Exception("لم يتم العثور على أي صور أو فيديوهات في هذا الرابط، أو أن الحساب خاص.")

        await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر/عناصر)...**")
        user_settings = get_user_settings(chat_id)

        photos = []
        videos = []
        other_files = []

        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm')
        image_extensions = ('.jpg', '.jpeg', '.png', '.webp')

        for fpath in downloaded_files:
            ext = os.path.splitext(fpath)[1].lower()
            if ext in image_extensions:
                photos.append(fpath)
            elif ext in video_extensions:
                videos.append(fpath)
            else:
                other_files.append(fpath)

        # 1. إرسال الصور
        if photos:
            if len(photos) == 1:
                await bot.send_file(chat_id, photos[0], caption="✅ **تم تنزيل الصورة بنجاح!**", force_document=as_doc)
            else:
                await bot.send_file(chat_id, photos, caption=f"📸 **تم تنزيل ألبوم الصور ({len(photos)} صورة):**")

        # 2. إرسال الفيديوهات
        for vid in videos:
            duration, width, height, thumb_path = get_video_metadata_and_thumb(vid)
            attr = []
            if not target_fmt.endswith('mp3'):
                attr.append(DocumentAttributeVideo(
                    duration=duration, 
                    w=width, 
                    h=height, 
                    supports_streaming=user_settings['streaming_mode']
                ))
            
            await bot.send_file(
                chat_id,
                vid,
                caption=f"🎥 **تم تنزيل الفيديو بنجاح!**\n📁 `{os.path.basename(vid)}`",
                force_document=as_doc,
                thumb=thumb_path,
                attributes=attr
            )

        # 3. إرسال باقي الملفات
        for oth in other_files:
            await bot.send_file(chat_id, oth, caption=f"📄 `{os.path.basename(oth)}`", force_document=as_doc)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(task_dir):
            try: shutil.rmtree(task_dir)
            except: pass

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(
        f"🚀 **أهلاً بك في بوت التحميل المباشر ({VERSION})**\n\n"
        "⚡ **التحميل مباشر وسريع بدون إشعارات الانتظار!**\n\n"
        "📸 **إنستغرام:** ريلز، منشورات، صور وألبومات.\n"
        "🎵 **تيك توك:** فيديوهات وسلاسل صور.\n"
        "𝕏 **منصة X:** فيديوهات وصور التغريدات.\n\n"
        "أرسل رابط المنشور للبدء مباشرة!"
    )

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    # البدء في معالجة كل رابط مباشرة وبشكل متوازي فور الاستلام
    for u in urls:
        asyncio.create_task(
            start_direct_execution(
                chat_id=chat_id,
                url=u,
                filename=get_clean_filename(u),
                as_doc=False,
                quality='best',
                target_fmt='mp4'
            )
        )

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بالتحميل المباشر الآن!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
