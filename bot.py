import os
import shutil
import threading
import time
import gc
import re
import base64
import json
import sys
import mimetypes
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- التحديث والتثبيت التلقائي للمكتبات ---
def update_and_install_libraries():
    try:
        import subprocess
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow", "aiohttp", "aiosqlite", "telethon", "requests", "psutil", "beautifulsoup4"], check=True)
        print("✅ تم تثبيت وتحديث المكتبات بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_and_install_libraries()

import asyncio
import requests
import yt_dlp
import aiosqlite
import aiohttp
import psutil
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# تحديد حد أقصى للعمليات الثقيلة المتزامنة
MAX_CONCURRENT_TASKS = asyncio.Semaphore(3)

# --- إدارة قاعدة البيانات (SQLite) ---
DB_FILE = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                snapshots INTEGER,
                social_snapshots INTEGER,
                quality TEXT,
                font_size TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pending_tasks (
                task_key TEXT PRIMARY KEY,
                url TEXT,
                task_type TEXT,
                created_at REAL
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                username TEXT PRIMARY KEY,
                added_at REAL
            )
        ''')
        await conn.commit()

asyncio.run(init_db())

async def get_user_config(chat_id):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT snapshots, social_snapshots, quality, font_size FROM user_settings WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
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
                await set_user_config(chat_id, default_config)
                return default_config

async def set_user_config(chat_id, config):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO user_settings (chat_id, snapshots, social_snapshots, quality, font_size)
            VALUES (?, ?, ?, ?, ?)
        ''', (chat_id, int(config["snapshots"]), int(config["social_snapshots"]), config["quality"], config["font_size"]))
        await conn.commit()

async def save_task(task_key, url, task_type):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO pending_tasks VALUES (?, ?, ?, ?)", (task_key, url, task_type, time.time()))
        await conn.commit()

async def pop_task(task_key):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT url, task_type FROM pending_tasks WHERE task_key = ?", (task_key,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await conn.execute("DELETE FROM pending_tasks WHERE task_key = ?", (task_key,))
                await conn.commit()
                return row[0], row[1]
    return None, None

# --- إدارة الأعضاء المسموح لهم ---
async def add_allowed_user(username):
    username = username.strip().lstrip('@').lower()
    if not username: return False
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO allowed_users VALUES (?, ?)", (username, time.time()))
        await conn.commit()
    return True

async def remove_allowed_user(username):
    username = username.strip().lstrip('@').lower()
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("DELETE FROM allowed_users WHERE username = ?", (username,))
        await conn.commit()

async def get_allowed_users():
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT username FROM allowed_users") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def is_allowed(event):
    chat_id = event.chat_id
    if chat_id == OWNER_ID:
        return True
    
    sender = await event.get_sender()
    if sender and getattr(sender, 'username', None):
        username = sender.username.lower()
        allowed_list = await get_allowed_users()
        if username in allowed_list:
            return True
    return False

# --- الإعدادات والحماية الخاصة ---
VERSION = "v87.0-Direct-Stream-Engine-2026"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "5414125521"))
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_CANCEL_EVENTS = {}
WAITING_FOR_USERNAME = {}

FONT_SIZE_MAP = {
    "small": 2.18,
    "medium": 3.25,
    "large": 5.35,
    "xlarge": 10.45
}

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8,ar;q=0.8',
    'Referer': 'https://www.google.com/',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Connection': 'keep-alive'
}

def is_owner(chat_id):
    return chat_id == OWNER_ID

def clean_download_folder():
    folder = "downloads"
    cleaned_bytes = 0
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    cleaned_bytes += os.path.getsize(fp)
                    os.remove(fp)
                except Exception: pass
            for d in dirs:
                try: shutil.rmtree(os.path.join(root, d))
                except Exception: pass
    else:
        os.makedirs(folder, exist_ok=True)
    gc.collect()
    return cleaned_bytes

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

def is_dailymotion_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['dailymotion.com', 'dai.ly']) or '/video/' in url.lower()

def is_whatsapp_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['wa.me', 'whatsapp.com', 'api.whatsapp.com'])

def clean_url(url):
    if is_dailymotion_url(url):
        if not url.startswith('http'):
            return f"https://www.dailymotion.com{url}"
        return url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    complex_domains = [
        'twitter.com', 'x.com', 'instagram.com', 'instagr.am', 
        'tiktok.com', 'vt.tiktok.com', 'facebook.com', 'fb.watch', 'fb.com',
        'reddit.com', 'redd.it', 'pinterest.com', 'pin.it',
        'threads.net', 'snapchat.com', 'vk.com', 'vimeo.com',
        'youtube.com', 'youtu.be', 'dailymotion.com', 'dai.ly',
        'wa.me', 'whatsapp.com'
    ]
    return any(x in domain for x in complex_domains) or is_dailymotion_url(url)

def is_youtube_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['youtube.com', 'youtu.be'])

def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

def is_instagram_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['instagram.com', 'instagr.am'])

def is_tiktok_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['tiktok.com', 'vt.tiktok.com'])

def is_pinterest_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['pinterest.com', 'pin.it'])

def is_reddit_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['reddit.com', 'redd.it'])

def is_profile_url(url):
    if is_whatsapp_url(url):
        return True

    parsed = urlparse(url)
    path = parsed.path.strip('/')
    domain = parsed.netloc.lower()

    if not path:
        return False

    if any(x in domain for x in ['instagram.com', 'instagr.am']):
        if not any(k in path for k in ['p/', 'reel/', 'reels/', 'stories/', 'tv/']):
            return True
    elif any(x in domain for x in ['twitter.com', 'x.com']):
        if not any(k in path for k in ['status/', 'i/']):
            return True
    elif any(x in domain for x in ['tiktok.com']):
        if path.startswith('@') and not any(k in path for k in ['/video/', '/photo/']):
            return True
    elif any(x in domain for x in ['youtube.com', 'youtu.be']):
        if any(path.startswith(k) for k in ['@', 'channel/', 'c/', 'user/']):
            return True
    elif any(x in domain for x in ['pinterest.com', 'pin.it']):
        if not path.startswith('pin/'):
            return True

    return False

def get_clean_filename(url):
    if is_complex_url(url): return "media_download"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'): return filename.replace('.m3u8', '.mp4')
    if filename.endswith('.mpd'): return filename.replace('.mpd', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv', '.jpg', '.jpeg', '.png', '.webp']): 
        return "downloaded_media"
    return filename

async def convert_to_mp4(input_path):
    base, ext = os.path.splitext(input_path)
    if ext.lower() == '.mp4':
        return input_path
    
    output_path = f"{base}_converted.mp4"
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart',
        output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        try: os.remove(input_path)
        except: pass
        return output_path
    return input_path

async def get_video_metadata_and_thumb(file_path):
    duration, width, height = 0, 1280, 720
    thumb_path = f"{file_path}_thumb.jpg"
    
    base_path = os.path.splitext(file_path)[0]
    for ext in ['.jpg', '.png', '.webp']:
        possible_thumb = base_path + ext
        if os.path.exists(possible_thumb):
            try:
                with Image.open(possible_thumb) as img:
                    img.convert('RGB').save(thumb_path, 'JPEG', quality=90)
                break
            except Exception: pass

    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration:stream=width,height,rotation',
            '-of', 'json', file_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        data = json.loads(stdout.decode('utf-8'))
        
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

        if not os.path.exists(thumb_path):
            ff_cmd = [
                'ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path,
                '-vframes', '1', '-q:v', '2', thumb_path
            ]
            proc_thumb = await asyncio.create_subprocess_exec(*ff_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc_thumb.wait()
        
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            thumb_path = None
            
    except Exception:
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

async def extract_9_frames(video_path, duration, chat_id=None):
    frames = []
    if duration <= 0: return frames

    interval = duration / 10.0
    timestamps = [interval * i for i in range(1, 10)]
    base_dir = os.path.dirname(video_path)
    
    user_conf = await get_user_config(chat_id) if chat_id else None
    font_choice = user_conf["font_size"] if user_conf else "large"
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
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

        if os.path.exists(out_name) and os.path.getsize(out_name) > 0:
            add_transparent_text_center(out_name, time_str, font_ratio=font_ratio)
            frames.append(out_name)

    return frames

async def split_video_file(filepath, max_size_mb=1950):
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if file_size_mb <= max_size_mb: return [filepath]

    duration, _, _, _ = await get_video_metadata_and_thumb(filepath)
    if duration <= 0: return [filepath]

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
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if os.path.exists(out_part) and os.path.getsize(out_part) > 0:
            split_files.append(out_part)

    if split_files:
        try: os.remove(filepath)
        except: pass
        return split_files
    return [filepath]

async def trim_video_clip(filepath, start_time, end_time):
    base_dir = os.path.dirname(filepath)
    filename_without_ext, ext = os.path.splitext(os.path.basename(filepath))
    out_trimmed = os.path.join(base_dir, f"{filename_without_ext}_trimmed{ext}")

    cmd = [
        'ffmpeg', '-y', '-ss', str(start_time), '-to', str(end_time),
        '-i', filepath, '-c', 'copy', out_trimmed
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()

    if os.path.exists(out_trimmed) and os.path.getsize(out_trimmed) > 0:
        try: os.remove(filepath)
        except: pass
        return out_trimmed
    return filepath

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    units = ['B', 'KB', 'MB', 'GB']
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

def format_time(seconds):
    if seconds <= 0: return "0 ثانية"
    seconds = int(seconds)
    mins = seconds // 60
    secs = seconds % 60
    if mins > 0: return f"{mins} دقيقة و {secs} ثانية"
    return f"{secs} ثانية"

# --- محرك v87 لجلب الصورة الشخصية (Profile Picture / Avatar) ---
async def download_profile_avatar(url, task_dir):
    try:
        if is_whatsapp_url(url):
            async with aiohttp.ClientSession() as session:
                custom_headers = BROWSER_HEADERS.copy()
                custom_headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                async with session.get(url, headers=custom_headers, timeout=20) as r:
                    if r.status == 200:
                        html = await r.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        img_url = None
                        og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
                        if og_img and og_img.get("content"):
                            img_url = og_img["content"]

                        if img_url:
                            img_url = unquote(img_url).replace('&amp;', '&')
                            async with session.get(img_url, headers=custom_headers, timeout=20) as img_res:
                                if img_res.status == 200:
                                    filepath = os.path.join(task_dir, "profile_avatar.jpg")
                                    with open(filepath, 'wb') as f:
                                        f.write(await img_res.read())
                                    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                                        return True

        cmd = [
            "gallery-dl",
            "--directory", task_dir,
            "--filename", "profile_avatar.{ext}",
            "--option", "extractor.instagram.include=avatar",
            "--option", "extractor.twitter.include=avatar",
            "--user-agent", BROWSER_HEADERS['User-Agent']
        ]
        if is_instagram_url(url) and os.path.exists(INSTAGRAM_COOKIES_FILE):
            cmd.extend(["--cookies", INSTAGRAM_COOKIES_FILE])
        elif is_x_url(url) and os.path.exists(X_COOKIES_FILE):
            cmd.extend(["--cookies", X_COOKIES_FILE])
        
        cmd.append(url)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except Exception: pass

        if any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir)):
            return True

        loop = asyncio.get_event_loop()
        ydl_opts = {
            'outtmpl': os.path.join(task_dir, 'profile_avatar.%(ext)s'),
            'skip_download': True,
            'writethumbnails': True,
            'quiet': True,
            'no_warnings': True,
            'headers': BROWSER_HEADERS
        }
        if is_instagram_url(url) and os.path.exists(INSTAGRAM_COOKIES_FILE):
            ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE
        elif is_x_url(url) and os.path.exists(X_COOKIES_FILE):
            ydl_opts['cookiefile'] = X_COOKIES_FILE

        def run_ydl_avatar():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)

        try:
            await loop.run_in_executor(None, run_ydl_avatar)
        except Exception: pass

        if any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir)):
            return True

    except Exception as e:
        print(f"Profile Avatar Engine Error: {e}")

    return any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir))

# --- محرك التنزيل الخاص بـ yt-dlp باقي المنصات ---
def download_with_ytdlp(url, task_dir, fmt='mp4', quality='best'):
    out_template = os.path.join(task_dir, '%(title).30s_%(id)s.%(ext)s')
    headers = BROWSER_HEADERS.copy()

    ydl_opts = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'headers': headers,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'writethumbnails': True,
        'allow_playlist_files': True,
        'geo_bypass': True,
        'age_limit': 99,
    }

    if is_x_url(url) and os.path.exists(X_COOKIES_FILE):
        ydl_opts['cookiefile'] = X_COOKIES_FILE
    elif is_instagram_url(url) and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE

    if fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }]
    else:
        ydl_opts['merge_output_format'] = 'mp4'
        if quality != 'best':
            ydl_opts['format'] = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        else:
            ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
            
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"yt-dlp error: {e}")

async def gallery_dl_fallback_engine(url, task_dir, prefix="media"):
    try:
        cmd = [
            "gallery-dl",
            "--directory", task_dir,
            "--filename", f"{prefix}_{{id}}_{{num}}.{{ext}}",
            "--user-agent", BROWSER_HEADERS['User-Agent'],
            url
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=45)
    except Exception as e:
        print(f"gallery-dl error ({prefix}): {e}")

async def deep_sanitize_image(file_path):
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
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=15)

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

# --- محرك التنزيل المباشر المحسّن مع استئناف التحميل المقطوع ---
async def download_direct_async(client, chat_id, url, filepath, status_msg, cancel_event, task_id):
    headers = BROWSER_HEADERS.copy()
    
    # دعم تحميل مقاطع الـ HLS (.m3u8) عبر FFmpeg مباشرة في حالة وجود رابط مباشر للبث
    if '.m3u8' in url.lower() or '.mpd' in url.lower():
        cmd = ['ffmpeg', '-y', '-headers', f"User-Agent: {headers['User-Agent']}\r\n", '-i', url, '-c', 'copy', filepath]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return

    downloaded = 0
    if os.path.exists(filepath):
        downloaded = os.path.getsize(filepath)
        if downloaded > 0:
            headers['Range'] = f"bytes={downloaded}-"

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, ssl=False) as response:
            if response.status not in [200, 206]:
                response.raise_for_status()

            is_resumed = (response.status == 206)
            
            if not is_resumed:
                downloaded = 0
                mode = 'wb'
                total_size = int(response.headers.get('content-length', 0))
            else:
                mode = 'ab'
                content_range = response.headers.get('content-range', '')
                if content_range and '/' in content_range:
                    total_size = int(content_range.split('/')[-1])
                else:
                    total_size = downloaded + int(response.headers.get('content-length', 0))

            start_time = time.time()
            last_update_time = 0
            chunk_size = 512 * 1024

            cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

            with open(filepath, mode) as f:
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
                            resumed_tag = "🔄 (مستأنف) " if is_resumed else ""

                            text = (
                                f"📥 **جاري التنزيل المباشر...** {resumed_tag}\n"
                                f"[{bar}] {percent:.1f}%\n"
                                f"📦 الحجم: `{format_size(downloaded)}` / `{format_size(total_size)}`\n"
                                f"⚡ السرعة: `{format_size(speed)}/s`\n"
                                f"⏱️ الوقت المنقضي: `{format_time(elapsed)}`\n"
                                f"⏳ المتبقي تقريباً: `{format_time(rem_time)}`"
                            )
                            try:
                                await status_msg.edit(text, buttons=cancel_btn)
                            except: pass

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None, trim_times=None, is_avatar_task=False):
    async with MAX_CONCURRENT_TASKS:
        task_id = f"task_{int(time.time() * 1000)}"
        cancel_event = threading.Event()
        ACTIVE_CANCEL_EVENTS[task_id] = cancel_event

        cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

        if not status_msg:
            status_msg = await bot.send_message(chat_id, "⏳ **جاري تحضير الطلب...**", buttons=cancel_btn)
        else:
            await status_msg.edit("⏳ **جاري تحضير الطلب...**", buttons=cancel_btn)
            
        task_dir = os.path.join("downloads", task_id)
        os.makedirs(task_dir, exist_ok=True)
        
        user_config = await get_user_config(chat_id)

        try:
            loop = asyncio.get_event_loop()
            target_url = clean_url(url) if (is_complex_url(url) and not is_whatsapp_url(url)) else url
            is_social = is_complex_url(target_url) if target_url else False
            
            if cancel_event.is_set(): raise Exception("CANCELLED")

            if is_avatar_task:
                await status_msg.edit("👤 **جاري استخراج صورة الملف الشخصي عبر المحرك المحسّن...**", buttons=cancel_btn)
                success = await download_profile_avatar(target_url, task_dir)
                if not success:
                    raise Exception("لم نتمكن من جلب الصورة الشخصية لهذا الحساب.")

            elif target_url and is_social:
                await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
                if cancel_event.is_set(): raise Exception("CANCELLED")

                downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
                if not downloaded:
                    await gallery_dl_fallback_engine(target_url, task_dir, "fallback")

            elif target_url:
                filepath = os.path.join(task_dir, filename)
                upload_start_time = time.time()
                last_upload_update = 0

                async def upload_progress_callback(current, total):
                    if cancel_event.is_set(): raise Exception("CANCELLED")
                    nonlocal last_upload_update
                    now = time.time()
                    if now - last_upload_update > 2.0 or current == total:
                        last_upload_update = now
                        elapsed = now - upload_start_time
                        speed = current / elapsed if elapsed > 0 else 0
                        percent = (current / total * 100) if total > 0 else 0
                        filled = int(percent // 10)
                        bar = "█" * filled + "░" * (10 - filled)
                        rem_time = (total - current) / speed if speed > 0 and total > 0 else 0
                        
                        text = (
                            f"📤 **جاري رفع الملف إلى تيليجرام...**\n"
                            f"[{bar}] {percent:.1f}%\n"
                            f"📦 الحجم: `{format_size(current)}` / `{format_size(total)}`\n"
                            f"⚡ السرعة: `{format_size(speed)}/s`\n"
                            f"⏱️ الوقت المنقضي: `{format_time(elapsed)}`\n"
                            f"⏳ المتبقي: `{format_time(rem_time)}`"
                        )
                        try:
                            await status_msg.edit(text, buttons=cancel_btn)
                        except: pass

                await download_direct_async(bot, chat_id, target_url, filepath, status_msg, cancel_event, task_id)
                
                if cancel_event.is_set(): raise Exception("CANCELLED")
                
                if trim_times:
                    await status_msg.edit("✂️ **جاري قص المقطع المباشر...**")
                    filepath = await trim_video_clip(filepath, trim_times[0], trim_times[1])

                await status_msg.edit("📤 **جاري تجهيز الرفع...**", buttons=cancel_btn)
                
                if target_fmt == 'mp3':
                    base, _ = os.path.splitext(filepath)
                    mp3_path = base + ".mp3"
                    proc = await asyncio.create_subprocess_exec('ffmpeg', '-y', '-i', filepath, '-vn', '-ab', '320k', mp3_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await proc.wait()
                    if os.path.exists(mp3_path):
                        filepath = mp3_path
                else:
                    filepath = await convert_to_mp4(filepath)
                
                video_files = await split_video_file(filepath)

                for idx, vid_file in enumerate(video_files, start=1):
                    part_caption = f" (Part {idx}/{len(video_files)})" if len(video_files) > 1 else ""
                    
                    if as_doc:
                        await bot.send_file(chat_id, vid_file, force_document=True, caption=part_caption, progress_callback=upload_progress_callback)
                    else:
                        ext = os.path.splitext(vid_file)[1].lower()
                        if ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
                            duration, width, height, thumb_path = await get_video_metadata_and_thumb(vid_file)
                            attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                            await bot.send_file(chat_id, vid_file, caption=part_caption, thumb=thumb_path, attributes=attr, progress_callback=upload_progress_callback)
                            
                            should_take_snaps = user_config.get("snapshots", True) and (not is_social or user_config.get("social_snapshots", False))
                            
                            if duration > 0 and should_take_snaps and idx == 1:
                                await status_msg.edit("📸 **جاري التقاط 9 صور من الفيديو وتجهيز الألبوم...**")
                                frames = await extract_9_frames(vid_file, duration, chat_id=chat_id)
                                if frames:
                                    await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات المصورة من الفيديو مع التوقيتات:**", album=True)
                                    for fr in frames:
                                        try: os.remove(fr)
                                        except: pass

                            if thumb_path and os.path.exists(thumb_path):
                                try: os.remove(thumb_path)
                                except: pass
                        elif ext in ['.mp3', '.wav', '.m4a', '.aac']:
                            await bot.send_file(chat_id, vid_file, caption=part_caption, progress_callback=upload_progress_callback)
                        else:
                            await bot.send_file(chat_id, vid_file, caption=part_caption, force_document=True, progress_callback=upload_progress_callback)
                
                await status_msg.delete()
                return

            downloaded_files = []
            for root, _, files in os.walk(task_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    if os.path.isfile(fpath) and os.path.getsize(fpath) > 500 and not file.endswith('_thumb.jpg') and not file.endswith('_clean.jpg') and not file.endswith('_final.jpg') and not file.startswith('frame_'):
                        downloaded_files.append(fpath)

            if not downloaded_files:
                raise Exception("تعذر الوصول إلى المحتوى. تأكد من صحة الرابط.")

            await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر)...**", buttons=cancel_btn)

            for vid in downloaded_files:
                ext = os.path.splitext(vid)[1].lower()
                if ext in ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm']:
                    vid = await convert_to_mp4(vid)
                    duration, width, height, thumb_path = await get_video_metadata_and_thumb(vid)
                    attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                    await bot.send_file(chat_id, vid, caption="🎥 **تم تنزيل الفيديو بنجاح!**", force_document=as_doc, thumb=thumb_path, attributes=attr, supports_streaming=True)
                else:
                    await bot.send_file(chat_id, vid, force_document=as_doc)

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
            gc.collect()

# --- الأوامر الرئيسية ومعالجة التفاعل ---

@bot.on(events.NewMessage(pattern=r"^/clean$"))
async def clean_handler(event):
    if not await is_allowed(event): return
    freed = clean_download_folder()
    await event.respond(f"🧹 **تم تنظيف المجلدات المؤقتة بنجاح!**\n💾 المساحة المحررة: `{format_size(freed)}`")

@bot.on(events.NewMessage(pattern=r"^/df$"))
async def disk_status_handler(event):
    if not await is_allowed(event): return
    disk = shutil.disk_usage("/")
    mem = psutil.virtual_memory()
    
    msg = (
        "📊 **حالة السيرفر والتخزين الحالي:**\n\n"
        f"💾 **مساحة القرص:**\n"
        f"• الإجمالي: `{format_size(disk.total)}`\n"
        f"• المستهلك: `{format_size(disk.used)}`\n"
        f"• المتبقي: `{format_size(disk.free)}` ({100 - disk.percent:.1f}% فارغ)\n\n"
        f"🧠 **الذاكرة العشوائية (RAM):**\n"
        f"• الإجمالي: `{format_size(mem.total)}`\n"
        f"• المستهلك: `{format_size(mem.used)}` ({mem.percent}%)\n"
        f"• المتاح: `{format_size(mem.available)}`"
    )
    await event.respond(msg)

@bot.on(events.NewMessage(pattern=r"^/restart$"))
async def restart_handler(event):
    if not is_owner(event.chat_id): return
    await event.respond("🔄 **جاري إعادة تشغيل سكريبت البوت...**")
    os.execl(sys.executable, sys.executable, *sys.argv)

@bot.on(events.CallbackQuery(pattern=r"^cancel_"))
async def cancel_callback_handler(event):
    if not await is_allowed(event):
        await event.answer("⚠️ ليس لديك صلاحية استخدام هذا البوت.", alert=True)
        return
    data = event.data.decode("utf-8").split("_")
    task_id = "_".join(data[1:])
    if task_id in ACTIVE_CANCEL_EVENTS:
        ACTIVE_CANCEL_EVENTS[task_id].set()
        await event.answer("🛑 جاري إلغاء العملية...", alert=True)
    else:
        await event.answer("⚠️ العملية غير موجودة أو انتهت بالفعل.", alert=True)

# معالج مخصص لاكتشاف الروابط وتوجيه التنزيل
@bot.on(events.NewMessage)
async def handle_user_input_and_urls(event):
    if not await is_allowed(event):
        if event.text and (event.text.startswith("http") or event.text.startswith("/")):
            await event.respond("🔒 **عذراً، هذا البوت مخصص للاستخدام الخاص ولا يمتلك حسابك صلاحية الوصول.**")
        return

    chat_id = event.chat_id

    if WAITING_FOR_USERNAME.get(chat_id):
        WAITING_FOR_USERNAME.pop(chat_id, None)
        input_text = event.text.strip()
        if await add_allowed_user(input_text):
            clean_u = input_text.lstrip('@')
            await event.respond(f"✅ **تمت إضافة العضو `@{clean_u}` بنجاح وإعطاؤه صلاحية استخدام البوت!**")
        else:
            await event.respond("❌ **تعذر إضافة اسم المستخدم، يرجى التأكد من كتابته بشكل صحيح.**")
        return

    if event.text and event.text.startswith("/"):
        return

    raw_text = event.text or ""
    urls = re.findall(r"https?://\S+|/video/\S+", raw_text)
    if not urls: return
    
    for u in urls:
        clean_u = clean_url(u) if not is_whatsapp_url(u) else u

        if is_profile_url(clean_u):
            task_key = f"prof_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "profile")
            buttons = [
                [
                    Button.inline("👤 تحميل الصورة الشخصية (صورة)", data=f"prof_img_{task_key}"),
                    Button.inline("📄 تحميل كـ مستند (JPG)", data=f"prof_doc_{task_key}")
                ]
            ]
            await event.respond("👤 **تم التعرف على رابط ملف شخصي. اختر طريقة تنزيل الصورة:**", buttons=buttons)

        elif not is_complex_url(clean_u):
            task_key = f"dir_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "direct")
            buttons = [
                [
                    Button.inline("🎬 MP4", data=f"dir_mp4_{task_key}"),
                    Button.inline("🎵 MP3", data=f"dir_mp3_{task_key}"),
                    Button.inline("📄 مستند", data=f"dir_doc_{task_key}")
                ]
            ]
            await event.respond("📌 **تم رصد رابط مباشر. اختر صيغة التحميل المناسبة:**", buttons=buttons)
            
        else:
            user_config = await get_user_config(chat_id)
            asyncio.create_task(
                start_direct_execution(
                    chat_id=chat_id,
                    url=clean_u,
                    filename=get_clean_filename(clean_u),
                    as_doc=False,
                    quality=user_config["quality"],
                    target_fmt='mp4'
                )
            )

@bot.on(events.CallbackQuery(pattern=r"^(dir_|ig_|tt_|q_|prof_)"))
async def global_callback_handler(event):
    if not await is_allowed(event): return
    data = event.data.decode("utf-8").split("_")
    prefix = data[0]
    choice = data[1]
    task_key = "_".join(data[2:])
    
    url, _ = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار، يرجى إعادة إرسال الرابط.", alert=True)
        return
        
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    target_fmt = 'mp3' if choice == 'mp3' else 'mp4'
    
    status_msg = await event.edit("⏳ **جاري معالجة الطلب وبدء التحميل...**", buttons=None)

    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=as_doc,
            quality='best' if choice not in ['480', '720', '1080'] else choice,
            target_fmt=target_fmt,
            status_msg=status_msg,
            is_avatar_task=(prefix == 'prof')
        )
    )

def main():
    print("🚀 جاري تشغيل بوت التنزيل الخاص (إصدار 2026)...")
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
