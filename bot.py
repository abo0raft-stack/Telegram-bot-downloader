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
from PIL import Image
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# --- الإعدادات ---
VERSION = "v71.0-Smart-Media-Detection-Fix"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

USER_SETTINGS = {}
PENDING_TASKS = {}

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive'
}

bot = TelegramClient('bot_session', API_ID, API_HASH)

# --- كاشف ذكي لنوع الوسائط ---
def detect_x_media_type(url):
    """يفحص الرابط بسرعة ليعرف هل هو فيديو أم صور"""
    try:
        api_url = "https://api.cobalt.tools/api/json"
        res = requests.post(api_url, json={"url": url}, headers={"Content-Type": "application/json"}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "picker":
                return "photo" # يحتوي على عدة صور
            elif data.get("status") == "stream":
                return "video" # فيديو
        return "video" # الافتراضي إذا فشل الفحص
    except:
        return "video"

# --- الدوال الأساسية ---
def is_x_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com'])

def is_complex_url(url):
    domain = urlparse(url).netloc.lower()
    return any(x in domain for x in ['twitter.com', 'x.com', 'instagram.com', 'instagr.am', 'tiktok.com', 'vt.tiktok.com'])

def get_clean_filename(url):
    return "media_download"

# [تم اختصار الدوال المكررة: download_with_ytdlp, x_media_engine, ... نفس الكود السابق تماماً مع المحافظة على الوظائف]
# لضمان عدم تكرار الكود هنا، تم وضع الهيكل الأساسي الذي يهمك وهو تحديث الـ Handler

async def start_direct_execution(chat_id, url, filename, as_doc=False, quality='best', media_msg=None, target_fmt='mp4', status_msg=None):
    # ... [نفس كود التنفيذ السابق الذي يعمل بشكل ممتاز] ...
    # (تم اختصار الجزء هنا لتوفير المساحة، استخدم نفس منطق الدوال المعتمدة في النسخة السابقة للرفع)
    pass

@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    urls = re.findall(r"https?://\S+", event.text)
    if not urls: return
    chat_id = event.chat_id
    
    for u in urls:
        clean_u = u.split('?')[0] if 'instagram' in u else u
        
        # --- التحديث الجوهري هنا ---
        if is_x_url(clean_u):
            msg = await event.respond("🔍 **جاري فحص الرابط...**")
            media_type = detect_x_media_type(clean_u)
            
            if media_type == "photo":
                # إذا كانت صور، ابدأ فوراً بدون أسئلة
                await msg.edit("📸 **تم اكتشاف صور، جاري التنزيل...**")
                asyncio.create_task(start_direct_execution(chat_id, clean_u, "x_photo.jpg", quality='best', status_msg=msg))
            else:
                # إذا كان فيديو، أظهر الأزرار
                task_key = f"{chat_id}_{int(time.time()*1000)}"
                PENDING_TASKS[task_key] = clean_u
                buttons = [
                    [Button.inline("🎬 عالية (1080p)", data=f"q_1080_{task_key}"), Button.inline("🎥 متوسطة (720p)", data=f"q_720_{task_key}")],
                    [Button.inline("📱 منخفضة (480p)", data=f"q_480_{task_key}"), Button.inline("🎵 صوت فقط (MP3)", data=f"q_mp3_{task_key}")]
                ]
                await msg.edit("🎬 **اختر جودة الفيديو المطلوبة لمنصة X:**", buttons=buttons)
        else:
            # معالجة الروابط الأخرى (تيك توك/إنستا) بشكل عادي
            asyncio.create_task(start_direct_execution(chat_id, clean_u, get_clean_filename(clean_u)))

# --- باقي الكود (Callback & Main) يبقى كما هو ---
@bot.on(events.CallbackQuery(pattern=r"^q_"))
async def quality_callback_handler(event):
    data = event.data.decode("utf-8").split("_")
    quality_choice = data[1]
    task_key = "_".join(data[2:])
    
    if task_key not in PENDING_TASKS:
        await event.answer("⚠️ انتهت صلاحية هذا الخيار.", alert=True)
        return
    url = PENDING_TASKS.pop(task_key)
    chat_id = event.chat_id
    target_fmt = 'mp3' if quality_choice == 'mp3' else 'mp4'
    quality_val = 'best' if quality_choice == '1080' else quality_choice
    status_msg = await event.edit("⏳ **جاري بدء التنزيل...**", buttons=None)
    
    # دمج الدوال السابقة هنا لضمان استمرارية العمل
    asyncio.create_task(start_direct_execution(chat_id, url, "media.mp4", as_doc=False, quality=quality_val, target_fmt=target_fmt, status_msg=status_msg))

def main():
    bot.start(bot_token=BOT_TOKEN)
    print("🤖 البوت يعمل بكفاءة مع الفحص الذكي للوسائط!")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
