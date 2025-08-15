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
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)

PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "in.pinterest.com",
    "ar.pinterest.com", "de.pinterest.com", "www.pinterest.co.uk", "pin.it"
}

WELCOME = (
    "Hi! 👋\n"
    "Send me a Pinterest Pin URL and I’ll download the **video first** (if public), "
    "or a high-quality image as a fallback.\n\nDeveloped by @Ghostnosd"
)

# =============== HTTP helpers ===============
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
    """Follow redirects (+ canonical / og:url) to reach a /pin/... page."""
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

# =============== Quality pickers ===============
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

# =============== Extractors ===============
async def try_pinresource(session: aiohttp.ClientSession, pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Newer, reliable endpoint:
    GET https://www.pinterest.com/resource/PinResource/get/?data={...}
      → resource_response.data.videos.video_list.*.url  (MP4 on pinimg)
      → resource_response.data.images.orig.url          (image)
    """
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
    """
    Widgets API – still public for many pins:
    https://widgets.pinterest.com/v3/pidgets/pins/info/?pin_ids=<ID>
      → data.pins[0].videos.video_list.*.url
      → data.pins[0].images.orig.url
    """
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
    """
    Fallback: parse page for __PWS_DATA__, meta tags, or sweep for pinimg CDN URLs.
    """
    html_text = await http_text(session, pin_url)
    if not html_text:
        return None, None

    soup = BeautifulSoup(html_text, "html.parser")

    # __PWS_DATA__ / initialReduxState
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

    # Meta tags
    mv = soup.find("meta", property="og:video") or \
         soup.find("meta", property="og:video:url") or \
         soup.find("meta", property="twitter:player:stream")
    if mv and mv.get("content"):
        return mv["content"], "video"

    mi = soup.find("meta", property="og:image")
    if mi and mi.get("content"):
        return mi["content"], "image"

    # Last resort: sweep for pinimg MP4
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
    if m:
        return m.group(0), "video"

    # or pinimg image
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp)", html_text, flags=re.I)
    if m:
        return m.group(0), "image"

    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (direct_url, type) with a strong preference for video if present.
    """
    # quick domain sanity check
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    # priority order: PinResource → pidgets → parse page
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
    return ".bin"

async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    """
    Save content to a temp file. Decide extension by Content-Type to avoid
    Telegram 'Image_process_failed' when extension doesn't match.
    """
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        ext = ext_from_content_type(r.headers.get("Content-Type", ""))
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(64 * 1024):
                f.write(chunk)
            return f.name

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
        await update.message.reply_text("Send a Pinterest Pin URL.")
        return
    url = m.group(0)

    await update.message.chat.send_action(ChatAction.TYPING)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
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
      → resource_response.data.videos.video_list.*.url  (MP4 on pinimg)
      → resource_response.data.images.orig.url          (image)
    """
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
    """
    Widgets API – still public for many pins:
    https://widgets.pinterest.com/v3/pidgets/pins/info/?pin_ids=<ID>
      → data.pins[0].videos.video_list.*.url
      → data.pins[0].images.orig.url
    """
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
    """
    Fallback: parse page for __PWS_DATA__, meta tags, or sweep for pinimg CDN URLs.
    """
    html_text = await http_text(session, pin_url)
    if not html_text:
        return None, None

    soup = BeautifulSoup(html_text, "html.parser")

    # __PWS_DATA__ / initialReduxState
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

    # Meta tags
    mv = soup.find("meta", property="og:video") or \
         soup.find("meta", property="og:video:url") or \
         soup.find("meta", property="twitter:player:stream")
    if mv and mv.get("content"):
        return mv["content"], "video"

    mi = soup.find("meta", property="og:image")
    if mi and mi.get("content"):
        return mi["content"], "image"

    # Last resort: sweep for pinimg MP4
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
    if m:
        return m.group(0), "video"

    # or pinimg image
    m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp)", html_text, flags=re.I)
    if m:
        return m.group(0), "image"

    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (direct_url, type) with a strong preference for video if present.
    """
    # quick domain sanity check
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass

    url = await expand_url(session, original_url)

    # priority order: PinResource → pidgets → parse page
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
    return ".bin"

async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    """
    Save content to a temp file. Decide extension by Content-Type to avoid
    Telegram 'Image_process_failed' when extension doesn't match.
    """
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        ext = ext_from_content_type(r.headers.get("Content-Type", ""))
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(64 * 1024):
                f.write(chunk)
            return f.name

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
    async with aiohttp.ClientSession(headers=HEADERS) as session:
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
    main()        p = pins[0]
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
    """PinResource الداخلي: قوي وواضح videos/images."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None, None
    try:
        api = "https://www.pinterest.com/resource/PinResource/get/"
        params = {"data": json.dumps({"options": {"id": pid}, "context": {}}, separators=(",", ":"))}
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
    """__PWS_DATA__ + meta + regex."""
    try:
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
            txt = re.sub(r"^[^{]*", "", sc.string.strip())
            txt = re.sub(r";?\s*$", "", txt)
            data = json.loads(txt)
            def deep_find(obj, keys):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in keys: return v
                        got = deep_find(v, keys)
                        if got is not None: return got
                elif isinstance(obj, list):
                    for it in obj:
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
        mv = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url") \
             or soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"): return mv["content"], "video"
        mi = soup.find("meta", property="og:image")
        if mi and mi.get("content"): return mi["content"], "image"
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html_text, flags=re.I)
        if m: return m.group(0), "video"
    except Exception as e:
        log.debug("parse page failed: %s", e)
    return None, None

async def extract_media(session: aiohttp.ClientSession, original_url: str) -> Tuple[Optional[str], Optional[str]]:
    """يرجع (url, type) – تفضيل الفيديو."""
    try:
        u = urlparse(original_url)
        if not (u.netloc in PIN_HOSTS or "pinterest.com/pin/" in original_url):
            return None, None
    except Exception:
        pass
    url = await expand_url(session, original_url)
    u, t = await try_pidgets(session, url)
    if u: return u, t
    u, t = await try_pinresource(session, url)
    if u: return u, t
    u, t = await try_parse_page(session, url)
    if u: return u, t
    return None, None

# =============== تنزيل وإرسال ===============
async def download_to_temp(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        low = url.lower()
        ext = ".mp4" if low.endswith(".mp4") else (".jpg" if (".jpg" in low or ".jpeg" in low) else ".bin")
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
            async for chunk in r.content.iter_chunked(64 * 1024):
                f.write(chunk)
            return f.name

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
        await update.message.reply_text("أرسل رابط Pin من Pinterest.")
        return
    url = m.group(0)
    await update.message.chat.send_action(ChatAction.TYPING)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        media_url, media_type = await extract_media(session, url)
        if not media_url:
            await update.message.reply_text("No public video/image found on this Pin (might be private or image-only).")
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
                if path and os.path.exists(path): os.remove(path)
            except Exception: pass

# =============== تشغيل البوت ===============
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
