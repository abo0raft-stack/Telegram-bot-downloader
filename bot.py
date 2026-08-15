import asyncio
import os
import threading
import time
import requests
import yt_dlp
import subprocess
from PIL import Image
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- الإعدادات ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

# --- دوال المساعدة ---
def get_video_metadata(filepath):
    try:
        parser = createParser(filepath)
        with parser:
            metadata = extractMetadata(parser)
            duration = int(metadata.get('duration').seconds) if metadata.has('duration') else 60
            width = int(metadata.get('width')) if metadata.has('width') else 1280
            height = int(metadata.get('height')) if metadata.has('height') else 720
            return duration, width, height
    except Exception as e:
        print(f"DEBUG: Metadata failed: {e}")
        return 60, 1280, 720

def run_ffmpeg(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"DEBUG: FFmpeg Error: {result.stderr}")
            return False
        return True
    except Exception as e:
        print(f"DEBUG: FFmpeg Exception: {e}")
        return False

# --- الوظيفة الرئيسية ---
async def start_execution(chat_id, url, filename_title):
    status_msg = await bot.send_message(chat_id, "⏳ جاري التحميل...")
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    
    # 1. التنزيل مع جلب الصورة المصغرة الأصلية
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'outtmpl': file_path,
        'writethumbnail': True,
    }
    
    try:
        def dl(): yt_dlp.YoutubeDL(ydl_opts).download([url])
        await asyncio.to_thread(dl)
    except Exception as e:
        await status_msg.edit(f"❌ خطأ في التحميل: {e}")
        return

    # 2. البحث عن الصورة المصغرة الأصلية
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    found_thumb = False
    base_name = os.path.splitext(file_path)[0]
    
    for ext in ['.jpg', '.webp', '.png', '.jpeg']:
        if os.path.exists(base_name + ext):
            try:
                im = Image.open(base_name + ext)
                im.thumbnail((320, 320))
                im.convert('RGB').save(thumb_path, 'JPEG')
                os.remove(base_name + ext)
                found_thumb = True
                print(f"DEBUG: Found original thumb: {base_name + ext}")
                break
            except: pass

    # الفالباك: إذا لم توجد صورة أصلية
    if not found_thumb:
        duration, _, _ = get_video_metadata(file_path)
        run_ffmpeg(['ffmpeg', '-ss', '00:00:05', '-i', file_path, '-vframes', '1', '-q:v', '2', '-y', thumb_path])

    # 3. استخراج 9 لقطات
    duration, w, h = get_video_metadata(file_path)
    shots_dir = f"downloads/shots_{status_msg.id}"
    os.makedirs(shots_dir, exist_ok=True)
    shots_list = []
    
    step = duration / 10
    for i in range(1, 10):
        ts = str(int(step * i))
        shot_file = os.path.join(shots_dir, f"shot_{i}.jpg")
        # أمر FFmpeg مفصل للضمان
        if run_ffmpeg(['ffmpeg', '-ss', ts, '-i', file_path, '-vframes', '1', '-q:v', '3', '-y', shot_file]):
            shots_list.append(shot_file)
            print(f"DEBUG: Created shot {i}")
        else:
            print(f"DEBUG: Failed shot {i}")

    # 4. الإرسال
    if shots_list:
        await bot.send_file(chat_id, shots_list, caption="📸 لقطات الفيديو:")
    
    await bot.send_file(
        chat_id, file_path, 
        caption=f"✅ {filename_title}",
        thumb=thumb_path if os.path.exists(thumb_path) else None,
        attributes=[DocumentAttributeVideo(duration=duration, w=w, h=h, supports_streaming=True)]
    )
    
    # 5. تنظيف
    await status_msg.delete()
    if os.path.exists(file_path): os.remove(file_path)
    if os.path.exists(thumb_path): os.remove(thumb_path)
    for f in shots_list: os.remove(f)
    if os.path.exists(shots_dir): os.rmdir(shots_dir)

# --- البوت ---
@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handler(event):
    await start_execution(event.chat_id, event.text.strip(), "video.mp4")

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
