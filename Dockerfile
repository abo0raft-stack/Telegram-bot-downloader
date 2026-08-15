FROM python:3.10-slim

# تثبيت ffmpeg والأدوات البرمجية الأساسية لبناء المكتبات ودعم الميديا
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل داخل الحاوية
WORKDIR /app

# نسخ ملف المتطلبات أولاً للاستفادة من Docker Cache
COPY requirements.txt .

# تثبيت جميع مكتبات البايثون المحددة
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع إلى الحاوية
COPY . .

# أمر تشغيل البوت الرئيسي
CMD ["python", "bot.py"]
