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
VERSION = "v40.0-UniversalWebExtractor"
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

def extract_web_info(url):
    """استخراج معلومات الصفحة المسبقة لمعرفة عنوان الفيديو وتهيئته"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'user_agent': BROWSER_HEADERS['User-Agent'],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', None)
            if title:
                # تنظيف العنوان من الرموز التي قد تفسد نظام الملفات
                clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                return f"{clean_title}.mp4"
    except Exception:
        pass
    
    # اسم افتراضي في حال تعذر جلب Title الصفحة
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or '.m3u8' in filename.lower() or '.html' in filename.lower():
        return "video_download.mp4"
    if not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3']):
        filename += ".mp4"
    return filename

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
    
    last_time = LAST_UPDATE_TIME.get(msg_id, 0)
    if (now - last_time) < 2.5: return

    last_bytes = LAST_BYTES.get(msg_id, current)
    bytes_delta = current - last_bytes
    speed = bytes_delta / max(now - last_time, 0.001)
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
        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء الرفع", data=f"cancel_{msg_id}")])
    except: pass

async def process_queue(chat_id):
    if chat_id not in QUEUE_LOCKS:
        QUEUE_LOCKS[chat_id] = asyncio.Lock()
        
    async with QUEUE_LOCKS[chat_id]:
        while chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
            task_data = DOWNLOAD_QUEUES[chat_id][0]
            await start_execution(chat_id, task_data['url'], task_data['custom_name'], task_data['as_doc'])
            if chat_id in DOWNLOAD_QUEUES and len(DOWNLOAD_QUEUES[chat_id]) > 0:
                DOWNLOAD_QUEUES[chat_id].pop(0)

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(f"🌐 **بوت التحميل الشامل من شبكة الويب ({VERSION})**\nأرسل رابط أي صفحة ويب تحتوي فيديو للبدء.")

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return

    chat_id = event.chat_id
    user_id = event.sender_id
    
    if len(urls) == 1:
        url = urls[0]
        checking_msg = await event.respond("🌐 **جاري تحليل الصفحة واستخراج المقطع...**")
        
        loop = asyncio.get_event_loop()
        default_name = await loop.run_in_executor(None, extract_web_info, url)
        await checking_msg.delete()

        USER_STATES[user_id] = {'url': url, 'custom_name': default_name}
        
        buttons = [
            [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف خام)", data="type_doc")],
            [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
        ]
        await event.respond(f"🌐 **تم تحليل الصفحة بنجاح!**\n📁 **الاسم المتوقع:** `{default_name}`", buttons=buttons)

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
                'as_doc': as_doc
            })
            USER_STATES.pop(user_id, None)
            asyncio.create_task(process_queue(chat_id))
            
    elif data.startswith("cancel_"):
        try:
            msg_id = int(data.split("_")[1])
            if msg_id in ACTIVE_DOWNLOADS:
                ACTIVE_DOWNLOADS[msg_id].set()
                await event.answer("جاري الإلغاء...", alert=False)
        except: pass

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

async def start_execution(chat_id, url, filename_title, as_doc=False):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري الفحص وبدء الاستخراج...**")
    msg_id = status_msg.id
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[msg_id] = cancel_event

    loop = asyncio.get_event_loop()
    
    # تنظيف اسم الملف
    safe_title = re.sub(r'[\\/*?:"<>|]', "", filename_title)
    file_path = f"downloads/{msg_id}_{safe_title}"
    thumb_path = f"downloads/thumb_{msg_id}.jpg"
    
    try:
        parsed_u = urlparse(url)
        referer_header = f"{parsed_u.scheme}://{parsed_u.netloc}/"

        await status_msg.edit("⚡ **جاري سحب المقطع من الصفحة...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{msg_id}")])

        def progress_hook_ytdlp(d):
            if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
            if d['status'] == 'downloading':
                now = time.time()
                last_t = LAST_UPDATE_TIME.get(msg_id, 0)
                if (now - last_t) < 2.0: return
                LAST_UPDATE_TIME[msg_id] = now
                
                p = d.get('_percent_str', '0%').strip()
                s = d.get('_speed_str', 'N/A').strip()
                eta = d.get('_eta_str', 'N/A').strip()
                downloaded_bytes = d.get('downloaded_bytes', 0)
                curr_mb = downloaded_bytes / (1024 * 1024)
                
                text = (
                    f"🚀 **جاري تنزيل المقطع...**\n"
                    f"📊 النسبة: `{p}`\n"
                    f"💾 الحجم: `{curr_mb:.1f}MB`\n"
                    f"🚀 السرعة: `{s}`\n"
                    f"⏳ المتبقي: `{eta}`"
                )
                
                async def safe_edit():
                    try:
                        await status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{msg_id}")])
                    except: pass
                
                asyncio.run_coroutine_threadsafe(safe_edit(), loop)

        # خيارات شاملة تلتقط الفيديو من أي صفحة
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',  # اختيار أفضل جودة متوفرة بالصفحة
            'outtmpl': file_path,
            'quiet': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'progress_hooks': [progress_hook_ytdlp],
            'merge_output_format': 'mp4',
            'user_agent': BROWSER_HEADERS['User-Agent'],
            'referer': referer_header,
            'concurrent_fragment_downloads': 8,
            'hls_use_mpegts': True,
            'retries': 10,
            'fragment_retries': 10,
            'buffersize': 1024 * 64,
        }

        await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        # معالجة امتداد الملف إذا تم حفظه بدون .mp4 تلقائياً
        if not os.path.exists(file_path):
            if os.path.exists(f"{file_path}.mp4"):
                file_path = f"{file_path}.mp4"
            elif os.path.exists(f"{file_path}.mkv"):
                file_path = f"{file_path}.mkv"

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            final_thumb = await loop.run_in_executor(None, generate_thumbnail_fallback, file_path, thumb_path, duration)

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            caption_text = f"🎬 **اسم الملف:** `{safe_title}`\n💾 **الحجم:** `{file_size_mb:.1f} MB`\n\n✅ **تم الرفع بنجاح!**"

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
            raise Exception("لم يتم العثور على مقطع فيديو شغال في الصفحة أو تعذر استخراجه.")

    except Exception as e:
        err_msg = str(e) if str(e) else repr(e)
        if "CANCELLED_BY_USER" in err_msg:
            try: await status_msg.edit("❌ **تم إلغاء العملية.**")
            except: pass
        else:
            try: await status_msg.edit(f"❌ **حدث خطأ أثناء المعالجة:**\n`{type(e).__name__}: {err_msg}`")
            except: pass

    finally:
        ACTIVE_DOWNLOADS.pop(msg_id, None)
        LAST_UPDATE_TIME.pop(msg_id, None)
        LAST_BYTES.pop(msg_id, None)
        
        for p in [file_path, thumb_path, f"{file_path}.mp4"]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
            
        gc.collect()

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
