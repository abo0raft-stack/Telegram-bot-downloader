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
VERSION = "v54.1-Instagram-Fix-2026"
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
        except Exception: pass

    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
        except Exception: pass

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive',
    'Referer': 'https://www.instagram.com/'
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

def get_clean_url(url):
    """تنظيف الرابط من أي معلمات إضافية مثل igsh لضمان عمل التحميل"""
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean_url

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
        'writethumbnails': True,
        'allow_playlist_files': True,
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
        ydl_opts['format'] = 'best/bestvideo+bestaudio'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def fallback_instagram_download(url, task_dir):
    """تحديث الدالة للتعامل مع الروابط النظيفة وجلب البيانات بشكل أدق"""
    try:
        # إضافة خيار التنسيق __a=1 للحصول على JSON مباشر
        json_url = url.rstrip('/') + '/?__a=1&__d=dis'
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        
        if os.path.exists(INSTAGRAM_COOKIES_FILE):
            try:
                with open(INSTAGRAM_COOKIES_FILE, 'r') as f:
                    for line in f:
                        if line.startswith('#') or not line.strip(): continue
                        parts = line.strip().split('\t')
                        if len(parts) >= 7: session.cookies.set(parts[5], parts[6], domain=parts[0])
            except Exception: pass

        res = session.get(json_url, timeout=20)
        if res.status_code == 200:
            data = res.json()
            # محاولة استخراج الوسائط من الهيكل الجديد
            media = data.get('graphql', {}).get('shortcode_media', {})
            if not media: media = data.get('items', [{}])[0]
            
            # معالجة الألبومات (Carousel)
            items = []
            carousel = media.get('edge_sidecar_to_children', {}).get('edges', [])
            if carousel:
                for node in carousel:
                    n = node.get('node', {})
                    if n.get('is_video'): items.append(n.get('video_url'))
                    else: items.append(n.get('display_url'))
            else:
                if media.get('is_video'): items.append(media.get('video_url'))
                else: items.append(media.get('display_url'))

            idx = 1
            for img_url in items:
                if not img_url: continue
                r = session.get(img_url, timeout=15)
                if r.status_code == 200:
                    ext = "mp4" if ".mp4" in img_url else "jpg"
                    fpath = os.path.join(task_dir, f"media_{idx}.{ext}")
                    with open(fpath, 'wb') as f:
                        f.write(r.content)
                    idx += 1
    except Exception as e:
        print(f"Fallback Error: {e}")

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4'):
    # تنظيف الرابط هنا قبل البدء
    clean_url = get_clean_url(url)
    
    status_msg = await bot.send_message(chat_id, "⏳ **جاري تنزيل المحتوى...**")
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        loop = asyncio.get_event_loop()
        
        if clean_url and is_complex_url(clean_url):
            await loop.run_in_executor(None, download_with_ytdlp, clean_url, task_dir, target_fmt, quality)
            
            downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
            if not downloaded and ('instagram.com' in clean_url or 'instagr.am' in clean_url):
                await loop.run_in_executor(None, fallback_instagram_download, clean_url, task_dir)
                
        elif clean_url:
            filepath = os.path.join(task_dir, filename)
            await loop.run_in_executor(None, lambda: requests.get(clean_url, stream=True, headers=BROWSER_HEADERS, timeout=30).raise_for_status() or open(filepath, 'wb').write(requests.get(clean_url, stream=True, headers=BROWSER_HEADERS).content))

        downloaded_files = [os.path.join(task_dir, f) for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]

        if not downloaded_files:
            raise Exception("لم يتم العثور على أي ملفات. (قد يكون المنشور خاصاً أو الرابط غير صحيح)")

        await status_msg.edit(f"📤 **جاري رفع {len(downloaded_files)} عنصر...**")
        
        # كود الإرسال كما هو (بدون تغيير)
        photos, videos, other_files = [], [], []
        for fpath in sorted(downloaded_files):
            ext = os.path.splitext(fpath)[1].lower()
            if ext in ('.jpg', '.jpeg', '.png', '.webp'): photos.append(fpath)
            elif ext in ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm'): videos.append(fpath)
            else: other_files.append(fpath)

        if photos:
            for i in range(0, len(photos), 10): await bot.send_file(chat_id, photos[i:i+10], caption=f"📸 **ألبوم صور ({len(photos)})**" if i==0 else "")
        for vid in videos:
            attr = [DocumentAttributeVideo(duration=0, w=0, h=0, supports_streaming=True)]
            await bot.send_file(chat_id, vid, attributes=attr)
        for oth in other_files:
            await bot.send_file(chat_id, oth)

        await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(task_dir): shutil.rmtree(task_dir)

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    for u in urls:
        asyncio.create_task(start_direct_execution(event.chat_id, u, "media"))

def main():
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
