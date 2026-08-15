import asyncio
import os
import shutil
import threading
import time
import gc
import re
import subprocess
from urllib.parse import unquote, urlparse
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# استيراد curl_cffi لتجاوز حظر الـ CDN والروابط المباشرة
from curl_cffi import requests as async_requests

# استيراد moviepy كدعم إضافي للتعامل مع الفيديو
try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

# --- الإعدادات المتغيرة ---
VERSION = "v33.1-CDNFix"
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

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

def get_clean_filename(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or not any(filename.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.flv']):
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
        print(f"Metadata Extraction Error: {e}")
        
    if duration == 0 and MOVIEPY_AVAILABLE:
        try:
            with VideoFileClip(filepath) as clip:
                duration = int(clip.duration)
                width, height = clip.size
        except Exception as e:
            print(f"MoviePy Metadata Error: {e}")

    return duration, width, height

def format_eta(seconds):
    if not seconds or seconds < 0: return "غير معروف"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}س {m}د {s}ث"
    if m > 0: return f"{m}د {s}ث"
    return f"{s}ث"

def compress_and_rescale_video(input_path, output_path, target_quality):
    scale_map = {'1080': 'scale=-2:1080', '720': 'scale=-2:720', '480': 'scale=-2:480'}
    if target_quality not in scale_map: return input_path
    vf_scale = scale_map[target_quality]
    
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', f"{vf_scale},fps=30", '-c:v', 'libx264',
        '-crf', '28', '-preset', 'ultrafast',
        '-c:a', 'aac', '-b:a', '128k', output_path
    ]
    process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path
    return input_path

def generate_thumbnail_fallback(video_path, thumb_path, duration):
    try:
        target_sec = "00:00:02" if duration > 3 else "00:00:00"
        cmd = ['ffmpeg', '-ss', target_sec, '-i', video_path, '-vframes', '1', '-q:v', '2', '-y', thumb_path]
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if process.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            im = Image.open(thumb_path)
            im.thumbnail((320, 320))
            im.convert('RGB').save(thumb_path, 'JPEG')
            im.close()
            return thumb_path
    except Exception as e:
        print(f"FFmpeg Thumbnail Error: {e}")
        
    if MOVIEPY_AVAILABLE:
        try:
            with VideoFileClip(video_path) as clip:
                frame_time = 2 if duration > 3 else 0
                clip.save_frame(thumb_path, t=frame_time)
                im = Image.open(thumb_path)
                im.thumbnail((320, 320))
                im.convert('RGB').save(thumb_path, 'JPEG')
                im.close()
                return thumb_path
        except Exception as e:
            print(f"MoviePy Thumbnail Error: {e}")
            
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
            cmd = ['ffmpeg', '-ss', str(ts), '-i', video_path, '-vframes', '1', '-q:v', '2', '-y', shot_file]
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

def download_with_curl_cffi(url, filepath, status_msg, loop, cancel_event):
    parsed_u = urlparse(url)
    origin_referer = f"{parsed_u.scheme}://{parsed_u.netloc}/"

    # هيدرات متكاملة ومحدثة للتحايل على حماية CDN
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Referer": origin_referer,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
    }

    response = async_requests.get(
        url, 
        headers=headers, 
        stream=True, 
        impersonate="chrome120", 
        verify=False,
        allow_redirects=True,
        timeout=60
    )
    
    if response.status_code not in [200, 206]:
        raise Exception(f"السيرفر أعاد استجابة مرفوضة بكود: {response.status_code}")

    total_bytes = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update = start_time

    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                if now - last_update >= 2.0:
                    last_update = now
                    speed = downloaded / (now - start_time) if (now - start_time) > 0 else 0
                    speed_mb = speed / (1024 * 1024)
                    percent = (downloaded / total_bytes * 100) if total_bytes > 0 else 0
                    curr_mb = downloaded / (1024 * 1024)
                    total_mb = total_bytes / (1024 * 1024) if total_bytes > 0 else 0
                    eta_sec = (total_bytes - downloaded) / speed if speed > 0 else 0

                    text = (
                        f"📥 **جاري التنزيل الذكي (curl_cffi)...**\n"
                        f"📊 النسبة: `{percent:.1f}%`\n"
                        f"💾 الحجم: `{curr_mb:.1f}MB / {total_mb:.1f}MB`\n"
                        f"🚀 السرعة: `{speed_mb:.2f} MB/s`\n"
                        f"⏳ المتبقي: `{format_eta(eta_sec)}`"
                    )
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit(text, buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")]), 
                        loop
                    )

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
        "✨ **تم التحديث والربط الكامل مع بيئة Docker وتجاوز حماية الـ CDN.**"
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
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التجهيز...**")
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event

    loop = asyncio.get_event_loop()
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    rescaled_path = f"downloads/rescaled_{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    temp_shots_dir = None
    
    try:
        await status_msg.edit("📥 **جاري التنزيل وتجاوز حظر CDN...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
        
        await loop.run_in_executor(None, download_with_curl_cffi, url, file_path, status_msg, loop, cancel_event)

        if cancel_event.is_set(): raise Exception("CANCELLED_BY_USER")

        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            if file_size < 100 * 1024: 
                raise Exception("الملف المحمل صغير جداً أو تم حجب الرابط.")

            if quality in ['1080', '720', '480']:
                await status_msg.edit(f"⚙️ **جاري تحويل الجودة والضغط إلى {quality}p...**")
                processed_file = await loop.run_in_executor(None, compress_and_rescale_video, file_path, rescaled_path, quality)
                if processed_file != file_path and os.path.exists(processed_file):
                    os.remove(file_path)
                    file_path = processed_file

            duration, width, height = await loop.run_in_executor(None, get_video_metadata, file_path)
            final_thumb = await loop.run_in_executor(None, generate_thumbnail_fallback, file_path, thumb_path, duration)

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            caption_text = (
                f"🎬 **اسم الملف:** `{filename_title}`\n"
                f"💾 **الحجم:** `{file_size_mb:.1f} MB`\n\n"
                f"✅ **تم التنزيل والرفع بنجاح!**"
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
                shots_list, temp_shots_dir = await loop.run_in_executor(None, generate_9_individual_shots, file_path, status_msg.id, duration)
                if shots_list:
                    await bot.send_file(chat_id, shots_list, caption="📸 **لقطات شاشة من الفيديو**")

            await status_msg.delete()

    except Exception as e:
        err_msg = str(e)
        if "CANCELLED_BY_USER" in err_msg:
            await status_msg.edit("❌ **تم إلغاء العملية.**")
        else:
            await status_msg.edit(f"❌ **حدث خطأ أثناء المعالجة:**\n`{err_msg}`")

    finally:
        ACTIVE_DOWNLOADS.pop(status_msg.id, None)
        LAST_UPDATE_TIME.pop(status_msg.id, None)
        LAST_BYTES.pop(status_msg.id, None)
        
        for p in [file_path, rescaled_path, thumb_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
                
        if temp_shots_dir and os.path.exists(temp_shots_dir):
            shutil.rmtree(temp_shots_dir, ignore_errors=True)
            
        gc.collect()

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
