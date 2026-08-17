import os
import re
import asyncio
import base64
import requests
import yt_dlp
from telethon import TelegramClient, events

# --- الإعدادات الأساسية ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ملف الكوكيز الخاص بمنصة X
X_COOKIES_FILE = "x_cookies.txt"

def setup_x_cookies():
    """
    يقوم بفك تشفير الكوكيز من متغير البيئة X_COOKIES_BASE64 وإنشاء الملف
    """
    x_b64 = os.environ.get("X_COOKIES_BASE64", "")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
            print("✅ تم تحميل ملف الكوكيز بنجاح!")
        except Exception as e:
            print(f"⚠️ خطأ أثناء فك تشفير الكوكيز: {e}")

# تشغيل إعداد الكوكيز فور تشغيل البوت
setup_x_cookies()

bot = TelegramClient('bot_session', API_ID, API_HASH)

# --- المحرك الذكي لجلب جميع الوسائط (فيديو + صور) ---
async def smart_fetch_x_media(url):
    """
    يجلب جميع الصور والفيديوهات من التغريدة بأعلى جودة
    """
    downloaded_files = []
    
    # 1. إعدادات yt-dlp المحدثة لتشمل الصور والفيديوهات
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'writethumbnail': True,
        'outtmpl': 'downloads/%(id)s_%(autonumber)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'cookiefile': X_COOKIES_FILE if os.path.exists(X_COOKIES_FILE) else None,
    }

    try:
        # محاولة التنزيل المباشر باستخدام yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                entries = info.get('entries', [info])
                for entry in entries:
                    if 'requested_downloads' in entry:
                        for d in entry['requested_downloads']:
                            if os.path.exists(d['filepath']):
                                downloaded_files.append(d['filepath'])
                    else:
                        filename = ydl.prepare_filename(entry)
                        if os.path.exists(filename):
                            downloaded_files.append(filename)

    except Exception as e:
        print(f"yt-dlp Fetch Error: {e}")

    # 2. خطة أمان إضافية (Fallback): في حال كان الرابط يحتوي على صور فقط وفشل yt-dlp
    if not downloaded_files:
        try:
            api_url = f"https://api.cobalt.tools/api/json"
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            payload = {"url": url}
            
            response = requests.post(api_url, json=payload, headers=headers, timeout=12)
            if response.status_code == 200:
                data = response.json()
                # التعامل مع الاستجابة المتعددة (الصور والفيديوهات)
                if data.get("status") == "picker":
                    for item in data.get("picker", []):
                        media_url = item.get("url")
                        if media_url:
                            r = requests.get(media_url, stream=True)
                            ext = "jpg" if item.get("type") == "photo" else "mp4"
                            filepath = f"downloads/cobalt_{os.urandom(4).hex()}.{ext}"
                            os.makedirs("downloads", exist_ok=True)
                            with open(filepath, 'wb') as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                            downloaded_files.append(filepath)
                elif data.get("status") == "url":
                    media_url = data.get("url")
                    r = requests.get(media_url, stream=True)
                    filepath = f"downloads/cobalt_{os.urandom(4).hex()}.mp4"
                    os.makedirs("downloads", exist_ok=True)
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    downloaded_files.append(filepath)
        except Exception as e:
            print(f"Fallback Fetch Error: {e}")

    return downloaded_files

# --- معالج الرسائل ---
@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    url = event.text.strip()
    
    if any(domain in url for domain in ['twitter.com', 'x.com']):
        msg = await event.respond("🔄 **جاري جلب الوسائط (فيديو + صور) بأعلى جودة...**")
        
        # استدعاء الجلب الذكي
        files = await smart_fetch_x_media(url)
        
        if not files:
            await msg.edit("❌ **عذراً، فشل جلب الوسائط. يرجى التأكد من الكوكيز أو الرابط.**")
            return
        
        await msg.edit("✅ **تم الجلب! جاري الإرسال...**")
        
        # إرسال الملفات بالكامل
        for file_path in files:
            try:
                await event.respond(file=file_path)
            except Exception as e:
                print(f"Error sending file: {e}")
            finally:
                # التنظيف المباشر للملف من السيرفر بعد الإرسال
                if os.path.exists(file_path):
                    os.remove(file_path)
        
        await msg.delete()
    else:
        await event.respond("🔗 **جاري معالجة الرابط...**")

def main():
    print("🤖 البوت يعمل بجلب ذكي للوسائط ولدعم الكوكيز المباشر!")
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
