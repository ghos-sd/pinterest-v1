# -*- coding: utf-8 -*-
"""
Pinterest Downloader Bot
- يحمّل فيديوهات Pinterest أولاً (إن وُجدت)، وي fallback لصورة عالية الجودة.
- يعمل مع python-telegram-bot v20+ و aiohttp.
- عيّن متغيّر البيئة BOT_TOKEN قبل التشغيل.

Developed by @Ghostnosd
"""
import os
import re
import json
import tempfile
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# -------------------- إعدادات عامة --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("pinterest-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)

PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "pin.it",
    "ar.pinterest.com", "in.pinterest.com", "de.pinterest.com",
    "www.pinterest.co.uk", "www.pinterest.de", "www.pinterest.es",
}

WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** if available, "
    "or a high-quality image as a fallback.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd"
)

# -------------------- أدوات HTTP --------------------
async def http_get_text(session: aiohttp.ClientSession, url: str, **kw) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        log.debug("GET text failed %s: %s", url, e)
        return None

async def http_get_json(session: aiohttp.ClientSession, url: str, **kw) -> Optional[dict]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except Exception as e:
        log.debug("GET json failed %s: %s", url, e)
        return None

async def expand_url(session: aiohttp.ClientSession, url: str) -> str:
    """
    يتبع التحويلات، ويحاول التقاط canonical/og:url للوصول لصفحة /pin/… الأصلية.
    """
    try:
        async with session.get(url, headers=HEADERS, allow_redirects=True, timeout=HTTP_TIMEOUT) as r:
            final_url = str(r.url)
            text = await r.text()
        if "/pin/" in final_url:
            return final_url
        soup = BeautifulSoup(text, "html.parser")
        can = soup.find("link", rel="canonical")
        if can and "/pin/" in (can.get("href") or ""):
            return can["href"]
        og = soup.find("meta", property="og:url")
        if og and "/pin/" in (og.get("content") or ""):
            return og["content"]
        return final_url
    except Exception:
        return url

def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

# -------------------- انتقاء أفضل جودة --------------------
def pick_best_video(vlist: dict) -> Optional[str]:
    if not isinstance(vlist, dict):
        return None
    order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for q in order:
        d = vlist.get(q)
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    for d in vlist.values():
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    return None

def pick_best_image(images: dict) -> Optional[str]:
    if not isinstance(images, dict):
        return None
    if isinstance(images.get("orig"), dict) and images["orig"].get("url"):
        return images["orig"]["url"]
    best_u, best_area = None, -1
    for d in images.values():
        if isinstance(d, dict) and d.get("url"):
            w, h = int(d.get("width") or 0), int(d.get("height") or 0)
            area = w * h
            if area >= best_area:
                best_area, best_u = area, d["url"]
    return best_u

# -------------------- طرق الاستخراج (الفيديو أولًا) --------------------
async def try_pidgets(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    واجهة widgets.pinterest (عامة) — غالبًا ترجع video_list أو images.
    """
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None

    data = await http_get_json(
        session,
        "https://widgets.pinterest.com/v3/pidgets/pins/info/",
        params={"pin_ids": pid},
    )
    if not data:
        return None, None

    pins = ((data.get("data") or {}).get("pins") or [])
    if not pins:
        return None, None

    pin = pins[0]

    # فيديو أولاً
    vlist = ((pin.get("videos") or {}).get("video_list")) or {}
    vurl = pick_best_video(vlist)
    if vurl:
        return vurl, "video"

    # صورة fallback
    img_url = pick_best_image(pin.get("images") or {})
    if img_url:
        return img_url, "image"

    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يقرأ __PWS_DATA__ / initialReduxState و resourceResponses من الصفحة نفسها،
    ثم يحاول إيجاد video_list أو images.
    """
    html_text = await http_get_text(session, pin_url)
    if not html_text:
        return None, None

    soup = BeautifulSoup(html_text, "html.parser")
    sc = soup.find("script", id="__PWS_DATA__")
    if not (sc and sc.string):
        for s in soup.find_all("script"):
            if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                sc = s
                break

    if sc and sc.string:
        try:
            txt = re.sub(r"^[^{]*", "", sc.string.strip())
            txt = re.sub(r";?\s*$", "", txt)
            data = json.loads(txt)
        except Exception:
            data = None

        def deep_find(obj, keys):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in keys:
                        return v
                    got = deep_find(v, keys)
                    if got is not None:
                        return got
            elif isinstance(obj, list):
                for it in obj:
                    got = deep_find(it, keys)
                    if got is not None:
                        return got
            return None

        if isinstance(data, dict):
            vlist = deep_find(data, ("video_list",))
            if isinstance(vlist, dict):
                vurl = pick_best_video(vlist)
                if vurl:
                    return vurl, "video"

            images = deep_find(data, ("images",))
            if isinstance(images, dict):
                iurl = pick_best_image(images)
                if iurl:
                    return iurl, "image"

    # Meta tags fallback
    mv = soup.find("meta", property="og:video") or \
         soup.find("meta", property="og:video:url") or \
         soup.find("meta", property="twitter:player:stream")
    if mv and mv.get("content"):
        return mv["content"], "video"

    mi = soup.find("meta", property="og:image")
    if mi and mi.get("content"):
        return mi["content"], "image"

    # Sweep لأي mp4 من pinimg
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
    if m:
        return m.group(0), "video"

    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يحاول استخراج (media_url, media_type) مع تفضيل الفيديو.
    """
    # تأكّد إن الرابط لـ Pinterest
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    # 1) pidgets أولًا (أقوى للفيديوهات)
    media_url, media_type = await try_pidgets(session, url)
    if media_url:
        return media_url, media_type

    # 2) تحليل الصفحة الحديثة
    media_url, media_type = await try_parse_page(session, url)
    if media_url:
        return media_url, media_type

    return None, None

# -------------------- تنزيل وإرسال --------------------
async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    """
    يحفظ المحتوى في ملف مؤقّت ويعيد المسار.
    """
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        # استنتج الامتداد
        low = url.lower()
        if low.endswith(".mp4"):
            suf = ".mp4"
        elif any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp")):
            # Pinterest قد يرجّع webp
            suf = ".jpg" if ".jpg" in low or ".jpeg" in low else (".png" if ".png" in low else ".webp")
        else:
            # حدّس حسب نوع المحتوى
            ct = r.headers.get("Content-Type", "").lower()
            if "mp4" in ct:
                suf = ".mp4"
            elif "png" in ct:
                suf = ".png"
            elif "webp" in ct:
                suf = ".webp"
            else:
                suf = ".jpg"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suf) as f:
            async for chunk in r.content.iter_chunked(1024 * 64):
                f.write(chunk)
            return f.name

# -------------------- Telegram Handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return

    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return
    url = m.group(0)

    # حالة الكتابة/الرفع
    await update.effective_chat.send_action(ChatAction.TYPING)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, media_type = await extract_media(session, url)

        if not media_url:
            await update.message.reply_text("Failed: No video/image found on this Pin (or it is private).")
            return

        try:
            await update.effective_chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_PHOTO
            )
            temp_path = await download_to_temp(session, media_url)

            if media_type == "video":
                await update.message.reply_video(video=InputFile(temp_path), caption="Downloaded ✅")
            else:
                await update.message.reply_photo(photo=InputFile(temp_path), caption="Downloaded ✅ (image)")
        except Exception as e:
            log.exception("Send failed")
            await update.message.reply_text(f"Download failed: {e}")
        finally:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

# -------------------- تشغيل البوت --------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Pinterest bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
