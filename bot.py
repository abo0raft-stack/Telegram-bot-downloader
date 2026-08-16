import asyncio
import os
import shutil
import threading
import time
import gc
import re
import traceback
import subprocess
from urllib.parse import unquote, urlparse
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v36.0-FFmpegM3U8Fix"
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
    'Accept': '*/*',
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
                print(f"Clean error: {e}")
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
    if '.m3u8' in filename.lower() or not filename:
        filename = "video_download.mp4"
    elif not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3']):
        filename += ".mp4"
    return filename

def download_m3u8_native(url, output_path, cancel_event):
    """تنزيل وتجميع روابط M3U8 مباشرة باستخدام FFmpeg لتفادي الأخطاء 487"""
    cmd = [
        'ffmpeg',
        '-y',
        '-headers', f"User-Agent: {BROWSER_HEADERS['User-Agent']}\r\n",
        '-i', url,
        '-c', 'copy',
        '-bsf:a', 'aac_adtstoasc',
        '-movflags', '+faststart',
        output_path
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    
    while process.poll() is None:
        if cancel_event.is_set():
            process.kill()
            raise Exception("CANCELLED_BY_USER")
        time.sleep(1)
        
    if process.returncode != 0 and (not os.path.exists(output_path) or os.path.getsize(output_path) == 0):
        raise Exception(f"فشل FFmpeg في تنزيل المقطع (رمز الخروج: {process.returncode})")

def get_video_metadata(filepath):
    duration, width, height = 0, 1280, 720
    try:
        parser = createParser(filepath)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    if metadata.has("duration"): duration = int(metadata.get('duration').seconds)
                    if metadata.has("width"): width = int(metadata.get('width'))
                    if metadata.has("height"): height = int(metadata.get('height'))
    except Exception as e:
        print(f"Metadata skip: {e}")
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
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            im = Image.open(thumb_path)
            im.thumbnail((320, 320))
            im.convert('RGB').save(thumb_path, 'JPEG')
            im.close()
            return thumb_path
    except Exception as e:
        print(f"Thumb skip: {e}")
    return None

async def upload_progress_callback(current, total, status_msg, cancel_event):
    if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
    now = time.time()
    msg_id = status_msg.id
    
    if msg_id not in LAST_UPDATE_TIME:
        LAST_UPDATE_TIME[msg_id] = now
        LAST_BYTES[msg_id] = current
        return

    time_delta = now - LAST_UPDATE_TIME[msg_id]
    if time_delta < 2.5: return

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
            await start_execution(chat_id, task_data['url'], task_data['custom_name'], task_data['as_doc'], task_data['quality'])
            if chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
                DOWNLOAD_QUEUES[chat_id].pop(0)

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(f"🚀 **بوت التحميل الشامل ({VERSION})**\nأرسل رابط المقطع للبدء.")

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
            [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
        ]
        await event.respond(f"🔗 **تم استلام الرابط!**\n📁 **الاسم:** `{default_name}`", buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    chat_id = event.chat_id
    
    if data == "ask_rename":
        if user_id in USER_STATES:
            USER_STATES[user_id]['state'] = 'waiting_for_name'
            await event.edit("✏️ **أرسل الاسم الجديد للملف:**")

    elif data in ["type_stream", "type_doc"]:
        state_data = USER_STATES.get(user_id)
        if state_data:
            as_doc = (data == "type_doc")
            await event.delete()
            
            if chat_id not in DOWNLOAD_QUEUES:
                DOWNLOAD_QUEUES[chat_id] = []
                
            DOWNLOAD_QUEUES[chat_id].append({
                'url': state_data['url'],
                'custom_name': state_data['custom_name'],
                'as_doc': as_doc,
                'quality': 'best'
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
        if not new_name.lower().endswith('.mp4'): new_name += ".mp4"
        url = USER_STATES[user_id]['url']
        USER_STATES.pop(user_id, None)
        
        USER_STATES[user_id] = {'url': url, 'custom_name': new_name}
        buttons = [[Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف)", data="type_doc")]]
        await event.respond(f"✅ تم تغيير الاسم إلى: `{new_name}`", buttons=buttons)

async def start_execution(chat_id, url, filename_title, as_doc=False, quality='best'):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التحضير...**")
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event

    loop = asyncio.get_event_loop()
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    
    try:
        await status_msg.edit("⚡ **جاري التحميل المعالج (FFmpeg Direct)...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])

        # تنزيل عبر FFmpeg مباشرة لتفادي مشاكل الأجزاء التالفة والأرقام المبهمة
        await loop.run_in_executor(None, download_m3u8_native, url, file_path, cancel_event)

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            final_thumb = await loop.run_in_executor(None, generate_thumbnail_fallback, file_path, thumb_path, duration)

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            caption_text = f"🎬 **اسم الملف:** `{filename_title}`\n💾 **الحجم:** `{file_size_mb:.1f} MB`\n\n✅ **تم الرفع بنجاح!**"

            await status_msg.edit("📤 **جاري البدء بالرفع إلى تليجرام...**")
            thumb_to_send = final_thumb if (final_thumb and os.path.exists(final_thumb)) else None

            if as_doc:
                await bot.send_file(
                    chat_id, file_path, caption=caption_text, thumb=thumb_to_send,
                    force_document=True,
                    progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                )
            else:
                video_attributes = DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)
                await bot.send_file(
                    chat_id, file_path, caption=caption_text, thumb=thumb_to_send,
                    attributes=[video_attributes], supports_streaming=True,
                    progress_callback=lambda c, t: upload_progress_callback(c, t, status_msg, cancel_event)
                )

            await status_msg.delete()
        else:
            raise Exception("لم يتم العثور على الملف بعد التنزيل أو أن حجمه صفر.")

    except Exception as e:
        err_msg = str(e)
        if "CANCELLED_BY_USER" in err_msg:
            await status_msg.edit("❌ **تم إلغاء العملية.**")
        else:
            # استخراج تفاصيل خطأ صريحة بدلاً من طباعة الكائنات المباشرة
            detailed_err = f"{type(e).__name__}: {err_msg if err_msg else repr(e)}"
            await status_msg.edit(f"❌ **حدث خطأ أثناء المعالجة:**\n`{detailed_err}`")

    finally:
        ACTIVE_DOWNLOADS.pop(status_msg.id, None)
        LAST_UPDATE_TIME.pop(status_msg.id, None)
        LAST_BYTES.pop(status_msg.id, None)
        
        for p in [file_path, thumb_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
            
        gc.collect()

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
