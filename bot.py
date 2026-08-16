import asyncio
import os
import shutil
import threading
import time
import gc
import re
import requests
import yt_dlp
import subprocess
from urllib.parse import unquote, urlparse
from PIL import Image, ImageDraw, ImageFont
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors import FloodWaitError
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v48.4-Fix429Error"
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

FONT_SIZES = {
    'small': {'ratio': 0.04, 'min': 18, 'label': '🔍 صغير'},
    'medium': {'ratio': 0.07, 'min': 30, 'label': '📐 وسط'},
    'large': {'ratio': 0.12, 'min': 50, 'label': '📢 كبير'},
    'xlarge': {'ratio': 0.18, 'min': 75, 'label': '💥 كبير جداً'}
}

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Ch-Ua': '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1'
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
                print(f"Failed to delete {file_path}. Reason: {e}")
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

def get_clean_filename(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if filename.endswith('.m3u8'):
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

            margin = int(img.width * 0.02)
            x = img.width - text_w - margin
            y = margin

            padding_x = int(font_size * 0.35)
            padding_y = int(font_size * 0.2)
            rect_box = [x - padding_x, y - padding_y, x + text_w + padding_x, y + text_h + padding_y]

            if text_color == "black":
                fill_color = (0, 0, 0, 255)
                box_color = (255, 255, 255, 230)
            else:
                fill_color = (255, 255, 255, 255)
                box_color = (0, 0, 0, 210)

            draw.rectangle(rect_box, fill=box_color)
            draw.text((x, y), time_str, fill=fill_color, font=font)

            final_img = Image.alpha_composite(img, txt_layer).convert("RGB")
            final_img.save(image_path, quality=95)
    except Exception as e:
        print(f"Error drawing timestamp on image: {e}")

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

def download_direct_file(url, filepath, status_msg, loop, cancel_event):
    parsed_url = urlparse(url)
    referer_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    headers = BROWSER_HEADERS.copy()
    headers['Referer'] = referer_url
    
    session = requests.Session()
    session.verify = False
    
    # محاولة الحصول على المورد مع إضافة تأخير طفيف ومقبض استجابة للخطأ 429
    response = session.get(url, stream=True, headers=headers, timeout=60, allow_redirects=True)
    
    # في حال مواجهة 429 نلقي استثناء محدد للتحويل الآلي إلى yt-dlp
    if response.status_code == 429:
        raise Exception("TOO_MANY_REQUESTS_429")
        
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=4*1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                asyncio.run_coroutine_threadsafe(
                    progress_callback(downloaded, total_size, status_msg, "📥 **جاري التحميل المباشر السريع...**", start_time, cancel_event),
                    loop
                )

def download_with_ytdlp(url, filepath, fmt='mp4', quality='best'):
    ydl_opts = {
        'outtmpl': filepath,
        'quiet': True,
        'no_warnings': True,
        'headers': BROWSER_HEADERS,
        'concurrent_fragment_downloads': 5,
        'retries': 10,
        'fragment_retries': 10
    }
    
    if fmt in ['mp3', 'audio_mp3']:
        ydl_opts['format'] = 'bestaudio/best'
    elif quality != 'best':
        ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        ydl_opts['format'] = 'best'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

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
        "1️⃣ أرسل أي رابط لتحميله واختيار صيغته (MP4, MP3, MKV)\n"
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
            
    elif data == "choose_quality":
        quality_buttons = [
            [Button.inline("🔝 أفضل جودة المتاحة", data="set_q_best")],
            [Button.inline("📺 1080p", data="set_q_1080"), Button.inline("📺 720p", data="set_q_720")],
            [Button.inline("📱 480p", data="set_q_480")]
        ]
        await event.edit("⚙️ **اختر الجودة المطلوبة:**", buttons=quality_buttons)
        
    elif data.startswith("set_q_"):
        q_map = {'set_q_best': 'best', 'set_q_1080': '1080', 'set_q_720': '720', 'set_q_480': '480'}
        selected_q = q_map.get(data, 'best')
        if user_id in USER_STATES:
            USER_STATES[user_id]['quality'] = selected_q
            
        q_label = {"best": "أفضل جودة", "1080": "1080p", "720": "720p", "480": "480p"}[selected_q]
        buttons = [
            [Button.inline("🎬 صيغة MP