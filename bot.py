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

# --- (بقية دوال الإعدادات والمكتبات كما هي) ---
def update_libraries():
    try: subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
update_libraries()

# --- دالة التنزيل المباشر المحدثة لدعم الإلغاء ---
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
                    # زر الإلغاء
                    cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
                    msg_text = f"📥 **جاري تنزيل الملف...**\n\n[{bar}] {percent:.1f}%\n⚡ **السرعة:** {human_readable_size(downloaded / elapsed if elapsed > 0 else 0)}/s"
                    asyncio.run_coroutine_threadsafe(status_msg.edit(msg_text, buttons=cancel_btn), loop)

# --- التنفيذ المباشر المحدث لدعم الإلغاء والرفع ---
async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', target_fmt='mp4', status_msg=None):
    if not status_msg: status_msg = await bot.send_message(chat_id, "⏳ **جاري التحضير...**")
    
    cancel_event = threading.Event()
    task_dir = os.path.join("downloads", f"task_{int(time.time() * 1000)}")
    os.makedirs(task_dir, exist_ok=True)
    ACTIVE_TASKS[id(cancel_event)] = {'event': cancel_event, 'dir': task_dir, 'msg': status_msg}

    try:
        loop = asyncio.get_event_loop()
        # [منطق التحميل...]
        # (استدعِ دوال التحميل ومرر لها cancel_event)
        filepath = os.path.join(task_dir, filename)
        await loop.run_in_executor(None, download_direct_file_with_progress, url, filepath, cancel_event, loop, status_msg)

        # كولباك الرفع مع الإلغاء
        async def upload_progress_callback(current, total):
            if cancel_event.is_set(): raise Exception("CANCELLED")
            # [تحديث الرسالة مع زر الإلغاء]
            cancel_btn = [Button.inline("❌ إلغاء العملية", data=f"cancel_{id(cancel_event)}")]
            await status_msg.edit(f"📤 **جاري الرفع...**", buttons=cancel_btn)

        # [عملية الرفع...]
        # (استخدم upload_progress_callback هنا)

    except Exception as e:
        if str(e) == "CANCELLED": await status_msg.edit("🚫 **تم إلغاء العملية.**")
        else: await status_msg.edit(f"❌ **خطأ:** `{str(e)}`")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        ACTIVE_TASKS.pop(id(cancel_event), None)

# --- معالجة الضغط على زر الإلغاء ---
@bot.on(events.CallbackQuery(pattern=b"cancel_"))
async def cancel_handler(event):
    cancel_id = int(event.data.decode('utf-8').split("_")[1])
    task = ACTIVE_TASKS.get(cancel_id)
    if task:
        task['event'].set()
        await event.edit("🚫 **جاري إيقاف العملية وتنظيف الملفات...**")
    else:
        await event.answer("⚠️ لا توجد عملية نشطة.")

# --- تعديل معالجة الرسائل لإضافة خيارات الصيغ ---
@bot.on(events.NewMessage)
async def handle_message(event):
    text = event.text.strip()
    if text.startswith('http'):
        # إذا كان الرابط مباشراً (وليس من منصات معقدة)
        if not is_complex_url(text):
            task_key = f"dl_{int(time.time())}"
            PENDING_TASKS[task_key] = {'url': text, 'filename': get_clean_filename(text)}
            buttons = [
                [Button.inline("🎥 فيديو (MP4)", data=f"fmt_mp4_{task_key}"), Button.inline("🎵 صوت (MP3)", data=f"fmt_mp3_{task_key}")],
                [Button.inline("📁 مستند", data=f"fmt_doc_{task_key}")]
            ]
            await event.respond("🔗 **تم اكتشاف رابط مباشر، اختر الصيغة:**", buttons=buttons)
        else:
            # المنصات المعقدة تعمل كالسابق تلقائياً
            await start_direct_execution(event.chat_id, text, get_clean_filename(text))

# --- معالجة اختيار الصيغ ---
@bot.on(events.CallbackQuery(pattern=b"fmt_"))
async def fmt_handler(event):
    data = event.data.decode('utf-8')
    _, fmt, task_key = data.split("_")
    task_info = PENDING_TASKS.pop(task_key, None)
    if not task_info: return await event.answer("انتهت صلاحية الطلب.")
    
    await event.edit("🚀 **جاري البدء...**")
    as_doc = (fmt == "doc")
    await start_direct_execution(event.chat_id, task_info['url'], task_info['filename'], as_doc=as_doc, target_fmt=fmt)

# [حافظ على باقي دوال الكود السابقة كما هي]
bot.start(bot_token=BOT_TOKEN)
bot.run_until_disconnected()
