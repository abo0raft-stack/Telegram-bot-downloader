import asyncio
import os
import re
import shutil
import threading
import time
import requests
import yt_dlp
import subprocess
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo, BotCommand, BotCommandScopeDefault
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.errors import FloodWaitError
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
VERSION = "v28.0-ProMax-MultiEngine"
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
        "✨ **المميزات المفعلة حديثاً:**\n"
        " 📱 **كشف ذكي لمنصات TikTok / Reels** (تحميل سريع وبدون علامة مائية).\n"
        " 📂 **دعم كامل لقوائم التشغيل (Playlist)** مع إمكانية تحديد المقاطع.\n"
        " 📦 **تحميل مجمع (Batch)** بإرسال مجموعة روابط دفعة واحدة.\n"
        " 🧲 **دعم روابط التورنت المباشرة ورابط الماجنت (Magnet Leech)**.\n"
        " 🎬 **تنزيل الفيديو والجودات متعددة / MP3 / اللقطات الـ 9**.\n\n"
        "👇 **أرسل الرابط أو القائمة أو رابط الماجنت للبدء!**"
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

def format_eta(seconds):
    if not seconds or seconds < 0: return "غير معروف"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}س {m}د {s}ث"
    if m > 0: return f"{m}د {s}ث"
    return f"{s}ث"

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

# --- توليد 9 لقطات منفصلة لألبوم الصور ---
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

# --- تحديث شريط تقدم الرفع ---
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

# --- دالة تحميل ملف التورنت/الماجنت عبر aria2c ---
def download_torrent_file(torrent_url, output_dir, status_msg, loop, cancel_event):
    cmd = [
        'aria2c',
        '--dir=' + output_dir,
        '--seed-time=0',
        '--max-connection-per-server=8',
        '--summary-interval=2',
        torrent_url
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    
    while True:
        if cancel_event.is_set():
            process.terminate()
            raise Exception("CANCELLED_BY_USER")
            
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
            
        if "DL:" in line:
            clean_line = line.strip()
            asyncio.run_coroutine_threadsafe(
                status_msg.edit(f"🧲 **جاري تحميل التورنت (Aria2)...**\n\n`{clean_line}`", 
                                buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")]),
                loop
            )
            
    if process.returncode != 0:
        raise Exception("فشل تنزيل ملف التورنت.")

# --- استقبال الرسائل والروابط ---
@bot.on(events.NewMessage)
async def message_router(event):
    if not event.text: return
    text = event.text.strip()
    
    if text.startswith('/start'): return

    # 1. حالة رابط التورنت والماجنت (Magnet / Torrent)
    if text.startswith("magnet:?") or text.endswith(".torrent"):
        await start_execution(event.chat_id, text, "Torrent_Download", as_doc=True, is_torrent=True)
        return

    # 2. تحليل الرابط أو الروابط المجمعة (Batch Detection)
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls: return

    # حالة الروابط المجمعة (Batch Batch)
    if len(urls) > 1:
        await event.respond(f"📦 **تم اكتشاف ({len(urls)}) روابط مجمعة!**\nجاري إضافتها إلى طابور التحميل والتنفيذ...")
        for idx, u in enumerate(urls, 1):
            default_name = f"Batch_File_{idx}.mp4"
            await start_execution(event.chat_id, u, default_name, as_doc=False, quality='best')
            await asyncio.sleep(2)
        return

    url = urls[0]

    # 3. الكشف الذكي عن منصات الفيديو القصير (TikTok / Instagram Reels / Short Videos)
    is_short_platform = any(domain in url.lower() for domain in ['tiktok.com', 'instagram.com/reel', 'youtube.com/shorts'])
    if is_short_platform:
        await event.respond("📱 **تم التعرف على فيديو قصير!**\nجاري التحميل المباشر بأعلى جودة وبدون علامات مائية...")
        default_name = f"Short_{int(time.time())}.mp4"
        await start_execution(event.chat_id, url, default_name, as_doc=False, quality='best')
        return

    # 4. الكشف عن قائمة التشغيل (Playlist)
    if "list=" in url or "playlist" in url.lower():
        USER_STATES[event.sender_id] = {'playlist_url': url}
        buttons = [
            [Button.inline("📜 تحميل القائمة كاملة", data="pl_all")],
            [Button.inline("🔢 تحميل أول 5 مقاطع فقط", data="pl_top5")]
        ]
        await event.respond("📂 **تم كشف قائمة تشغيل (Playlist)!**\nاختر آلية التحميل المطلوبة:", buttons=buttons)
        return

    # 5. التعامل مع الرابط الفردي المعتاد
    default_name = url.rsplit('/', 1)[-1].rsplit('?', 1)[0]
    if not default_name.endswith(('.mp4', '.mkv', '.avi', '.mov', '.mp3')): 
        default_name += ".mp4"
        
    USER_STATES[event.sender_id] = {'url': url, 'custom_name': default_name, 'quality': 'best'}
    
    buttons = [
        [Button.inline("🎬 تنزيل (فيديو)", data="type_stream"), Button.inline("📁 تنزيل (ملف خام)", data="type_doc")],
        [Button.inline("🎯 الجودة: [أفضل جودة 🥇]", data="choose_quality")],
        [Button.inline("✏️ إعادة تسمية الملف", data="ask_rename")]
    ]
    await event.respond(f"🔗 **تم استلام الرابط!**\n\n📁 **الاسم:** `{default_name}`", buttons=buttons)

# --- معالجة الأزرار التفاعلية ---
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.sender_id
    
    if data.startswith("pl_"):
        pl_data = USER_STATES.get(user_id, {})
        pl_url = pl_data.get('playlist_url')
        if pl_url:
            await event.delete()
            max_items = 5 if data == "pl_top5" else None
            await start_playlist_download(event.chat_id, pl_url, max_items=max_items)
            
    elif data == "ask_rename":
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
        await event.edit(f"✅ **تم اختيار:** `{q_label}`", buttons=buttons)

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

# --- دالة التنزيل لقوائم التشغيل Playlist ---
async def start_playlist_download(chat_id, playlist_url, max_items=None):
    status_msg = await bot.send_message(chat_id, "📂 **جاري استخراج عناصر قائمة التشغيل...**")
    loop = asyncio.get_event_loop()
    
    def extract_pl():
        ydl_opts = {'extract_flat': True, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(playlist_url, download=False)
            
    try:
        info = await loop.run_in_executor(None, extract_pl)
        entries = info.get('entries', [])
        if not entries:
            await status_msg.edit("❌ **تعذر قراءة عناصر القائمة أو أنها فارغة.**")
            return
            
        total = len(entries) if not max_items else min(len(entries), max_items)
        await status_msg.edit(f"📂 **تم العثور على {len(entries)} عنصر.** جاري بدء تحميل {total} مقطع...")
        
        for idx, item in enumerate(entries[:total], 1):
            item_url = item.get('url') or f"https://www.youtube.com/watch?v={item.get('id')}"
            item_title = item.get('title', f"Video_{idx}") + ".mp4"
            await start_execution(chat_id, item_url, item_title, as_doc=False, quality='best')
            await asyncio.sleep(2)
            
    except Exception as e:
        await status_msg.edit(f"❌ خطأ أثناء معالجة القائمة: `{str(e)}`")

# --- التنفيذ الأساسي والمحرك الشامل ---
async def start_execution(chat_id, url, filename_title, as_doc=False, quality='best', is_torrent=False):
    status_msg = await bot.send_message(chat_id, "🔍 **جاري التحضير...**")
    cancel_event = threading.Event()
    ACTIVE_DOWNLOADS[status_msg.id] = cancel_event
    await status_msg.edit("🔍 **جاري التحضير...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])

    loop = asyncio.get_event_loop()
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    temp_shots_dir = None
    
    try:
        if is_torrent:
            await status_msg.edit("🧲 **بدء استخراج ملفات التورنت/الماجنت...**", buttons=[Button.inline("❌ إلغاء", data=f"cancel_{status_msg.id}")])
            out_torrent_dir = f"downloads/torrent_{status_msg.id}"
            os.makedirs(out_torrent_dir, exist_ok=True)
            
            await loop.run_in_executor(None, download_torrent_file, url, out_torrent_dir, status_msg, loop, cancel_event)
            
            files = [os.path.join(out_torrent_dir, f) for f in os.listdir(out_torrent_dir) if os.path.isfile(os.path.join(out_torrent_dir, f))]
            if files:
                file_path = files[0]
            else:
                raise Exception("لم يتم العثور على ملفات صالحة في التورنت.")

        else:
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
                f"🎬 **اسم الملف:** `{os.path.basename(file_path)}`\n"
                f"🎯 **الجودة/النوع:** `{quality}`\n\n"
                f"✅ **تم التحميل والرفع بنجاح!**"
            )

            await status_msg.edit("📤 **جاري بدء الرفع إلى تليجرام...**", buttons=None)
            thumb_to_send = final_thumb if (final_thumb and os.path.exists(final_thumb)) else None

            retry = True
            while retry:
                try:
                    if as_doc or quality == 'audio' or not file_path.endswith(('.mp4', '.mkv', '.avi', '.mov')):
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

            if quality != 'audio' and file_path.endswith(('.mp4', '.mkv', '.avi', '.mov')):
                await status_msg.edit("📸 **جاري استخراج وإرسال ألبوم اللقطات الـ 9...**", buttons=None)

                shots_list, temp_shots_dir = await loop.run_in_executor(
                    None, generate_9_individual_shots, file_path, status_msg.id, duration
                )
                
                if shots_list:
                    try:
                        await bot.send_file(
                            chat_id, 
                            shots_list, 
                            caption=f"📸 **ألبوم اللقطات المنفصلة للفيديو:** `{filename_title}`"
                        )
                    except Exception as ex:
                        print(f"Error sending shots album: {ex}")

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
    clean_download_folder()
    
    await bot.start(bot_token=BOT_TOKEN)
    
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code='ar',
        commands=[
            BotCommand(command="start", description="🚀 بدء التشغيل واختبار البوت")
        ]
    ))
    
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
