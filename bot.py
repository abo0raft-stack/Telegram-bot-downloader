import os
import re
import asyncio
import yt_dlp
from telethon import TelegramClient, events

# --- الإعدادات ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

bot = TelegramClient('bot_session', API_ID, API_HASH)

# --- إعدادات yt-dlp ---
YDL_OPTIONS = {
    'format': 'best',
    'quiet': True,
    'no_warnings': True,
    'cookiefile': 'x_cookies.txt' if os.path.exists('x_cookies.txt') else None,
}

# --- المحرك الذكي لجلب الوسائط ---
async def smart_fetch_x_media(url):
    """يجلب جميع الوسائط (فيديو وصور) من تغريدة X ويرجع قائمة بالملفات"""
    downloaded_files = []
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # إذا كان هناك عدة ملفات (تغريدة فيها وسائط متعددة)
            entries = info.get('entries', [info])
            
            for entry in entries:
                # التأكد من وجود مسار الملف
                filename = ydl.prepare_filename(entry)
                if os.path.exists(filename):
                    downloaded_files.append(filename)
                elif 'requested_downloads' in entry:
                    for d in entry['requested_downloads']:
                        if os.path.exists(d['filepath']):
                            downloaded_files.append(d['filepath'])
                            
        return downloaded_files
    except Exception as e:
        print(f"Error fetching: {e}")
        return []

# --- المعالج الرئيسي ---
@bot.on(events.NewMessage(pattern=r"(https?://\S+)"))
async def url_handler(event):
    url = event.text.strip()
    
    # فحص هل الرابط من X/Twitter
    if any(domain in url for domain in ['twitter.com', 'x.com']):
        msg = await event.respond("🔄 **جاري جلب جميع الوسائط (فيديو + صور) بأعلى جودة...**")
        
        # استدعاء المحرك الذكي
        files = await smart_fetch_x_media(url)
        
        if not files:
            await msg.edit("❌ **عذراً، فشل جلب الوسائط. قد يكون الحساب خاصاً أو الرابط غير صالح.**")
            return
        
        await msg.edit("✅ **تم الجلب! جاري الإرسال...**")
        
        # إرسال الملفات المستخرجة
        for file in files:
            try:
                await event.respond(file=file)
                # حذف الملف بعد الإرسال لتنظيف السيرفر
                os.remove(file)
            except:
                pass
        
        await msg.delete()
    else:
        # معالجة الروابط الأخرى (إنستغرام، تيك توك، إلخ) كما كنت تفعل سابقاً
        await event.respond("🔗 **جاري معالجة الرابط...**")
        # [ضع هنا دالة المعالجة المعتادة لباقي المنصات]

def main():
    print("🤖 البوت يعمل بوضع (Smart Fetch) - لا حاجة للاختيارات!")
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
