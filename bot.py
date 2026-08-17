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
import math
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- الإعدادات ---
VERSION = "v72.0-VideoScreenshots-CancelButton"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}
PENDING_TASKS = {}
ACTIVE_TASKS = {} # تخزين المهام النشطة للإلغاء

# --- التحديث التلقائي للمكتبات ---
def update_libraries():
    try:
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ تم تحديث المكتبات بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_libraries()

# --- التعامل مع الكوكيز ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(x_b64.strip()))
        except Exception: pass

    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(ig_b64.strip()))
        except Exception: pass

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8',
    'Referer': 'https://www.tiktok.com/',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
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

# --- دوال مساعدة لحساب الحجم والوقت والسرعة ---
def human_readable_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def human_readable_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s" if m > 0 else f"{s}s"

def create_progress_bar(percentage, length=10):
    filled = int(length * percentage // 100)
    return "█" * filled + "░" * (length - filled)

def clean_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com', 'vt.tiktok.com', 'youtube.com', 'youtu.be', 'facebook.com'])

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

# --- دالة إضافة نص التوقيت الشفاف الكبيرة أسفل منتصف الصورة ---
def draw_timestamp_on_image(image_path, timestamp_seconds):
    try:
        # تنسيق الوقت إلى MM:SS أو HH:MM:SS
        seconds = int(timestamp_seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        with Image.open(image_path).convert("RGBA") as base:
            txt_layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)
            
            # حساب حجم الخط نسبةً إلى ارتفاع الصورة
            font_size = max(24, int(base.height * 0.08))
            
            font = None
            font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            ]
            for font_p in font_paths:
                if os.path.exists(font_p):
                    try:
                        font = ImageFont.truetype(font_p, font_size)
                        break
                    except Exception: pass
            if not font:
                font = ImageFont.load_default()

            # تحديد أبعاد النص
            if hasattr(draw, 'textbbox'):
                bbox = draw.textbbox((0, 0), time_str, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
            else:
                text_width, text_height = draw.textsize(time_str, font=font)

            # موقع النص: أسفل منتصف الصورة مع هامش سفي بسيط
            x = (base.width - text_width) / 2
            margin_bottom = int(base.height * 0.06)
            y = base.height - text_height - margin_bottom

            # لون أبيض شبه شفاف (شفافية ~55% / Alpha=140)
            text_color = (255, 255, 255, 140)
            
            # رسم النص الرئيسي
            draw.text((x, y), time_str, font=font, fill=text_color)
            
            # دمج الطبقتين وإعادة حفظ الصورة
            composite = Image.alpha_composite(base, txt_layer)
            composite.convert("RGB").save(image_path, "JPEG", quality=95)
            
    except Exception as e:
        print(f"Error drawing timestamp on image: {e}")

# --- دالة استخراج 9 لقطات مع إضافة التوقيت ---
def extract_9_screenshots(video_path, duration):
    screenshots = []
    if duration <= 0:
        duration = 30
    
    step = duration / 10
    base_dir = os.path.dirname(video_path)
    
    for i in range(1, 10):
        timestamp = step * i
        out_img = os.path.join(base_dir, f"shot_{i}_{int(time.time()*1000)}.jpg")
        cmd = [
            'ffmpeg', '-y', '-ss', str(timestamp), '-i', video_path,
            '-vframes', '1', '-q:v', '2', out_img
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(out_img) and os.path.getsize(out_img) > 0:
            # إضافة نص التوقيت الشفاف أسفل النص
            draw_timestamp_on_image(out_img, timestamp)
            # تطهير الصورة
            out_img = deep_sanitize_image(out_img)
            screenshots.append(out_img)
            
    return screenshots

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

def instagram_carousel_and_photo_engine(url, task_dir):
    try:
        cmd = ["gallery-dl", "--directory", task_dir, "--filename", "ig_{id}_{num}.{ext}"]
        if os.path.exists(INSTAGRAM_COOKIES_FILE):
            cmd.extend(["--cookies", INSTAGRAM_COOKIES_FILE])
        cmd.append(url)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        
        if any(os.path.isfile(os.path.join(task_dir, f)) for f in os.listdir(task_dir)):
            return

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
                    filepath = os.path.join(task_dir, f"ig_photo_{idx}.{ext}")
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk: f.write(chunk)
    except Exception as e:
        print(f"Instagram engine error: {e}")

def download_direct_file_with_progress(url, filepath, cancel_event, loop, status_msg):
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    total_size = int(res.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update = 0

    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=1024*512):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_update > 2.5:
                    last_update = now
                    elapsed = now - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    percent = (downloaded / total_size * 100) if total_size > 0 else 0
                    rem_size = total_size - downloaded if total_size >= downloaded else 0
                    bar = create_progress_bar(percent)
                    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                    msg_text = (
                        f"📥 **جاري تنزيل الملف...**\n\n"
                        f"[{bar}] {percent:.1f}%\n"
                        f"⏱️ **وقت التنزيل:** {human_readable_time(elapsed)}\n"
                        f"💾 **الحجم:** {human_readable_size(total_size)}\n"
                        f"📊 **تم تنزيل:** {human_readable_size(downloaded)} | **متبقي:** {human_readable_size(rem_size)}\n"
                        f"⚡ **السرعة:** {human_readable_size(speed)}/s"
                    )
                    asyncio.run_coroutine_threadsafe(status_msg.edit(msg_text, buttons=cancel_btn), loop)

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

# --- التنفيذ المباشر المحدث وشريط الرفع ---
async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None):
    if not status_msg:
        status_msg = await bot.send_message(chat_id, "⏳ **جاري تحضير المحتوى...**")
    else:
        await status_msg.edit("⏳ **جاري تحضير المحتوى...**")
        
    cancel_event = threading.Event()
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    ACTIVE_TASKS[id(cancel_event)] = {'event': cancel_event, 'dir': task_dir, 'msg': status_msg}
    
    try:
        loop = asyncio.get_event_loop()
        target_url = clean_url(url) if is_complex_url(url) else url
        
        if target_url and is_complex_url(target_url):
            await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
            
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
            await loop.run_in_executor(None, download_direct_file_with_progress, target_url, filepath, cancel_event, loop, status_msg)
        elif media_msg:
            filepath = os.path.join(task_dir, filename)
            await bot.download_media(media_msg, file=filepath)

        # --- تنقية وتجهيز الملفات ---
        downloaded_files = []
        for root, _, files in os.walk(task_dir):
            for file in files:
                fpath = os.path.join(root, file)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 500 and not file.endswith('_thumb.jpg') and not file.endswith('_clean.jpg') and not file.endswith('_final.jpg') and not 'shot_' in file:
                    
                    base_name, current_ext = os.path.splitext(file)
                    current_ext = current_ext.lower()
                    
                    if "none" in file.lower() or current_ext not in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mkv', '.mov', '.avi', '.mp3']:
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
            raise Exception("تعذر الوصول إلى الملف المطلوب.")

        # --- كولباك الرفع ---
        start_upload_time = time.time()
        last_upload_update = [0]

        async def upload_progress_callback(current, total):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            now = time.time()
            if now - last_upload_update[0] > 2.5:
                last_upload_update[0] = now
                elapsed = now - start_upload_time
                speed = current / elapsed if elapsed > 0 else 0
                percent = (current / total * 100) if total > 0 else 0
                rem_size = total - current if total >= current else 0
                bar = create_progress_bar(percent)
                cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                msg_text = (
                    f"📤 **جاري رفع المحتوى إلى تليجرام...**\n\n"
                    f"[{bar}] {percent:.1f}%\n"
                    f"⏱️ **الوقت المنقضي:** {human_readable_time(elapsed)}\n"
                    f"💾 **الحجم الكلي:** {human_readable_size(total)}\n"
                    f"📊 **تم رفع:** {human_readable_size(current)} | **متبقي:** {human_readable_size(rem_size)}\n"
                    f"⚡ **السرعة:** {human_readable_size(speed)}/s"
                )
                try:
                    await status_msg.edit(msg_text, buttons=cancel_btn)
                except Exception: pass

        # رفع الملفات
        video_files = [f for f in downloaded_files if f.lower().endswith(('.mp4', '.mkv', '.mov', '.avi'))]
        
        for file in downloaded_files:
            if cancel_event.is_set(): raise Exception("CANCELLED")
            file_ext = os.path.splitext(file)[1].lower()
            
            if file_ext in ['.mp4', '.mkv', '.mov', '.avi'] and not as_doc:
                duration, width, height, thumb = await loop.run_in_executor(None, get_video_metadata_and_thumb, file)
                
                # استخراج 9 لقطات شاشة مع التوقيت المكتوب عليها
                shots = await loop.run_in_executor(None, extract_9_screenshots, file, duration)
                if shots:
                    try:
                        await bot.send_file(chat_id, shots, caption="📸 **لقطات شاشة من الفيديو**")
                    except Exception as e:
                        print(f"Error sending album screenshots: {e}")
                    for s in shots:
                        try: os.remove(s)
                        except: pass

                attributes = [DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True
                )]
                
                await bot.send_file(
                    chat_id,
                    file,
                    thumb=thumb,
                    attributes=attributes,
                    progress_callback=upload_progress_callback,
                    supports_streaming=True
                )
                if thumb and os.path.exists(thumb): os.remove(thumb)
            else:
                await bot.send_file(
                    chat_id,
                    file,
                    force_document=as_doc,
                    progress_callback=upload_progress_callback
                )

        await status_msg.delete()

    except Exception as e:
        if str(e) == "CANCELLED":
            await status_msg.edit("❌ **تم إلغاء العملية بنجاح.**")
        else:
            await status_msg.edit(f"⚠️ **حدث خطأ:** {e}")
            
    finally:
        ACTIVE_TASKS.pop(id(cancel_event), None)
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)
        gc.collect()

# --- معالجة زر الإلغاء ---
@bot.on(events.CallbackQuery(pattern=r"^cancel_(\d+)"))
async def handle_cancel(event):
    task_id = int(event.pattern_match.group(1))
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]['event'].set()
        await event.answer("جاري إلغاء العملية...", alert=True)
    else:
        await event.answer("العملية منتهية أو تم إلغاؤها بالفعل.", alert=True)

# --- تشغيل البوت ---
print("🚀 جاري تشغيل البوت...")
bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
