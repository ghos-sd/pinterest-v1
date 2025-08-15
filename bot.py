# -*- coding: utf-8 -*-
import os, re, json, time, logging
from typing import Optional, Tuple, Any
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ===== إعدادات عامة =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-bot")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
HTTP_TIMEOUT = 25

PIN_HOSTS = (
    "pinterest.com","www.pinterest.com","pin.it",
    "in.pinterest.com","www.pinterest.co.uk"
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


# ===== أدوات مساعدة للشبكة =====
def expand_url(url: str) -> str:
    """توسيع الروابط المختصرة (pin.it) وجلب canonical/og:url لو توفر."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        final_url = r.url or url
        if "/pin/" in final_url:
            return final_url

        soup = BeautifulSoup(r.text, "html.parser")
        can = soup.find("link", rel="canonical")
        if can and "/pin/" in (can.get("href") or ""):
            return can["href"]
        og = soup.find("meta", property="og:url")
        if og and "/pin/" in (og.get("content") or ""):
            return og["content"]
        return final_url
    except Exception:
        return url


def get_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


# ===== انتقاء أفضل روابط الصورة/الفيديو =====
def _pick_best_image(images: dict) -> Optional[str]:
    if not isinstance(images, dict):
        return None
    if "orig" in images and isinstance(images["orig"], dict):
        u = images["orig"].get("url")
        if u:
            return u
    best_u, best_area = None, -1
    for v in images.values():
        if isinstance(v, dict):
            u = v.get("url")
            h = v.get("height", 0) or 0
            w = v.get("width", 0) or 0
            area = (h * w) if (h and w) else 0
            if u and area >= best_area:
                best_area, best_u = area, u
    return best_u


def _pick_best_video(video_list: dict) -> Optional[str]:
    if not isinstance(video_list, dict):
        return None
    order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for q in order:
        if q in video_list and isinstance(video_list[q], dict):
            u = video_list[q].get("url")
            if u:
                return u
    for v in video_list.values():
        if isinstance(v, dict):
            u = v.get("url")
            if u:
                return u
    return None


# ===== مصادر الاستخراج (بدون API رسمي) =====
def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None


def try_pidgets(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Widgets/pidgets قديم لكنه غالباً مفيد: يعيد فيديو أو صورة."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None
    try:
        r = requests.get(
            "https://widgets.pinterest.com/v3/pidgets/pins/info/",
            params={"pin_ids": pid},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None, None
        data = r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins:
            return None, None
        pin = pins[0]

        # فيديو؟
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = _pick_best_video(vlist)
        if vurl:
            return vurl, "video"

        # صورة؟
        img_url = _pick_best_image(pin.get("images") or {})
        if img_url:
            return img_url, "image"
    except Exception:
        pass
    return None, None


def try_oembed(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """oEmbed العام: غالباً يعطي thumbnail للصورة."""
    try:
        r = requests.get(
            "https://www.pinterest.com/oembed.json",
            params={"url": pin_url},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None, None
        data = r.json()
        thumb = data.get("thumbnail_url")
        if thumb:
            return thumb, "image"
    except Exception:
        pass
    return None, None


def try_meta_fallback(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """نقرأ og:video / og:image مباشرة من صفحة Pin."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url") \
             or soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"], "video"
        mi = soup.find("meta", property="og:image")
        if mi and mi.get("content"):
            return mi["content"], "image"
    except Exception:
        pass
    return None, None


def extract_pinterest_media(pin_url: str) -> Tuple[str, str]:
    """يرجع (media_url, media_type) حيث media_type = image|video."""
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)

    # 1) pidgets (أفضل خيار غالباً)
    u, t = try_pidgets(url)
    if u:
        return u, t

    # 2) oEmbed كـ fallback سريع (صور فقط غالباً)
    u, t = try_oembed(url)
    if u:
        return u, t

    # 3) ميتا تاجز من الصفحة
    u, t = try_meta_fallback(url)
    if u:
        return u, t

    raise ValueError("لم أعثر على صورة أو فيديو في هذا الرابط. قد يكون خاص/Protected.")


# ===== أدوات الإرسال والحجم =====
def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False


async def send_media(update: Update, media_url: str, media_type: str, filename_hint: str = ""):
    # نحمّل المحتوى أولاً لنتحكم بالحجم ونوع الإرسال
    with requests.get(media_url, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        content = r.content

    cap = "تم التحميل ✅"
    if media_type == "video":
        # حدود تيليجرام تختلف (قد تسمح حتى ~50-200MB حسب النوع)، نستخدم 45MB كحد آمن للفيديو
        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=content,
                filename=filename_hint or "pinterest_video.mp4",
                caption=cap + " (أرسلته كمستند بسبب الحجم)"
            )
        else:
            await update.message.reply_video(
                video=content,
                filename=filename_hint or "pinterest_video.mp4",
                caption=cap
            )
    else:
        # للصورة نرسل Photo لو <= 9MB وإلا Document
        if len(content) > 9 * 1024 * 1024:
            await update.message.reply_document(
                document=content,
                filename=filename_hint or "pinterest_image.jpg",
                caption=cap + " (أرسلته كمستند بسبب الحجم)"
            )
        else:
            await update.message.reply_photo(
                photo=content,
                caption=cap
            )


# ===== أوامر البوت =====
HELP_TEXT = (
    "أرسل رابط Pin من Pinterest وسأحمّله لك (فيديو أو صورة) — بدون أي API.\n\n"
    "مثال:\n"
    "https://www.pinterest.com/pin/123456789/\n\n"
    "الأوامر:\n"
    "/start — ترحيب وطريقة الاستخدام\n"
    "/help — هذه المساعدة"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً 👋\n" + HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return

    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("يبدو أن الرابط ليس من Pinterest. أرسل رابط Pin صحيح.")
        return

    status = await update.message.reply_text("⏳ جارِ استخراج الوسائط...")
    try:
        media_url, media_type = extract_pinterest_media(url)
        log.info("Found media: %s (%s)", media_url, media_type)
        # اسم بسيط للملف
        fname = os.path.basename(urlparse(media_url).path)
        if media_type == "video" and not fname.endswith(".mp4"):
            fname += ".mp4"
        elif media_type == "image" and not re.search(r"\.(jpg|jpeg|png|webp)$", fname, re.I):
            fname += ".jpg"

        await send_media(update, media_url, media_type, filename_hint=fname)
        await status.delete()
    except Exception as e:
        log.exception("Processing failed")
        await status.edit_text(
            f"تعذر التحميل: {e}\n"
            "تأكد أن الرابط عام (وليس من داخل تطبيق/حساب خاص)."
        )


def main():
    if not BOT_TOKEN:
        raise SystemExit("الرجاء ضبط متغير البيئة BOT_TOKEN بقيمة توكن البوت.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
