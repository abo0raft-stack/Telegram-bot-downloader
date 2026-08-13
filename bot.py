import asyncio
import os
import re
import sys
import time
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
import aiohttp
import yt_dlp
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# --- التحديث الإصدار ---
VERSION = "v9.0 Pro"

# --- جلب المتغيرات من إعدادات Render ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

# التحقق من وجود المتغيرات
if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ خطأ: يرجى إدخال API_ID و API_HASH و BOT_TOKEN في Environment Variables على Render!")
    sys.exit(1)

# --- ترويسات حقيقية لتجاوز الحظر والتعرف على الملفات ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Not)A;Brand";v="99", "Google Chrome";v="127", "Chromium";v="127"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# --- سيرفر إبقاء الخدمة مستيقظة في Render ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot status: OK")
    def log_message(self, format, *args): pass

def run_health_server():
    try:
        httpd = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        print(f"🌐 HealthCheck Server started on port {PORT}")
        httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")

threading.Thread(target=run_health_server, daemon=True).start()

# --- إعداد العميل ---
bot = TelegramClient('bot_session', int(API_ID), API_HASH)

def format_bytes(size):
    if not size: return "غير معروف"
    return f"{size / (1024 * 1024 * 1024):.2f} GB" if size >= 1024*1024*1024 else f"{size / (1024 * 1024):.1f} MB"

async def safe_edit(msg, text):
    try: await msg.edit(text, parse_mode="markdown")
    except Exception: pass

# --- محرك التنزيل المباشر مع دعم العداد واستخراج الاسم الأصلي ---
async def download_direct(url, status_msg):
    req_headers = HEADERS.copy()
    match = re.match(r"https?://([^/]+)", url)
    if match:
        req_headers["Referer"] = f"{match.group(0)}/"

    timeout = aiohttp.ClientTimeout(total=21600, connect=60)
    try:
        async with aiohttp.ClientSession(headers=req_headers, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", None

                content_type = resp.headers.get("Content-Type", "").lower()
                # إذا كانت الاستجابة صفحة HTML وليست ملفاً مباشرة
                if "text/html" in content_type:
                    return False, "IS_HTML", None

                # محاولة استخراج اسم الملف الأصلي من الترويسة
                cd = resp.headers.get("Content-Disposition", "")
                filename = None
                if "filename=" in cd:
                    filename_match = re.findall(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd)
                    if filename_match:
                        filename = filename_match[0].strip()

                if not filename:
                    clean_url = url.split("?")[0].split("#")[0]
                    filename = os.path.basename(clean_url) or f"file_{int(time.time())}"

                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                last_update = 0

                with open(filename, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update > 4:
                            last_update = now
                            prog = f"📥 **جاري التحميل المباشر ({VERSION})...**\n\n"
                            prog += f"📦 الملف: `{filename}`\n"
                            prog += f"🚀 تم تنزيل: `{format_bytes(downloaded)}`"
                            if total_size > 0:
                                p = (downloaded / total_size) * 100
                                prog += f" / `{format_bytes(total_size)}` (`{p:.1f}%`)"
                            await safe_edit(status_msg, prog)

                return True, filename, total_size
    except Exception as e:
        return False, str(e), None

# --- محرك yt-dlp التناوبي للملفات والروابط المعقدة ---
async def download_ytdlp(url, status_msg):
    loop = asyncio.get_event_loop()
    out_tmpl = f"download_{int(time.time())}_%(title)s.%(ext)s"
    
    def progress_hook(d):
        if d['status'] == 'downloading':
            try:
                p = d.get('_percent_str', '0%')
                s = d.get('_speed_str', 'N/A')
                e = d.get('_eta_str', 'N/A')
                txt = f"📥 **جاري التنزيل عبر المحرك المتقدم ({VERSION})...**\n\n📊 النسبة: `{p}`\n🚀 السرعة: `{s}`\n⏱ الوقت المتبقي: `{e}`"
                asyncio.run_coroutine_threadsafe(safe_edit(status_msg, txt), loop)
            except Exception: pass

    ydl_opts = {
        'outtmpl': out_tmpl,
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        'http_headers': HEADERS,
        'progress_hooks': [progress_hook],
    }

    def run_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    try:
        filepath = await loop.run_in_executor(None, run_dl)
        if filepath and os.path.exists(filepath):
            return True, filepath
    except Exception as e:
        print(f"yt-dlp error: {e}")

    return False, "فشل استخراج التحميل من الصفحة."

# --- المعاينة الذكية للفيديوهات ---
def get_video_meta(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip().split("\n")
        return int(out[0]), int(out[1]), int(float(out[2]))
    except Exception: return 1280, 720, 0

def make_thumb(path):
    thumb = f"{path}_thumb.jpg"
    try:
        cmd = ["ffmpeg", "-y", "-ss", "00:00:02", "-i", path, "-vframes", "1", "-vf", "scale=320:-1", thumb]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumb if os.path.exists(thumb) else None
    except Exception: return None

# --- الأوامر ---
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    await event.respond(
        f"مرحباً بك في بوت التنزيل والرفع المباشر الشامل ({VERSION}) 🚀\n\n"
        "💡 **المميزات:**\n"
        "• دعم جميع أنواع الملفات (فيديو، صوت، تطبيق APK، ضغط ZIP، مستندات...)\n"
        "• استخراج الترويسة والتعرف على الامتداد الأصلي للملف تلقائياً.\n"
        "• عداد تفاعلي يوضح السرعة والنسبة المئوية.\n\n"
        "أرسل رابط الملف ليتم معالجته فوراً!"
    )

@bot.on(events.NewMessage(pattern=r"^https?://"))
async def handle_url(event):
    url = event.text.strip()
    status_msg = await event.respond("⏳ **جاري تحليل الرابط والتعرف على نوع الملف...**")

    # 1. محاولة التنزيل المباشر أولاً
    success, result, size = await download_direct(url, status_msg)
    filename = result if success else None

    # 2. الانتقال لمحرك yt-dlp إذا كان الرابط لصفحة HTML أو فشل التحميل المباشر
    if not success:
        await safe_edit(status_msg, "🛡️ **الرابط يحتاج تحليل/تجاوز، جاري التنزيل عبر المحرك المتقدم...**")
        success, filepath = await download_ytdlp(url, status_msg)
        if success:
            filename = filepath
        else:
            await safe_edit(status_msg, f"❌ **فشل التحميل:**\n`{filepath}`")
            return

    file_size = os.path.getsize(filename)
    await safe_edit(status_msg, f"📤 **جاري الرفع إلى تليجرام ({VERSION})...**\n📦 الملف: `{os.path.basename(filename)}`\n📏 الحجم: `{format_bytes(file_size)}`")

    is_video = filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
    thumb = None
    attributes = None

    if is_video:
        w, h, dur = get_video_meta(filename)
        thumb = make_thumb(filename)
        attributes = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)]

    try:
        await bot.send_file(
            event.chat_id,
            filename,
            caption=f"✅ **تم التحميل والرفع بنجاح ({VERSION})!**\n📄 **الاسم:** `{os.path.basename(filename)}`\n📦 **الحجم:** `{format_bytes(file_size)}`",
            thumb=thumb,
            attributes=attributes,
            supports_streaming=is_video,
            reply_to=event.id
        )
        await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ خطأ أثناء الرفع إلى تليجرام:\n`{str(e)}`")
    finally:
        if filename and os.path.exists(filename): os.remove(filename)
        if thumb and os.path.exists(thumb): os.remove(thumb)

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    print(f"✅ Bot {VERSION} connected and running successfully!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
