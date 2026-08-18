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
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
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
VERSION = "v77.0-Selective-Snapshots-Edition"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

# تخزين إعدادات المستخدمين في الذاكرة
USER_SETTINGS = {}
PENDING_TASKS = {}
DIRECT_TASKS = {}
ACTIVE_CANCEL_EVENTS = {}

FONT_SIZE_MAP = {
    "small": 2.18,    # كبير نسبياً
    "medium": 3.25,   # واضح جداً
    "large": 5.35,    # ضخم
    "xlarge": 10.45    # عملاق
}

def get_user_config(chat_id):
    if chat_id not in USER_SETTINGS:
        USER_SETTINGS[chat_id] = {
            "snapshots": True,               # تفعيل اللقطات بصفة عامة
            "social_snapshots": False,       # تعطيل اللقطات لمنصات التواصل (افتراضياً: الروابط المباشرة فقط)
            "quality": "720",                # الجودة الافتراضية
            "font_size": "large"             # حجم الخط الافتراضي
        }
    return USER_SETTINGS[chat_id]

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Referer': 'https://www.tiktok.com/',
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
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com', 'vt.tiktok.com'])

def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

def is_instagram_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['instagram.com', 'instagr.am'])

def is_tiktok_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['tiktok.com', 'vt.tiktok.com'])

def get_clean_filename(url):
    if is_complex_url(url): return "media_download"
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'): return filename.replace('.m3u8', '.mp4')
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv', '.jpg', '.jpeg', '.png', '.webp']): 
        return "downloaded_media"
    return filename

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

# --- إضافة النص والوقت في وسط الصورة بحجم خط مكبر وقابل للتعديل ---
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

# --- استخراج 9 لقطات ألبوم من الفيديو ---
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

    if is_x_url(url) and os.path.exists(X_COOKIES_FILE):
        ydl_opts['cookiefile'] = X_COOKIES_FILE
    elif is_instagram_url(url) and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE

    if 'tiktok.com' in url_lower:
        ydl_opts['format'] = 'best'
    elif fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif quality != 'best':
        ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        ydl_opts['format'] = 'best/bestvideo+bestaudio'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"yt-dlp error: {e}")

def tiktok_multi_engine(url, task_dir):
    try:
        cmd = ["gallery-dl", "--directory", task_dir, "--filename", "tt_{id}_{num}.{ext}", url]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return
    except Exception as e:
        print(f"TikTok gallery-dl error: {e}")

    try:
        api_url = "https://api.cobalt.tools/api/json"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_HEADERS['User-Agent']
        }
        res = requests.post(api_url, json={"url": url}, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status")
            urls_to_dl = []
            if status in ["stream", "redirect"]:
                urls_to_dl.append(data.get("url"))
            elif status == "picker":
                for item in data.get("picker", []):
                    urls_to_dl.append(item.get("url"))

            for idx, media_url in enumerate(urls_to_dl, start=1):
                if not media_url: continue
                r = requests.get(media_url, stream=True, headers=BROWSER_HEADERS, timeout=30)
                if r.status_code == 200:
                    is_vid = ".mp4" in media_url or "video" in r.headers.get("content-type", "")
                    ext = "mp4" if is_vid else "jpg"
                    filepath = os.path.join(task_dir, f"tt_photo_{idx}.{ext}")
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk: f.write(chunk)
    except Exception as e:
        print(f"TikTok Cobalt API error: {e}")

# --- محرك تنزيل إنستغرام المحسّن لاستخراج أعلى جودة ممكنة للصور ---
def instagram_carousel_and_photo_engine(url, task_dir):
    try:
        # إجبار gallery-dl على جلب الدقة الأصلية والكاملة لمنشورات وألبومات إنستغرام
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
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        
        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return

        # المحرك الثانوى المزدوج باستهداف التنزيل المباشر بجودة أصلية عبر yt-dlp للصور
        ydl_opts = {
            'outtmpl': os.path.join(task_dir, 'ig_hd_%(id)s_%(autonumber)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'headers': BROWSER_HEADERS,
            'writethumbnails': True,
            'format': 'best',
        }
        if os.path.exists(INSTAGRAM_COOKIES_FILE):
            ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return

        # المحرك الثالث الداعم (Cobalt API) بجودة مرتفعة عند تعذر الاستخراج المباشر
        api_url = "https://api.cobalt.tools/api/json"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_HEADERS['User-Agent']
        }
        res = requests.post(api_url, json={"url": url, "downloadMode": "max"}, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status")
            urls_to_dl = []
            if status in ["stream", "redirect"]:
                urls_to_dl.append(data.get("url"))
            elif status == "picker":
                for item in data.get("picker", []):
                    urls_to_dl.append(item.get("url"))

            for idx, media_url in enumerate(urls_to_dl, start=1):
                if not media_url: continue
                r = requests.get(media_url, stream=True, headers=BROWSER_HEADERS, timeout=30)
                if r.status_code == 200:
                    is_vid = ".mp4" in media_url or "video" in r.headers.get("content-type", "")
                    ext = "mp4" if is_vid else "jpg"
                    filepath = os.path.join(task_dir, f"ig_photo_{idx}.{ext}")
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk: f.write(chunk)
    except Exception as e:
        print(f"Instagram engine error: {e}")

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

async def download_direct_with_progress(client, chat_id, url, filepath, status_msg, cancel_event, task_id):
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    total_size = int(res.headers.get('content-length', 0))
    
    downloaded = 0
    start_time = time.time()
    last_update_time = 0
    chunk_size = 256 * 1024

    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{task_id}")]

    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=chunk_size):
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
                        f"⏱️ الوقت المنقضي: `{int(elapsed)} ثانية`\n"
                        f"⏳ المتبقي تقريباً: `{int(rem_time)} ثانية`"
                    )
                    try:
                        await status_msg.edit(text, buttons=cancel_btn)
                    except: pass

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None):
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
    
    user_config = get_user_config(chat_id)

    try:
        loop = asyncio.get_event_loop()
        target_url = clean_url(url) if is_complex_url(url) else url
        is_social = is_complex_url(target_url) if target_url else False
        
        if cancel_event.is_set(): raise Exception("CANCELLED")

        if target_url and is_social:
            await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
            
            if cancel_event.is_set(): raise Exception("CANCELLED")

            if is_instagram_url(target_url):
                downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
                if not downloaded:
                    await loop.run_in_executor(None, instagram_carousel_and_photo_engine, target_url, task_dir)

            if is_tiktok_url(target_url):
                downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
                if not downloaded:
                    await loop.run_in_executor(None, tiktok_multi_engine, target_url, task_dir)

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
                        f"⏱️ الوقت المنقضي: `{int(elapsed)} ثانية`\n"
                        f"⏳ المتبقي: `{int(rem_time)} ثانية`"
                    )
                    try:
                        await status_msg.edit(text, buttons=cancel_btn)
                    except: pass

            await download_direct_with_progress(bot, chat_id, target_url, filepath, status_msg, cancel_event, task_id)
            
            if cancel_event.is_set(): raise Exception("CANCELLED")
            await status_msg.edit("📤 **جاري تجهيز الرفع...**", buttons=cancel_btn)
            
            if target_fmt == 'mp3':
                base, _ = os.path.splitext(filepath)
                mp3_path = base + ".mp3"
                subprocess.run(['ffmpeg', '-y', '-i', filepath, '-vn', '-ab', '192k', mp3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(mp3_path):
                    filepath = mp3_path
            
            if as_doc:
                await bot.send_file(chat_id, filepath, force_document=True, progress_callback=upload_progress_callback)
            else:
                ext = os.path.splitext(filepath)[1].lower()
                if ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
                    duration, width, height, thumb_path = get_video_metadata_and_thumb(filepath)
                    attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                    await bot.send_file(chat_id, filepath, thumb=thumb_path, attributes=attr, progress_callback=upload_progress_callback)
                    
                    # التحقق من شرط أخذ اللقطات الـ 9 للروابط المباشرة
                    should_take_snaps = user_config.get("snapshots", True) and (not is_social or user_config.get("social_snapshots", False))
                    
                    if duration > 0 and should_take_snaps:
                        await status_msg.edit("📸 **جاري التقاط 9 صور من الفيديو وتجهيز الألبوم...**")
                        frames = extract_9_frames(filepath, duration, chat_id=chat_id)
                        if frames:
                            await bot.send_file(chat_id, frames, caption="📸 **ألبوم اللقطات المصورة من الفيديو مع التوقيتات:**")
                            for fr in frames:
                                try: os.remove(fr)
                                except: pass

                    if thumb_path and os.path.exists(thumb_path):
                        try: os.remove(thumb_path)
                        except: pass
                elif ext in ['.mp3', '.wav', '.m4a', '.aac']:
                    await bot.send_file(chat_id, filepath, progress_callback=upload_progress_callback)
                else:
                    await bot.send_file(chat_id, filepath, force_document=True, progress_callback=upload_progress_callback)
            
            await status_msg.delete()
            return

        elif media_msg:
            filepath = os.path.join(task_dir, filename)
            await bot.download_media(media_msg, file=filepath)

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
            raise Exception("تعذر الوصول إلى المحتوى. تأكد من صحة الرابط.")

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

        if photos:
            sent_as_album = False
            if len(photos) > 1 and not as_doc:
                try:
                    uploaded_handles = []
                    for p in photos[:10]:
                        uploaded_file = await bot.upload_file(p)
                        uploaded_handles.append(uploaded_file)
                    await bot.send_file(chat_id, uploaded_handles, caption=f"📸 **تم تنزيل ألبوم الصور ({len(photos)} صورة):**")
                    sent_as_album = True
                except Exception as album_err:
                    print(f"Album upload failed: {album_err}")

            if not sent_as_album:
                for idx, p in enumerate(photos, start=1):
                    try:
                        uploaded_single = await bot.upload_file(p)
                        cap = f"📸 **صورة ({idx}/{len(photos)}):**" if len(photos) > 1 else "📸 **تم تنزيل الصورة بنجاح!**"
                        await bot.send_file(chat_id, uploaded_single, caption=cap, force_document=as_doc)
                    except Exception as single_err:
                        await bot.send_file(chat_id, p, force_document=True)

        for vid in videos:
            if cancel_event.is_set(): raise Exception("CANCELLED")
            duration, width, height, thumb_path = get_video_metadata_and_thumb(vid)
            attr = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
            await bot.send_file(chat_id, vid, caption=f"🎥 **تم تنزيل الفيديو بنجاح!**\n📁 `{os.path.basename(vid)}`", force_document=False, thumb=thumb_path, attributes=attr)
            
            # التحقق من خيار التقاط اللقطات منصات التواصل بطلب المستخدم
            should_take_snaps = user_config.get("snapshots", True) and (not is_social or user_config.get("social_snapshots", False))

            if duration > 0 and should_take_snaps:
                await status_msg.edit("📸 **جاري توليد ألبوم اللقطات الـ 9 للفيديو...**")
                frames = extract_9_frames(vid, duration, chat_id=chat_id)
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

# --- واجهة إعدادات التحكم الشاملة ---
def build_settings_buttons(chat_id):
    config = get_user_config(chat_id)
    
    snap_status = "✅ مفعّلة (عام)" if config["snapshots"] else "❌ معطلة"
    social_snap_status = "✅ مفعّلة" if config["social_snapshots"] else "❌ معطلة (مباشر فقط)"
    
    font_labels = {
        "small": "متوسط",
        "medium": "كبير جداً",
        "large": "ضخم (الافتراضي)",
        "xlarge": "عملاق"
    }
    
    buttons = [
        [
            Button.inline(f"📸 لقطات الفيديو: {snap_status}", data="toggle_snaps")
        ],
        [
            Button.inline(f"🌐 لقطات منصات التواصل: {social_snap_status}", data="toggle_social_snaps")
        ],
        [
            Button.inline(f"🎥 الجودة: {config['quality']}p", data="change_qual"),
            Button.inline(f"🔤 الخط: {font_labels.get(config['font_size'], 'ضخم')}", data="change_font")
        ],
        [
            Button.inline("❌ إغلاق اللوحة", data="close_settings")
        ]
    ]
    return buttons

async def send_settings_menu(event):
    chat_id = event.chat_id
    msg = "⚙️ **لوحة التحكم والإعدادات الخاصّة بالبوت:**\n\nقم بالضغط على الأزرار أدناه لتعديل خيارات التحميل والتقاط الصور."
    buttons = build_settings_buttons(chat_id)
    
    await event.respond(msg, buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/settings$"))
async def settings_handler(event):
    await send_settings_menu(event)

@bot.on(events.CallbackQuery(pattern=r"^(toggle_snaps|toggle_social_snaps|change_qual|change_font|close_settings)$"))
async def settings_callback_handler(event):
    chat_id = event.chat_id
    data = event.data.decode("utf-8")
    config = get_user_config(chat_id)

    if data == "toggle_snaps":
        config["snapshots"] = not config["snapshots"]
        await event.answer("تم تغيير حالة ألبوم اللقطات العام!")

    elif data == "toggle_social_snaps":
        config["social_snapshots"] = not config["social_snapshots"]
        state_txt = "تفعيل" if config["social_snapshots"] else "إيقاف"
        await event.answer(f"تم {state_txt} التقاط الصور من روابط منصات التواصل الاجتماعي!")

    elif data == "change_qual":
        qualities = ["480", "720", "1080"]
        current_idx = qualities.index(config["quality"]) if config["quality"] in qualities else 1
        config["quality"] = qualities[(current_idx + 1) % len(qualities)]
        await event.answer(f"تم اختيار الجودة الافتراضية: {config['quality']}p")

    elif data == "change_font":
        fonts = ["small", "medium", "large", "xlarge"]
        current_idx = fonts.index(config["font_size"]) if config["font_size"] in fonts else 2
        config["font_size"] = fonts[(current_idx + 1) % len(fonts)]
        await event.answer(f"تم تعديل حجم الخط!")

    elif data == "close_settings":
        await event.delete()
        return

    # تعديل نفس الرسالة مباشرة والأزرار بدون إعادة إرسال رسالة جديدة
    msg = "⚙️ **لوحة التحكم والإعدادات الخاصّة بالبوت:**\n\nقم بالضغط على الأزرار أدناه لتعديل خيارات التحميل والتقاط الصور."
    new_buttons = build_settings_buttons(chat_id)
    await event.edit(msg, buttons=new_buttons)

# --- رسالة البداية وسجل التحديثات ---
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        "🤖 **أهلاً بك في بوت التنزيل المباشر والشامل!**\n\n"
        "✨ **أبرز ميزات وخدمات البوت:**\n\n"
        "🔗 **الروابط المباشرة:**\n"
        "• تنزيل ملفات الفيديو المباشرة بجميع الامتدادات مثل (`.mp4`, `.mkv`, `.avi` وغيرها).\n"
        "• إمكانية حفظ الفيديو كـ **مستند** أو تحويله إلى **مقطع صوتي MP3** مباشرة.\n"
        "• توليد ألبوم مكون من **9 لقطات مصورة** مع التوقيت في المنتصف تلقائياً.\n\n"
        "🌐 **منصات التواصل الاجتماعي:**\n"
        "• **منصة X (تويتر سابقاً):** تنزيل الفيديوهات بخيارات جودة متعددة (1080p, 720p, 480p) أو MP3.\n"
        "• **إنستغرام (Instagram):** تنزيل الفيديوهات، الريلز، وألبومات الصور الكاملة.\n"
        "• **تيك توك (TikTok):** تنزيل الفيديوهات بدون علامة مائية، ودعم ألبومات الصور.\n\n"
        "📊 **شريط تقدم مفصل:** متابعة سرعة التنزيل/الرفع، الحجم المستهلك، والوقت المتبقي.\n"
        "⚡ **إلغاء فورية:** إمكانية إلغاء أي عملية بنقرة زر واحدة.\n\n"
        "⚙️ لتعديل إعدادات الجودة واللقطات وحجم الخط أرسل: `/settings`"
    )
    
    buttons = [
        [Button.inline("📜 سجل التحديثات", data="show_changelog")]
    ]
    
    await event.respond(welcome_text, buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^(show_changelog|close_changelog)$"))
async def changelog_callback_handler(event):
    data = event.data.decode("utf-8")
    
    if data == "show_changelog":
        changelog_text = (
            f"📜 **سجل التحديثات والتعديلات ({VERSION}):**\n\n"
            "1️⃣ **التحكم بلقطات التواصل:** إمكانية إيقاف/تفعيل التقاط 9 لقطات من روابط منصات التواصل عبر الإعدادات بلمسة زر.\n"
            "2️⃣ **الروابط المباشرة فقط:** حصر إرسال الصور التلقائية بالفيديوهات المباشرة كخيار افتراضي.\n"
            "3️⃣ **إلغاء العمليات:** إضافة زر `❌ إلغاء العملية` لإيقاف التحميل أو الرفع في أي وقت.\n"
            "4️⃣ **توقيت اللقطات:** كتابة وقت اللقطة بوضوح باللون الأبيض الشفاف في المنتصف بحجم خط ممتاز.\n"
            "5️⃣ **شريط التقدم المفصل:** عرض الحجم، السرعة، الوقت المنقضي والمتبقي بدقة.\n\n"
            "📌 الإصدار الحالي: `v77.0-Selective-Snapshots-Edition`"
        )
        buttons = [[Button.inline("❌ إغلاق السجل", data="close_changelog")]]
        await event.respond(changelog_text, buttons=buttons)
        await event.answer()
        
    elif data == "close_changelog":
        await event.delete()

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    for u in urls:
        clean_u = u.split('?')[0] if is_instagram_url(u) else u
        
        if is_x_url(clean_u):
            task_key = f"{chat_id}_{int(time.time()*1000)}"
            PENDING_TASKS[task_key] = clean_u
            buttons = [
                [Button.inline("🎬 عالية (1080p)", data=f"q_1080_{task_key}"), Button.inline("🎥 متوسطة (720p)", data=f"q_720_{task_key}")],
                [Button.inline("📱 منخفضة (480p)", data=f"q_480_{task_key}"), Button.inline("🎵 صوت فقط (MP3)", data=f"q_mp3_{task_key}")]
            ]
            await event.respond("🎬 **اختر جودة الفيديو المطلوبة لمنصة X:**", buttons=buttons)
            
        elif not is_complex_url(clean_u):
            task_key = f"dir_{chat_id}_{int(time.time()*1000)}"
            DIRECT_TASKS[task_key] = clean_u
            
            buttons = [
                [
                    Button.inline("🎬 MP4", data=f"dir_mp4_{task_key}"),
                    Button.inline("🎵 MP3", data=f"dir_mp3_{task_key}"),
                    Button.inline("📄 مستند", data=f"dir_doc_{task_key}")
                ]
            ]
            await event.respond("📌 **تم رصد رابط مباشر. اختر صيغة التحميل المناسبة:**", buttons=buttons)
            
        else:
            user_config = get_user_config(chat_id)
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

@bot.on(events.CallbackQuery(pattern=r"^q_"))
async def quality_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    quality_choice = data[1]
    task_key = "_".join(data[2:])
    
    if task_key not in PENDING_TASKS:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار، يرجى إعادة إرسال الرابط.", alert=True)
        return
        
    url = PENDING_TASKS.pop(task_key)
    chat_id = event.chat_id
    target_fmt = 'mp3' if quality_choice == 'mp3' else 'mp4'
    quality_val = 'best' if quality_choice == '1080' else quality_choice
    status_msg = await event.edit("⏳ **تم استلام طلبك، جاري بدء التنزيل...**", buttons=None)
    
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

@bot.on(events.CallbackQuery(pattern=r"^dir_"))
async def direct_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    choice = data[1] # mp4, mp3, doc
    task_key = "_".join(data[2:])
    
    if task_key not in DIRECT_TASKS:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار، يرجى إعادة إرسال الرابط.", alert=True)
        return
        
    url = DIRECT_TASKS.pop(task_key)
    chat_id = event.chat_id
    
    as_doc = (choice == 'doc')
    target_fmt = choice if choice in ['mp4', 'mp3'] else 'mp4'
    
    status_msg = await event.edit("⏳ **جاري بدء التنزيل المباشر...**", buttons=None)
    
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

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بأعلى كفاءة مع الواجهة الجديدة وسجل التحديثات!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
