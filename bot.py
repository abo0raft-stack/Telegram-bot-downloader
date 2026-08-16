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
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v34.0-Fix446SafeUpload"
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_DOWNLOADS = {}
LAST_UPDATE_TIME = {}
LAST_BYTES = {}
USER_STATES = {}

DOWNLOAD_QUEUES = {}
QUEUE_LOCKS = {}

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive',
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

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

def get_clean_filename(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if '.m3u8' in filename.lower():
        filename = filename.split('.m3u8')[0] + '.mp4'
    elif not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv']):
        filename = "video_download.mp4"
    return filename

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
        print(f"Metadata Extraction Safe Skip: {e}")
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
        cmd = ['ffmpeg', '-ss', target_sec, '-i', video_path, '-vframes', '1', '-q:v', '2', '-y', thumb_path]
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if process.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            im = Image.open(thumb_path)
            im.thumbnail((320, 320))
            im.convert('RGB').save(thumb_path, 'JPEG')
            im.close()
            return thumb_path
    except Exception as e:
        print(f"Thumbnail Safe Skip: {e}")
    return None

def generate_9_individual_shots(video_path, msg_id, duration):
    shots = []
    temp_dir = f"downloads/shots_{msg_id}"
    try:
        if duration <= 0: duration = 60
        step = duration / 10
        timestamps = [max(1, int(step * i)) for i in range(1, 10)]
        
        os.makedirs(temp_dir, exist_ok=True)
        
        for idx, ts in enumerate(timestamps):
            shot_file = os.path.join(temp_dir, f"shot_{idx+1}.jpg")
            cmd = ['ffmpeg', '-ss', str(ts), '-i', video_path, '-vframes', '1', '-q:v', '2', '-y', shot_file]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if os.path.exists(shot_file) and os.path.getsize(shot_file) > 0:
                shots.append(shot_file)
    except Exception as e:
        print(f"Shots Safe Skip: {e}")
            
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

    text = (
        f"📤 **جاري الرفع إلى تليجرام...**\n"
        f"📊 النسبة: `{percent:.1f}%`\n"
        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
        f"🚀 السرعة: `{speed_mb:.2f} MB/s`\n"
        f"⏳ المتبقي: `{format_eta(eta_sec)}`"
    )
    try: 
        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء الرفع", data=f"cancel_{status_msg.id}")])
    except: pass

async def process_queue(chat_id):
    if chat_id not in QUEUE_LOCKS:
        QUEUE_LOCKS[chat_id] = asyncio.Lock()
        
    async with QUEUE_LOCKS[chat_id]:
        while chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
            task_data = DOWNLOAD_QUEUES[chat_id][0]
            url = task_data['url']
            filename = task_data['custom_name']
            as_doc = task_data['as_doc']
            quality = task_data['quality']
            
            await start_execution(chat_id, url, filename, as_doc, quality)
            
            if chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
                DOWNLOAD_QUEUES[chat_id].pop(0)

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        f"🚀 **أهلاً بك في بوت التنزيل والرفع الشامل ({VERSION})**\n\n"
        "✨ **أرسل الرابط مباشرة للبدء!**"
    )
    await event.respond(welcome_text)

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return

    chat_id = event.chat_id
    user_id = event.sender_id
    
    if len(urls) == 1:
        url = urls[0]
        default_name = get_clean_filename(url)
        USER_STATES[user_id] = {'url': url, 'custom_name': default_name, 'quality': 'best'}
        
        buttons = [
            [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف خام)", data="type_doc")],
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
            DOWNLOAD_QUEUES[chat_id].append({'url': u, 'custom_name': d_name, 'as_doc': False, 'quality': 'best'})
            added_count += 1
            
        await event.respond(f"📥 **تمت إضافة {added_count} روابط إلى الطابور!**")
        asyncio.create_task(process_queue(chat_id))

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    chat_id = event.chat_id
    
    if data == "ask_rename":
        if user_id in USER_STATES:
            USER_STATES[user_id]['state'] = 'waiting_for_name'
            await event.edit("✏️ **أرسل الآن الاسم الجديد للملف:**")
            
    elif data == "choose_quality":
        quality_buttons = [
            [Button.inline("🔝 أفضل جودة المتاحة", data="set_q_best")],
            [Button.inline("📺 1080p", data="set_q_1080"), Button.inline("📺 720p", data="set_q_720")],
            [Button.inline("📱 480p", data="set_q_480"), Button.inline("🎵 صوت MP3", data="set_q_audio")]
        ]
        await event.edit("⚙️ **اختر الجودة المطلوبة:**", buttons=quality_buttons)
        
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
            
            if chat_id not in DOWNLOAD_QUEUES:
                DOWNLOAD_QUEUES[chat_id] = []
                
            DOWNLOAD_QUEUES[chat_id].append({
                'url': state_data['url'],
                'custom_name': state_data['custom_name'],
                'as_doc': as_doc,
                'quality': quality
            })
            USER_STATES.pop(user_id, None)
            asyncio.create_task(process_queue(chat_id))
            
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

async def start_execution(chat_id, url, filename_title, as_doc=False, quality='best'):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التحضير...**")
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event

    loop = asyncio.get_event_loop()
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    temp_shots_dir = None
    
    try:
        parsed_u = urlparse(url)
        referer_header = f"{parsed_u.scheme}://{parsed_u.netloc}/"

        await status_msg.edit("⚡ **جاري التنزيل السريع...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])

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
                curr_mb = downloaded_bytes / (1024 * 1024)
                
                text = (
                    f"📡 **جاري تنزيل M3U8...**\n"
                    f"📊 النسبة: `{p}`\n"
                    f"💾 الحجم المجمّع: `{curr_mb:.1f}MB`\n"
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
            'outtmpl': file_path,
            'quiet': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'progress_hooks': [progress_hook_ytdlp],
            'concurrent_fragment_downloads': 16,
            'merge_output_format': 'mp4',
            'user_agent': BROWSER_HEADERS['User-Agent'],
            'referer': referer_header,
        }

        await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        if os.path.exists(file_path):
            if os.path.getsize(file_path) < 10 * 1024:
                raise Exception("الملف المحمل غير مكتمل أو تالف.")

            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            final_thumb = await loop.run_in_executor(None, generate_thumbnail_fallback, file_path, thumb_path, duration)

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            caption_text = (
                f"🎬 **اسم الملف:** `{filename_title}`\n"
                f"💾 **الحجم:** `{file_size_mb:.1f} MB`\n\n"
                f"✅ **تم الرفع بنجاح!**"
            )

            await status_msg.edit("📤 **جاري بدء الرفع إلى تليجرام...**")

            thumb_to_send = final_thumb if (final_thumb and os.path.exists(final_thumb)) else None

            if as_doc or quality == 'audio':
                await bot.send_file(
                    chat_id, file_path, caption=caption_text, thumb=thumb_to_send,
                    force_document=as_doc,
                    progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                )
            else:
                video_attributes = DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)
                await bot.send_file(
                    chat_id, file_path, caption=caption_text, thumb=thumb_to_send,
                    attributes=[video_attributes], supports_streaming=True,
                    progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                )

            if quality != 'audio':
                try:
                    shots_list, temp_shots_dir = await loop.run_in_executor(None, generate_9_individual_shots, file_path, status_msg.id, duration)
                    if shots_list:
                        await bot.send_file(chat_id, shots_list, caption="📸 **لقطات شاشة من الفيديو**")
                except Exception as shot_e:
                    print(f"Shots Error Ignored: {shot_e}")

            await status_msg.delete()

    except Exception as e:
        err_msg = str(e)
        if hasattr(e, 'user_message'): err_msg = e.user_message
        elif hasattr(e, 'msg'): err_msg = e.msg
        
        if "CANCELLED_BY_USER" in err_msg:
            await status_msg.edit("❌ **تم إلغاء العملية.**")
        else:
            await status_msg.edit(f"❌ **حدث خطأ أثناء المعالجة:**\n`{err_msg}`")

    finally:
        ACTIVE_DOWNLOADS.pop(status_msg.id, None)
        LAST_UPDATE_TIME.pop(status_msg.id, None)
        LAST_BYTES.pop(status_msg.id, None)
        
        for p in [file_path, thumb_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
                
        if temp_shots_dir and os.path.exists(temp_shots_dir):
            shutil.rmtree(temp_shots_dir, ignore_errors=True)
            
        gc.collect()

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
