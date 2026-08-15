import asyncio
import os
import threading
import time
import requests
import yt_dlp
import subprocess
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v23.0-UploadVideoFirst"
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_DOWNLOADS = {}
LAST_UPDATE_TIME = {}
LAST_BYTES = {}
USER_STATES = {}

# --- سيرفر وهمي لتفادي توقف الخدمة ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        f"🚀 **أهلاً بك في بوت التنزيل والرفع المتقدم ({VERSION})**\n\n"
        "✨ **المميزات:**\n"
        " 🎬 **رفع الفيديو أولاً** مع مشغل ميديا وشريط تقدم مباشر.\n"
        " 📸 **إرسال 9 لقطات شاشة بعد الانتهاء من الرفع** تلقائياً.\n"
        " 🎨 **الصورة المصغرة الأصلية** التابعة للفيديو.\n"
        " ✏️ **إعادة تسمية الملف** قبل البدء.\n\n"
        "👇 **أرسل أي رابط للبدء!**"
    )
    await event.respond(welcome_text)

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

# --- توليد صورة مصغرة عبر FFmpeg ---
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

# --- توليد 9 لقطات منفصلة ---
def generate_9_individual_shots(video_path, msg_id, duration):
    if duration <= 0:
        duration = 60
        
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

# --- تحديث شريط تقدم الرفع ---
async def upload_progress_callback(current, total, status_msg, cancel_event):
    if cancel_event.is_set(): 
        raise Exception("CANCELLED_BY_USER")
    
    now = time.time()
    msg_id = status_msg.id
    
    if msg_id not in LAST_UPDATE_TIME:
        LAST_UPDATE_TIME[msg_id] = now
        LAST_BYTES[msg_id] = current
        return

    time_delta = now - LAST_UPDATE_TIME[msg_id]
    if time_delta < 2.0: 
        return

    bytes_delta = current - LAST_BYTES[msg_id]
    speed = bytes_delta / time_delta
    speed_mb = speed / (1024 * 1024)

    LAST_UPDATE_TIME[msg_id] = now
    LAST_BYTES[msg_id] = current

    percent = (current / total) * 100 if total > 0 else 0
    curr_mb, total_mb = current / (1024 * 1024), total / (1024 * 1024)
    
    text = (
        f"📤 **جاري الرفع إلى تليجرام...**\n"
        f"📊 النسبة: `{percent:.1f}%`\n"
        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
        f"🚀 السرعة: `{speed_mb:.2f} MB/s`"
    )
    try: 
        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء الرفع", data=f"cancel_{status_msg.id}")])
    except: pass

# --- دالة التحميل المباشر ---
def download_direct_file(url, filepath, status_msg, loop, cancel_event):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    response = requests.get(url, stream=True, headers=headers, timeout=30)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update_time = start_time
    last_downloaded = 0
    
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024*1024):
            if cancel_event.is_set(): 
                raise Exception("CANCELLED_BY_USER")
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
                    
                    last_update_time = now
                    last_downloaded = downloaded
                    
                    text = (
                        f"📥 **جاري التحميل المباشر...**\n"
                        f"📊 النسبة: `{percent:.1f}%`\n"
                        f"💾 الحجم: `{downloaded_mb:.1f}MB / {total_mb:.1f}MB`\n"
                        f"🚀 السرعة: `{speed_mb:.2f} MB/s`"
                    )
                    
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit(text, buttons=[Button.inline("❌ إلغاء التحميل", data=f"cancel_{status_msg.id}")]), 
                        loop
                    )

# --- استقبال الروابط ---
@bot.on(events.NewMessage(pattern=r"^https?://"))
async def url_handler(event):
    url = event.text.strip()
    default_name = url.rsplit('/', 1)[-1].rsplit('?', 1)[0]
    if not default_name.endswith(('.mp4', '.mkv', '.avi', '.mov')): 
        default_name += ".mp4"
        
    USER_STATES[event.sender_id] = {'url': url, 'custom_name': default_name}
    buttons = [
        [Button.inline("🚀 بدء التنزيل مباشرة", data="start_dl_default")],
        [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
    ]
    await event.respond(f"🔗 **تم استلام الرابط!**\n\n📁 **الاسم الافتراضي:** `{default_name}`", buttons=buttons)

# --- معالجة الأزرار ---
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    
    if data == "ask_rename":
        USER_STATES[user_id]['state'] = 'waiting_for_name'
        await event.edit("✏️ **أرسل الآن الاسم الجديد للملف:**")
    elif data == "start_dl_default":
        state_data = USER_STATES.get(user_id)
        if state_data:
            await event.delete()
            await start_execution(event.chat_id, state_data['url'], state_data['custom_name'])
    elif data.startswith("cancel_"):
        msg_id = event.message_id
        if msg_id in ACTIVE_DOWNLOADS:
            ACTIVE_DOWNLOADS[msg_id].set()
            await event.answer("جاري الإلغاء...", alert=False)

# --- استقبال الاسم الجديد ---
@bot.on(events.NewMessage)
async def text_handler(event):
    user_id = event.sender_id
    if user_id in USER_STATES and USER_STATES[user_id].get('state') == 'waiting_for_name':
        new_name = event.text.strip()
        if not new_name.endswith(('.mp4', '.mkv', '.avi', '.mov')): 
            new_name += ".mp4"
        url = USER_STATES[user_id]['url']
        USER_STATES.pop(user_id, None)
        await event.respond(f"✅ تم تغيير الاسم إلى: `{new_name}`\n🚀 **بدء العملية...**")
        await start_execution(event.chat_id, url, new_name)

# --- التنفيذ الأساسي ---
async def start_execution(chat_id, url, filename_title):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التحضير...**", buttons=[Button.inline("❌ إلغاء", data="cancel_init")])
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event
    loop = asyncio.get_event_loop()

    file_path = f"downloads/{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    temp_shots_dir = None
    
    try:
        is_direct_url = any(url.lower().rsplit('?')[0].endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov'])
        
        if is_direct_url:
            await status_msg.edit("📥 **بدء التحميل المباشر...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
            await loop.run_in_executor(None, download_direct_file, url, file_path, status_msg, loop, cancel_event)
        else:
            def progress_hook_ytdlp(d):
                if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
                if d['status'] == 'downloading':
                    now = time.time()
                    if status_msg.id in LAST_UPDATE_TIME and (now - LAST_UPDATE_TIME[status_msg.id]) < 2.0: return
                    LAST_UPDATE_TIME[status_msg.id] = now
                    
                    p = d.get('_percent_str', '0%').strip()
                    s = d.get('_speed_str', 'N/A').strip()
                    downloaded_bytes = d.get('downloaded_bytes', 0)
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    
                    curr_mb = downloaded_bytes / (1024 * 1024)
                    total_mb = total_bytes / (1024 * 1024) if total_bytes > 0 else 0
                    
                    text = (
                        f"📥 **جاري التحميل...**\n"
                        f"📊 النسبة: `{p}`\n"
                        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
                        f"🚀 السرعة: `{s}`"
                    )
                    asyncio.run_coroutine_threadsafe(status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")]), loop)

            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'outtmpl': file_path,
                'writethumbnail': True,
                'progress_hooks': [progress_hook_ytdlp]
            }
            await status_msg.edit("⏳ **بدء التحميل...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
            await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        if os.path.exists(file_path):
            if os.path.getsize(file_path) < 1024 * 1024: 
                raise Exception("الملف المحمل صغير جداً أو تالف.")

            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            
            # --- 1. إعداد الصورة المصغرة للفيديو الرئيسي ---
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

            # --- 2. رفع الفيديو إلى تليجرام أولاً ---
            video_attributes = DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True
            )

            caption_text = (
                f"🎬 **اسم الملف:** `{filename_title}`\n\n"
                f"✅ **تم التحميل والرفع بنجاح!**"
            )

            await status_msg.edit("📤 **جاري بدء الرفع إلى تليجرام...**", buttons=None)

            thumb_to_send = final_thumb if (final_thumb and os.path.exists(final_thumb)) else None

            await bot.send_file(
                chat_id, 
                file_path, 
                caption=caption_text,
                thumb=thumb_to_send,
                attributes=[video_attributes],
                supports_streaming=True,
                progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
            )

            # --- 3. توليد وإرسال اللقطات التسع بعد اكتمال رفع الفيديو ---
            await status_msg.edit("📸 **تم رفع الفيديو! جاري استخراج وإرسال اللقطات التسع...**", buttons=None)

            shots_list, temp_shots_dir = await loop.run_in_executor(
                None, generate_9_individual_shots, file_path, status_msg.id, duration
            )
            
            if shots_list:
                try:
                    await bot.send_file(
                        chat_id, 
                        shots_list, 
                        caption=f"📸 **اللقطات المنفصلة للفيديو:** `{filename_title}`"
                    )
                except Exception as ex:
                    print(f"Error sending shots album: {ex}")

            # حذف رسالة الحالة بعد كل شيء
            await status_msg.delete()

    except Exception as e:
        if "CANCELLED_BY_USER" in str(e):
            await status_msg.edit("🛑 **تم إلغاء العملية.**", buttons=None)
        else:
            await status_msg.edit(f"❌ خطأ: `{str(e)}`", buttons=None)
            
    finally:
        ACTIVE_DOWNLOADS.pop(status_msg.id, None)
        LAST_UPDATE_TIME.pop(status_msg.id, None)
        LAST_BYTES.pop(status_msg.id, None)
        
        for path in [file_path, thumb_path]:
            if os.path.exists(path):
                try: os.remove(path)
                except: pass
                
        if temp_shots_dir and os.path.exists(temp_shots_dir):
            for f in os.listdir(temp_shots_dir):
                try: os.remove(os.path.join(temp_shots_dir, f))
                except: pass
            try: os.rmdir(temp_shots_dir)
            except: pass

async def main():
    os.makedirs("downloads", exist_ok=True)
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
