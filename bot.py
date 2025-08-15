# -*- coding: utf-8 -*-
import os, re, json, logging, time
from typing import Optional, Tuple, Any, Dict

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ----------------- Logging -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-video-bot")

# ----------------- Constants -----------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS: Dict[str, str] = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
HTTP_TIMEOUT = 25
PIN_HOSTS = (
    "pinterest.com", "www.pinterest.com",
    "in.pinterest.com", "www.pinterest.co.uk",
    "pin.it"
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ----------------- HTTP helpers -----------------
def expand_url(url: str) -> str:
    """
    يتعامل مع روابط pin.it المختصرة ويستخرج رابط الـ Pin النهائي.
    يحاول أيضاً استخدام canonical/og:url عند الحاجة.
    """
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


# ----------------- Deep search helpers -----------------
def _find_in_dict(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    """بحث عميق في تراكيب JSON عن أول مفتاح مطابق من keys."""
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    return v
                found = _find_in_dict(v, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _find_in_dict(item, keys)
                if found is not None:
                    return found
    except Exception:
        pass
    return None


def _pick_best_video(video_list: dict) -> Optional[str]:
    """اختيار أفضل جودة للفيديو إن وجدت."""
    if not isinstance(video_list, dict):
        return None

    # أحياناً تأتي البنية بالشكل {"videos": {"video_list": {...}}}
    if "video_list" in video_list and isinstance(video_list["video_list"], dict):
        video_list = video_list["video_list"]

    quality_order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for key in quality_order:
        if key in video_list and isinstance(video_list[key], dict):
            u = video_list[key].get("url")
            if u:
                return u

    # أي أول URL
    for val in video_list.values():
        if isinstance(val, dict):
            u = val.get("url")
            if u:
                return u
    return None


def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None


# ----------------- Extract video only -----------------
def try_pidgets(pin_url: str) -> Optional[str]:
    """API قديم (widgets.pinterest) عادة يكفي للفيديوهات العامة."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None
    try:
        r = requests.get(
            "https://widgets.pinterest.com/v3/pidgets/pins/info/",
            params={"pin_ids": pid},
            headers=HEADERS, timeout=HTTP_TIMEOUT
        )
        if r.status_code != 200:
            return None
        data = r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins:
            return None
        pin = pins[0]
        vurl = _pick_best_video((pin.get("videos") or {}))
        return vurl
    except Exception as e:
        log.warning("pidgets failed: %s", e)
        return None


def try_page_json(pin_url: str) -> Optional[str]:
    """
    يحاول قراءة سكربت __PWS_DATA__ أو initialReduxState ثم يبحث عن video_list.
    """
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__PWS_DATA__")
        if not script or not script.string:
            # بدائل أخرى
            for sc in soup.find_all("script"):
                if sc.string and ("initialReduxState" in sc.string or "__PWS_DATA__" in sc.string):
                    script = sc
                    break

        if not script or not script.string:
            return None

        text = script.string.strip()
        # نظّف أي نص قبل '{' وأي ';' في النهاية
        text = re.sub(r"^[^{]*", "", text)
        text = re.sub(r";?\s*$", "", text)

        data = json.loads(text)

        redux = data
        for key in ("props", "initialReduxState"):
            if isinstance(redux, dict) and key in redux:
                redux = redux[key]

        video_list = _find_in_dict(redux, ("video_list", "videos"))
        if not video_list:
            # بعض الأحيان ضمن resourceResponses
            rr = _find_in_dict(data, ("resourceResponses",))
            if rr:
                video_list = _find_in_dict(rr, ("video_list", "videos"))

        if video_list:
            return _pick_best_video(video_list)
    except Exception as e:
        log.warning("page json parse failed: %s", e)
    return None


def try_meta_video(pin_url: str) -> Optional[str]:
    """كحل أخير: og:video / twitter:player:stream."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        mv = (soup.find("meta", property="og:video") or
              soup.find("meta", property="og:video:url") or
              soup.find("meta", property="twitter:player:stream"))
        if mv and mv.get("content"):
            return mv["content"]
    except Exception as e:
        log.warning("meta fallback failed: %s", e)
    return None


def extract_video_url(pin_url: str) -> str:
    """
    يُرجع رابط فيديو MP4 مباشر إن وُجد،
    وإلا يرفع ValueError.
    """
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)

    # 1) pidgets
    v = try_pidgets(url)
    if v:
        return v

    # 2) JSON داخل الصفحة
    v = try_page_json(url)
    if v:
        return v

    # 3) meta tags
    v = try_meta_video(url)
    if v:
        return v

    raise ValueError("No video found on this Pin (it might be image-only or private).")


# ----------------- Bot text -----------------
HELP_TEXT = (
    "Send me a public **Pinterest Pin** link and I’ll fetch the **video** for you.\n\n"
    "• Example:\n"
    "  https://www.pinterest.com/pin/123456789/\n\n"
    "Notes:\n"
    "• Videos only (images are ignored).\n"
    "• Private/blocked Pins aren’t supported.\n\n"
    "Developed by @Ghostnosd — optimized for reliability on Pinterest.\n"
)

AR_INTRO = (
    "مرحباً 👋\n"
    "أنا بوت تحميل **فيديوهات** Pinterest فقط (لا يدعم الصور).\n\n"
    "أرسل رابط Pin عام وسأحاول تنزيل الفيديو بأفضل جودة متاحة.\n"
    "ملاحظة: الروابط الخاصة/المحمية غير مدعومة.\n\n"
    "تم التطوير بواسطة @Ghostnosd.\n"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = AR_INTRO + "\n———\n" + HELP_TEXT
    await update.message.reply_text(text, disable_web_page_preview=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, disable_web_page_preview=True)


def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False


def suggest_video_name(media_url: str) -> str:
    name = os.path.basename(urlparse(media_url).path) or f"pinterest_{int(time.time())}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.message.text or "").strip()
    if not msg:
        return

    m = re.search(r"(https?://\S+)", msg)
    if not m:
        await update.message.reply_text("أرسل رابط Pin من Pinterest يحتوي فيديو.")
        return

    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("هذا لا يبدو رابط Pin صحيح من Pinterest.")
        return

    status = await update.message.reply_text("⏳ Processing…")
    try:
        vurl = extract_video_url(url)
        log.info("Video URL: %s", vurl)

        with requests.get(vurl, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            content = r.content

        fname = suggest_video_name(vurl)

        # لو الحجم كبير جداً، أرسل كمستند
        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=content,
                filename=fname,
                caption="✅ Downloaded (sent as document due to size)"
            )
        else:
            await update.message.reply_video(
                video=content,
                filename=fname,
                caption="✅ Downloaded"
            )

        await status.delete()
    except Exception as e:
        log.exception("Video download failed")
        await status.edit_text(
            f"Failed: {e}\n"
            "Make sure the Pin is public and contains a video."
        )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started (videos only).")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
