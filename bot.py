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
import mimetypes
import math
from PIL import Image
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- الإعدادات ---
VERSION = "v71.0-CancelButton-Stable"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}
PENDING_TASKS = {}
ACTIVE_TASKS = {} # تخزين المهام النشطة للإلغاء

# --- التحديث التلقائي للمكتبات ---
def update_libraries():
    try:
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

update_libraries()

# --- التعامل مع الكوكيز ---
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        with open(X_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(x_b64.strip()))
    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        with open(INSTAGRAM_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(ig_b64.strip()))

setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive'
}

# --- دوال مساعدة ---
def human_readable_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def human_readable_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s" if m > 0 else f"{s}s"

def create_progress_bar(percentage, length=10):
    filled = int(length * percentage // 100)
    return "█" * filled + "░" * (length - filled)

# --- محركات التنزيل ---
def download_direct_file_with_progress(url, filepath, cancel_event, loop, status_msg):
    res = requests.get(url, stream=True, headers=BROWSER_HEADERS, timeout=30, verify=False)
    res.raise_for_status()
    total_size = int(res.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    last_update = 0

    with open(filepath, 'wb') as f:
        for chunk in res.iter_content(chunk_size=1024*512):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_update > 2.5:
                    last_update = now
                    elapsed = now - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    percent = (downloaded / total_size * 100) if total_size > 0 else 0
                    bar = create_progress_bar(percent)
                    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                    msg_text = f"📥 **جاري تنزيل الملف...**\n\n[{bar}] {percent:.1f}%\n⏱️ **الوقت:** {human_readable_time(elapsed)}\n💾 **الحجم:** {human_readable_size(total_size)}\n⚡ **السرعة:** {human_readable_size(speed)}/s"
                    asyncio.run_coroutine_threadsafe(status_msg.edit(msg_text, buttons=cancel_btn), loop)

# --- دالة التنفيذ الرئيسية ---
async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None):
    if not status_msg: status_msg = await bot.send_message(chat_id, "⏳ **جاري التحضير...**")
    
    cancel_event = threading.Event()
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    ACTIVE_TASKS[id(cancel_event)] = {'event': cancel_event, 'dir': task_dir, 'msg': status_msg}

    try:
        loop = asyncio.get_event_loop()
        
        # [هنا توضع منطق التنزيل المعتاد (yt-dlp, cobalt الخ...)]
        # اختصارا للمساحة هنا، يتم استدعاء دوال التنزيل التي تتقبل cancel_event
        # (يفترض دمج الدوال السابقة كما هي مع تمرير cancel_event)
        
        # --- كولباك الرفع مع الإلغاء ---
        start_upload_time = time.time()
        last_up = [0]
        async def upload_progress_callback(current, total):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            now = time.time()
            if now - last_up[0] > 2.5:
                last_up[0] = now
                percent = (current / total * 100) if total > 0 else 0
                bar = create_progress_bar(percent)
                cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                await status_msg.edit(f"📤 **جاري الرفع...**\n\n[{bar}] {percent:.1f}%\n💾 **تم:** {human_readable_size(current)} / {human_readable_size(total)}", buttons=cancel_btn)

        # [تتم عملية الرفع هنا باستخدام upload_progress_callback]
        
    except Exception as e:
        if str(e) == "CANCELLED":
            await status_msg.edit("🚫 **تم إلغاء العملية بنجاح.**")
        else:
            await status_msg.edit(f"❌ **حدث خطأ:**\n`{str(e)}`")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        ACTIVE_TASKS.pop(id(cancel_event), None)

# --- معالجة الإلغاء ---
@bot.on(events.CallbackQuery(pattern=b"cancel_"))
async def cancel_handler(event):
    cancel_id = int(event.data.decode('utf-8').split("_")[1])
    task = ACTIVE_TASKS.get(cancel_id)
    if task:
        task['event'].set()
        await event.edit("🚫 **جاري إيقاف العملية وتنظيف الملفات...**")
    else:
        await event.answer("⚠️ لا توجد عملية نشطة.")

# --- التشغيل ---
bot = TelegramClient('bot_session', API_ID, API_HASH)
print("🚀 البوت يعمل الآن...")
bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
