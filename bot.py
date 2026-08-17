import asyncio
import os
import shutil
import threading
import time
import gc
import re
import base64
import subprocess
import json
import mimetypes
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- تحديث المكتبات الأساسية ---
def update_libraries():
    try:
        # تأكدنا من تثبيت gallery-dl لأنه الأقوى في التعامل مع صور تيك توك
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ تم تحديث مكتبات التنزيل بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_libraries()

# --- الإعدادات ---
VERSION = "v63.0-Universal-Media-Engine"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}
PENDING_TASKS = {}
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

# --- تنظيف وتهيئة المجلدات ---
def clean_download_folder():
    folder = "downloads"
    if os.path.exists(folder):
        shutil.rmtree(folder)
    os.makedirs(folder, exist_ok=True)
    gc.collect()

clean_download_folder()

bot = TelegramClient('bot_session', API_ID, API_HASH)

# --- محركات التنزيل ---
def run_downloader(url, task_dir):
    """المحرك الشامل: يستخدم gallery-dl للصور و yt-dlp للفيديوهات"""
    
    # 1. محاولة استخدام gallery-dl (الأفضل للصور وتيك توك وإنستغرام)
    try:
        cmd = ["gallery-dl", "-d", task_dir, "--filename", "{id}_{num}.{extension}", url]
        subprocess.run(cmd, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass

    # 2. محاولة استخدام yt-dlp (للتحقق من الفيديوهات إذا لم يجد gallery-dl شيئاً)
    files_count = len(os.listdir(task_dir))
    if files_count == 0:
        ydl_opts = {
            'outtmpl': os.path.join(task_dir, '%(title).30s_%(id)s.%(ext)s'),
            'quiet': True,
            'ignoreerrors': True,
            'format': 'best'
        }
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except: pass

# --- منطق تشغيل البوت ---
def get_video_metadata_and_thumb(file_path):
    duration, width, height = 0, 1280, 720
    thumb_path = f"{file_path}_thumb.jpg"
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=width,height', '-of', 'json', file_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        if 'format' in data: duration = int(float(data['format'].get('duration', 0)))
        if 'streams' in data and len(data['streams']) > 0:
            width = int(data['streams'][0].get('width', 1280))
            height = int(data['streams'][0].get('height', 720))
        subprocess.run(['ffmpeg', '-y', '-ss', '00:00:01', '-i', file_path, '-vframes', '1', '-q:v', '2', thumb_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: thumb_path = None
    return duration, width, height, thumb_path

async def start_direct_execution(chat_id, url, status_msg):
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        await status_msg.edit("⏳ **جاري فحص وتنزيل المحتوى...**")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_downloader, url, task_dir)
        
        # تصحيح الامتدادات
        downloaded_files = []
        for root, _, files in os.walk(task_dir):
            for file in files:
                fpath = os.path.join(root, file)
                if os.path.getsize(fpath) > 500:
                    # تصحيح اسم الملف إذا كان تالفاً
                    ext = os.path.splitext(file)[1]
                    if not ext or "none" in file.lower():
                        mime, _ = mimetypes.guess_type(fpath)
                        ext = mimetypes.guess_extension(mime) if mime else ".jpg"
                        new_name = f"media_{int(time.time())}.{ext.replace('.', '')}"
                        os.rename(fpath, os.path.join(root, new_name))
                        fpath = os.path.join(root, new_name)
                    downloaded_files.append(fpath)

        if not downloaded_files:
            raise Exception("تعذر العثور على ملفات. تأكد أن المنشور عام.")

        await status_msg.edit(f"📤 **تم التنزيل، جاري الرفع ({len(downloaded_files)} عنصر)...**")
        
        photos, videos = [], []
        for f in downloaded_files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')): photos.append(f)
            else: videos.append(f)

        if photos:
            if len(photos) == 1: await bot.send_file(chat_id, photos[0])
            else: await bot.send_file(chat_id, photos, caption=f"📸 ألبوم: {len(photos)} صورة")

        for vid in videos:
            dur, w, h, thumb = get_video_metadata_and_thumb(vid)
            await bot.send_file(chat_id, vid, thumb=thumb, attributes=[DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)])
            if thumb and os.path.exists(thumb): os.remove(thumb)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        if os.path.exists(task_dir): shutil.rmtree(task_dir)

# --- معالجة الأوامر ---
@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    chat_id = event.chat_id
    url = event.text
    status_msg = await event.respond("⚡ **جارٍ المعالجة...**")
    await start_direct_execution(chat_id, url, status_msg)

# --- تشغيل البوت ---
def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل (محرك الصور المحدث: Gallery-DL)")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
