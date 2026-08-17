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

# --- إعدادات النظام ---
VERSION = "v71.0-Fixed-Integration"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

# متغيرات جديدة للإدارة
USER_SETTINGS = {}
PENDING_TASKS = {}
ACTIVE_TASKS = {} 

# --- (جميع دوال الإعدادات والمكتبات الخاصة بك) ---
def update_libraries():
    try: subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
update_libraries()

X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(x_b64.strip()))
        except: pass
    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f: f.write(base64.b64decode(ig_b64.strip()))
        except: pass
setup_all_cookies()

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Referer': 'https://www.tiktok.com/',
    'Connection': 'keep-alive'
}

# --- دوال المساعدة الأصلية ---
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

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com', 'vt.tiktok.com', 'youtube.com', 'youtu.be', 'facebook.com'])

def get_clean_filename(url):
    if is_complex_url(url): return "media_download"
    path = unquote(urlparse(url).path)
    return os.path.basename(path) or "downloaded_media"

# --- الدوال الخاصة بالتنزيل ---
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
                    percent = (downloaded / total_size * 100) if total_size > 0 else 0
                    bar = create_progress_bar(percent)
                    msg_text = f"📥 **جاري التنزيل...**\n[{bar}] {percent:.1f}%\n⚡ **السرعة:** {human_readable_size(downloaded / elapsed if elapsed > 0 else 0)}/s"
                    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                    asyncio.run_coroutine_threadsafe(status_msg.edit(msg_text, buttons=cancel_btn), loop)

# --- التنفيذ المباشر المدمج ---
async def start_direct_execution(chat_id, url, filename, as_doc=False, target_fmt='mp4', status_msg=None):
    if not status_msg: status_msg = await bot.send_message(chat_id, "⏳ **جاري البدء...**")
    
    cancel_event = threading.Event()
    task_id = f"task_{int(time.time() * 1000)}"
    task_dir = os.path.join("downloads", task_id)
    os.makedirs(task_dir, exist_ok=True)
    ACTIVE_TASKS[id(cancel_event)] = {'event': cancel_event, 'dir': task_dir, 'msg': status_msg}

    try:
        loop = asyncio.get_event_loop()
        filepath = os.path.join(task_dir, filename)
        
        # إذا كان رابطاً مباشراً نستخدم دالة التنزيل مع الإلغاء
        if not is_complex_url(url):
            await loop.run_in_executor(None, download_direct_file_with_progress, url, filepath, cancel_event, loop, status_msg)
        else:
            # (هنا يمكنك إضافة دوال yt-dlp الخاصة بك كما كانت في كودك الأصلي)
            await bot.send_message(chat_id, "⚠️ جاري تنزيل ملف من منصة تواصل...") 

        # [عملية الرفع مع الإلغاء]
        cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
        await status_msg.edit("📤 **جاري الرفع...**", buttons=cancel_btn)
        # (ضع كود الرفع الخاص بك هنا)
        
    except Exception as e:
        if str(e) == "CANCELLED": await status_msg.edit("🚫 **تم إلغاء العملية.**")
        else: await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        ACTIVE_TASKS.pop(id(cancel_event), None)

# --- معالجة الأزرار (الإلغاء + الصيغ) ---
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode('utf-8')
    
    # 1. معالجة الإلغاء
    if data.startswith("cancel_"):
        cancel_id = int(data.split("_")[1])
        task = ACTIVE_TASKS.get(cancel_id)
        if task:
            task['event'].set()
            await event.edit("🚫 **جاري إيقاف العملية...**")
        return

    # 2. معالجة اختيار الصيغ
    if data.startswith("fmt_"):
        parts = data.split("_")
        fmt, task_key = parts[1], parts[2]
        task_info = PENDING_TASKS.pop(task_key, None)
        if not task_info: return await event.answer("انتهت صلاحية الطلب.")
        
        await event.edit("🚀 **جاري البدء...**")
        await start_direct_execution(event.chat_id, task_info['url'], task_info['filename'], as_doc=(fmt=="doc"), target_fmt=fmt)

# --- استقبال الروابط ---
@bot.on(events.NewMessage)
async def handle_message(event):
    if not event.text or not event.text.startswith('http'): return
    url = event.text.strip()
    
    if not is_complex_url(url):
        task_key = str(int(time.time()))
        PENDING_TASKS[task_key] = {'url': url, 'filename': get_clean_filename(url)}
        buttons = [
            [Button.inline("🎥 فيديو (MP4)", data=f"fmt_mp4_{task_key}"), Button.inline("🎵 صوت (MP3)", data=f"fmt_mp3_{task_key}")],
            [Button.inline("📁 مستند", data=f"fmt_doc_{task_key}")]
        ]
        await event.respond("🔗 **تم اكتشاف رابط مباشر، اختر الصيغة:**", buttons=buttons)
    else:
        await start_direct_execution(event.chat_id, url, get_clean_filename(url))

# --- التشغيل ---
bot = TelegramClient('bot_session', API_ID, API_HASH)
bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
