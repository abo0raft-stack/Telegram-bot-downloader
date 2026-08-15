import asyncio
import os
import shutil
import threading
import time
import requests
import yt_dlp
import subprocess
from urllib.parse import unquote, urlparse
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors import FloodWaitError
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v26.2-403BypassFixed"
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_DOWNLOADS = {}
LAST_UPDATE_TIME = {}
LAST_BYTES = {}
USER_STATES = {}

# --- تنظيف الملفات المتبقية عند بدء التشغيل ---
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

clean_download_folder()

# --- سيرفر وهمي لتفادي توقف الخدمة ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

# --- استخراج اسم الملف المباشر بشكل دقيق ---
def get_clean_filename(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv']):
        filename = "video_download.mp4"
    return filename

# --- استخراج أبعاد ومدة الفيديو ---
def get_video_metadata(filepath):
    duration, width, height = 0, 1280, 720
    try:
        parser = createParser(filepath)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    if metadata.has("duration"): 
                        duration = int(metadata.get('duration').seconds)
                    if metadata.has("width"): 
                        width = int(metadata.get('width'))
                    if metadata.has("height"): 
                        height = int(metadata.get('height'))
    except Exception as e:
        print(f"Metadata Extraction Error: {e}")
    return duration, width, height

def format_eta(seconds):
    if not seconds or seconds < 0: return "غير معروف"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}س {m}د {s}ث"
    if m > 0: return f"{m}د {s}ث"
    return f"{s}ث"

def generate_thumbnail_fallback(video_path, thumb_path, duration):
    try:
        target_sec = "00:00:02" if duration > 3 else "00:00:00"
        cmd = [
            'ffmpeg', '-ss', target_sec,
            '-i', video_path,
            '-vframes', '1',
            '-q:v', '2',
            '-y', thumb_path
        ]
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if process.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            im = Image.open(thumb_path)
            im.thumbnail((320, 320))
            im.convert('RGB').save(thumb_path, 'JPEG')
            return thumb_path
    except Exception as e:
        print(f"Thumbnail Exception: {e}")
    return None

def generate_9_individual_shots(video_path, msg_id, duration):
    if duration <= 0: duration = 60
    shots = []
    step = duration / 10
    timestamps = [max(1, int(step * i)) for i in range(1, 10)]
    
    temp_dir = f"downloads/shots_{msg_id}"
    os.makedirs(temp_dir, exist_ok=True)
    
    for idx, ts in enumerate(timestamps):
        shot_file = os.path.join(temp_dir, f"shot_{idx+1}.jpg")
        try:
            cmd = [
                'ffmpeg', '-ss', str(ts),
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '2',
                '-y', shot_file
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if os.path.exists(shot_file) and os.path.getsize(shot_file) > 0:
                shots.append(shot_file)
        except Exception as e:
            print(f"Shot {idx+1} Error: {e}")
            
    return shots, temp_dir

async def upload_progress_callback(current, total, status_msg, cancel_event):
    if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
    
    now = time.time()
    msg_id = status_msg.id
    
    if msg_id not in LAST_UPDATE_TIME:
        LAST_UPDATE_TIME[msg_id] = now
        LAST_BYTES[msg_id] = current
        return

    time_delta = now - LAST_UPDATE_TIME[msg_id]
    if time_delta < 2.0: return

    bytes_delta = current - LAST_BYTES[msg_id]
    speed = bytes_delta / time_delta
    speed_mb = speed / (1024 * 1024)

    LAST_UPDATE_TIME[msg_id] = now
    LAST_BYTES[msg_id] = current

    percent = (current / total) * 100 if total > 0 else 0
    curr_mb, total_mb = current / (1024 * 1024), total / (1024 * 1024)
    
    eta_sec = (total - current) / speed if speed > 0 else 0
    eta_str = format_eta(eta_sec)

    text = (
        f"📤 **جاري الرفع إلى تليجرام...**\n"
        f"📊 النسبة: `{percent:.1f}%`\n"
        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
        f"🚀 السرعة: `{speed_mb:.2f} MB/s`\n"
        f"⏳ المتبقي: `{eta_str}`"
    )
    try: 
        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء الرفع", data=f"cancel_{status_msg.id}")])
    except: pass

# --- دالة التحميل المباشر المحدثة لتجاوز حظر 403 ---
def download_direct_file(url, filepath, status_msg, loop, cancel_event):
    parsed_url = urlparse(url)
    domain_referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
        'Accept-Language': 'ar,en-US;q=0.7,en;q=0.3',
        'Referer': domain_referer,
        'Connection': 'keep-alive'
    }
    
    session = requests.Session()
    response = session.get(url, stream=True, headers=headers, timeout=60, allow_redirects=True)
    
    if response.status_code == 403:
        raise Exception("HTTP_403_FORBIDDEN")
        
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update_time = start_time
    last_downloaded = 0
    
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                
                now = time.time()
                time_delta = now - last_update_time
                
                if time_delta >= 2.0:
                    speed = (downloaded - last_downloaded) / time_delta
                    speed_mb = speed / (1024 * 1024)
                    
                    percent = (downloaded / total_size * 100) if total_size > 0 else 0
                    downloaded_mb = downloaded / (1024 * 1024)
                    total_mb = total_size / (1024 * 1024)
                    
                    eta_sec = (total_size - downloaded) / speed if speed > 0 else 0
                    eta_str = format_eta(eta_sec)

                    last_update_time = now
                    last_downloaded = downloaded
                    
                    text = (
                        f"📥 **جاري التحميل المباشر...**\n"
                        f"📊 النسبة: `{percent:.1f}%`\n"
                        f"💾 الحجم: `{downloaded_mb:.1f}MB / {total_mb:.1f}MB`\n"
                        f"🚀 السرعة: `{speed_mb:.2f} MB/s`\n"
                        f"⏳ المتبقي: `{eta_str}`"
                    )
                    
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit(text, buttons=[Button.inline("❌ إلغاء التحميل", data=f"cancel_{status_msg.id}")]), 
                        loop
                    )

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        f"🚀 **أهلاً بك في بوت التنزيل والرفع الشامل ({VERSION})**\n\n"
        "✨ **المميزات المفعلة:**\n"
        " 🎬 **رفع كـ فيديو ميديا أو ملف مستند**.\n"
        " 🎯 **اختيار الجودة (Best / 720p / 480p / MP3 صوت)**.\n"
        " 📸 **إرسال 9 لقطات شاشة كألبوم صور مجمع**.\n"
        " 🎨 **الصورة المصغرة الأصلية** التابعة للفيديو.\n"
        " ✏️ **إعادة تسمية الملف** بسهولة.\n\n"
        "👇 **أرسل أي رابط للبدء!**"
    )
    await event.respond(welcome_text)

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def url_handler(event):
    url = event.text.strip()
    default_name = get_clean_filename(url)
        
    USER_STATES[event.sender_id] = {'url': url, 'custom_name': default_name, 'quality': 'best'}
    
    buttons = [
        [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف خام)", data="type_doc")],
        [Button.inline("🎯 الجودة: [أفضل جودة 🥇]", data="choose_quality")],
        [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
    ]
    await event.respond(f"🔗 **تم استلام الرابط!**\n\n📁 **الاسم المتوقع:** `{default_name}`", buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    
    if data == "ask_rename":
        if user_id in USER_STATES:
            USER_STATES[user_id]['state'] = 'waiting_for_name'
            await event.edit("✏️ **أرسل الآن الاسم الجديد للملف:**")
            
    elif data == "choose_quality":
        quality_buttons = [
            [Button.inline("🔝 أفضل جودة المتاحة", data="set_q_best")],
            [Button.inline("📺 1080p", data="set_q_1080"), Button.inline("📺 720p", data="set_q_720")],
            [Button.inline("📱 480p", data="set_q_480"), Button.inline("🎵 صوت فقط MP3", data="set_q_audio")]
        ]
        await event.edit("⚙️ **اختر الجودة المطلوبة للتحميل:**", buttons=quality_buttons)
        
    elif data.startswith("set_q_"):
        q_map = {'set_q_best': 'best', 'set_q_1080': '1080', 'set_q_720': '720', 'set_q_480': '480', 'set_q_audio': 'audio'}
        selected_q = q_map.get(data, 'best')
        if user_id in USER_STATES:
            USER_STATES[user_id]['quality'] = selected_q
            
        q_label = {"best": "أفضل جودة", "1080": "1080p", "720": "720p", "480": "480p", "audio": "صوت MP3"}[selected_q]
        buttons = [
            [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف خام)", data="type_doc")],
            [Button.inline(f"🎯 الجودة: [{q_label}]", data="choose_quality")],
            [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
        ]
        await event.edit(f"✅ **تم اختيار:** `{q_label}`\nاختر صيغة الرفع:", buttons=buttons)

    elif data in ["type_stream", "type_doc"]:
        state_data = USER_STATES.get(user_id)
        if state_data:
            as_doc = (data == "type_doc")
            quality = state_data.get('quality', 'best')
            await event.delete()
            await start_execution(event.chat_id, state_data['url'], state_data['custom_name'], as_doc, quality)
            
    elif data.startswith("cancel_"):
        msg_id = event.message_id
        if msg_id in ACTIVE_DOWNLOADS:
            ACTIVE_DOWNLOADS[msg_id].set()
            await event.answer("جاري الإلغاء...", alert=False)

@bot.on(events.NewMessage)
async def text_handler(event):
    user_id = event.sender_id
    if user_id in USER_STATES and USER_STATES[user_id].get('state') == 'waiting_for_name':
        new_name = event.text.strip()
        quality = USER_STATES[user_id].get('quality', 'best')
        if not any(new_name.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3']):
            new_name += ".mp4"
        url = USER_STATES[user_id]['url']
        USER_STATES.pop(user_id, None)
        
        USER_STATES[user_id] = {'url': url, 'custom_name': new_name, 'quality': quality}
        buttons = [
            [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف)", data="type_doc")],
            [Button.inline("🎯 تغيير الجودة", data="choose_quality")]
        ]
        await event.respond(f"✅ تم تغيير الاسم إلى: `{new_name}`\nاختر طريقة الرفع:", buttons=buttons)

# --- تنفيذ عملية التحميل والرفع ---
async def start_execution(chat_id, url, filename_title, as_doc=False, quality='best'):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التحضير...**")
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event
    await status_msg.edit("🔍 **جاري التحضير...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])

    loop = asyncio.get_event_loop()
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    temp_shots_dir = None
    
    try:
        clean_url_path = urlparse(url).path.lower()
        is_direct_url = any(clean_url_path.endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv'])
        
        if is_direct_url:
            await status_msg.edit("📥 **بدء التحميل المباشر...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
            try:
                await loop.run_in_executor(None, download_direct_file, url, file_path, status_msg, loop, cancel_event)
            except Exception as direct_err:
                if "HTTP_403_FORBIDDEN" in str(direct_err):
                    await status_msg.edit("⚠️ **تم كشف حماية السيرفر، جاري التجاوز عبر yt-dlp...**")
                    parsed_u = urlparse(url)
                    ydl_opts = {
                        'format': 'best',
                        'outtmpl': file_path,
                        'quiet': True,
                        'http_headers': {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Referer': f"{parsed_u.scheme}://{parsed_u.netloc}/"
                        }
                    }
                    await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
                else:
                    raise direct_err
        else:
            def progress_hook_ytdlp(d):
                if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
                if d['status'] == 'downloading':
                    now = time.time()
                    if status_msg.id in LAST_UPDATE_TIME and (now - LAST_UPDATE_TIME[status_msg.id]) < 2.0: return
                    LAST_UPDATE_TIME[status_msg.id] = now
                    
                    p = d.get('_percent_str', '0%').strip()
                    s = d.get('_speed_str', 'N/A').strip()
                    eta = d.get('_eta_str', 'N/A').strip()
                    downloaded_bytes = d.get('downloaded_bytes', 0)
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    
                    curr_mb = downloaded_bytes / (1024 * 1024)
                    total_mb = total_bytes / (1024 * 1024) if total_bytes > 0 else 0
                    
                    text = (
                        f"📥 **جاري التحميل...**\n"
                        f"📊 النسبة: `{p}`\n"
                        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
                        f"🚀 السرعة: `{s}`\n"
                        f"⏳ المتبقي: `{eta}`"
                    )
                    asyncio.run_coroutine_threadsafe(status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")]), loop)

            format_opt = 'best'
            if quality == '1080': format_opt = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
            elif quality == '720': format_opt = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            elif quality == '480': format_opt = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
            elif quality == 'audio': format_opt = 'bestaudio/best'

            ydl_opts = {
                'format': format_opt,
                'quiet': True,
                'outtmpl': file_path,
                'writethumbnail': True,
                'progress_hooks': [progress_hook_ytdlp]
            }
            
            if quality == 'audio':
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]

            await status_msg.edit("⏳ **بدء التحميل...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
            await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        if quality == 'audio' and not file_path.endswith('.mp3'):
            base_p = os.path.splitext(file_path)[0]
            if os.path.exists(f"{base_p}.mp3"): file_path = f"{base_p}.mp3"

        if os.path.exists(file_path):
            if os.path.getsize(file_path) < 10 * 1024: 
                raise Exception("الملف المحمل صغير جداً أو تالف.")

            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            
            final_thumb = None
            base_filename = os.path.splitext(file_path)[0]
            
            for ext in ['.jpg', '.webp', '.png', '.jpeg']:
                potential_thumb = base_filename + ext
                if os.path.exists(potential_thumb):
                    try:
                        im = Image.open(potential_thumb)
                        im.thumbnail((320, 320))
                        im.convert('RGB').save(thumb_path, 'JPEG')
                        final_thumb = thumb_path
                        os.remove(potential_thumb)
                        break
                    except Exception as e:
                        print(f"Thumb Conversion Error: {e}")

            if not final_thumb or not os.path.exists(final_thumb):
                final_thumb = await loop.run_in_executor(None, generate_thumbnail_fallback, file_path, thumb_path, duration)

            caption_text = (
                f"🎬 **اسم الملف:** `{filename_title}`\n"
                f"🎯 **الجودة:** `{quality}`\n\n"
                f"✅ **تم التحميل والرفع بنجاح!**"
            )

            await status_msg.edit("📤 **جاري بدء الرفع إلى تليجرام...**", buttons=None)

            thumb_to_send = final_thumb if (final_thumb and os.path.exists(final_thumb)) else None

            retry = True
            while retry:
                try:
                    if as_doc or quality == 'audio':
                        await bot.send_file(
                            chat_id, 
                            file_path, 
                            caption=caption_text,
                            thumb=thumb_to_send,
                            force_document=as_doc,
                            progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                        )
                    else:
                        video_attributes = DocumentAttributeVideo(
                            duration=duration,
                            w=width,
                            h=height,
                            supports_streaming=True
                        )
                        await bot.send_file(
                            chat_id, 
                            file_path, 
                            caption=caption_text,
                            thumb=thumb_to_send,
                            attributes=[video_attributes],
                            supports_streaming=True,
                            progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                        )
                    retry = False
                except FloodWaitError as fwe:
                    await status_msg.edit(f"⏳ **فرض تلجرام الانتظار لمدة {fwe.seconds} ثانية... سيتتابع الرفع تلقائياً.**")
                    await asyncio.sleep(fwe.seconds)

            if quality != 'audio':
                await status_msg.edit("📸 **تم رفع الملف! جاري استخراج وإرسال ألبوم اللقطات...**", buttons=None)
                shots_list, temp_shots_dir = await loop.run_in_executor(None, generate_9_individual_shots, file_path, status_msg.id, duration)
                if shots_list:
                    await bot.send_file(chat_id, shots_list, caption="📸 **لقطات شاشة من الفيديو**")

            await status_msg.delete()

    except Exception as e:
        err_msg = str(e)
        if "CANCELLED_BY_USER" in err_msg:
            await status_msg.edit("❌ **تم إلغاء العملية بواسطة المستخدم.**", buttons=None)
        else:
            await status_msg.edit(f"❌ **حدث خطأ أثناء المعالجة:**\n`{err_msg}`", buttons=None)

    finally:
        ACTIVE_DOWNLOADS.pop(status_msg.id, None)
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(thumb_path): os.remove(thumb_path)
        if temp_shots_dir and os.path.exists(temp_shots_dir):
            shutil.rmtree(temp_shots_dir, ignore_errors=True)

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
