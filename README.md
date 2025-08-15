# Pinterest Telegram Bot (No API)

بوت تليجرام لتحميل فيديوهات وصور Pinterest بدون استخدام أي API رسمي.
- يدعم روابط `pin.it` المختصرة ويحوّلها تلقائياً.
- يحاول جلب أفضل جودة للفيديو/الصورة.
- يرسل الوسائط مباشرة في المحادثة (ويستخدم Document لو الملف كبير).

## المتطلبات
- Python 3.10+
- توكن بوت من BotFather مخزّن في متغيّر البيئة `BOT_TOKEN`.

## التشغيل محلياً
```bash
pip install -r requirements.txt
export BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
python bot.py
