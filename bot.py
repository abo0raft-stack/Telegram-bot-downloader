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
import subprocess
import asyncio
import aiohttp
import aiosqlite
import psutil
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- التثبيت التلقائي للمكتبات الأساسية ---
def auto_install_packages():
    try:
        subprocess.run(
            ["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow", "aiohttp",
             "aiosqlite", "telethon", "requests", "psutil", "beautifulsoup4", "aria2p"],
            check=True, capture_output=True
        )
        print("✅ تم تثبيت جميع المكتبات.")
    except Exception as e:
        print(f"⚠️ فشل التثبيت: {e}")

auto_install_packages()

import yt_dlp
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- الثوابت والإعدادات العامة ---
VERSION = "v87.0-Enhanced-2026"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "5414125521"))
PORT = int(os.environ.get("PORT", 8080))
MAX_CONCURRENT_TASKS = asyncio.Semaphore(5)  # رفع الحد إلى 5

DB_FILE = "bot_database.db"
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"
ARIA2_RPC = os.environ.get("ARIA2_RPC", "http://localhost:6800/jsonrpc")

FONT_SIZE_MAP = {
    "small": 2.18,
    "medium": 3.25,
    "large": 5.35,
    "xlarge": 10.45
}

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8,ar;q=0.8',
    'Referer': 'https://www.google.com/',
    'Connection': 'keep-alive'
}

ACTIVE_CANCEL_EVENTS = {}
WAITING_FOR_USERNAME = {}

# --- إعداد الكوكيز من المتغيرات البيئية ---
def setup_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
        except Exception:
            pass
    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
        except Exception:
            pass

setup_cookies()

# --- قاعدة البيانات ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                snapshots INTEGER,
                social_snapshots INTEGER,
                quality TEXT,
                font_size TEXT,
                download_format TEXT,
                parallel_downloads INTEGER
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
        async with conn.execute(
            "SELECT snapshots, social_snapshots, quality, font_size, download_format, parallel_downloads "
            "FROM user_settings WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "snapshots": bool(row[0]),
                    "social_snapshots": bool(row[1]),
                    "quality": row[2] or "720",
                    "font_size": row[3] or "large",
                    "download_format": row[4] or "mp4",
                    "parallel_downloads": row[5] or 4
                }
            else:
                default = {
                    "snapshots": True,
                    "social_snapshots": False,
                    "quality": "720",
                    "font_size": "large",
                    "download_format": "mp4",
                    "parallel_downloads": 4
                }
                await set_user_config(chat_id, default)
                return default

async def set_user_config(chat_id, config):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO user_settings
            (chat_id, snapshots, social_snapshots, quality, font_size, download_format, parallel_downloads)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            chat_id,
            int(config["snapshots"]),
            int(config["social_snapshots"]),
            config["quality"],
            config["font_size"],
            config.get("download_format", "mp4"),
            config.get("parallel_downloads", 4)
        ))
        await conn.commit()

async def save_task(task_key, url, task_type):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO pending_tasks VALUES (?, ?, ?, ?)",
            (task_key, url, task_type, time.time())
        )
        await conn.commit()

async def pop_task(task_key):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute(
            "SELECT url, task_type FROM pending_tasks WHERE task_key = ?", (task_key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await conn.execute("DELETE FROM pending_tasks WHERE task_key = ?", (task_key,))
                await conn.commit()
                return row[0], row[1]
    return None, None

# --- إدارة الأعضاء ---
async def add_allowed_user(username):
    username = username.strip().lstrip('@').lower()
    if not username:
        return False
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
        allowed = await get_allowed_users()
        if username in allowed:
            return True
    return False

# --- أدوات مساعدة ---
def clean_download_folder():
    folder = "downloads"
    cleaned = 0
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    cleaned += os.path.getsize(fp)
                    os.remove(fp)
                except Exception:
                    pass
            for d in dirs:
                try:
                    shutil.rmtree(os.path.join(root, d))
                except Exception:
                    pass
    else:
        os.makedirs(folder, exist_ok=True)
    gc.collect()
    return cleaned

clean_download_folder()

def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
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

def is_owner(chat_id):
    return chat_id == OWNER_ID

# --- كشف المنصات ---
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

def is_dailymotion_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['dailymotion.com', 'dai.ly']) or '/video/' in url.lower()

def is_facebook_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['facebook.com', 'fb.watch'])

def is_whatsapp_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['wa.me', 'whatsapp.com', 'api.whatsapp.com'])

def is_pinterest_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['pinterest.com', 'pin.it'])

def is_reddit_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['reddit.com', 'redd.it'])

def is_vk_url(url):
    domain = urlparse(url).netloc.lower()
    return 'vk.com' in domain

def is_tumblr_url(url):
    domain = urlparse(url).netloc.lower()
    return 'tumblr.com' in domain

def is_complex_url(url):
    domains = [
        'twitter.com', 'x.com', 'instagram.com', 'instagr.am',
        'tiktok.com', 'vt.tiktok.com', 'facebook.com', 'fb.watch',
        'reddit.com', 'redd.it', 'pinterest.com', 'pin.it',
        'youtube.com', 'youtu.be', 'dailymotion.com', 'dai.ly',
        'wa.me', 'whatsapp.com', 'vk.com', 'tumblr.com'
    ]
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in domains) or is_dailymotion_url(url)

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

def clean_url(url):
    if is_dailymotion_url(url):
        if not url.startswith('http'):
            return f"https://www.dailymotion.com{url}"
        return url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def get_clean_filename(url):
    if is_complex_url(url):
        return "media_download"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'):
        return filename.replace('.m3u8', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv', '.jpg', '.jpeg', '.png', '.webp']):
        return "downloaded_media"
    return filename

# --- وظائف معالجة الفيديو والصورة ---
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
        try:
            os.remove(input_path)
        except:
            pass
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
            except:
                pass
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration:stream=width,height,rotation',
            '-of', 'json', file_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
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
        print(f"Error adding text: {e}")

async def extract_9_frames(video_path, duration, chat_id=None):
    frames = []
    if duration <= 0:
        return frames
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
        cmd = ['ffmpeg', '-y', '-ss', str(ts), '-i', video_path, '-vframes', '1', '-q:v', '2', out_name]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if os.path.exists(out_name) and os.path.getsize(out_name) > 0:
            add_transparent_text_center(out_name, time_str, font_ratio=font_ratio)
            frames.append(out_name)
    return frames

async def split_video_file(filepath, max_size_mb=1950):
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return [filepath]
    duration, _, _, _ = await get_video_metadata_and_thumb(filepath)
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
        cmd = ['ffmpeg', '-y', '-ss', str(start_sec), '-t', str(segment_time), '-i', filepath, '-c', 'copy', out_part]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if os.path.exists(out_part) and os.path.getsize(out_part) > 0:
            split_files.append(out_part)
    if split_files:
        try:
            os.remove(filepath)
        except:
            pass
        return split_files
    return [filepath]

async def trim_video_clip(filepath, start_time, end_time):
    base_dir = os.path.dirname(filepath)
    filename_without_ext, ext = os.path.splitext(os.path.basename(filepath))
    out_trimmed = os.path.join(base_dir, f"{filename_without_ext}_trimmed{ext}")
    cmd = ['ffmpeg', '-y', '-ss', str(start_time), '-to', str(end_time), '-i', filepath, '-c', 'copy', out_trimmed]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if os.path.exists(out_trimmed) and os.path.getsize(out_trimmed) > 0:
        try:
            os.remove(filepath)
        except:
            pass
        return out_trimmed
    return filepath

async def deep_sanitize_image(file_path):
    try:
        base_dir = os.path.dirname(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        cleaned_path = os.path.join(base_dir, f"{base_name}_clean.jpg")
        with Image.open(file_path) as img:
            rgb_img = img.convert('RGB')
            w, h = rgb_img.size
            if w % 2 != 0:
                w -= 1
            if h % 2 != 0:
                h -= 1
            if w > 0 and h > 0:
                rgb_img = rgb_img.resize((w, h), Image.Resampling.LANCZOS)
            rgb_img.save(cleaned_path, 'JPEG', quality=95)
        ffmpeg_path = os.path.join(base_dir, f"{base_name}_final.jpg")
        cmd = ['ffmpeg', '-y', '-i', cleaned_path, '-map_metadata', '-1', '-vf', 'format=yuv420p', '-q:v', '2', ffmpeg_path]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=15)
        target_file = ffmpeg_path if (os.path.exists(ffmpeg_path) and os.path.getsize(ffmpeg_path) > 100) else cleaned_path
        if os.path.exists(file_path) and file_path != target_file:
            try:
                os.remove(file_path)
            except:
                pass
        if os.path.exists(cleaned_path) and cleaned_path != target_file:
            try:
                os.remove(cleaned_path)
            except:
                pass
        return target_file
    except Exception as e:
        print(f"Image sanitize error: {e}")
        return file_path

# --- محرك الأفاتار المحسن (v87) ---
async def download_profile_avatar(url, task_dir):
    try:
        # 1. واتساب
        if is_whatsapp_url(url):
            async with aiohttp.ClientSession() as session:
                headers = BROWSER_HEADERS.copy()
                headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                async with session.get(url, headers=headers, timeout=20) as r:
                    if r.status == 200:
                        html = await r.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
                        if og_img and og_img.get("content"):
                            img_url = og_img["content"]
                            img_url = unquote(img_url).replace('&amp;', '&')
                            async with session.get(img_url, headers=headers, timeout=20) as img_res:
                                if img_res.status == 200:
                                    filepath = os.path.join(task_dir, "profile_avatar.jpg")
                                    with open(filepath, 'wb') as f:
                                        f.write(await img_res.read())
                                    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                                        return True

        # 2. gallery-dl
        cmd = [
            "gallery-dl",
            "--directory", task_dir,
            "--filename", "profile_avatar.{ext}",
            "--option", "extractor.instagram.include=avatar",
            "--option", "extractor.twitter.include=avatar",
            "--option", "extractor.facebook.include=avatar",
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
        except:
            pass
        if any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir)):
            return True

        # 3. yt-dlp thumbnail
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
        except:
            pass
        if any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir)):
            return True

        # 4. Cobalt API
        api_url = "https://api.cobalt.tools/api/json"
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": BROWSER_HEADERS['User-Agent']}
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json={"url": url}, headers=headers, timeout=15) as res:
                if res.status == 200:
                    data = await res.json()
                    img_url = data.get("url") or (data.get("picker", [{}])[0].get("url") if data.get("picker") else None)
                    if img_url:
                        async with session.get(img_url, headers=BROWSER_HEADERS, timeout=20) as r:
                            if r.status == 200:
                                filepath = os.path.join(task_dir, "profile_avatar.jpg")
                                with open(filepath, 'wb') as f:
                                    f.write(await r.read())
                                return True

        # 5. محاولة استخراج الصورة الشخصية عبر requests مباشرة (للحسابات العامة)
        if is_instagram_url(url):
            # استخدام endpoint غير رسمي لجلب الصورة الشخصية
            username = urlparse(url).path.strip('/').split('/')[0]
            if username:
                profile_pic_url = f"https://www.instagram.com/{username}/?__a=1"
                async with aiohttp.ClientSession() as session:
                    async with session.get(profile_pic_url, headers=BROWSER_HEADERS) as r:
                        if r.status == 200:
                            data = await r.json()
                            pic_url = data.get('graphql', {}).get('user', {}).get('profile_pic_url_hd')
                            if pic_url:
                                async with session.get(pic_url, headers=BROWSER_HEADERS) as img_res:
                                    if img_res.status == 200:
                                        filepath = os.path.join(task_dir, "profile_avatar.jpg")
                                        with open(filepath, 'wb') as f:
                                            f.write(await img_res.read())
                                        return True
    except Exception as e:
        print(f"v87 Avatar Engine Error: {e}")

    return any(os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 1000 for f in os.listdir(task_dir))

# --- تحميل Dailymotion خاص ---
async def download_dailymotion_video(event, url, quality_choice, status_msg):
    chat_id = event.chat_id
    task_id = f"dm_{int(time.time() * 1000)}"
    cancel_event = threading.Event()
    ACTIVE_CANCEL_EVENTS[task_id] = cancel_event

    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]
    if not status_msg:
        status_msg = await event.respond("⏳ **جاري بدء التحميل من Dailymotion...**", buttons=cancel_btn)
    else:
        await status_msg.edit("⏳ **جاري بدء التحميل من Dailymotion...**", buttons=cancel_btn)

    loop = asyncio.get_event_loop()
    last_update_time = [0]

    def progress_hook(d):
        if cancel_event.is_set():
            raise Exception("CANCELLED")
        if d['status'] == 'downloading':
            now = time.time()
            if now - last_update_time[0] >= 2.0:
                last_update_time[0] = now
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                speed = d.get('speed', 0) or 0
                eta = d.get('eta', 0) or 0
                percentage = (downloaded / total * 100) if total > 0 else 0
                completed = int(percentage // 10)
                bar = "█" * completed + "░" * (10 - completed)
                text = (
                    f"📥 **جاري تنزيل الفيديو من Dailymotion...**\n\n"
                    f"[{bar}] {percentage:.1f}%\n"
                    f"🚀 **السرعة:** {format_size(speed)}/s\n"
                    f"📦 **الحجم:** {format_size(downloaded)} / {format_size(total)}\n"
                    f"⏱️ **المتبقي:** {format_time(eta)}"
                )
                asyncio.run_coroutine_threadsafe(status_msg.edit(text, buttons=cancel_btn), loop)

    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)

    headers = BROWSER_HEADERS.copy()
    headers['Referer'] = 'https://www.dailymotion.com/'
    fmt = 'bestaudio/best' if quality_choice == 'mp3' else (
        f"bestvideo[height<={quality_choice}][ext=mp4]+bestaudio/best"
        if quality_choice != '1080' else 'bestvideo[ext=mp4]+bestaudio/best'
    )

    ydl_opts = {
        'format': fmt,
        'outtmpl': os.path.join(task_dir, '%(title).30s_%(id)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
        'headers': headers,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'merge_output_format': 'mp4'
    }

    try:
        if cancel_event.is_set():
            raise Exception("CANCELLED")

        def run_dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)

        file_path = await loop.run_in_executor(None, run_dl)
        if cancel_event.is_set():
            raise Exception("CANCELLED")

        file_path = await convert_to_mp4(file_path)

        start_time = time.time()
        last_upload_update = [0]
        await status_msg.edit("📤 **اكتمل التنزيل! جاري التحضير للرفع...**", buttons=cancel_btn)

        async def upload_progress_callback(current, total):
            if cancel_event.is_set():
                raise Exception("CANCELLED")
            now = time.time()
            if now - last_upload_update[0] >= 2.0 or current == total:
                last_upload_update[0] = now
                elapsed = now - start_time
                speed = current / elapsed if elapsed > 0 else 0
                percentage = (current / total * 100) if total > 0 else 0
                completed = int(percentage // 10)
                bar = "█" * completed + "░" * (10 - completed)
                eta = (total - current) / speed if speed > 0 else 0
                text = (
                    f"📤 **جاري رفع الفيديو إلى تلجرام...**\n\n"
                    f"[{bar}] {percentage:.1f}%\n"
                    f"🚀 **السرعة:** {format_size(speed)}/s\n"
                    f"📦 **الحجم:** {format_size(current)} / {format_size(total)}\n"
                    f"⏱️ **المتبقي:** {format_time(eta)}"
                )
                try:
                    await status_msg.edit(text, buttons=cancel_btn)
                except:
                    pass

        duration, width, height, thumb_path = await get_video_metadata_and_thumb(file_path)
        attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]

        await bot.send_file(
            chat_id,
            file_path,
            caption="🎥 **تم التنزيل بنجاح من Dailymotion!**",
            thumb=thumb_path,
            attributes=attr,
            supports_streaming=True,
            progress_callback=upload_progress_callback
        )

        user_config = await get_user_config(chat_id)
        if duration > 0 and user_config.get("snapshots", True):
            await status_msg.edit("📸 **جاري التقاط 9 صور من الفيديو...**")
            frames = await extract_9_frames(file_path, duration, chat_id=chat_id)
            if frames:
                await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات المصورة من الفيديو:**", album=True)
                for fr in frames:
                    try:
                        os.remove(fr)
                    except:
                        pass

        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except:
                pass

        await status_msg.delete()

    except Exception as e:
        if str(e) == "CANCELLED":
            await status_msg.edit("🛑 **تم إلغاء العملية بناءً على طلبك.**", buttons=None)
        else:
            await status_msg.edit(f"❌ **حدث خطأ أثناء العملية:**\n`{str(e)}`", buttons=None)

    finally:
        ACTIVE_CANCEL_EVENTS.pop(task_id, None)
        if os.path.exists(task_dir):
            try:
                shutil.rmtree(task_dir)
            except:
                pass
        gc.collect()

# --- محرك التحميل المباشر المتوازي (جديد) ---
async def download_direct_parallel(chat_id, url, filepath, status_msg, cancel_event, task_id, num_connections=4):
    """تحميل ملفات كبيرة باستخدام اتصالات متوازية (جزئية) مع إمكانية الاستئناف."""
    headers = BROWSER_HEADERS.copy()
    session = aiohttp.ClientSession(headers=headers)

    try:
        # الحصول على حجم الملف أولاً
        async with session.head(url, ssl=False) as resp:
            if resp.status not in (200, 206):
                raise Exception(f"HTTP {resp.status}")
            total_size = int(resp.headers.get('content-length', 0))
            if total_size == 0:
                # تحميل عادي
                async with session.get(url, ssl=False) as r:
                    if r.status != 200:
                        raise Exception(f"HTTP {r.status}")
                    with open(filepath, 'wb') as f:
                        downloaded = 0
                        start_time = time.time()
                        last_update = 0
                        async for chunk in r.content.iter_chunked(256*1024):
                            if cancel_event.is_set():
                                raise Exception("CANCELLED")
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if now - last_update > 1.5:
                                last_update = now
                                speed = downloaded / (now - start_time) if (now - start_time) > 0 else 0
                                percent = 100
                                bar = "██████████"
                                text = (
                                    f"📥 **تحميل مباشر...**\n"
                                    f"[{bar}] 100%\n"
                                    f"📦 الحجم: `{format_size(downloaded)}`\n"
                                    f"⚡ السرعة: `{format_size(speed)}/s`"
                                )
                                await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")])
                return

        # تحميل متوازي باستخدام أجزاء
        part_size = min(10 * 1024 * 1024, total_size // num_connections + 1)  # 10MB per chunk
        ranges = []
        for i in range(0, total_size, part_size):
            end = min(i + part_size - 1, total_size - 1)
            ranges.append((i, end))

        # فتح الملف للكتابة في الوضع الثنائي
        with open(filepath, 'wb') as f:
            f.truncate(total_size)  # حجز المساحة

        # إنشاء مهام التحميل
        async def download_part(start, end, part_num):
            headers_range = headers.copy()
            headers_range['Range'] = f'bytes={start}-{end}'
            async with session.get(url, headers=headers_range, ssl=False) as resp:
                if resp.status not in (200, 206):
                    raise Exception(f"Part {part_num} failed with status {resp.status}")
                data = await resp.read()
                # كتابة في الموضع الصحيح
                with open(filepath, 'r+b') as f_part:
                    f_part.seek(start)
                    f_part.write(data)
                return len(data)

        tasks = []
        for idx, (start, end) in enumerate(ranges):
            tasks.append(download_part(start, end, idx + 1))

        # تقدم التحميل
        downloaded = 0
        start_time = time.time()
        last_update = 0
        while tasks:
            # تحديث التقدم كل ثانية
            await asyncio.sleep(0.5)
            if cancel_event.is_set():
                raise Exception("CANCELLED")
            # لا يمكننا تتبع التقدم بسهولة مع الكتابة المباشرة، لذلك نقيس حجم الملف
            current_size = os.path.getsize(filepath)
            if current_size > total_size:
                current_size = total_size
            downloaded = current_size
            now = time.time()
            if now - last_update > 1.5:
                last_update = now
                speed = downloaded / (now - start_time) if (now - start_time) > 0 else 0
                percent = (downloaded / total_size * 100) if total_size > 0 else 0
                completed = int(percent // 10)
                bar = "█" * completed + "░" * (10 - completed)
                rem_time = (total_size - downloaded) / speed if speed > 0 and total_size > 0 else 0
                text = (
                    f"📥 **تحميل متوازي (4 أجزاء)...**\n"
                    f"[{bar}] {percent:.1f}%\n"
                    f"📦 الحجم: `{format_size(downloaded)}` / `{format_size(total_size)}`\n"
                    f"⚡ السرعة: `{format_size(speed)}/s`\n"
                    f"⏳ المتبقي: `{format_time(rem_time)}`"
                )
                await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{task_id}")])

            # التحقق من انتهاء جميع المهام
            if all(task.done() for task in tasks):
                break

        # انتظار اكتمال جميع المهام
        await asyncio.gather(*tasks)
        # التأكد من حجم الملف
        final_size = os.path.getsize(filepath)
        if final_size != total_size:
            raise Exception("التحميل غير مكتمل، الحجم غير متطابق")

    except Exception as e:
        raise
    finally:
        await session.close()

# --- دالة التنزيل الرئيسية (معدلة) ---
async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best',
                                 media_msg=None, target_fmt='mp4', status_msg=None,
                                 trim_times=None, is_avatar_task=False, is_playlist=False):
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

            if cancel_event.is_set():
                raise Exception("CANCELLED")

            # --- مهمة صورة شخصية ---
            if is_avatar_task:
                await status_msg.edit("👤 **جاري استخراج صورة الملف الشخصي عبر المحرك الجديد (v87)...**", buttons=cancel_btn)
                success = await download_profile_avatar(target_url, task_dir)
                if not success:
                    raise Exception("لم نتمكن من جلب الصورة الشخصية لهذا الحساب.")
                # متابعة الرفع كصورة

            # --- تحميل من منصات اجتماعية ---
            elif target_url and is_social:
                # استخدام yt-dlp مع خيارات محسنة
                await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
                if cancel_event.is_set():
                    raise Exception("CANCELLED")

                downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
                if not downloaded:
                    # محاولة المحركات البديلة
                    if is_instagram_url(target_url):
                        await instagram_carousel_and_photo_engine(target_url, task_dir)
                    elif is_tiktok_url(target_url):
                        await tiktok_multi_engine(target_url, task_dir)
                    elif is_dailymotion_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "dm")
                    elif is_pinterest_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "pin")
                    elif is_reddit_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "reddit")
                    elif is_facebook_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "fb")
                    elif is_vk_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "vk")
                    elif is_tumblr_url(target_url):
                        await gallery_dl_fallback_engine(target_url, task_dir, "tumblr")

            # --- تحميل مباشر (غير اجتماعي) ---
            elif target_url:
                filepath = os.path.join(task_dir, filename)
                parallel = user_config.get("parallel_downloads", 4)

                # إذا كان الملف كبيراً، استخدم التحميل المتوازي
                # نحاول الحصول على حجم الملف أولاً
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.head(target_url, headers=BROWSER_HEADERS, ssl=False) as resp:
                            if resp.status == 200:
                                total_size = int(resp.headers.get('content-length', 0))
                                if total_size > 50 * 1024 * 1024 and parallel > 1:
                                    await download_direct_parallel(chat_id, target_url, filepath, status_msg, cancel_event, task_id, parallel)
                                else:
                                    await download_direct_async(chat_id, target_url, filepath, status_msg, cancel_event, task_id)
                            else:
                                # fallback
                                await download_direct_async(chat_id, target_url, filepath, status_msg, cancel_event, task_id)
                except:
                    # fallback
                    await download_direct_async(chat_id, target_url, filepath, status_msg, cancel_event, task_id)

                if cancel_event.is_set():
                    raise Exception("CANCELLED")

                if trim_times:
                    await status_msg.edit("✂️ **جاري قص المقطع...**")
                    filepath = await trim_video_clip(filepath, trim_times[0], trim_times[1])

                await status_msg.edit("📤 **جاري تجهيز الرفع...**", buttons=cancel_btn)

                if target_fmt == 'mp3':
                    base, _ = os.path.splitext(filepath)
                    mp3_path = base + ".mp3"
                    proc = await asyncio.create_subprocess_exec('ffmpeg', '-y', '-i', filepath, '-vn', '-ab', '320k', mp3_path,
                                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await proc.wait()
                    if os.path.exists(mp3_path):
                        filepath = mp3_path
                else:
                    filepath = await convert_to_mp4(filepath)

                video_files = await split_video_file(filepath)

                upload_start_time = time.time()
                last_upload_update = 0

                async def upload_progress_callback(current, total):
                    if cancel_event.is_set():
                        raise Exception("CANCELLED")
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
                            f"📤 **جاري رفع الملف...**\n"
                            f"[{bar}] {percent:.1f}%\n"
                            f"📦 الحجم: `{format_size(current)}` / `{format_size(total)}`\n"
                            f"⚡ السرعة: `{format_size(speed)}/s`\n"
                            f"⏳ المتبقي: `{format_time(rem_time)}`"
                        )
                        try:
                            await status_msg.edit(text, buttons=cancel_btn)
                        except:
                            pass

                for idx, vid_file in enumerate(video_files, start=1):
                    part_caption = f" (Part {idx}/{len(video_files)})" if len(video_files) > 1 else ""
                    if as_doc:
                        await bot.send_file(chat_id, vid_file, force_document=True, caption=part_caption,
                                            progress_callback=upload_progress_callback)
                    else:
                        ext = os.path.splitext(vid_file)[1].lower()
                        if ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
                            duration, width, height, thumb_path = await get_video_metadata_and_thumb(vid_file)
                            attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                            await bot.send_file(chat_id, vid_file, caption=part_caption, thumb=thumb_path,
                                                attributes=attr, progress_callback=upload_progress_callback)

                            should_take_snaps = user_config.get("snapshots", True) and (
                                    not is_social or user_config.get("social_snapshots", False))
                            if duration > 0 and should_take_snaps and idx == 1:
                                await status_msg.edit("📸 **جاري التقاط 9 صور من الفيديو...**")
                                frames = await extract_9_frames(vid_file, duration, chat_id=chat_id)
                                if frames:
                                    await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات المصورة:**", album=True)
                                    for fr in frames:
                                        try:
                                            os.remove(fr)
                                        except:
                                            pass
                            if thumb_path and os.path.exists(thumb_path):
                                try:
                                    os.remove(thumb_path)
                                except:
                                    pass
                        elif ext in ['.mp3', '.wav', '.m4a', '.aac']:
                            await bot.send_file(chat_id, vid_file, caption=part_caption,
                                                progress_callback=upload_progress_callback)
                        else:
                            await bot.send_file(chat_id, vid_file, caption=part_caption, force_document=True,
                                                progress_callback=upload_progress_callback)
                await status_msg.delete()
                return

            elif media_msg:
                filepath = os.path.join(task_dir, filename)
                await bot.download_media(media_msg, file=filepath)

            if cancel_event.is_set():
                raise Exception("CANCELLED")

            # --- تجميع الملفات التي تم تنزيلها ---
            downloaded_files = []
            for root, _, files in os.walk(task_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    if os.path.isfile(fpath) and os.path.getsize(fpath) > 500 and not file.endswith('_thumb.jpg') and not file.endswith('_clean.jpg') and not file.endswith('_final.jpg') and not file.startswith('frame_'):
                        base_name, current_ext = os.path.splitext(file)
                        current_ext = current_ext.lower()
                        if "none" in file.lower() or current_ext not in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mkv', '.mov', '.avi', '.mp3', '.m4a', '.webm']:
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
                            fpath = await deep_sanitize_image(fpath)
                        downloaded_files.append(fpath)

            if not downloaded_files:
                raise Exception("تعذر الوصول إلى المحتوى. تأكد من صحة الرابط.")

            await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر)...**", buttons=cancel_btn)

            photos, videos, audio_files, other_files = [], [], [], []
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm')
            image_extensions = ('.jpg', '.jpeg', '.png')
            audio_extensions = ('.mp3', '.m4a', '.wav', '.flac', '.aac', '.opus')

            for fpath in sorted(downloaded_files):
                ext = os.path.splitext(fpath)[1].lower()
                if ext in image_extensions:
                    photos.append(fpath)
                elif ext in video_extensions:
                    videos.append(fpath)
                elif ext in audio_extensions:
                    audio_files.append(fpath)
                else:
                    other_files.append(fpath)

            # رفع الصور
            if photos:
                if len(photos) > 1 and not as_doc:
                    try:
                        for chunk_idx in range(0, len(photos), 10):
                            chunk_photos = photos[chunk_idx:chunk_idx + 10]
                            await bot.send_file(
                                chat_id,
                                chunk_photos,
                                caption=f"📸 **ألبوم الصور ({chunk_idx+1}-{chunk_idx+len(chunk_photos)}/{len(photos)}):**",
                                album=True
                            )
                    except:
                        for idx, p in enumerate(photos, start=1):
                            await bot.send_file(chat_id, p, caption=f"📸 **صورة ({idx}/{len(photos)}):**")
                else:
                    for idx, p in enumerate(photos, start=1):
                        try:
                            base_p = os.path.splitext(p)[0] + ".jpg"
                            if p != base_p:
                                os.rename(p, base_p)
                                p = base_p
                            if as_doc:
                                await bot.send_file(chat_id, p, caption=f"📄 **مستند صورة ({idx}/{len(photos)}):**", force_document=True)
                            else:
                                cap = "👤 **تم تنزيل الصورة الشخصية بنجاح!**" if is_avatar_task else (
                                    f"📸 **صورة ({idx}/{len(photos)}):**" if len(photos) > 1 else "📸 **تم تنزيل الصورة بنجاح!**")
                                await bot.send_file(chat_id, p, caption=cap, force_document=False)
                        except:
                            await bot.send_file(chat_id, p, force_document=True)

            # رفع الفيديوهات
            for vid in videos:
                if cancel_event.is_set():
                    raise Exception("CANCELLED")
                vid = await convert_to_mp4(vid)
                if trim_times:
                    vid = await trim_video_clip(vid, trim_times[0], trim_times[1])
                split_vids = await split_video_file(vid)
                for idx, part_vid in enumerate(split_vids, start=1):
                    part_caption = f" (Part {idx}/{len(split_vids)})" if len(split_vids) > 1 else ""
                    duration, width, height, thumb_path = await get_video_metadata_and_thumb(part_vid)
                    attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                    await bot.send_file(
                        chat_id,
                        part_vid,
                        caption=f"🎥 **تم تنزيل الفيديو بنجاح!**{part_caption}\n📁 `{os.path.basename(part_vid)}`",
                        force_document=as_doc,
                        thumb=thumb_path,
                        attributes=attr,
                        supports_streaming=True
                    )
                    should_take_snaps = user_config.get("snapshots", True) and (
                                not is_social or user_config.get("social_snapshots", False))
                    if duration > 0 and should_take_snaps and idx == 1:
                        await status_msg.edit("📸 **جاري توليد ألبوم اللقطات...**")
                        frames = await extract_9_frames(part_vid, duration, chat_id=chat_id)
                        if frames:
                            await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات 9 المصورة:**", album=True)
                            for fr in frames:
                                try:
                                    os.remove(fr)
                                except:
                                    pass
                    if thumb_path and os.path.exists(thumb_path):
                        try:
                            os.remove(thumb_path)
                        except:
                            pass

            # رفع الملفات الصوتية والأخرى
            for aud in audio_files:
                if cancel_event.is_set():
                    raise Exception("CANCELLED")
                await bot.send_file(chat_id, aud, caption=f"🎵 **تم استخراج الملف الصوتي:**\n📁 `{os.path.basename(aud)}`")

            for oth in other_files:
                if cancel_event.is_set():
                    raise Exception("CANCELLED")
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
                try:
                    shutil.rmtree(task_dir)
                except:
                    pass
            gc.collect()

# --- دوال مساعدة للتحميل (yt-dlp, gallery-dl, إلخ) ---
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
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
    else:
        ydl_opts['merge_output_format'] = 'mp4'
        if quality != 'best':
            ydl_opts['format'] = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        else:
            ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]

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

async def tiktok_multi_engine(url, task_dir):
    await gallery_dl_fallback_engine(url, task_dir, "tt")
    if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
        return
    try:
        api_url = "https://api.cobalt.tools/api/json"
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": BROWSER_HEADERS['User-Agent']}
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json={"url": url}, headers=headers, timeout=15) as res:
                if res.status == 200:
                    data = await res.json()
                    status = data.get("status")
                    urls_to_dl = []
                    if status in ["stream", "redirect"]:
                        urls_to_dl.append(data.get("url"))
                    elif status == "picker":
                        for item in data.get("picker", []):
                            urls_to_dl.append(item.get("url"))
                    for idx, media_url in enumerate(urls_to_dl, start=1):
                        if not media_url:
                            continue
                        async with session.get(media_url, headers=BROWSER_HEADERS, timeout=30) as r:
                            if r.status == 200:
                                is_vid = ".mp4" in media_url or "video" in r.headers.get("content-type", "")
                                ext = "mp4" if is_vid else "jpg"
                                filepath = os.path.join(task_dir, f"tt_photo_{idx}.{ext}")
                                with open(filepath, 'wb') as f:
                                    f.write(await r.read())
    except Exception as e:
        print(f"TikTok Cobalt API error: {e}")

async def instagram_carousel_and_photo_engine(url, task_dir):
    try:
        cmd = [
            "gallery-dl",
            "--directory", task_dir,
            "--filename", "ig_{id}_{num}.{ext}",
            "--option", "extractor.instagram.post-quality=max",
            "--option", "extractor.instagram.include=posts",
            "--user-agent", BROWSER_HEADERS['User-Agent']
        ]
        if os.path.exists(INSTAGRAM_COOKIES_FILE):
            cmd.extend(["--cookies", INSTAGRAM_COOKIES_FILE])
        cmd.append(url)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=40)
        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return
        loop = asyncio.get_event_loop()
        ydl_opts = {
            'outtmpl': os.path.join(task_dir, 'ig_hd_%(id)s_%(autonumber)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'headers': BROWSER_HEADERS,
            'writethumbnails': True,
            'format': 'best',
            'merge_output_format': 'mp4'
        }
        if os.path.exists(INSTAGRAM_COOKIES_FILE):
            ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE
        await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return
        api_url = "https://api.cobalt.tools/api/json"
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": BROWSER_HEADERS['User-Agent']}
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json={"url": url, "downloadMode": "max"}, headers=headers, timeout=15) as res:
                if res.status == 200:
                    data = await res.json()
                    status = data.get("status")
                    urls_to_dl = []
                    if status in ["stream", "redirect"]:
                        urls_to_dl.append(data.get("url"))
                    elif status == "picker":
                        for item in data.get("picker", []):
                            urls_to_dl.append(item.get("url"))
                    for idx, media_url in enumerate(urls_to_dl, start=1):
                        if not media_url:
                            continue
                        async with session.get(media_url, headers=BROWSER_HEADERS, timeout=30) as r:
                            if r.status == 200:
                                is_vid = ".mp4" in media_url or "video" in r.headers.get("content-type", "")
                                ext = "mp4" if is_vid else "jpg"
                                filepath = os.path.join(task_dir, f"ig_photo_{idx}.{ext}")
                                with open(filepath, 'wb') as f:
                                    f.write(await r.read())
    except Exception as e:
        print(f"Instagram engine error: {e}")

async def download_direct_async(chat_id, url, filepath, status_msg, cancel_event, task_id):
    """تحميل عادي (غير متوازي) مع استئناف."""
    headers = BROWSER_HEADERS.copy()
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
            chunk_size = 256 * 1024
            cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]
            with open(filepath, mode) as f:
                async for chunk in response.content.iter_chunked(chunk_size):
                    if cancel_event.is_set():
                        raise Exception("CANCELLED")
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
                                f"⏳ المتبقي: `{format_time(rem_time)}`"
                            )
                            try:
                                await status_msg.edit(text, buttons=cancel_btn)
                            except:
                                pass

# --- خادم الصحة (Health Check) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

# --- البوت ---
bot = TelegramClient('bot_session', API_ID, API_HASH)

# --- أوامر البوت الأساسية ---
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    if not await is_allowed(event):
        await event.respond("🔒 **عذراً، هذا البوت مخصص للاستخدام الخاص ولا يمتلك حسابك صلاحية الوصول.**")
        return
    welcome = (
        f"🤖 **مرحباً بك في بوت التنزيل v{ VERSION }**\n\n"
        "🛠️ **الأوامر المتاحة:**\n"
        "• `/settings` - لوحة الإعدادات.\n"
        "• `/clean` - تنظيف الملفات المؤقتة.\n"
        "• `/df` - عرض مساحة التخزين والذاكرة.\n"
        "• `/restart` - إعادة تشغيل البوت (للمالك فقط).\n"
        "• `/trim [بداية] [نهاية] [رابط]` - قص مقطع فيديو.\n\n"
        "📥 **أرسل رابطاً مباشراً أو من منصة اجتماعية لبدء التحميل.**"
    )
    buttons = [[Button.inline("📜 سجل التحديثات", data="show_changelog")]]
    await event.respond(welcome, buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/clean$"))
async def clean_handler(event):
    if not await is_allowed(event):
        return
    freed = clean_download_folder()
    await event.respond(f"🧹 **تم تنظيف المجلدات المؤقتة!**\n💾 المساحة المحررة: `{format_size(freed)}`")

@bot.on(events.NewMessage(pattern=r"^/df$"))
async def disk_status_handler(event):
    if not await is_allowed(event):
        return
    disk = shutil.disk_usage("/")
    mem = psutil.virtual_memory()
    msg = (
        "📊 **حالة السيرفر:**\n\n"
        f"💾 **القرص:**\n"
        f"• الإجمالي: `{format_size(disk.total)}`\n"
        f"• المستهلك: `{format_size(disk.used)}`\n"
        f"• المتبقي: `{format_size(disk.free)}` ({100 - disk.percent:.1f}% فارغ)\n\n"
        f"🧠 **الذاكرة:**\n"
        f"• الإجمالي: `{format_size(mem.total)}`\n"
        f"• المستهلك: `{format_size(mem.used)}` ({mem.percent}%)\n"
        f"• المتاح: `{format_size(mem.available)}`"
    )
    await event.respond(msg)

@bot.on(events.NewMessage(pattern=r"^/restart$"))
async def restart_handler(event):
    if not is_owner(event.chat_id):
        return
    await event.respond("🔄 **جاري إعادة تشغيل البوت...**")
    os.execl(sys.executable, sys.executable, *sys.argv)

@bot.on(events.NewMessage(pattern=r"^/trim\s+(\S+)\s+(\S+)\s+(https?://\S+)"))
async def trim_handler(event):
    if not await is_allowed(event):
        await event.respond("🔒 عذراً، غير مسموح.")
        return
    start_t = event.pattern_match.group(1)
    end_t = event.pattern_match.group(2)
    url = event.pattern_match.group(3)
    chat_id = event.chat_id
    user_config = await get_user_config(chat_id)
    status_msg = await event.respond(f"✂️ **جاري قص المقطع من ({start_t}) إلى ({end_t})...**")
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=False,
            quality=user_config["quality"],
            target_fmt='mp4',
            status_msg=status_msg,
            trim_times=(start_t, end_t)
        )
    )

# --- لوحة الإعدادات ---
async def build_settings_buttons(chat_id):
    config = await get_user_config(chat_id)
    snap_status = "✅ مفعّلة" if config["snapshots"] else "❌ معطلة"
    social_snap_status = "✅ مفعّلة" if config["social_snapshots"] else "❌ معطلة (مباشر فقط)"
    font_labels = {"small": "متوسط", "medium": "كبير جداً", "large": "ضخم (الافتراضي)", "xlarge": "عملاق"}
    buttons = [
        [Button.inline(f"📸 لقطات الفيديو: {snap_status}", data="toggle_snaps")],
        [Button.inline(f"🌐 لقطات منصات التواصل: {social_snap_status}", data="toggle_social_snaps")],
        [
            Button.inline(f"🎥 الجودة: {config['quality']}p", data="change_qual"),
            Button.inline(f"🔤 الخط: {font_labels.get(config['font_size'], 'ضخم')}", data="change_font")
        ],
        [
            Button.inline(f"📦 الصيغة: {config.get('download_format', 'mp4')}", data="change_format"),
            Button.inline(f"⚡ التحميل المتوازي: {config.get('parallel_downloads', 4)}", data="change_parallel")
        ],
        [
            Button.inline("➕ إضافة عضو", data="add_user"),
            Button.inline("👥 قائمة الأعضاء", data="list_users")
        ],
        [Button.inline("❌ إغلاق", data="close_settings")]
    ]
    return buttons

async def send_settings_menu(event):
    chat_id = event.chat_id
    msg = "⚙️ **لوحة الإعدادات:**\nاختر الخيار المناسب."
    buttons = await build_settings_buttons(chat_id)
    await event.respond(msg, buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/settings$"))
async def settings_handler(event):
    if not await is_allowed(event):
        await event.respond("🔒 غير مسموح.")
        return
    await send_settings_menu(event)

@bot.on(events.CallbackQuery(pattern=r"^(toggle_snaps|toggle_social_snaps|change_qual|change_font|change_format|change_parallel|add_user|list_users|close_settings|rmuser_.*)$"))
async def settings_callback_handler(event):
    if not await is_allowed(event):
        await event.answer("⚠️ غير مسموح.", alert=True)
        return
    chat_id = event.chat_id
    data = event.data.decode("utf-8")
    config = await get_user_config(chat_id)

    if data == "toggle_snaps":
        config["snapshots"] = not config["snapshots"]
        await set_user_config(chat_id, config)
        await event.answer("تم التبديل.")
    elif data == "toggle_social_snaps":
        config["social_snapshots"] = not config["social_snapshots"]
        await set_user_config(chat_id, config)
        await event.answer("تم التبديل.")
    elif data == "change_qual":
        qualities = ["480", "720", "1080"]
        current_idx = qualities.index(config["quality"]) if config["quality"] in qualities else 1
        config["quality"] = qualities[(current_idx + 1) % len(qualities)]
        await set_user_config(chat_id, config)
        await event.answer(f"الجودة: {config['quality']}p")
    elif data == "change_font":
        fonts = ["small", "medium", "large", "xlarge"]
        current_idx = fonts.index(config["font_size"]) if config["font_size"] in fonts else 2
        config["font_size"] = fonts[(current_idx + 1) % len(fonts)]
        await set_user_config(chat_id, config)
        await event.answer("تم تغيير حجم الخط.")
    elif data == "change_format":
        formats = ["mp4", "mkv", "avi"]
        current_idx = formats.index(config.get("download_format", "mp4")) if config.get("download_format") in formats else 0
        config["download_format"] = formats[(current_idx + 1) % len(formats)]
        await set_user_config(chat_id, config)
        await event.answer(f"الصيغة: {config['download_format']}")
    elif data == "change_parallel":
        parallels = [1, 2, 4, 8]
        current_idx = parallels.index(config.get("parallel_downloads", 4)) if config.get("parallel_downloads") in parallels else 2
        config["parallel_downloads"] = parallels[(current_idx + 1) % len(parallels)]
        await set_user_config(chat_id, config)
        await event.answer(f"التوازي: {config['parallel_downloads']}")
    elif data == "add_user":
        if not is_owner(chat_id):
            await event.answer("فقط المالك يمكنه الإضافة.", alert=True)
            return
        WAITING_FOR_USERNAME[chat_id] = True
        await event.respond("👤 **أرسل يوزر العضو (مثال: @username):**")
        await event.answer()
        return
    elif data == "list_users":
        allowed = await get_allowed_users()
        if not allowed:
            await event.respond("👥 **لا يوجد أعضاء مضافون.**")
        else:
            msg_u = "👥 **الأعضاء المسموح لهم:**\n\n"
            btns_u = []
            for u in allowed:
                msg_u += f"• `@{u}`\n"
                if is_owner(chat_id):
                    btns_u.append([Button.inline(f"❌ إزالة @{u}", data=f"rmuser_{u}")])
            btns_u.append([Button.inline("⬅️ العودة", data="back_to_settings")])
            await event.respond(msg_u, buttons=btns_u)
        await event.answer()
        return
    elif data.startswith("rmuser_"):
        if not is_owner(chat_id):
            await event.answer("فقط المالك يمكنه الإزالة.", alert=True)
            return
        u_to_rm = data.replace("rmuser_", "")
        await remove_allowed_user(u_to_rm)
        await event.answer(f"تمت إزالة @{u_to_rm}")
        await event.delete()
        return
    elif data == "close_settings":
        await event.delete()
        return

    # تحديث الأزرار
    new_buttons = await build_settings_buttons(chat_id)
    await event.edit("⚙️ **لوحة الإعدادات:**", buttons=new_buttons)

@bot.on(events.CallbackQuery(pattern=r"^back_to_settings$"))
async def back_to_settings_handler(event):
    if not await is_allowed(event):
        return
    await event.delete()
    await send_settings_menu(event)

# --- معالج الإلغاء ---
@bot.on(events.CallbackQuery(pattern=r"^cancel_"))
async def cancel_callback_handler(event):
    if not await is_allowed(event):
        await event.answer("⚠️ غير مسموح.", alert=True)
        return
    data = event.data.decode("utf-8").split("_")
    task_id = "_".join(data[1:])
    if task_id in ACTIVE_CANCEL_EVENTS:
        ACTIVE_CANCEL_EVENTS[task_id].set()
        await event.answer("🛑 جاري الإلغاء...", alert=True)
    else:
        await event.answer("⚠️ العملية غير موجودة.", alert=True)

# --- معالج الرسائل النصية (الروابط) ---
@bot.on(events.NewMessage)
async def handle_user_input_and_urls(event):
    if not await is_allowed(event):
        if event.text and (event.text.startswith("http") or event.text.startswith("/")):
            await event.respond("🔒 غير مسموح.")
        return

    chat_id = event.chat_id

    # إضافة عضو
    if WAITING_FOR_USERNAME.get(chat_id):
        WAITING_FOR_USERNAME.pop(chat_id, None)
        input_text = event.text.strip()
        if await add_allowed_user(input_text):
            clean_u = input_text.lstrip('@')
            await event.respond(f"✅ **تمت إضافة `@{clean_u}` بنجاح!**")
        else:
            await event.respond("❌ **فشل الإضافة.**")
        return

    if event.text and event.text.startswith("/trim"):
        return

    raw_text = event.text or ""
    urls = re.findall(r"https?://\S+|/video/\S+", raw_text)
    if not urls:
        return

    for u in urls:
        if u.startswith('/video/'):
            u = f"https://www.dailymotion.com{u}"
        clean_u = u.split('?')[0] if (is_instagram_url(u) or is_youtube_url(u)) else u
        clean_u = clean_url(clean_u) if not is_whatsapp_url(u) else u

        # الكشف عن أنواع الروابط
        if is_profile_url(clean_u):
            task_key = f"prof_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "profile")
            buttons = [
                [Button.inline("👤 صورة (عادية)", data=f"prof_img_{task_key}"),
                 Button.inline("📄 مستند (JPG)", data=f"prof_doc_{task_key}")]
            ]
            await event.respond("👤 **رابط ملف شخصي. اختر طريقة تنزيل الصورة:**", buttons=buttons)

        elif is_youtube_url(clean_u):
            task_key = f"yt_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "youtube")
            buttons = [
                [Button.inline("🎬 1080p", data=f"q_1080_{task_key}"), Button.inline("🎥 720p", data=f"q_720_{task_key}")],
                [Button.inline("📱 480p", data=f"q_480_{task_key}"), Button.inline("🎵 MP3", data=f"q_mp3_{task_key}")]
            ]
            await event.respond("🔴 **رابط يوتيوب. اختر الجودة:**", buttons=buttons)

        elif is_dailymotion_url(clean_u):
            task_key = f"dm_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "dailymotion")
            buttons = [
                [Button.inline("🎬 1080p", data=f"q_1080_{task_key}"), Button.inline("🎥 720p", data=f"q_720_{task_key}")],
                [Button.inline("📱 480p", data=f"q_480_{task_key}"), Button.inline("🎵 MP3", data=f"q_mp3_{task_key}")]
            ]
            await event.respond("📺 **رابط Dailymotion. اختر الجودة:**", buttons=buttons)

        elif is_tiktok_url(clean_u):
            task_key = f"tt_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "tiktok")
            buttons = [
                [Button.inline("📸 صور (ألبوم)", data=f"tt_photo_{task_key}"),
                 Button.inline("📄 مستند", data=f"tt_doc_{task_key}")],
                [Button.inline("🎥 فيديو 720p", data=f"q_720_{task_key}"),
                 Button.inline("🎬 1080p", data=f"q_1080_{task_key}")]
            ]
            await event.respond("📸 **رابط تيك توك. اختر الخيار:**", buttons=buttons)

        elif is_x_url(clean_u):
            task_key = f"x_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "x")
            buttons = [
                [Button.inline("🎬 1080p", data=f"q_1080_{task_key}"), Button.inline("🎥 720p", data=f"q_720_{task_key}")],
                [Button.inline("📱 480p", data=f"q_480_{task_key}"), Button.inline("🎵 MP3", data=f"q_mp3_{task_key}")]
            ]
            await event.respond("🐦 **رابط X. اختر الجودة:**", buttons=buttons)

        elif is_instagram_url(clean_u):
            u_lower = clean_u.lower()
            is_ig_video = "/reel/" in u_lower or "/tv/" in u_lower
            task_key = f"ig_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "instagram")
            if is_ig_video:
                buttons = [
                    [Button.inline("🎬 1080p", data=f"q_1080_{task_key}"), Button.inline("🎥 720p", data=f"q_720_{task_key}")],
                    [Button.inline("📱 480p", data=f"q_480_{task_key}"), Button.inline("🎵 MP3", data=f"q_mp3_{task_key}")]
                ]
                await event.respond("🎬 **رابط فيديو إنستغرام. اختر الجودة:**", buttons=buttons)
            else:
                buttons = [
                    [Button.inline("📸 صور (ألبوم)", data=f"ig_photo_{task_key}"),
                     Button.inline("📄 مستند", data=f"ig_doc_{task_key}")],
                    [Button.inline("🎥 فيديو 720p", data=f"q_720_{task_key}"),
                     Button.inline("🎬 1080p", data=f"q_1080_{task_key}")]
                ]
                await event.respond("📸 **رابط منشور إنستغرام. اختر الخيار:**", buttons=buttons)

        elif is_facebook_url(clean_u):
            task_key = f"fb_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "facebook")
            buttons = [
                [Button.inline("🎬 1080p", data=f"q_1080_{task_key}"), Button.inline("🎥 720p", data=f"q_720_{task_key}")],
                [Button.inline("📱 480p", data=f"q_480_{task_key}"), Button.inline("🎵 MP3", data=f"q_mp3_{task_key}")]
            ]
            await event.respond("📘 **رابط فيسبوك. اختر الجودة:**", buttons=buttons)

        elif not is_complex_url(clean_u):
            task_key = f"dir_{chat_id}_{int(time.time()*1000)}"
            await save_task(task_key, clean_u, "direct")
            buttons = [
                [Button.inline("🎬 MP4", data=f"dir_mp4_{task_key}"),
                 Button.inline("🎵 MP3", data=f"dir_mp3_{task_key}"),
                 Button.inline("📄 مستند", data=f"dir_doc_{task_key}")]
            ]
            await event.respond("📌 **رابط مباشر. اختر الصيغة:**", buttons=buttons)

        else:
            user_config = await get_user_config(chat_id)
            asyncio.create_task(
                start_direct_execution(
                    chat_id=chat_id,
                    url=clean_u,
                    filename=get_clean_filename(clean_u),
                    as_doc=False,
                    quality=user_config["quality"],
                    target_fmt=user_config.get("download_format", "mp4")
                )
            )

# --- معالجات الأزرار الخاصة بالجودة والمنصات ---
@bot.on(events.CallbackQuery(pattern=r"^q_"))
async def quality_callback_handler(event):
    if not await is_allowed(event):
        return
    data = event.data.decode("utf-8").split("_")
    quality_choice = data[1]
    task_key = "_".join(data[2:])
    url, task_type = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت صلاحية الخيار.", alert=True)
        return
    chat_id = event.chat_id

    if task_type == "dailymotion" or is_dailymotion_url(url):
        status_msg = await event.edit("⏳ **جاري التحميل من Dailymotion...**", buttons=None)
        asyncio.create_task(
            download_dailymotion_video(
                event=event,
                url=url,
                quality_choice=quality_choice,
                status_msg=status_msg
            )
        )
    else:
        target_fmt = 'mp3' if quality_choice == 'mp3' else 'mp4'
        quality_val = 'best' if quality_choice == '1080' else quality_choice
        status_msg = await event.edit("⏳ **جاري التحميل...**", buttons=None)
        asyncio.create_task(
            start_direct_execution(
                chat_id=chat_id,
                url=url,
                filename=get_clean_filename(url),
                as_doc=False,
                quality=quality_val,
                target_fmt=target_fmt,
                status_msg=status_msg
            )
        )

@bot.on(events.CallbackQuery(pattern=r"^prof_"))
async def profile_callback_handler(event):
    if not await is_allowed(event):
        return
    data = event.data.decode("utf-8").split("_")
    choice = data[1]
    task_key = "_".join(data[2:])
    url, _ = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت الصلاحية.", alert=True)
        return
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    status_msg = await event.edit("⏳ **جاري تنزيل الصورة الشخصية...**", buttons=None)
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename="profile_avatar.jpg",
            as_doc=as_doc,
            quality='best',
            target_fmt='mp4',
            status_msg=status_msg,
            is_avatar_task=True
        )
    )

@bot.on(events.CallbackQuery(pattern=r"^tt_"))
async def tiktok_callback_handler(event):
    if not await is_allowed(event):
        return
    data = event.data.decode("utf-8").split("_")
    choice = data[1]
    task_key = "_".join(data[2:])
    url, _ = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت الصلاحية.", alert=True)
        return
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    status_msg = await event.edit("⏳ **جاري التحميل من تيك توك...**", buttons=None)
    user_config = await get_user_config(chat_id)
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=as_doc,
            quality=user_config["quality"],
            target_fmt='mp4',
            status_msg=status_msg
        )
    )

@bot.on(events.CallbackQuery(pattern=r"^ig_"))
async def instagram_callback_handler(event):
    if not await is_allowed(event):
        return
    data = event.data.decode("utf-8").split("_")
    choice = data[1]
    task_key = "_".join(data[2:])
    url, _ = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت الصلاحية.", alert=True)
        return
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    status_msg = await event.edit("⏳ **جاري التحميل من إنستغرام...**", buttons=None)
    user_config = await get_user_config(chat_id)
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=as_doc,
            quality=user_config["quality"],
            target_fmt='mp4',
            status_msg=status_msg
        )
    )

@bot.on(events.CallbackQuery(pattern=r"^dir_"))
async def direct_callback_handler(event):
    if not await is_allowed(event):
        return
    data = event.data.decode("utf-8").split("_")
    choice = data[1]
    task_key = "_".join(data[2:])
    url, _ = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت الصلاحية.", alert=True)
        return
    chat_id = event.chat_id
    as_doc = (choice == 'doc')
    target_fmt = choice if choice in ['mp4', 'mp3'] else 'mp4'
    status_msg = await event.edit("⏳ **جاري التحميل المباشر...**", buttons=None)
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=as_doc,
            quality='best',
            target_fmt=target_fmt,
            status_msg=status_msg
        )
    )

# --- سجل التحديثات ---
@bot.on(events.CallbackQuery(pattern=r"^(show_changelog|close_changelog)$"))
async def changelog_callback_handler(event):
    if not await is_allowed(event):
        await event.answer("⚠️ غير مسموح.", alert=True)
        return
    data = event.data.decode("utf-8")
    if data == "show_changelog":
        changelog = (
            f"📜 **سجل التحديثات v{ VERSION }**\n\n"
            "✨ **محرك تحميل متوازي** لرفع سرعة التنزيلات الكبيرة.\n"
            "🔄 **إمكانية الاستئناف** للتحميلات المباشرة.\n"
            "📺 **دعم فيسبوك** (Reels والفيديوهات).\n"
            "🖼️ **تحسين استخراج الصور الشخصية** بإضافة محرك خامس.\n"
            "📦 **خيارات جديدة في الإعدادات** (الصيغة الافتراضية، عدد التحميلات المتوازية).\n"
            "🐞 **إصلاحات متعددة** للأخطاء وتحسين استقرار البوت.\n"
            f"📌 الإصدار: `{VERSION}`"
        )
        buttons = [[Button.inline("❌ إغلاق", data="close_changelog")]]
        await event.respond(changelog, buttons=buttons)
        await event.answer()
    elif data == "close_changelog":
        await event.delete()

# --- تشغيل البوت ---
def main():
    bot.start(bot_token=BOT_TOKEN)
    print(f"🚀 البوت يعمل ({VERSION}) | المالك: {OWNER_ID}")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()