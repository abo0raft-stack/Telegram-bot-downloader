import os
import shutil
import threading
import time
import gc
import re
import base64
import json
import sys
import mimetypes
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- التحديث والتثبيت التلقائي للمكتبات ---
def update_and_install_libraries():
    try:
        import subprocess
        subprocess.run(["pip", "install", "-U", "yt-dlp", "gallery-dl", "Pillow", "aiohttp", "aiosqlite", "telethon", "requests", "psutil"], check=True)
        print("✅ تم تثبيت وتحديث المكتبات بنجاح.")
    except Exception as e:
        print(f"⚠️ فشل التحديث التلقائي: {e}")

update_and_install_libraries()

import asyncio
import requests
import yt_dlp
import aiosqlite
import aiohttp
import psutil
from PIL import Image, ImageDraw, ImageFont
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

# تحديد حد أقصى للعمليات الثقيلة المتزامنة
MAX_CONCURRENT_TASKS = asyncio.Semaphore(2)

# --- إدارة قاعدة البيانات (SQLite) ---
DB_FILE = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                snapshots INTEGER,
                social_snapshots INTEGER,
                quality TEXT,
                font_size TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pending_tasks (
                task_key TEXT PRIMARY KEY,
                url TEXT,
                task_type TEXT,
                created_at REAL
            )
        ''')
        await conn.commit()

asyncio.run(init_db())

async def get_user_config(chat_id):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT snapshots, social_snapshots, quality, font_size FROM user_settings WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "snapshots": bool(row[0]),
                    "social_snapshots": bool(row[1]),
                    "quality": row[2],
                    "font_size": row[3]
                }
            else:
                default_config = {
                    "snapshots": True,
                    "social_snapshots": False,
                    "quality": "720",
                    "font_size": "large"
                }
                await set_user_config(chat_id, default_config)
                return default_config

async def set_user_config(chat_id, config):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO user_settings (chat_id, snapshots, social_snapshots, quality, font_size)
            VALUES (?, ?, ?, ?, ?)
        ''', (chat_id, int(config["snapshots"]), int(config["social_snapshots"]), config["quality"], config["font_size"]))
        await conn.commit()

async def save_task(task_key, url, task_type):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO pending_tasks VALUES (?, ?, ?, ?)", (task_key, url, task_type, time.time()))
        await conn.commit()

async def pop_task(task_key):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT url, task_type FROM pending_tasks WHERE task_key = ?", (task_key,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await conn.execute("DELETE FROM pending_tasks WHERE task_key = ?", (task_key,))
                await conn.commit()
                return row[0], row[1]
    return None, None

# --- الإعدادات والحماية الخاصة ---
VERSION = "v82.0-Dailymotion-Fix"
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "5414125521"))
PORT = int(os.environ.get("PORT", 8080))

ACTIVE_CANCEL_EVENTS = {}

FONT_SIZE_MAP = {
    "small": 2.18,
    "medium": 3.25,
    "large": 5.35,
    "xlarge": 10.45
}

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
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8,ar;q=0.8',
    'Referer': 'https://www.google.com/',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Connection': 'keep-alive'
}

def is_owner(chat_id):
    return chat_id == OWNER_ID

def clean_download_folder():
    folder = "downloads"
    cleaned_bytes = 0
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    cleaned_bytes += os.path.getsize(fp)
                    os.remove(fp)
                except Exception: pass
            for d in dirs:
                try: shutil.rmtree(os.path.join(root, d))
                except Exception: pass
    else:
        os.makedirs(folder, exist_ok=True)
    gc.collect()
    return cleaned_bytes

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

# ... (the rest of bot.py unchanged) ...

def main():
    bot.start(bot_token=BOT_TOKEN)
    print(f"🤖 البوت محمي بنجاح ويعمل حصرياً للمالك: {OWNER_ID}")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
