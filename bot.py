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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse
from PIL import Image, ImageDraw, ImageFont
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v51.0-TikTok_Cookies_Fix"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_DOWNLOADS = {}
LAST_UPDATE_TIME = {}
USER_STATES = {}
USER_SETTINGS = {}

DOWNLOAD_QUEUES = {}
QUEUE_LOCKS = {}

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

FONT_SIZES = {
    'small': {'ratio': 0.08, 'min': 35, 'label': '🔍 صغير'},
    'medium': {'ratio': 0.14, 'min': 60, 'label': '📐 وسط'},
    'large': {'ratio': 0.22, 'min': 100, 'label': '📢 كبير'},
    'xlarge': {'ratio': 0.32, 'min': 150, 'label': '💥 كبير جداً'}
}

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive'
}

def get_user_settings(user_id):
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {'send_screenshots': True, 'streaming_mode': True, 'font_size': 'large'}
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
    if is_complex_url(url): return "media_download.mp4"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'): return filename.replace('.m3u8', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv']): return "video_download.mp4"
    return filename

def change_extension(filename, new_ext):
    return f"{os.path.splitext(filename)[0]}.{new_ext.strip('.')}"

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

def download_with_ytdlp(url, filepath, fmt='mp4', quality='best'):
    ydl_opts = {
        'outtmpl': filepath,
        'quiet': True,
        'no_warnings': True,
        'headers': BROWSER_HEADERS,
        'nocheckcertificate': True,
        'ignoreerrors': True,
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
    if is_complex_url(url):
        download_with_ytdlp(url, filepath)
        return
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=4*1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            if chunk: f.write(chunk)

async def start_execution(chat_id, url, filename, as_doc, quality, media_msg, target_fmt):
    status_msg = await bot.send_message(chat_id, "⏳ **جاري المعالجة...**")
    cancel_event = threading.Event()
    os.makedirs("downloads", exist_ok=True)
    filepath = os.path.join("downloads", filename)
    try:
        loop = asyncio.get_event_loop()
        if url and is_complex_url(url):
            await loop.run_in_executor(None, download_with_ytdlp, url, filepath, target_fmt, quality)
        elif url:
            await loop.run_in_executor(None, download_direct_file, url, filepath, cancel_event)
        elif media_msg:
            await bot.download_media(media_msg, file=filepath)

        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0: raise Exception("الملف فارغ أو فشل التحميل.")

        await status_msg.edit("📤 **جاري الرفع...**")
        duration, width, height, thumb_path = get_video_metadata_and_thumb(filepath)
        user_settings = get_user_settings(chat_id)
        
        attr = []
        if not target_fmt.endswith('mp3'):
            attr.append(DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=user_settings['streaming_mode']))

        await bot.send_file(chat_id, filepath, caption=f"✅ **تم التحميل:** `{os.path.basename(filepath)}`", force_document=as_doc, thumb=thumb_path, attributes=attr)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except: pass

async def process_queue(chat_id):
    if chat_id not in QUEUE_LOCKS: QUEUE_LOCKS[chat_id] = asyncio.Lock()
    async with QUEUE_LOCKS[chat_id]:
        while chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
            task = DOWNLOAD_QUEUES[chat_id].pop(0)
            await start_execution(chat_id, task.get('url'), task['custom_name'], task['as_doc'], task['quality'], task.get('media_msg'), task.get('fmt', 'mp4'))

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(f"🚀 **أهلاً بك في بوت التحميل ({VERSION})**\nأرسل أي رابط (X, Insta, TikTok) أو ملف للبدء.")

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    if chat_id not in DOWNLOAD_QUEUES: DOWNLOAD_QUEUES[chat_id] = []
    
    for u in urls:
        DOWNLOAD_QUEUES[chat_id].append({'url': u, 'custom_name': get_clean_filename(u), 'as_doc': False, 'quality': 'best', 'fmt': 'mp4'})
    
    await event.respond(f"📥 **تمت إضافة {len(urls)} طلب(ات) للمعالجة!**")
    asyncio.create_task(process_queue(chat_id))

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بنجاح!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
