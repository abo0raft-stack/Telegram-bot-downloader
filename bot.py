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
import sqlite3
import aiohttp
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import unquote, urlparse, urljoin
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- التحديث التلقائي للمكتبات ---
def update_libraries():
    try:
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow", "aiohttp", "beautifulsoup4"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ تم تحديث مكتبات التنزيل ومعالجة الصور بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_libraries()

# --- إدارة قاعدة البيانات (SQLite) ---
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id INTEGER PRIMARY KEY,
            snapshots INTEGER,
            social_snapshots INTEGER,
            quality TEXT,
            font_size TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_tasks (
            task_key TEXT PRIMARY KEY,
            url TEXT,
            task_type TEXT,
            created_at REAL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_user_config(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT snapshots, social_snapshots, quality, font_size FROM user_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "snapshots": bool(row[0]),
            "social_snapshots": bool(row[1]),
            "quality": row[2],
            "font_size": row[3]
        }
    else:
        default_config = {
            "snapshots": True,
            "social_snapshots": False,
            "quality": "720",
            "font_size": "large"
        }
        set_user_config(chat_id, default_config)
        return default_config

def set_user_config(chat_id, config):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_settings (chat_id, snapshots, social_snapshots, quality, font_size)
        VALUES (?, ?, ?, ?, ?)
    ''', (chat_id, int(config["snapshots"]), int(config["social_snapshots"]), config["quality"], config["font_size"]))
    conn.commit()
    conn.close()

def save_task(task_key, url, task_type):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pending_tasks VALUES (?, ?, ?, ?)", (task_key, url, task_type, time.time()))
    conn.commit()
    conn.close()

def pop_task(task_key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT url, task_type FROM pending_tasks WHERE task_key = ?", (task_key,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM pending_tasks WHERE task_key = ?", (task_key,))
        conn.commit()
        conn.close()
        return row[0], row[1]
    conn.close()
    return None, None

# --- الإعدادات ---
VERSION = "v81.0-SmartExtractor-Edition"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_CANCEL_EVENTS = {}

FONT_SIZE_MAP = {
    "small": 2.18,
    "medium": 3.25,
    "large": 5.35,
    "xlarge": 10.45
}

# --- التعامل مع ملفات الكوكيز ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"
XNXX_COOKIES_FILE = "xnxx_cookies.txt"

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

    xnxx_b64 = os.environ.get("XNXX_COOKIES_BASE64")
    if xnxx_b64:
        try:
            with open(XNXX_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(xnxx_b64.strip()))
        except Exception: pass

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Referer': 'https://www.google.com/',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
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

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    complex_domains = [
        'twitter.com', 'x.com', 'instagram.com', 'instagr.am', 
        'tiktok.com', 'vt.tiktok.com', 'facebook.com', 'fb.watch', 'fb.com',
        'reddit.com', 'redd.it', 'pinterest.com', 'pin.it',
        'threads.net', 'snapchat.com', 'vk.com', 'vimeo.com', 'xnxx.com', 'youtube.com', 'youtu.be'
    ]
    return any(x in domain for x in complex_domains)

def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

def is_instagram_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['instagram.com', 'instagr.am'])

def is_xnxx_url(url):
    domain = urlparse(url).netloc.lower()
    return 'xnxx.com' in domain

def is_tiktok_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['tiktok.com', 'vt.tiktok.com'])

def is_pinterest_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['pinterest.com', 'pin.it'])

def is_reddit_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['reddit.com', 'redd.it'])

def get_clean_filename(url):
    if is_complex_url(url): return "media_download"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'): return filename.replace('.m3u8', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv', '.jpg', '.jpeg', '.png', '.webp']): 
        return "downloaded_media"
    return filename

# --- محرك السحب الذكي للتعرف على أوساط الفيديو بالصفحة ---
async def extract_direct_video_urls(url):
    direct_urls = []
    try:
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
            async with session.get(url, timeout=10, ssl=False) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # 1. البحث عن وسم <video> أو <source>
                    for vid in soup.find_all(['video', 'source']):
                        src = vid.get('src')
                        if src:
                            direct_urls.append(urljoin(url, src))
                    
                    # 2. البحث عن وسم og:video في Meta tags
                    for meta in soup.find_all('meta'):
                        prop = meta.get('property', '') or meta.get('name', '')
                        if 'video' in prop.lower() and meta.get('content'):
                            direct_urls.append(urljoin(url, meta.get('content')))
                            
                    # 3. البحث في السكربتات المضمنة عن روابط mp4 أو m3u8
                    found_in_js = re.findall(r'https?://[^\s\'"]+\.(?:mp4|m3u8)[^\s\'"]*', html)
                    direct_urls.extend(found_in_js)
    except Exception as e:
        print(f"Smart Scraper error: {e}")

    # إزالة التكرار
    unique_urls = list(set(direct_urls))
    return unique_urls

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
                    width = int(stream['width'])
                    height = int(stream['height'])
                    
                    side_data = stream.get('side_data_list', [])
                    for side in side_data:
                        if abs(side.get('rotation', 0)) in [90, 270]:
                            width, height = height, width
                    break

        subprocess.run([
            'ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path,
            '-vframes', '1', '-q:v', '2', thumb_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            thumb_path = None
            
    except Exception as e:
        thumb_path = None

    return duration, width, height, thumb_path

def add_transparent_text_center(image_path, text, font_ratio=0.35):
    try:
        with Image.open(image_path).convert("RGBA") as base:
            txt_layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)
            
            w, h = base.size
            font_size = max(40, int(min(w, h) * font_ratio))
            
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            x = (w - text_w) / 2
            y = (h - text_h) / 2

            draw.text((x, y), text, font=font, fill=(255, 255, 255, 160))

            out = Image.alpha_composite(base, txt_layer)
            out.convert("RGB").save(image_path, "JPEG", quality=95)
    except Exception as e:
        print(f"Error adding text to frame: {e}")

def extract_9_frames(video_path, duration, chat_id=None):
    frames = []
    if duration <= 0:
        return frames

    interval = duration / 10.0
    timestamps = [interval * i for i in range(1, 10)]

    base_dir = os.path.dirname(video_path)
    
    font_choice = get_user_config(chat_id)["font_size"] if chat_id else "large"
    font_ratio = FONT_SIZE_MAP.get(font_choice, 0.35)

    for idx, ts in enumerate(timestamps, start=1):
        out_name = os.path.join(base_dir, f"frame_{idx}.jpg")
        
        hrs = int(ts // 3600)
        mins = int((ts % 3600) // 60)
        secs = int(ts % 60)
        time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"

        cmd = [
            'ffmpeg', '-y', '-ss', str(ts), '-i', video_path,
            '-vframes', '1', '-q:v', '2', out_name
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(out_name) and os.path.getsize(out_name) > 0:
            add_transparent_text_center(out_name, time_str, font_ratio=font_ratio)
            frames.append(out_name)

    return frames

# --- التقسيم التلقائي للفيديوهات الكبيرة ---
def split_video_file(filepath, max_size_mb=1950):
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return [filepath]

    duration, _, _, _ = get_video_metadata_and_thumb(filepath)
    if duration <= 0:
        return [filepath]

    parts_count = int(file_size_mb // max_size_mb) + 1
    segment_time = int(duration / parts_count)

    base_dir = os.path.dirname(filepath)
    filename_without_ext, ext = os.path.splitext(os.path.basename(filepath))

    split_files = []
    for i in range(parts_count):
        start_sec = i * segment_time
        out_part = os.path.join(base_dir, f"{filename_without_ext}_part{i+1}{ext}")
        cmd = [
            'ffmpeg', '-y', '-ss', str(start_sec), '-t', str(segment_time),
            '-i', filepath, '-c', 'copy', out_part
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(out_part) and os.path.getsize(out_part) > 0:
            split_files.append(out_part)

    if split_files:
        try: os.remove(filepath)
        except: pass
        return split_files
    return [filepath]

# --- قص جزء محدد من الفيديو ---
def trim_video_clip(filepath, start_time, end_time):
    base_dir = os.path.dirname(filepath)
    filename_without_ext, ext = os.path.splitext(os.path.basename(filepath))
    out_trimmed = os.path.join(base_dir, f"{filename_without_ext}_trimmed{ext}")

    cmd = [
        'ffmpeg', '-y', '-ss', str(start_time), '-to', str(end_time),
        '-i', filepath, '-c', 'copy', out_trimmed
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(out_trimmed) and os.path.getsize(out_trimmed) > 0:
        try: os.remove(filepath)
        except: pass
        return out_trimmed
    return filepath

# --- دوال تنزيل المنصات ---
def download_with_ytdlp(url, task_dir, fmt='mp4', quality='720'):
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

    if is_x_url(url) and os.path.exists(X_COOKIES_FILE):
        ydl_opts['cookiefile'] = X_COOKIES_FILE
    elif is_instagram_url(url) and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE
    elif is_xnxx_url(url) and os.path.exists(XNXX_COOKIES_FILE):
        ydl_opts['cookiefile'] = XNXX_COOKIES_FILE

    if fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif quality and quality != 'best':
        ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"yt-dlp error: {e}")

def gallery_dl_fallback_engine(url, task_dir, prefix="media"):
    try:
        cmd = [
            "gallery-dl",
            "--directory", task_dir,
            "--filename", f"{prefix}_{{id}}_{{num}}.{{ext}}",
            "--user-agent", BROWSER_HEADERS['User-Agent'],
            url
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    except Exception as e:
        print(f"gallery-dl error ({prefix}): {e}")

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
        cmd = [
            'ffmpeg', '-y', '-i', cleaned_path,
            '-map_metadata', '-1',
            '-vf', 'format=yuv420p',
            '-q:v', '2',
            ffmpeg_path
        ]
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
        print(f"Image deep sanitize error: {e}")
        return file_path

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    units = ['B', 'KB', 'MB', 'GB']
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

def format_time(seconds):
    if seconds <= 0:
        return "0 ثانية"
    seconds = int(seconds)
    mins = seconds // 60
    secs = seconds % 60
    if mins > 0:
        return f"{mins} دقيقة و {secs} ثانية"
    return f"{secs} ثانية"

# --- التنزيل غير المتزامن المحسّن بـ Aiohttp ---
async def download_direct_async(client, chat_id, url, filepath, status_msg, cancel_event, task_id):
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        async with session.get(url, ssl=False) as response:
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            
            downloaded = 0
            start_time = time.time()
            last_update_time = 0
            chunk_size = 256 * 1024

            cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

            with open(filepath, 'wb') as f:
                async for chunk in response.content.iter_chunked(chunk_size):
                    if cancel_event.is_set(): raise Exception("CANCELLED")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        current_time = time.time()
                        
                        if current_time - last_update_time > 1.8 or downloaded == total_size:
                            last_update_time = current_time
                            elapsed = current_time - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            
                            percent = (downloaded / total_size * 100) if total_size > 0 else 0
                            filled = int(percent // 10)
                            bar = "█" * filled + "░" * (10 - filled)
                            
                            rem_time = (total_size - downloaded) / speed if speed > 0 and total_size > 0 else 0
                            
                            text = (
                                f"📥 **جاري التنزيل المباشر...**\n"
                                f"[{bar}] {percent:.1f}%\n"
                                f"📦 الحجم: `{format_size(downloaded)}` / `{format_size(total_size)}`\n"
                                f"⚡ السرعة: `{format_size(speed)}/s`\n"
                                f"⏱️ الوقت المنقضي: `{format_time(elapsed)}`\n"
                                f"⏳ المتبقي تقريباً: `{format_time(rem_time)}`"
                            )
                            try:
                                await status_msg.edit(text, buttons=cancel_btn)
                            except: pass

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='720', media_msg=None, target_fmt='mp4', status_msg=None, trim_times=None):
    task_id = f"task_{int(time.time() * 1000)}"
    cancel_event = threading.Event()
    ACTIVE_CANCEL_EVENTS[task_id] = cancel_event

    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

    if not status_msg:
        status_msg = await bot.send_message(chat_id, "⏳ **جاري تحضير واستخراج الفيديو...**", buttons=cancel_btn)
    else:
        await status_msg.edit("⏳ **جاري تحضير واستخراج الفيديو...**", buttons=cancel_btn)
        
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    user_config = get_user_config(chat_id)

    try:
        loop = asyncio.get_event_loop()
        target_url = clean_url(url) if is_complex_url(url) else url
        
        if cancel_event.is_set(): raise Exception("CANCELLED")

        # 1. المحاولة بـ yt-dlp أولاً (تدعم آلاف المواقع المباشرة وصفحات الفيديو)
        await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
        
        # 2. في حال عدم السحب بواسطة yt-dlp يتم الاستعانة بـ gallery-dl
        downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
        if not downloaded:
            await loop.run_in_executor(None, gallery_dl_fallback_engine, target_url, task_dir, "web")
            downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]

        # 3. محرك التنقيب الذكي Smart Scraper لسحب الروابط المباشرة من أوساط HTML في حال فشل المكتبات
        if not downloaded:
            extracted_urls = await extract_direct_video_urls(target_url)
            if extracted_urls:
                filepath = os.path.join(task_dir, filename)
                try:
                    await download_direct_async(bot, chat_id, extracted_urls[0], filepath, status_msg, cancel_event, task_id)
                except Exception:
                    pass

        # 4. التنزيل المباشر كـ Fallback أخير في حال كان الرابط ملفاً مباشراً
        downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
        if not downloaded and not is_complex_url(target_url):
            filepath = os.path.join(task_dir, filename)
            await download_direct_async(bot, chat_id, target_url, filepath, status_msg, cancel_event, task_id)

        if cancel_event.is_set(): raise Exception("CANCELLED")

        downloaded_files = []
        for root, _, files in os.walk(task_dir):
            for file in files:
                fpath = os.path.join(root, file)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 500 and not file.endswith('_thumb.jpg') and not file.endswith('_clean.jpg') and not file.endswith('_final.jpg') and not file.startswith('frame_'):
                    
                    base_name, current_ext = os.path.splitext(file)
                    current_ext = current_ext.lower()
                    
                    if "none" in file.lower() or current_ext not in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mkv', '.mov', '.avi']:
                        mime_type, _ = mimetypes.guess_type(fpath)
                        new_ext = ".jpg"
                        if mime_type:
                            guessed_ext = mimetypes.guess_extension(mime_type)
                            if guessed_ext:
                                new_ext = '.jpg' if guessed_ext == '.jpe' else guessed_ext
                        
                        clean_base = base_name.replace("None", "media").replace("none", "media")
                        if not clean_base or clean_base == "media":
                            clean_base = f"media_{int(time.time()*1000)}"
                            
                        new_fpath = os.path.join(root, f"{clean_base}{new_ext}")
                        os.rename(fpath, new_fpath)
                        fpath = new_fpath
                        current_ext = os.path.splitext(fpath)[1].lower()

                    if current_ext in ['.jpg', '.jpeg', '.png', '.webp']:
                        fpath = await loop.run_in_executor(None, deep_sanitize_image, fpath)

                    downloaded_files.append(fpath)

        if not downloaded_files:
            raise Exception("تعذر استخراج الفيديو أو الميديا من هذا الرابط. تأكد من صحة الرابط أو عمل السيرفر.")

        await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر)...**", buttons=cancel_btn)

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

        for vid in videos:
            if cancel_event.is_set(): raise Exception("CANCELLED")
            
            if trim_times:
                vid = trim_video_clip(vid, trim_times[0], trim_times[1])

            split_vids = split_video_file(vid)

            for idx, part_vid in enumerate(split_vids, start=1):
                part_caption = f" (Part {idx}/{len(split_vids)})" if len(split_vids) > 1 else ""
                duration, width, height, thumb_path = get_video_metadata_and_thumb(part_vid)
                attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                
                await bot.send_file(chat_id, part_vid, caption=f"🎥 **تم تنزيل الفيديو بنجاح!**{part_caption}\n📁 `{os.path.basename(part_vid)}`", force_document=as_doc, thumb=thumb_path, attributes=attr)
                
                should_take_snaps = user_config.get("snapshots", True) and (not is_complex_url(target_url) or user_config.get("social_snapshots", False))

                if duration > 0 and should_take_snaps and idx == 1:
                    await status_msg.edit("📸 **جاري توليد ألبوم اللقطات الـ 9 للفيديو...**")
                    frames = extract_9_frames(part_vid, duration, chat_id=chat_id)
                    if frames:
                        await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات 9 المصورة مع أوقاتها من الفيديو:**")
                        for fr in frames:
                            try: os.remove(fr)
                            except: pass

                if thumb_path and os.path.exists(thumb_path):
                    try: os.remove(thumb_path)
                    except: pass

        for oth in other_files:
            if cancel_event.is_set(): raise Exception("CANCELLED")
            await bot.send_file(chat_id, oth, caption=f"📄 `{os.path.basename(oth)}`", force_document=True)

        await status_msg.delete()

    except Exception as e:
        if str(e) == "CANCELLED":
            await status_msg.edit("🛑 **تم إلغاء العملية بناءً على طلبك.**", buttons=None)
        else:
            await status_msg.edit(f"❌ **خطأ:** `{str(e)}`", buttons=None)
    finally:
        ACTIVE_CANCEL_EVENTS.pop(task_id, None)
        if os.path.exists(task_dir):
            try: shutil.rmtree(task_dir)
            except: pass

@bot.on(events.CallbackQuery(pattern=r"^cancel_"))
async def cancel_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    task_id = "_".join(data[1:])
    if task_id in ACTIVE_CANCEL_EVENTS:
        ACTIVE_CANCEL_EVENTS[task_id].set()
        await event.answer("🛑 جاري إلغاء العملية...", alert=True)
    else:
        await event.answer("⚠️ العملية غير موجودة أو انتهت بالفعل.", alert=True)

# --- معالجة استقبال جميع الروابط وعرض أزرار الجودة شاملة 360p إلى 1080p ---
@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    if event.text.startswith("/trim"):
        return

    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    for u in urls:
        clean_u = u.split('?')[0] if is_instagram_url(u) else u
        
        # إنشاء مفتاح المهمة وتخزينه
        task_key = f"qsel_{chat_id}_{int(time.time()*1000)}"
        save_task(task_key, clean_u, "quality_select")
        
        # أزرار التنزيل باختيار الجودات من 360p إلى 1080p بالإضافة للصوت والمستند
        buttons = [
            [
                Button.inline("🌟 1080p", data=f"q_1080_{task_key}"),
                Button.inline("🎬 720p", data=f"q_720_{task_key}")
            ],
            [
                Button.inline("📱 480p", data=f"q_480_{task_key}"),
                Button.inline("⚡ 360p", data=f"q_360_{task_key}")
            ],
            [
                Button.inline("🎵 صوت فقط (MP3)", data=f"q_mp3_{task_key}"),
                Button.inline("📄 كمستند (Document)", data=f"q_doc_{task_key}")
            ]
        ]
        await event.respond("🎬 **تم التعرف على الرابط! اختر جودة الفيديو أو الصيغة المطلوبة:**", buttons=buttons)

# --- معالجة اختيار الجودة من قبل المستخدم ---
@bot.on(events.CallbackQuery(pattern=r"^q_"))
async def quality_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    choice = data[1]
    task_key = "_".join(data[2:])
    
    url, _ = pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار، يرجى إعادة إرسال الرابط.", alert=True)
        return
        
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    target_fmt = 'mp3' if choice == 'mp3' else 'mp4'
    quality_val = choice if choice in ['1080', '720', '480', '360'] else '720'
    
    status_msg = await event.edit("⏳ **تم استلام طلبك، جاري بدء سحب وتنزيل الفيديو...**", buttons=None)
    
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=as_doc,
            quality=quality_val,
            target_fmt=target_fmt,
            status_msg=status_msg
        )
    )

# --- رسالة البداية وسجل التحديثات ---
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        "🤖 **أهلاً بك في بوت التحميل المباشر والذكي من أي رابط!**\n\n"
        "✨ **أبرز ميزات البوت المحدثة:**\n\n"
        "🔗 **التعرف الذكي والاستخراج:** سحب الفيديو من أي صفحة ويب أو رابط مباشر تلقائياً.\n"
        "📊 **خيارات الجودة المتعددة:** دعم اختيار الجودة بمرونة (`1080p`, `720p`, `480p`, `360p`).\n"
        "🎵 **تحويل MP3 وحفظ كمستندات:** خيارات سريعة لتحويل مقاطع الفيديو لصوتيات.\n"
        "✂️ **قص المقاطع:** أمر `/trim [بدء] [نهاية] [رابط]` لقص أجزاء محددة."
    )
    await event.respond(welcome_text)

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بكفاءة عالية مع خيارات الجودة والاستخراج الذكي!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
