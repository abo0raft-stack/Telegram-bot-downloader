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
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- الإعدادات ---
VERSION = "v58.0-X-Quality-Selection-Support"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}
PENDING_TASKS = {}  # لتخزين بيانات الروابط بانتظار اختيار الجودة

# --- التعامل مع ملفات الكوكيز ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
        except Exception: pass

    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
        except Exception: pass

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
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

def clean_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com'])

def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

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
        print(f"Metadata Extraction Error: {e}")
        thumb_path = None

    return duration, width, height, thumb_path

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

    if any(x in url_lower for x in ['twitter.com', 'x.com']) and os.path.exists(X_COOKIES_FILE):
        ydl_opts['cookiefile'] = X_COOKIES_FILE
    elif any(x in url_lower for x in ['instagram.com', 'instagr.am']) and os.path.exists(INSTAGRAM_COOKIES_FILE):
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def instagram_api_fallback(url, task_dir):
    try:
        api_url = "https://api.cobalt.tools/api/json"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_HEADERS['User-Agent']
        }
        payload = {"url": url}
        
        res = requests.post(api_url, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status")
            
            urls_to_download = []
            if status in ["stream", "redirect"]:
                urls_to_download.append(data.get("url"))
            elif status == "picker":
                for item in data.get("picker", []):
                    urls_to_download.append(item.get("url"))

            for idx, media_url in enumerate(urls_to_download, start=1):
                if not media_url: continue
                r = requests.get(media_url, stream=True, headers=BROWSER_HEADERS, timeout=30)
                if r.status_code == 200:
                    ext = "mp4" if ".mp4" in media_url or "video" in r.headers.get("content-type", "") else "jpg"
                    filepath = os.path.join(task_dir, f"ig_media_{idx}.{ext}")
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk: f.write(chunk)
    except Exception as e:
        print(f"Fallback Failed: {e}")

def download_direct_file(url, filepath, cancel_event):
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=4*1024*1024):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            if chunk: f.write(chunk)

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None):
    if not status_msg:
        status_msg = await bot.send_message(chat_id, "⏳ **جاري تنزيل المحتوى...**")
    else:
        await status_msg.edit("⏳ **جاري تنزيل المحتوى بالجودة المحددة...**")
        
    cancel_event = threading.Event()
    
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        loop = asyncio.get_event_loop()
        target_url = clean_url(url) if is_complex_url(url) else url
        
        if target_url and is_complex_url(target_url):
            await loop.run_in_executor(None, download_with_ytdlp, target_url, task_dir, target_fmt, quality)
            
            downloaded = [f for f in os.listdir(task_dir) if os.path.getsize(os.path.join(task_dir, f)) > 0]
            if not downloaded and ('instagram.com' in target_url or 'instagr.am' in target_url):
                await loop.run_in_executor(None, instagram_api_fallback, target_url, task_dir)
                
        elif target_url:
            filepath = os.path.join(task_dir, filename)
            await loop.run_in_executor(None, download_direct_file, target_url, filepath, cancel_event)
        elif media_msg:
            filepath = os.path.join(task_dir, filename)
            await bot.download_media(media_msg, file=filepath)

        downloaded_files = [
            os.path.join(task_dir, f) for f in os.listdir(task_dir)
            if os.path.isfile(os.path.join(task_dir, f)) and os.path.getsize(os.path.join(task_dir, f)) > 0
            and not f.endswith('_thumb.jpg')
        ]

        if not downloaded_files:
            raise Exception("لم يتم العثور على أي صور أو فيديوهات في هذا الرابط، أو أن الحساب خاص.")

        await status_msg.edit(f"📤 **جاري رفع المحتوى ({len(downloaded_files)} عنصر/عناصر)...**")
        user_settings = get_user_settings(chat_id)

        photos, videos, other_files = [], [], []
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm')
        image_extensions = ('.jpg', '.jpeg', '.png', '.webp')

        for fpath in sorted(downloaded_files):
            ext = os.path.splitext(fpath)[1].lower()
            if ext in image_extensions:
                photos.append(fpath)
            elif ext in video_extensions:
                videos.append(fpath)
            else:
                other_files.append(fpath)

        # 1. إرسال الصور
        if photos:
            if len(photos) == 1:
                await bot.send_file(chat_id, photos[0], caption="✅ **تم تنزيل الصورة بنجاح!**", force_document=as_doc)
            else:
                for i in range(0, len(photos), 10):
                    batch = photos[i:i+10]
                    await bot.send_file(chat_id, batch, caption=f"📸 **تم تنزيل ألبوم الصور ({len(photos)} صورة):**" if i == 0 else "")

        # 2. إرسال الفيديوهات كمشغل ميديا متكامل
        for vid in videos:
            duration, width, height, thumb_path = get_video_metadata_and_thumb(vid)
            attr = [DocumentAttributeVideo(
                duration=duration, 
                w=width, 
                h=height, 
                supports_streaming=True
            )]
            
            await bot.send_file(
                chat_id,
                vid,
                caption=f"🎥 **تم تنزيل الفيديو بنجاح!**\n📁 `{os.path.basename(vid)}`",
                force_document=False,
                thumb=thumb_path,
                attributes=attr
            )
            
            if thumb_path and os.path.exists(thumb_path):
                try: os.remove(thumb_path)
                except: pass

        # 3. إرسال ملفات الصوت وباقي الملفات
        for oth in other_files:
            await bot.send_file(chat_id, oth, caption=f"📄 `{os.path.basename(oth)}`", force_document=(target_fmt != 'mp3'))

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(task_dir):
            try: shutil.rmtree(task_dir)
            except: pass

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(
        f"🚀 **أهلاً بك في بوت التحميل المباشر ({VERSION})**\n\n"
        "⚡ **تحميل سريع ومباشر يدعم:**\n"
        "📸 **إنستغرام:** الفيديوهات، الصور الفردية، وألبومات الصور المتعددة (Carousel).\n"
        "🎵 **تيك توك:** الفيديوهات وسلاسل الصور.\n"
        "𝕏 **منصة X:** اختيار جودة الفيديو (1080p, 720p, 480p, MP3) وتنزيل الصور.\n\n"
        "أرسل رابط المنشور مباشرة للبدء!"
    )

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    for u in urls:
        # إذا كان الرابط من منصة X، نقوم بإظهار أزرار اختيار الجودة
        if is_x_url(u):
            task_key = f"{chat_id}_{int(time.time()*1000)}"
            PENDING_TASKS[task_key] = u
            
            buttons = [
                [
                    Button.inline("🎬 عالية (1080p)", data=f"q_1080_{task_key}"),
                    Button.inline("🎥 متوسطة (720p)", data=f"q_720_{task_key}")
                ],
                [
                    Button.inline("📱 منخفضة (480p)", data=f"q_480_{task_key}"),
                    Button.inline("🎵 صوت فقط (MP3)", data=f"q_mp3_{task_key}")
                ]
            ]
            
            await event.respond("🎬 **اختر جودة الفيديو المطلوبة لمنصة X:**", buttons=buttons)
        else:
            asyncio.create_task(
                start_direct_execution(
                    chat_id=chat_id,
                    url=u,
                    filename=get_clean_filename(u),
                    as_doc=False,
                    quality='best',
                    target_fmt='mp4'
                )
            )

@bot.on(events.CallbackQuery(pattern=r"^q_"))
async def quality_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    quality_choice = data[1]
    task_key = "_".join(data[2:])
    
    if task_key not in PENDING_TASKS:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار، يرجى إعادة إرسال الرابط.", alert=True)
        return
        
    url = PENDING_TASKS.pop(task_key)
    chat_id = event.chat_id
    
    target_fmt = 'mp3' if quality_choice == 'mp3' else 'mp4'
    quality_val = 'best' if quality_choice == '1080' else quality_choice
    
    status_msg = await event.edit("⏳ **تم استلام طلبك، جاري بدء التنزيل...**", buttons=None)
    
    asyncio.create_task(
        start_direct_execution(
            chat_id=chat_id,
            url=url,
            filename=get_clean_filename(url),
            as_doc=False,
            quality=quality_val,
            target_fmt=target_fmt,
            status_msg=status_msg
        )
    )

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بكفاءة مع دعم اختيار جودة فيديوهات X!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
