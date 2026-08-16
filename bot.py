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
VERSION = "v49.0-TwitterCookies-Railway"
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

# --- التعامل مع ملف الكوكيز المشفّر من متغيرات بيئة Railway ---
COOKIES_FILE_PATH = "cookies.txt"

def setup_cookies():
    """استخراج الكوكيز المشفّرة بـ Base64 وإنشاء ملف مؤقت للخدمة"""
    cookies_b64 = os.environ.get("X_COOKIES_BASE64")
    if cookies_b64:
        try:
            decoded_bytes = base64.b64decode(cookies_b64.strip())
            with open(COOKIES_FILE_PATH, "wb") as f:
                f.write(decoded_bytes)
            print("✅ تم تحميل وتجهيز ملف الكوكيز من متغيرات البيئة بنجاح.")
            return COOKIES_FILE_PATH
        except Exception as e:
            print(f"❌ خطأ أثناء فك تشفير الكوكيز: {e}")
    else:
        print("⚠️ لم يتم العثور على متغير البيئة X_COOKIES_BASE64. سيتم التحميل بدون كوكيز.")
    return None

# تجهيز الكوكيز فور تشغيل السكريبت
setup_cookies()

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
        USER_SETTINGS[user_id] = {
            'send_screenshots': True,
            'streaming_mode': True,
            'font_size': 'large'
        }
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
            except Exception as e:
                print(f"Failed to delete {file_path}: {e}")
    else:
        os.makedirs(folder, exist_ok=True)
    gc.collect()

clean_download_folder()

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

bot = TelegramClient('bot_session', API_ID, API_HASH)

def is_twitter_or_complex_url(url):
    """التحقق مما إذا كان الرابط يتطلب yt-dlp مثل تويتر/إكس"""
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'twimg.com'])

def get_clean_filename(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if is_twitter_or_complex_url(url):
        filename = "twitter_video.mp4"
    elif filename.endswith('.m3u8'):
        filename = filename.replace('.m3u8', '.mp4')
    elif not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv']):
        filename = "video_download.mp4"
    return filename

def change_extension(filename, new_ext):
    name_without_ext = os.path.splitext(filename)[0]
    return f"{name_without_ext}.{new_ext.strip('.')}"

def get_video_metadata_and_thumb(file_path):
    duration = 0
    width = 0
    height = 0
    thumb_path = f"{file_path}.jpg"
    
    try:
        parser = createParser(file_path)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    if metadata.has('duration'):
                        duration = int(metadata.get('duration').seconds)
                    if metadata.has('width'):
                        width = metadata.get('width')
                    if metadata.has('height'):
                        height = metadata.get('height')
    except Exception:
        pass

    cmd = [
        'ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path,
        '-vframes', '1', '-q:v', '2', thumb_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
        thumb_path = None

    return duration, width, height, thumb_path

def add_timestamp_to_image(image_path, time_str, text_color="white", size_mode="large"):
    try:
        config = FONT_SIZES.get(size_mode, FONT_SIZES['large'])
        
        with Image.open(image_path) as img:
            img = img.convert("RGBA")
            font_size = max(config['min'], int(img.height * config['ratio']))
            
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

            txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)

            bbox = draw.textbbox((0, 0), time_str, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            margin = int(img.width * 0.03)
            x = img.width - text_w - margin
            y = margin

            padding_x = int(font_size * 0.35)
            padding_y = int(font_size * 0.2)
            rect_box = [x - padding_x, y - padding_y, x + text_w + padding_x, y + text_h + padding_y]

            fill_color = (255, 255, 255, 255)
            box_color = (0, 0, 0, 210)

            draw.rectangle(rect_box, fill=box_color)
            draw.text((x, y), time_str, fill=fill_color, font=font)

            final_img = Image.alpha_composite(img, txt_layer).convert("RGB")
            final_img.save(image_path, quality=95)
    except Exception as e:
        print(f"Error drawing timestamp: {e}")

def generate_grid_screenshots(file_path, duration, size_mode="large"):
    screenshots = []
    if duration <= 0:
        duration = 60
        
    step = duration / 10.0
    for i in range(1, 10):
        timestamp = step * i
        m, s = divmod(int(timestamp), 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        
        ss_path = f"{file_path}_ss_{i}.jpg"
        cmd = [
            'ffmpeg', '-y', '-ss', str(timestamp), '-i', file_path,
            '-vframes', '1', '-q:v', '2', ss_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(ss_path) and os.path.getsize(ss_path) > 0:
            add_timestamp_to_image(ss_path, time_str, text_color="white", size_mode=size_mode)
            screenshots.append(ss_path)
            
    return screenshots

def convert_or_rescale_video(input_path, output_path, target_format):
    cmd = ['ffmpeg', '-y', '-threads', '0', '-i', input_path]
    
    if target_format in ['audio_mp3', 'mp3']:
        cmd.extend(['-vn', '-acodec', 'libmp3lame', '-q:a', '2', output_path])
    elif target_format == 'mkv':
        cmd.extend(['-c:v', 'copy', '-c:a', 'copy', output_path])
    else:
        scale_map = {'1080': 'scale=-2:1080', '720': 'scale=-2:720', '480': 'scale=-2:480'}
        vf_scale = scale_map.get(target_format, None)
        
        if vf_scale:
            cmd.extend(['-vf', f"{vf_scale},fps=30", '-c:v', 'libx264', '-crf', '30', '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '96k', output_path])
        else:
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', output_path])
        
    process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path
    return input_path

def format_eta(seconds):
    if not seconds or seconds < 0: return "غير معروف"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}س {m}د {s}ث"
    if m > 0: return f"{m}د {s}ث"
    return f"{s}ث"

def create_progress_bar(percentage):
    completed = int(percentage // 10)
    return "█" * completed + "░" * (10 - completed)

async def progress_callback(current, total, status_msg, action_text, start_time, cancel_event):
    if cancel_event.is_set():
        raise Exception("CANCELLED_BY_USER")

    msg_id = status_msg.id
    now = time.time()
    
    if msg_id in LAST_UPDATE_TIME and (now - LAST_UPDATE_TIME[msg_id]) < 3.0 and current != total:
        return
        
    LAST_UPDATE_TIME[msg_id] = now
    
    elapsed_time = now - start_time
    percentage = (current / total) * 100 if total > 0 else 0
    speed = current / elapsed_time if elapsed_time > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    
    curr_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    speed_mb = speed / (1024 * 1024)
    
    progress_bar = create_progress_bar(percentage)
    
    text = (
        f"{action_text}\n"
        f"[{progress_bar}] `{percentage:.1f}%`\n"
        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
        f"🚀 السرعة: `{speed_mb:.2f} MB/s`\n"
        f"⏳ المتبقي: `{format_eta(eta)}`"
    )
    
    try:
        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{msg_id}")])
    except Exception:
        pass

def download_with_ytdlp(url, filepath, fmt='mp4', quality='best'):
    """التحميل عبر yt-dlp لدعم منصات Twitter/X مع ملف الكوكيز"""
    ydl_opts = {
        'outtmpl': filepath,
        'quiet': True,
        'no_warnings': True,
        'headers': BROWSER_HEADERS,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'retries': 5,
        'fragment_retries': 5
    }

    # ربط ملف الكوكيز إن وجد
    if os.path.exists(COOKIES_FILE_PATH):
        ydl_opts['cookiefile'] = COOKIES_FILE_PATH

    if fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
    elif quality != 'best':
        ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        ydl_opts['format'] = 'best'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def download_chunk(url, headers, start, end, chunk_file, cancel_event):
    if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
    req_headers = headers.copy()
    req_headers['Range'] = f"bytes={start}-{end}"
    
    res = requests.get(url, headers=req_headers, stream=True, timeout=20, verify=False)
    res.raise_for_status()
    
    with open(chunk_file, 'wb') as f:
        for chunk in res.iter_content(chunk_size=512*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
            if chunk:
                f.write(chunk)

def download_direct_file(url, filepath, cancel_event, num_threads=8):
    # توجيه روابط Twitter/X تلقائياً إلى yt-dlp
    if is_twitter_or_complex_url(url):
        download_with_ytdlp(url, filepath)
        return

    parsed_url = urlparse(url)
    headers = BROWSER_HEADERS.copy()
    headers['Referer'] = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    
    file_size = 0
    accept_ranges = False

    try:
        head_res = requests.head(url, headers=headers, allow_redirects=True, timeout=5, verify=False)
        if head_res.status_code == 200:
            file_size = int(head_res.headers.get('content-length', 0))
            accept_ranges = head_res.headers.get('accept-ranges', 'none') == 'bytes' or 'content-range' in head_res.headers
    except Exception:
        pass

    if file_size == 0 or not accept_ranges:
        response = requests.get(url, stream=True, headers=headers, timeout=30, verify=False)
        response.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=4*1024*1024):
                if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
                if chunk:
                    f.write(chunk)
        return

    chunk_size = file_size // num_threads
    futures = []
    chunk_files = []
    
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        for i in range(num_threads):
            start = i * chunk_size
            end = file_size - 1 if i == num_threads - 1 else (start + chunk_size - 1)
            chunk_file = f"{filepath}.part_{i}"
            chunk_files.append(chunk_file)
            
            future = executor.submit(download_chunk, url, headers, start, end, chunk_file, cancel_event)
            futures.append(future)

        for future in futures:
            future.result()

    with open(filepath, 'wb') as final_f:
        for chunk_file in chunk_files:
            if os.path.exists(chunk_file):
                with open(chunk_file, 'rb') as cf:
                    shutil.copyfileobj(cf, final_f)
                os.remove(chunk_file)

async def start_execution(chat_id, url, filename, as_doc, quality, media_msg, target_fmt):
    status_msg = await bot.send_message(chat_id, "⏳ **بدء معالجة الطلب...**")
    cancel_event = threading.Event()
    
    save_dir = "downloads"
    os.makedirs(save_dir, exist_ok=True)
    filepath = os.path.join(save_dir, filename)

    try:
        await status_msg.edit("📥 **جاري تحميل الملف...**")
        
        loop = asyncio.get_event_loop()
        
        if url and is_twitter_or_complex_url(url):
            await loop.run_in_executor(None, download_with_ytdlp, url, filepath, target_fmt, quality)
        elif url:
            await loop.run_in_executor(None, download_direct_file, url, filepath, cancel_event)
        elif media_msg:
            start_time = time.time()
            await bot.download_media(
                media_msg, 
                file=filepath, 
                progress_callback=lambda c, t: loop.create_task(
                    progress_callback(c, t, status_msg, "📥 **جاري سحب الفيديو المحلي...**", start_time, cancel_event)
                )
            )

        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            raise Exception("فشل التنزيل أو أن الملف فارغ!")

        # التجميع والتحويل
        if target_fmt in ['audio_mp3', 'mp3', 'mkv'] or quality != 'best':
            await status_msg.edit("⚙️ **جاري تحويل ومعالجة الملف...**")
            converted_path = os.path.join(save_dir, f"converted_{filename}")
            filepath = await loop.run_in_executor(None, convert_or_rescale_video, filepath, converted_path, target_fmt if target_fmt != 'mp4' else quality)

        # استخراج الميتابايت والصورة المصغرة
        duration, width, height, thumb_path = get_video_metadata_and_thumb(filepath)
        user_settings = get_user_settings(chat_id)

        # إنتاج الألبوم 9 صور
        ss_paths = []
        if user_settings['send_screenshots'] and not target_fmt.endswith('mp3'):
            await status_msg.edit("📸 **جاري توليد ألبوم اللقطات...**")
            ss_paths = await loop.run_in_executor(None, generate_grid_screenshots, filepath, duration, user_settings['font_size'])

        await status_msg.edit("📤 **جاري الرفع إلى تلغرام...**")
        start_upload = time.time()

        if ss_paths:
            await bot.send_file(chat_id, ss_paths, caption="📸 **لقطات شاشة من داخل الفيديو:**")

        attributes = []
        if not target_fmt.endswith('mp3'):
            attributes.append(DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=user_settings['streaming_mode']
            ))

        await bot.send_file(
            chat_id,
            filepath,
            caption=f"✅ **تم التحميل بنجاح!**\n📁 `{os.path.basename(filepath)}`",
            force_document=as_doc,
            thumb=thumb_path,
            attributes=attributes,
            progress_callback=lambda c, t: loop.create_task(
                progress_callback(c, t, status_msg, "📤 **جاري الرفع...**", start_upload, cancel_event)
            )
        )

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit(f"❌ **حدث خطأ:** `{str(e)}`")
    finally:
        # التنظيف
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except: pass

async def process_queue(chat_id):
    if chat_id not in QUEUE_LOCKS:
        QUEUE_LOCKS[chat_id] = asyncio.Lock()
        
    async with QUEUE_LOCKS[chat_id]:
        while chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
            task_data = DOWNLOAD_QUEUES[chat_id][0]
            url = task_data.get('url')
            media_msg = task_data.get('media_msg')
            filename = task_data['custom_name']
            as_doc = task_data['as_doc']
            quality = task_data['quality']
            target_fmt = task_data.get('fmt', 'mp4')
            
            await start_execution(chat_id, url, filename, as_doc, quality, media_msg, target_fmt)
            
            if chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
                DOWNLOAD_QUEUES[chat_id].pop(0)

def build_settings_buttons(user_id):
    settings = get_user_settings(user_id)
    current_size_label = FONT_SIZES.get(settings['font_size'], FONT_SIZES['large'])['label']
    
    return [
        [Button.inline(f"📸 ألبوم اللقطات الـ 9: {'✅ مفعل' if settings['send_screenshots'] else '❌ معطل'}", data="toggle_ss")],
        [Button.inline(f"🔤 حجم خط التوقيت: [{current_size_label}]", data="choose_fontsize")],
        [Button.inline(f"🎬 المشغل المباشر: {'✅ مفعل' if settings['streaming_mode'] else '❌ معطل'}", data="toggle_stream")],
        [Button.inline("❌ إغلاق القائمة", data="close_settings")]
    ]

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        f"🚀 **أهلاً بك في بوت التنزيل والتحويل الفائق ({VERSION})**\n\n"
        "✨ **الخدمات المتاحة:**\n"
        "1️⃣ أرسل أي رابط مباشر أو رابط تويتر (X) لتحميله\n"
        "2️⃣ أرسل فيديو محلي لتغيير صيغته، تحويله إلى MP3، أو تعديل الجودة!\n"
        "3️⃣ أرسل الأمر /settings للتحكم بخصائص وميزات البوت وتغيير حجم خط التوقيت."
    )
    await event.respond(welcome_text)

@bot.on(events.NewMessage(pattern=r"^/settings$"))
async def settings_handler(event):
    user_id = event.sender_id
    buttons = build_settings_buttons(user_id)
    await event.respond("⚙️ **لوحة تحكم وإعدادات البوت:**\nانقر على أي ميزة لتغييرها:", buttons=buttons)

@bot.on(events.NewMessage(func=lambda e: e.video or (e.document and e.document.mime_type and e.document.mime_type.startswith('video/'))))
async def video_file_handler(event):
    user_id = event.sender_id
    doc = event.document or event.video
    
    orig_name = "video.mp4"
    if hasattr(doc, 'attributes'):
        for attr in doc.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                orig_name = attr.file_name
                break

    USER_STATES[user_id] = {
        'media_msg': event.message,
        'custom_name': orig_name,
        'quality': 'best',
        'fmt': 'mp4'
    }

    buttons = [
        [Button.inline("🎬 تحويل إلى MP4", data="fmt_mp4"), Button.inline("🎵 تحويل إلى MP3", data="fmt_mp3")],
        [Button.inline("📦 تحويل إلى MKV", data="fmt_mkv")],
        [Button.inline("📁 رفع كملف خام دون تعديل", data="convert_doc")]
    ]
    await event.respond(f"📹 **تم استقبال الفيديو!**\n📁 **الاسم:** `{orig_name}`\n\nاختر الصيغة المطلوبة للتحويل:", buttons=buttons)

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return

    chat_id = event.chat_id
    user_id = event.sender_id
    
    if len(urls) == 1:
        url = urls[0]
        default_name = get_clean_filename(url)
        USER_STATES[user_id] = {'url': url, 'custom_name': default_name, 'quality': 'best', 'fmt': 'mp4'}
        
        buttons = [
            [Button.inline("🎬 صيغة MP4", data="fmt_mp4"), Button.inline("🎵 صيغة MP3", data="fmt_mp3"), Button.inline("📦 صيغة MKV", data="fmt_mkv")],
            [Button.inline("📁 تنزيل كـ (ملف خام)", data="type_doc")],
            [Button.inline("🎯 الجودة: [أفضل جودة 🥇]", data="choose_quality")],
            [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
        ]
        await event.respond(f"🔗 **تم استلام الرابط!**\n\n📁 **الاسم المتوقع:** `{default_name}`", buttons=buttons)
    else:
        if chat_id not in DOWNLOAD_QUEUES:
            DOWNLOAD_QUEUES[chat_id] = []
            
        added_count = 0
        for u in urls:
            d_name = get_clean_filename(u)
            DOWNLOAD_QUEUES[chat_id].append({'url': u, 'custom_name': d_name, 'as_doc': False, 'quality': 'best', 'fmt': 'mp4'})
            added_count += 1
            
        await event.respond(f"📥 **تمت إضافة {added_count} روابط إلى الطابور!**")
        asyncio.create_task(process_queue(chat_id))

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    chat_id = event.chat_id
    
    if data == "toggle_ss":
        settings = get_user_settings(user_id)
        settings['send_screenshots'] = not settings['send_screenshots']
        await event.edit("⚙️ **لوحة تحكم وإعدادات البوت:**", buttons=build_settings_buttons(user_id))

    elif data == "toggle_stream":
        settings = get_user_settings(user_id)
        settings['streaming_mode'] = not settings['streaming_mode']
        await event.edit("⚙️ **لوحة تحكم وإعدادات البوت:**", buttons=build_settings_buttons(user_id))

    elif data == "choose_fontsize":
        size_buttons = [
            [Button.inline("🔍 صغير", data="set_font_small"), Button.inline("📐 وسط", data="set_font_medium")],
            [Button.inline("📢 كبير", data="set_font_large"), Button.inline("💥 كبير جداً", data="set_font_xlarge")],
            [Button.inline("🔙 العودة للإعدادات", data="back_to_settings")]
        ]
        await event.edit("🔤 **اختر نمط حجم خط التوقيت على اللقطات:**", buttons=size_buttons)

    elif data.startswith("set_font_"):
        selected_size = data.replace("set_font_", "")
        settings = get_user_settings(user_id)
        settings['font_size'] = selected_size
        await event.edit("⚙️ **لوحة تحكم وإعدادات البوت:**", buttons=build_settings_buttons(user_id))

    elif data == "back_to_settings":
        await event.edit("⚙️ **لوحة تحكم وإعدادات البوت:**", buttons=build_settings_buttons(user_id))

    elif data == "close_settings":
        await event.delete()

    elif data == "ask_rename":
        if user_id in USER_STATES:
            USER_STATES[user_id]['state'] = 'waiting_for_name'
            await event.edit("✏️ **أرسل الآن الاسم الجديد للملف:**")

    elif data.startswith("fmt_"):
        selected_fmt = data.replace("fmt_", "")
        if user_id in USER_STATES:
            USER_STATES[user_id]['fmt'] = selected_fmt
            current_name = USER_STATES[user_id]['custom_name']
            USER_STATES[user_id]['custom_name'] = change_extension(current_name, selected_fmt)
            
            state_data = USER_STATES[user_id]
            
            if chat_id not in DOWNLOAD_QUEUES:
                DOWNLOAD_QUEUES[chat_id] = []
                
            DOWNLOAD_QUEUES[chat_id].append({
                'url': state_data.get('url'),
                'media_msg': state_data.get('media_msg'),
                'custom_name': state_data['custom_name'],
                'as_doc': False,
                'quality': state_data.get('quality', 'best'),
                'fmt': selected_fmt
            })
            USER_STATES.pop(user_id, None)
            await event.delete()
            asyncio.create_task(process_queue(chat_id))

    elif data in ["type_doc", "convert_doc"]:
        if user_id in USER_STATES:
            state_data = USER_STATES[user_id]
            if chat_id not in DOWNLOAD_QUEUES:
                DOWNLOAD_QUEUES[chat_id] = []
                
            DOWNLOAD_QUEUES[chat_id].append({
                'url': state_data.get('url'),
                'media_msg': state_data.get('media_msg'),
                'custom_name': state_data['custom_name'],
                'as_doc': True,
                'quality': state_data.get('quality', 'best'),
                'fmt': state_data.get('fmt', 'mp4')
            })
            USER_STATES.pop(user_id, None)
            await event.delete()
            asyncio.create_task(process_queue(chat_id))

    elif data == "choose_quality":
        q_buttons = [
            [Button.inline("🥇 1080p", data="q_1080"), Button.inline("🥈 720p", data="q_720")],
            [Button.inline("🥉 480p", data="q_480"), Button.inline("⚡ أفضل جودة", data="q_best")]
        ]
        await event.edit("🎬 **اختر الجودة المطلوبة:**", buttons=q_buttons)

    elif data.startswith("q_"):
        selected_q = data.replace("q_", "")
        if user_id in USER_STATES:
            USER_STATES[user_id]['quality'] = selected_q
            await event.edit(f"✅ **تم اختيار الجودة: {selected_q}**\nاختر الآن الصيغة للبدء:")

# تشغيل البوت
bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
