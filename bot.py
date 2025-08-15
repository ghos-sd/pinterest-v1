# -*- coding: utf-8 -*-
import os, re, json, tempfile, logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# =============== Logging & Config ===============
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("pinterest-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)

PIN_HOSTS = {
    "pinterest.com","www.pinterest.com","in.pinterest.com","ar.pinterest.com",
    "de.pinterest.com","www.pinterest.co.uk","pin.it"
}

WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** (if public MP4), "
    "or a high-quality image as a fallback.\n\nDeveloped by @Ghostnosd"
)

# =============== Helpers: HTTP ===============
async def http_text(session: aiohttp.ClientSession, url: str, **kw) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        log.debug("GET text fail %s: %s", url, e)
        return None

async def http_json(session: aiohttp.ClientSession, url: str, **kw) -> Optional[dict]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, **kw) as r:
            r.raise_for_status()
            return await r.json()
    except Exception as e:
        log.debug("GET json fail %s: %s", url, e)
        return None

async def expand_url(session: aiohttp.ClientSession, url: str) -> str:
    """Follow redirects and try to resolve canonical /pin/... URL."""
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

# =============== Pickers ===============
def pick_best_video(vlist: dict) -> Optional[str]:
    if not isinstance(vlist, dict): return None
    order = ["V_720P","V_640P","V_480P","V_360P","V_240P","V_EXP4"]
    for q in order:
        d = vlist.get(q)
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    for d in vlist.values():
        if isinstance(d, dict) and d.get("url"): return d["url"]
    return None

def pick_best_image(images: dict) -> Optional[str]:
    if not isinstance(images, dict): return None
    if isinstance(images.get("orig"), dict) and images["orig"].get("url"):
        return images["orig"]["url"]
    best, area = None, -1
    for d in images.values():
        if isinstance(d, dict) and d.get("url"):
            w, h = int(d.get("width") or 0), int(d.get("height") or 0)
            a = w*h
            if a >= area:
                area, best = a, d["url"]
    return best

# =============== New/Updated Extractors ===============
async def try_pinresource(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Uses the internal PinResource with a detailed field_set.
    This is the most reliable when the pin is public.
    """
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None

    api = "https://www.pinterest.com/resource/PinResource/get/"
    payload = {
        "options": {
            "id": pid,
            "field_set_key": "detailed",  # important on newer responses
        },
        "context": {}
    }
    params = {"data": json.dumps(payload, separators=(",", ":"))}
    data = await http_json(session, api, params=params)
    if not data:
        return None, None

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

    return None, None

async def try_pidgets(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Public widgets API fallback."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None
    api = "https://widgets.pinterest.com/v3/pidgets/pins/info/"
    data = await http_json(session, api, params={"pin_ids": pid})
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
    return None, None

async def try_oembed(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extra safety net: oEmbed returns a thumbnail URL for many pins."""
    api = "https://widgets.pinterest.com/oembed.json"
    data = await http_json(session, api, params={"url": pin_url})
    if not data:
        return None, None
    thumb = data.get("thumbnail_url") or data.get("thumbnail_width")
    if isinstance(thumb, str):
        return thumb, "image"
    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse __PWS_DATA__ / meta tags / regex as a last resort."""
    html_text = await http_text(session, pin_url)
    if not html_text:
        return None, None
    soup = BeautifulSoup(html_text, "html.parser")

    sc = soup.find("script", id="__PWS_DATA__")
    if not (sc and sc.string):
        for s in soup.find_all("script"):
            if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                sc = s; break

    if sc and sc.string:
        try:
            txt = re.sub(r"^[^{]*", "", sc.string.strip())
            txt = re.sub(r";?\s*$", "", txt)
            data = json.loads(txt)

            def deep_find(o, keys):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in keys: return v
                        got = deep_find(v, keys)
                        if got is not None: return got
                elif isinstance(o, list):
                    for it in o:
                        got = deep_find(it, keys)
                        if got is not None: return got
                return None

            vlist = deep_find(data, ("video_list",))
            if isinstance(vlist, dict):
                vurl = pick_best_video(vlist)
                if vurl: return vurl, "video"

            images = deep_find(data, ("images",))
            if isinstance(images, dict):
                iurl = pick_best_image(images)
                if iurl: return iurl, "image"
        except Exception as e:
            log.debug("parse __PWS_DATA__ fail: %s", e)

    mv = soup.find("meta", property="og:video") or \
         soup.find("meta", property="og:video:url") or \
         soup.find("meta", property="twitter:player:stream")
    if mv and mv.get("content"):
        return mv["content"], "video"

    mi = soup.find("meta", property="og:image")
    if mi and mi.get("content"):
        return mi["content"], "image"

    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
    if m: return m.group(0), "video"
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp)", html_text, flags=re.I)
    if m: return m.group(0), "image"
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    Returns (direct_url, kind, pin_page_url). Prefers MP4 video. kind is 'video' or 'image'.
    """
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None, original_url
    except Exception:
        pass

    pin_page = await expand_url(session, original_url)

    # Order matters: most reliable first
    for fn in (try_pinresource, try_pidgets, try_parse_page, try_oembed):
        try:
            media_url, kind = await fn(session, pin_page)
            if media_url:
                return media_url, kind, pin_page
        except Exception as e:
            log.debug("%s failed: %s", fn.__name__, e)

    return None, None, pin_page

# =============== Download & Send ===============
def ext_from_content_type(ct: str) -> str:
    if not ct: return ".bin"
    ct = ct.lower()
    if "video/mp4" in ct or "mp4" in ct: return ".mp4"
    if "image/jpeg" in ct or "jpg" in ct: return ".jpg"
    if "image/png" in ct or "png" in ct: return ".png"
    if "image/webp" in ct or "webp" in ct: return ".webp"
    return ".bin"

async def download_to_temp(session: aiohttp.ClientSession, url: str, referer: str) -> Tuple[str, str, int]:
    """
    Download file with proper Referer. Returns (path, content_type, size_bytes).
    """
    headers = dict(HEADERS)
    headers["Referer"] = referer or "https://www.pinterest.com/"
    async with session.get(url, headers=headers, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "") or ""
        size = int(r.headers.get("Content-Length") or 0)
        suffix = ext_from_content_type(ct)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            total = 0
            async for chunk in r.content.iter_chunked(128 * 1024):
                f.write(chunk)
                total += len(chunk)
            size = total or size
            return f.name, ct, size

# =============== Telegram ===============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text: return
    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("Send a Pinterest Pin URL.")
        return
    url = m.group(0)

    await update.message.chat.send_action(ChatAction.TYPING)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, kind, pin_page = await extract_media(session, url)
        if not media_url:
            await update.message.reply_text("Failed: No public media found on this Pin (maybe private or removed).")
            return

        # refuse HLS streams
        if media_url.endswith(".m3u8"):
            await update.message.reply_text("Found HLS stream only (not a direct MP4). Cannot send to Telegram.")
            return

        path = None
        try:
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if kind == "video" else ChatAction.UPLOAD_PHOTO
            )
            path, ct, size = await download_to_temp(session, media_url, referer=pin_page)

            # sanity: avoid sending tiny/HTML files
            if size < 50_000:  # 50KB
                raise RuntimeError("Downloaded file too small (likely blocked or HTML).")

            if kind == "video":
                await update.message.reply_video(video=InputFile(path), caption="Downloaded ✅")
            else:
                # If WEBP -> send as document to avoid Image_process_failed
                if "image/webp" in ct.lower():
                    await update.message.reply_document(document=InputFile(path), caption="Downloaded ✅ (webp)")
                else:
                    await update.message.reply_photo(photo=InputFile(path), caption="Downloaded ✅")

        except Exception as e:
            log.exception("Send failed")
            msg = str(e)
            if "Image_process_failed" in msg:
                await update.message.reply_text(
                    "Telegram couldn't process this image (often WEBP). I’ll try sending it as a file next time."
                )
            else:
                await update.message.reply_text(f"Download failed: {e}")
        finally:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

# =============== Run ===============
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
    main()                best_area, best_url = area, d["url"]
    return best_url

# =============== Extractors ===============
async def try_pinresource(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None

    api = "https://www.pinterest.com/resource/PinResource/get/"
    params = {"data": json.dumps({"options": {"id": pid}, "context": {}}, separators=(",", ":"))}
    data = await http_json(session, api, params=params)
    if not data:
        return None, None

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

    return None, None

async def try_pidgets(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None

    api = "https://widgets.pinterest.com/v3/pidgets/pins/info/"
    data = await http_json(session, api, params={"pin_ids": pid})
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

    return None, None

async def try_parse_page(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
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
        try:
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
        except Exception as e:
            log.debug("__PWS_DATA__ parse failed: %s", e)

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

    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp|avif)", html_text, flags=re.I)
    if m:
        return m.group(0), "image"

    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    for fn in (try_pinresource, try_pidgets, try_parse_page):
        try:
            media_url, kind = await fn(session, url)
            if media_url:
                return media_url, kind
        except Exception as e:
            log.debug("%s failed: %s", fn.__name__, e)

    return None, None

# =============== Download & Telegram send ===============
def ext_from_content_type(ct: str) -> str:
    if not ct:
        return ".bin"
    ct = ct.lower()
    if "mp4" in ct:
        return ".mp4"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "avif" in ct:
        return ".avif"
    return ".bin"

PINIMG_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.pinterest.com/",   # مهم ضد 403 من pinimg
    "Accept": "*/*",
}

async def fetch_binary(session: aiohttp.ClientSession, url: str) -> Tuple[bytes, str]:
    """
    حمل الملف كـ bytes وارجع (data, content_type).
    جرّب أولاً بهيدر Referer. لو 403 جرّب تاني بالهيدرز العادية.
    """
    for headers in (PINIMG_HEADERS, BASE_HEADERS):
        try:
            async with session.get(url, headers=headers, timeout=HTTP_TIMEOUT) as r:
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "").lower()
                data = await r.read()
                return data, ct
        except Exception as e:
            last_err = e
    raise last_err  # لو فشل الاثنين

def ensure_jpeg_if_needed(data: bytes, ct: str) -> Tuple[bytes, str]:
    """
    لو الصورة webp/avif → نحولها إلى JPEG (Telegram يدعم photo: jpg/png).
    """
    if ct.startswith("image/jpeg") or ct.startswith("image/jpg") or ct.startswith("image/png"):
        return data, ct

    if ct.startswith("image/webp") or ct.startswith("image/avif") or ct.startswith("image/heic"):
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=92, optimize=True)
            return out.getvalue(), "image/jpeg"
        except Exception as e:
            log.debug("convert to JPEG failed: %s", e)
            # هنرسل كـ document لاحقًا لو فشل التحويل
            return data, ct

    return data, ct

async def download_to_temp(session: aiohttp.ClientSession, url: str, media_type: str) -> Tuple[str, str]:
    """
    احفظ المحتوى في ملف مؤقت وارجع (path, real_content_type).
    - لو صورة webp/avif بنحوّلها إلى JPEG قبل الحفظ.
    - لو استلمنا HTML/نص نعتبره فشل.
    """
    data, ct = await fetch_binary(session, url)

    # حماية من HTML متخفي
    if "text/html" in ct or "text/plain" in ct:
        raise RuntimeError("Got HTML instead of media from CDN")

    # تحويل الصور غير المدعومة
    if media_type == "image":
        data, ct = ensure_jpeg_if_needed(data, ct)

    suffix = ext_from_content_type(ct)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(data)
        return f.name, ct

# =============== Telegram handlers ===============
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
    async with aiohttp.ClientSession(headers=BASE_HEADERS) as session:
        media_url, media_type = await extract_media(session, url)
        if not media_url:
            await update.message.reply_text(
                "Failed: No public video/image found on this Pin (it may be private or image-only)."
            )
            return

        path = None
        try:
            await update.message.chat.send_action(
                ChatAction.UPLOAD_VIDEO if media_type == "video" else ChatAction.UPLOAD_PHOTO
            )
            path, ct = await download_to_temp(session, media_url, media_type)

            # جرّب إرسال photo/ video
            if media_type == "video":
                await update.message.reply_video(video=InputFile(path), caption="Downloaded ✅")
            else:
                # لو الصورة ليست jpg/png، حاولنا نحولها؛ لو لسه غير مدعومة هنرسل document
                if not (ct.startswith("image/jpeg") or ct.startswith("image/png")):
                    await update.message.reply_document(document=InputFile(path), caption="Downloaded ✅ (image)")
                else:
                    await update.message.reply_photo(photo=InputFile(path), caption="Downloaded ✅ (image)")

        except Exception as e:
            log.exception("Send failed")
            # خطة بديلة: لو فشل كـ photo/video جرّب كـ document
            try:
                if path and os.path.exists(path):
                    await update.message.reply_document(document=InputFile(path), caption="Downloaded ✅ (file)")
                else:
                    raise
            except Exception:
                if "Image_process_failed" in str(e):
                    await update.message.reply_text(
                        "Download failed: Telegram could not process the file. It may be an unsupported image/video. I tried to convert images to JPEG automatically."
                    )
                else:
                    await update.message.reply_text(f"Download failed: {e}")
        finally:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

# =============== Run bot ===============
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
    main()
