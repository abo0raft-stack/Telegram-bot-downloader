# مسارات ملفات الكوكيز
X_COOKIES_FILE = "x_cookies.txt"
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"

def setup_all_cookies():
    """استخراج كوكيز إكس وإنستغرام من متغيرات البيئة"""
    
    # 1. كوكيز إكس (تويتر)
    x_b64 = os.environ.get("X_COOKIES_BASE64")
    if x_b64:
        try:
            with open(X_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(x_b64.strip()))
            print("✅ تم تجهيز كوكيز منصة X بنجاح.")
        except Exception as e:
            print(f"❌ خطأ في كوكيز X: {e}")

    # 2. كوكيز إنستغرام
    ig_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64")
    if ig_b64:
        try:
            with open(INSTAGRAM_COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(ig_b64.strip()))
            print("✅ تم تجهيز كوكيز إنستغرام بنجاح.")
        except Exception as e:
            print(f"❌ خطأ في كوكيز إنستغرام: {e}")

# تشغيل الدالة عند بدء السكريبت
setup_all_cookies()
