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
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

# --- سيرفر وهمي ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): 
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever(), daemon=True).start()

bot = TelegramClient('bot_session', int(API_ID), API_HASH)

# --- المهام ---
def get_video_metadata(filepath):
    duration, width, height = 0, 1280, 720
    try:
        parser = createParser(filepath)
        with parser:
            metadata = extractMetadata(parser)
            if metadata:
                if metadata.has("duration"): duration = int(metadata.get('duration').seconds)
                if metadata.has("width"): width = int(metadata.get('width'))
                if metadata.has("height"): height = int(metadata.get('height'))
    except: pass
    return duration, width, height

def run_ffmpeg(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)

async def start_execution(chat_id, url, filename_title):
    status_msg = await bot.send_message(chat_id, "⏳ جاري التحميل...")
    file_path = f"downloads/{status_msg.id}_{filename_title}"
    
    # 1. التحميل
    ydl_opts = {'format': 'best', 'outtmpl': file_path}
    yt_dlp.YoutubeDL(ydl_opts).download([url])

    # 2. استخراج الميتا واللقطات
    duration, w, h = get_video_metadata(file_path)
    
    # الصورة المصغرة
    thumb_path = f"downloads/thumb_{status_msg.id}.jpg"
    run_ffmpeg(['ffmpeg', '-y', '-ss', '00:00:02', '-i', file_path, '-vframes', '1', '-q:v', '2', thumb_path])
    if os.path.exists(thumb_path):
        im = Image.open(thumb_path); im.thumbnail((320,320)); im.save(thumb_path, 'JPEG')

    # اللقطات الـ 9
    shots_dir = f"downloads/shots_{status_msg.id}"
    os.makedirs(shots_dir, exist_ok=True)
    shots_list = []
    step = duration / 10
    for i in range(1, 10):
        ts = str(int(step * i))
        shot = os.path.join(shots_dir, f"shot_{i}.jpg")
        run_ffmpeg(['ffmpeg', '-y', '-ss', ts, '-i', file_path, '-vframes', '1', '-q:v', '3', shot])
        if os.path.exists(shot): shots_list.append(shot)

    # 3. الإرسال
    if shots_list:
        await bot.send_file(chat_id, shots_list, caption="📸 لقطات من الفيديو:")
    
    await bot.send_file(
        chat_id, file_path, 
        caption=f"✅ {filename_title}",
        thumb=thumb_path if os.path.exists(thumb_path) else None,
        attributes=[DocumentAttributeVideo(duration=duration, w=w, h=h, supports_streaming=True)]
    )
    
    await status_msg.delete()
    # تنظيف
    for f in [file_path, thumb_path] + shots_list:
        if os.path.exists(f): os.remove(f)

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handler(event):
    url = event.text.strip()
    await start_execution(event.chat_id, url, "video.mp4")

bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
