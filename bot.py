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
    order = ["V_1080P","V_720P","V_640P","V_480P","V_360P","V_240P","V_EXP4"]
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


# ===== أدوات بحث داخل JSON =====
def _find_in(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    return v
                found = _find_in(v, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = _find_in(it, keys)
                if found is not None:
                    return found
    except Exception:
        pass
    return None


# ===== مصادر الاستخراج (بدون API رسمي) =====
def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None


def try_pidgets(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Widgets/pidgets: يعيد فيديو أو صورة."""
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

        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = _pick_best_video(vlist)
        if vurl:
            return vurl, "video"

        img_url = _pick_best_image(pin.get("images") or {})
        if img_url:
            return img_url, "image"
    except Exception:
        pass
    return None, None


def try_pws_json(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يقرأ JSON الداخلي من سكربت __PWS_DATA__/initialReduxState
    ويبحث عن video_list أو images.
    """
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            # أي سكربت تاني فيه initialReduxState
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s
                    break
        if not sc or not sc.string:
            return None, None

        text = sc.string.strip()
        text = re.sub(r"^[^{]*", "", text)     # قبل أول {
        text = re.sub(r";?\s*$", "", text)     # ; في النهاية
        data = json.loads(text)

        # احتمالات مواقع الداتا
        redux = data
        for key in ("props", "initialReduxState"):
            if isinstance(redux, dict) and key in redux:
                redux = redux[key]

        video_list = _find_in(redux, ("video_list","videos"))
        if isinstance(video_list, dict) and "video_list" not in video_list:
            video_list = video_list.get("video_list", video_list)
        images = _find_in(redux, ("images",))

        if not video_list and not images:
            rr = _find_in(data, ("resourceResponses",))
            if rr:
                video_list = _find_in(rr, ("video_list","videos"))
                images = images or _find_in(rr, ("images",))

        if video_list:
            vurl = _pick_best_video(video_list)
            if vurl:
                return vurl, "video"
        if images:
            iurl = _pick_best_image(images)
            if iurl:
                return iurl, "image"
    except Exception:
        pass
    return None, None


def try_oembed(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
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

    # ترتيب المحاولات: pidgets → JSON الداخلي → oembed → meta
    for fn in (try_pidgets, try_pws_json, try_oembed, try_meta_fallback):
        u, t = fn(url)
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


def sniff_type_by_head(url: str) -> Optional[str]:
    """يرجع 'video' لو Content-Type فيديو حتى لو الرابط شكله صورة."""
    try:
        h = requests.head(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        ct = (h.headers.get("Content-Type") or "").lower()
        if ct.startswith("video/"):
            return "video"
        if ct.startswith("image/"):
            return "image"
    except Exception:
        pass
    # fallback من الامتداد
    p = urlparse(url).path.lower()
    if p.endswith(".mp4") or p.endswith(".m3u8"):
        return "video"
    if any(p.endswith(ext) for ext in (".jpg",".jpeg",".png",".webp")):
        return "image"
    return None


async def send_media(update: Update, media_url: str, media_type: str, filename_hint: str = ""):
    # صحّح النوع إن كان الـ HEAD يقول فيديو
    sniff = sniff_type_by_head(media_url)
    if sniff == "video":
        media_type = "video"

    with requests.get(media_url, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        content = r.content

    cap = "تم التحميل ✅"
    if media_type == "video":
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
