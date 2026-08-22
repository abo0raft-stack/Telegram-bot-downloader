# Telegram-bot-downloader

بوت تنزيل شخصي بلغة بايثون يعمل عبر Telethon؛ يستقبل روابط عبر تيليجرام ويقوم بتنزيل الوسائط من مصادر متعددة (YouTube, TikTok, Instagram, Dailymotion, روابط مباشرة، إلخ) ثم يرسلها إلى المحادثة بعد معالجة بسيطة (ffmpeg، إنشاء صور، تحويل الصيغ).

## المتطلبات الأساسية
- Python 3.10
- ffmpeg (مطلوب لمعالجة الفيديو/استخراج صور مصغرة)

## المتغيرات البيئية
راجع `.env.example` في المستودع. المتغيرات الأساسية:

- API_ID
- API_HASH
- BOT_TOKEN
- OWNER_ID
- PORT
- X_COOKIES_BASE64 (اختياري)
- INSTAGRAM_COOKIES_BASE64 (اختياري)

## كيف تشغّله

1) تشغيل محلي سريع

```bash
python -m pip install -r requirements.txt
# ضبط المتغيرات البيئية (يمكن نسخ .env.example إلى .env وملؤه)
export API_ID=123456
export API_HASH="your_api_hash"
export BOT_TOKEN="123456:ABC-DEF..."
export OWNER_ID=5414125521
python bot.py
