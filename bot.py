# -*- coding: utf-8 -*-
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

# ================== إعدادات عامة ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("pinterest-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)

PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "in.pinterest.com", "ar.pinterest.com",
    "de.pinterest.com", "www.pinterest.co.uk", "pin.it"
}

WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** (if public), "
    "or a high-quality image as a fallback.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd"
)

# ================== أدوات HTTP ==================
async def http_text(session: aiohttp.ClientSession, url: str, **kw) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        log.debug("GET text fail %s: %s", url, e)
        return None

async def expand_url(session: aiohttp.ClientSession, url: str) -> str:
    """اتّبع التحويلات وحاول استخراج canonical/og:url للوصول إلى صفحة /pin/..."""
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

# ================== انتقاء الجودة ==================
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
    best_url, best_area = None, -1
    for d in images.values():
        if isinstance(d, dict) and d.get("url"):
            w, h = int(d.get("width") or 0), int(d.get("height") or 0)
            area = w * h
            if area >= best_area:
                best_area, best_url = area, d["url"]
    return best_url

# ================== طرق الاستخراج ==================
async def try_pidgets(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Widgets pidgets (عام لمعظم الـ Pins): يرجع فيديو أو صورة."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None
    try:
        api = "https://widgets.pinterest.com/v3/pidgets/pins/info/"
        async with session.get(api, params={"pin_ids": pid}, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return None, None
            data = await r.json()

        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins:
            return None, None
        p = pins[0]

        vlist = ((p.get("videos") or {}).get("video_list")) or {}
        vurl = pick_best_video(vlist)
        if vurl:
            return vurl, "video"

        iurl = pick_best_image(p.get("images") or {})
        if iurl:
            return iurl, "image"
    except Exception as e:
        log.debug("pidgets failed: %s", e)
    return None, None

async def try_pinresource(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    واجهة Pinterest الداخلية: /resource/PinResource/get/
    غالبًا ترجع videos.images بوضوح.
    """
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None

    try:
        api = "https://www.pinterest.com/resource/PinResource/get/"
        params = {
            "data": json.dumps(
                {"options": {"id": pid}, "context": {}},
                separators=(",", ":")
            )
        }
        async with session.get(api, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return None, None
            data = await r.json()

        res = (data.get("resource_response") or {}).get("data") or data.get("data") or {}
        if not isinstance(res, dict):
            return None, None

        vlist = ((res.get("videos") or {}).get("video_list")) or {}
        vurl = pick_best_video(vlist)
        if vurl:
            return vurl, "video"

        iurl = pick_best_image(res.get("images") or {})
        if iurl:
            return iurl, "image"
    except Exception as e:
        log.debug("PinResource failed: %s", e)
    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """قراءة صفحة الـ Pin: __PWS_DATA__/Redux + meta/regex."""
    try:
        html_text = await http_text(session, pin_url)
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
            txt = re.sub(r"^[^{]*", "", sc.string.strip())
            txt = re.sub(r";?\s*$", "", txt)
            data = json.loads(txt)

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

        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"], "video"

        mi = soup.find("meta", property="og:image")
        if mi and mi.get("content"):
            return mi["content"], "image"

        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
        if m:
            return m.group(0), "video"
    except Exception as e:
        log.debug("parse page failed: %s", e)
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """إرجاع (media_url, media_type) مع تفضيل الفيديو."""
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    # 1) pidgets
    u, t = await try_pidgets(session, url)
    if u:
        return u, t

    # 2) PinResource (الجديد والقوي)
    u, t = await try_pinresource(session, url)
    if u:
        return u, t

    # 3) تحليل الصفحة
    u, t = await try_parse_page(session, url)
    if u:
        return u, t

    return None, None

# ================== تنزيل وإرسال ==================
async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        low = url.lower()
        ext = ".mp4" if low.endswith(".mp4") else (".jpg" if (".jpg" in low or ".jpeg" in low) else ".bin")
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(64 * 1024):
                f.write(chunk)
            return f.name

# ================== Telegram ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return
    url = m.group(0)

    await update.message.chat.send_action(ChatAction.TYPING)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, media_type = await extract_media(session, url)

        if not media_url:
            await update.message.reply_text(
                "Failed: No public video found on this Pin. "
                "If it’s image-only, private, or an Idea Pin without mp4, I’ll try to send an image instead."
            )
            return

        path = None
        try:
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_PHOTO
            )
            path = await download_to_temp(session, media_url)

            if media_type == "video":
                await update.message.reply_video(video=InputFile(path), caption="Downloaded ✅")
            else:
                await update.message.reply_photo(photo=InputFile(path), caption="Downloaded ✅ (image)")
        except Exception as e:
            log.exception("Send failed")
            await update.message.reply_text(f"Download failed: {e}")
        finally:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

# ================== تشغيل البوت ==================
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Pinterest bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()        async with session.get(api, params={"pin_ids": pid}, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins:
            return None, None
        pin = pins[0]
        vlist = ((pin.get("videos") or {}).get("video_list")) or {}
        vurl = _pick_best_video(vlist)
        if vurl:
            return vurl, "video"
        img_url = _pick_best_image(pin.get("images") or {})
        if img_url:
            return img_url, "image"
    except Exception:
        pass
    return None, None

async def _try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يفكك JSON داخل الصفحة (__PWS_DATA__/Redux) + meta tags + sweep mp4."""
    html_text = await _get_text(session, pin_url)
    if not html_text:
        return None, None
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not (sc and sc.string):
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s
                    break
        if sc and sc.string:
            txt = sc.string.strip()
            # قص أي مقدمة غير JSON
            i = txt.find("{")
            if i > 0:
                txt = txt[i:]
            if txt.endswith(";"):
                txt = txt[:-1]
            data = json.loads(txt)

            def deep_find(o, keys):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in keys:
                            return v
                        got = deep_find(v, keys)
                        if got is not None:
                            return got
                elif isinstance(o, list):
                    for it in o:
                        got = deep_find(it, keys)
                        if got is not None:
                            return got
                return None

            vlist = deep_find(data, ("video_list",))
            if isinstance(vlist, dict):
                vurl = _pick_best_video(vlist)
                if vurl:
                    return vurl, "video"

            images = deep_find(data, ("images",))
            if isinstance(images, dict):
                iurl = _pick_best_image(images)
                if iurl:
                    return iurl, "image"

        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"], "video"

        mi = soup.find("meta", property="og:image")
        if mi and mi.get("content"):
            return mi["content"], "image"

        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
        if m:
            return m.group(0), "video"

    except Exception as e:
        log.debug("parse page fail: %s", e)
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يعيد (media_url, type) مع تفضيل الفيديو."""
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await _expand_url(session, original_url)

    u1, t1 = await _try_pidgets(session, url)
    if u1:
        return u1, t1

    u2, t2 = await _try_parse_page(session, url)
    if u2:
        return u2, t2

    return None, None

# ----------------- تنزيل ملف مؤقت -----------------
async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        low = url.lower()
        ext = ".mp4" if low.endswith(".mp4") else (".jpg" if (".jpg" in low or ".jpeg" in low) else "")
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(1024 * 64):
                f.write(chunk)
            return f.name

# ----------------- Telegram -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

def _first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    url = _first_url(text)
    if not url:
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, media_type = await extract_media(session, url)

        if not media_url:
            await update.message.reply_text("Failed: No video/image found on this Pin (or it is private).")
            return

        try:
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_PHOTO
            )
            path = await download_to_temp(session, media_url)
            if media_type == "video":
                await update.message.reply_video(video=InputFile(path), caption="Downloaded ✅")
            else:
                await update.message.reply_photo(photo=InputFile(path), caption="Downloaded ✅ (image)")
        finally:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Pinterest bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
