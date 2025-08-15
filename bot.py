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

# ------------- إعدادات عامة -------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("pinterest-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)

PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "in.pinterest.com",
    "www.pinterest.co.uk", "ar.pinterest.com", "de.pinterest.com",
    "pin.it"
}

WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** (if available), "
    "or a high-quality image as a fallback.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd"
)

# ------------- أدوات مساعدة -------------
async def http_text(session: aiohttp.ClientSession, url: str, **kw) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        log.debug("GET text fail %s: %s", url, e)
        return None

async def http_bytes_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        ext = ".mp4" if url.lower().endswith(".mp4") else (
            ".jpg" if any(x in url.lower() for x in (".jpg", ".jpeg")) else ""
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(1024 * 64):
                f.write(chunk)
            return f.name

async def expand_pin_url(session: aiohttp.ClientSession, url: str) -> str:
    """اتبع التحويلات + استخدم canonical/og:url عشان نوصل لصفحة /pin/…"""
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
            try:
                w, h = int(d.get("width") or 0), int(d.get("height") or 0)
                area = w * h
            except Exception:
                area = 0
            if area >= best_area:
                best_area, best_u = area, d["url"]
    return best_u

# ------------- استخراج (فيديو أولًا) -------------
async def try_pidgets(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """API عام غير موثّق لكنه ينفع لمعظم الـ Pins العامة."""
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

        pin = pins[0]

        # فيديو
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = pick_best_video(vlist)
        if vurl:
            return vurl, "video"

        # صورة
        img_url = pick_best_image(pin.get("images") or {})
        if img_url:
            return img_url, "image"

    except Exception:
        pass
    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """نقرأ __PWS_DATA__/Redux + resourceResponses + meta tags."""
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
            try:
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

            if data:
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

        # Regex لأي mp4 من pinimg
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
        if m:
            return m.group(0), "video"

    except Exception:
        pass
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يرجع (media_url, type) مع تفضيل الفيديو دائماً."""
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    pin_url = await expand_pin_url(session, original_url)

    # 1) pidgets (أدق للفيديو)
    u, t = await try_pidgets(session, pin_url)
    if u:
        return u, t

    # 2) تحليل الصفحة
    u, t = await try_parse_page(session, pin_url)
    if u:
        return u, t

    return None, None

# ------------- Telegram -------------
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
            await update.message.reply_text("Failed: No video/image found on this Pin (or it is private).")
            return

        try:
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_PHOTO
            )
            path = await http_bytes_to_temp(session, media_url)

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
    main()        if not pins:
            return None, None
        pin = pins[0]
        # VIDEO
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = pick_best_video(vlist)
        if vurl:
            return vurl, "video"
        # IMAGE fallback
        img_url = pick_best_image(pin.get("images") or {})
        if img_url:
            return img_url, "image"
    except Exception:
        pass
    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """نقرأ __PWS_DATA__/Redux + resourceResponses داخل الصفحة."""
    try:
        html_text = await get(session, pin_url)
        soup = BeautifulSoup(html_text, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not (sc and sc.string):
            # أحيانًا السكربت بدون id واضح
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s
                    break
        if sc and sc.string:
            # قص أول حرف حتى بداية JSON
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

            # أولوية: فيديو
            vlist = deep_find(data, ("video_list",))
            if isinstance(vlist, dict):
                vurl = pick_best_video(vlist)
                if vurl:
                    return vurl, "video"

            # صورة
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

        # أخيرًا: sweeping لأي mp4 من pinimg
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
        if m:
            return m.group(0), "video"
    except Exception:
        pass
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يحاول استخراج (url, type) مع تفضيل الفيديو."""
    # تحقق الدومين
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    # 1) pidgets أولًا (أدق للفيديوهات)
    u, t = await try_pidgets(session, url)
    if u:
        return u, t

    # 2) تحليل الصفحة الحديثة
    u, t = await try_parse_page(session, url)
    if u:
        return u, t

    return None, None

# ------------- تنزيل وإرسال لتيليجرام -------------
async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    """يحفظ المحتوى في ملف مؤقت ويعيد مساره."""
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        # استنتج الامتداد
        ext = ".mp4" if url.lower().endswith(".mp4") else (".jpg" if ".jpg" in url.lower() or ".jpeg" in url.lower() else "")
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(1024 * 64):
                f.write(chunk)
            return f.name

# ------------- Telegram -------------
WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** (if available), "
    "or a high-quality image as a fallback.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    # سريع: التقط أول URL في الرسالة
    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return
    url = m.group(0)

    await update.message.chat.send_action(ChatAction.TYPING)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, media_type = await extract_media(session, url)

        if not media_url:
            await update.message.reply_text("Failed: No video/image found on this Pin (or it is private).")
            return

        # نزّل ثم أرسل — الفيديو أولًا
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
            # نظّف الملف المؤقت
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

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
    main()            if r.status != 200:
                return None, None
            data = await r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins: return None, None
        pin = pins[0]
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = _pick_best_video(vlist)
        if vurl: return vurl, "video"
        img = _pick_best_image(pin.get("images") or {})
        if img: return img, "image"
    except Exception as e:
        log.debug("pidgets fail: %s", e)
    return None, None

async def try_page_json(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يفحص __PWS_DATA__ و resourceResponses عن video_list ثم images."""
    html_text = await fetch_text(session, pin_url)
    if not html_text: return None, None
    soup = BeautifulSoup(html_text, "html.parser")
    sc = soup.find("script", id="__PWS_DATA__")
    if not (sc and sc.string):
        # fallback: أي سكربت فيه initialReduxState
        for s in soup.find_all("script"):
            if s.string and "initialReduxState" in s.string:
                sc = s; break
    if not (sc and sc.string):
        return None, None
    try:
        import json
        txt = re.sub(r"^[^{]*", "", sc.string.strip())
        txt = re.sub(r";?\s*$", "", txt)
        data = json.loads(txt)

        def deep_find(o, keys):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in keys: return v
                    f = deep_find(v, keys)
                    if f is not None: return f
            elif isinstance(o, list):
                for it in o:
                    f = deep_find(it, keys)
                    if f is not None: return f
            return None

        # أولاً الفيديو
        vlist = deep_find(data, ("video_list",))
        if isinstance(vlist, dict):
            vurl = _pick_best_video(vlist)
            if vurl: return vurl, "video"

        # ثانياً الصور
        imgs = deep_find(data, ("images","image_signature"))
        if isinstance(imgs, dict):
            img = _pick_best_image(imgs)
            if img: return img, "image"
    except Exception as e:
        log.debug("page json parse fail: %s", e)
    return None, None

async def try_meta_regex(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """أخيراً: og:video/og:image ثم sweeping mp4."""
    html_text = await fetch_text(session, pin_url)
    if not html_text: return None, None
    soup = BeautifulSoup(html_text, "html.parser")
    mv = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url") \
         or soup.find("meta", property="twitter:player:stream")
    if mv and mv.get("content"):
        return mv["content"], "video"
    mi = soup.find("meta", property="og:image")
    if mi and mi.get("content"):
        return mi["content"], "image"
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
    if m: return m.group(0), "video"
    return None, None

async def extract_media(session: aiohttp.ClientSession, url: str) -> Tuple[Optional[str], Optional[str]]:
    """يرجع (media_url, media_type). الفيديو أولاً ثم الصورة كبديل."""
    # وسّع pin.it وأي shorteners
    url = await expand_url(session, url)

    # 1) pidgets
    u, t = await try_pidgets(session, url)
    if t == "video":
        return u, t
    video_candidate = u if t == "video" else None
    image_candidate = u if t == "image" else None

    # 2) page json
    u2, t2 = await try_page_json(session, url)
    if t2 == "video":
        return u2, t2
    if not image_candidate and t2 == "image":
        image_candidate = u2

    # 3) meta/regex
    u3, t3 = await try_meta_regex(session, url)
    if t3 == "video":
        return u3, t3
    if not image_candidate and t3 == "image":
        image_candidate = u3

    # لو ما لقينا فيديو أبداً، استخدم الصورة (إن وُجدت)
    if image_candidate:
        return image_candidate, "image"

    # آخر فرصة: لو كان فيه video_candidate من خطوة 1
    if video_candidate:
        return video_candidate, "video"

    return None, None

def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False

def sug_filename(media_url: str, media_type: str) -> str:
    import os
    from urllib.parse import urlparse
    name = os.path.basename(urlparse(media_url).path)
    if not name or "." not in name:
        name = ("pin_video.mp4" if media_type == "video" else "pin_image.jpg")
    return name

# ----------------- Telegram -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt:
        return
    # التقط أول رابط شبيه Pinterest من الرسالة
    m = re.search(r"https?://\S+", txt)
    if not (m and looks_like_pin(m.group(0))):
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return
    pin_url = m.group(0)

    progress = await update.message.reply_text("🔎 Fetching…")
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            media_url, media_type = await extract_media(session, pin_url)
            if not media_url:
                await progress.edit_text("❌ No media found on this Pin (maybe private).")
                return

            fname = sug_filename(media_url, media_type)
            await progress.edit_text("⬇️ Downloading…")
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type=="video" else ChatAction.UPLOAD_PHOTO
            )

            data = await fetch_bytes(session, media_url)
            if not data:
                await progress.edit_text("❌ Download failed.")
                return

            bio = io.BytesIO(data); bio.name = fname
            if media_type == "video":
                await update.message.reply_video(bio, caption="Downloaded ✅ (video)")
            else:
                await update.message.reply_photo(bio, caption="Downloaded ✅ (image)")

            await progress.delete()
        except Exception as e:
            log.exception("handle failed")
            await progress.edit_text(f"❌ Error: {html.escape(str(e))}")

# ----------------- تشغيل البوت -----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in environment.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Pinterest bot running… (video-first, image fallback)")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()        pin = pins[0]
        vurl = _pick_best_video(((pin.get("videos") or {}).get("video_list")) or {})
        if vurl: return vurl, "video"
        img = _pick_best_image(pin.get("images") or {})
        if img: return img, "image"
    except Exception:
        pass
    return None, None

def _parse_pws_json(html: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يحاول استخراج video_list أو images من سكربت __PWS_DATA__ أو أي JSON مشابه.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            # fallback: أي سكربت فيه initialReduxState
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s; break
        if not sc or not sc.string:
            return None, None
        txt = sc.string.strip()
        # نظّف أي نص زائد قبل JSON
        txt = re.sub(r"^[^{]*", "", txt)
        txt = re.sub(r";?\s*$", "", txt)
        data = json.loads(txt)

        def deep_find(o):
            # يدور على video_list أو images أينما كانت
            if isinstance(o, dict):
                if "video_list" in o: return ("video", o["video_list"])
                if "videos" in o and isinstance(o["videos"], dict):
                    vl = o["videos"].get("video_list") or o["videos"]
                    return ("video", vl)
                if "images" in o: return ("image", o["images"])
                for v in o.values():
                    r = deep_find(v)
                    if r: return r
            elif isinstance(o, list):
                for it in o:
                    r = deep_find(it)
                    if r: return r
            return None

        found = deep_find(data)
        if found:
            kind, payload = found
            if kind == "video":
                v = _pick_best_video(payload or {})
                if v: return v, "video"
            elif kind == "image":
                u = _pick_best_image(payload or {})
                if u: return u, "image"
    except Exception:
        pass
    return None, None

def _meta_or_regex(html: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        # فيديو من الميتا
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content") and mv["content"].endswith(".mp4"):
            return mv["content"], "video"
        # صورة من الميتا
        mi = soup.find("meta", property="og:image") or soup.find("meta", property="og:image:secure_url")
        if mi and mi.get("content"):
            return mi["content"], "image"
        # Regex مباشر
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, re.I)
        if m: return m.group(0), "video"
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp)", html, re.I)
        if m: return m.group(0), "image"
    except Exception:
        pass
    return None, None

def extract_media_sync(pin_url: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    يُرجع (media_url, media_type, debug_source)
    media_type إما "video" أو "image"
    """
    url = _expand_url(pin_url)
    pid = _pin_id(url)

    # 1) pidgets أولاً (أقوى طريقة للعام)
    if pid:
        u, t = _try_pidgets(pid)
        if u: return u, t, "pidgets"

    # 2) صفحة الـ pin: JSON داخلي
    html = _get_html(url)
    u, t = _parse_pws_json(html)
    if u: return u, t, "__PWS_DATA__"

    # 3) ميتا/Regex
    u, t = _meta_or_regex(html)
    if u: return u, t, "meta/regex"

    return None, None, "none"

# ================== Telegram Bot ==================
PIN_URL_RE = re.compile(r"https?://(?:www\.)?(?:pin\.it|[a-z]{0,3}\.?pinterest\.com)/[^\s]+", re.I)

async def fetch_head_content_type(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.head(url, timeout=20) as r:
            if r.status in (200, 206):
                return r.headers.get("Content-Type","")
    except Exception:
        pass
    # fallback GET صغير
    try:
        async with session.get(url, timeout=20) as r:
            if r.status in (200, 206):
                return r.headers.get("Content-Type","")
    except Exception:
        pass
    return None

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a Pinterest Pin URL and I’ll download it.\n"
        "• Priority: video first.\n"
        "• If no video is found, I’ll send the image.\n\n"
        "Developed by @Ghostnosd"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = PIN_URL_RE.search(text)
    if not m:
        return
    pin_url = m.group(0)
    status = await update.message.reply_text("⏳ Processing…")

    # شغّل الاستخراج المتزامن في Thread حتى لا يعلق loop
    media_url, media_type, source = await asyncio.to_thread(extract_media_sync, pin_url)

    if not media_url:
        await status.edit_text("❌ No media found on this Pin (maybe private or image-only).")
        return

    # تأكد من نوع الكونتنت (عشان ما يرسل الفيديو كصورة والعكس)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        ctype = await fetch_head_content_type(session, media_url) or ""
    log.info("Found %s from %s | ctype=%s | %s", media_type, source, ctype, media_url)

    try:
        if media_type == "video" or "video" in ctype.lower() or media_url.lower().endswith(".mp4"):
            # فيديو أولاً
            await update.message.reply_video(
                video=media_url,
                supports_streaming=True,
                caption="Downloaded ✅"
            )
            await status.delete()
            return
        # وإلا نرسل صورة
        await update.message.reply_photo(
            photo=media_url,
            caption="Downloaded ✅ (image)"
        )
        await status.delete()
    except Exception as e:
        log.exception("Send failed")
        await status.edit_text(f"Failed to send: {e}")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Pinterest bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
