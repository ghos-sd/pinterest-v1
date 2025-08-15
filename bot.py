# -*- coding: utf-8 -*-
import os, re, json, logging
from typing import Optional, Tuple, Any
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("pin-video-bot")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
HTTP_TIMEOUT = 25
PIN_HOSTS = ("pinterest.com","www.pinterest.com","pin.it","in.pinterest.com","www.pinterest.co.uk")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ========= Helpers =========
def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False

def expand_url(url: str) -> str:
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

def _find_in(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys: return v
                f = _find_in(v, keys)
                if f is not None: return f
        elif isinstance(obj, list):
            for it in obj:
                f = _find_in(it, keys)
                if f is not None: return f
    except Exception:
        pass
    return None

def _pick_best_video(video_list: dict) -> Optional[str]:
    if not isinstance(video_list, dict): return None
    order = ["V_1080P","V_720P","V_640P","V_480P","V_360P","V_240P","V_EXP4"]
    for q in order:
        if q in video_list and isinstance(video_list[q], dict):
            u = video_list[q].get("url")
            if u: return u
    for v in video_list.values():
        if isinstance(v, dict):
            u = v.get("url")
            if u: return u
    return None

def _pick_best_image(images: dict) -> Optional[str]:
    if not isinstance(images, dict): return None
    if "orig" in images and isinstance(images["orig"], dict):
        u = images["orig"].get("url")
        if u: return u
    best_u, best_area = None, -1
    for v in images.values():
        if isinstance(v, dict):
            u = v.get("url")
            h = v.get("height", 0) or 0
            w = v.get("width", 0) or 0
            area = (h*w) if (h and w) else 0
            if u and area >= best_area:
                best_area, best_u = area, u
    return best_u

# ========= Video-first extractors =========
def try_html_mp4(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """أولوية 1: التقاط روابط MP4 مباشرة من HTML."""
    try:
        html = get_html(pin_url)
        hits = re.findall(r'https://v\.pinimg\.com/[^"\s]+?\.mp4', html)
        if hits:
            hits.sort(key=len, reverse=True)  # غالباً الأطول = أعلى جودة
            return hits[0], "video"
    except Exception:
        pass
    return None, None

def try_pws_json(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """أولوية 2: JSON الداخلي (__PWS_DATA__/initialReduxState)."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s; break
        if not sc or not sc.string:
            return None, None
        text = sc.string.strip()
        text = re.sub(r"^[^{]*", "", text); text = re.sub(r";?\s*$", "", text)
        data = json.loads(text)

        redux = data
        for key in ("props", "initialReduxState"):
            if isinstance(redux, dict) and key in redux: redux = redux[key]

        # فيديو أولاً
        video_list = _find_in(redux, ("video_list","videos"))
        if isinstance(video_list, dict) and "video_list" not in video_list:
            video_list = video_list.get("video_list", video_list)
        if not video_list:
            rr = _find_in(data, ("resourceResponses",))
            if rr:
                video_list = _find_in(rr, ("video_list","videos"))
        if video_list:
            vurl = _pick_best_video(video_list)
            if vurl: return vurl, "video"

        # صورة فقط (fallback)
        images = _find_in(redux, ("images",))
        if not images:
            rr = _find_in(data, ("resourceResponses",))
            if rr:
                images = _find_in(rr, ("images",))
        if images:
            iurl = _pick_best_image(images)
            if iurl: return iurl, "image"
    except Exception:
        pass
    return None, None

def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

def try_pidgets(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """أولوية 3: pidgets القديمة."""
    pid = pin_id_from_url(pin_url)
    if not pid: return None, None
    try:
        r = requests.get("https://widgets.pinterest.com/v3/pidgets/pins/info/",
                         params={"pin_ids": pid}, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200: return None, None
        data = r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins: return None, None
        pin = pins[0]
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        vurl = _pick_best_video(vlist)
        if vurl: return vurl, "video"
        img = _pick_best_image(pin.get("images") or {})
        if img: return img, "image"
    except Exception:
        pass
    return None, None

def try_meta_video(pin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """أولوية 4: og:video / twitter:player:stream."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"], "video"
        # كـ fallback صوري
        mi = soup.find("meta", property="og:image")
        if mi and mi.get("content"):
            return mi["content"], "image"
    except Exception:
        pass
    return None, None

def extract_media(pin_url: str) -> Tuple[str, str]:
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)
    # ترتيب صارم للفيديوهات أولاً
    for fn in (try_html_mp4, try_pws_json, try_pidgets, try_meta_video):
        u, t = fn(url)
        if u:
            return u, t
    raise ValueError("No media found. Pin may be private or blocked.")

# ========= Delivery =========
def sniff_type_by_head(url: str) -> Optional[str]:
    try:
        h = requests.head(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        ct = (h.headers.get("Content-Type") or "").lower()
        if ct.startswith("video/"): return "video"
        if ct.startswith("image/"): return "image"
    except Exception:
        pass
    p = urlparse(url).path.lower()
    if p.endswith(".mp4") or ".mp4?" in p: return "video"
    if any(p.endswith(x) for x in (".jpg",".jpeg",".png",".webp")): return "image"
    return None

async def send_media(update: Update, media_url: str, media_type: str):
    sniff = sniff_type_by_head(media_url)
    if sniff: media_type = sniff

    with requests.get(media_url, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        content = r.content

    # اسم مناسب
    fname = os.path.basename(urlparse(media_url).path)
    if media_type == "video" and not fname.lower().endswith(".mp4"):
        fname += ".mp4"
    if media_type == "image" and not re.search(r"\.(jpg|jpeg|png|webp)$", fname, re.I):
        fname += ".jpg"

    if media_type == "video":
        cap = "Downloaded ✅"
        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(document=content, filename=fname,
                                                caption=cap + " (sent as document due to size)")
        else:
            await update.message.reply_video(video=content, filename=fname, caption=cap)
    else:
        # صورة كـ خيار ثانوي فقط
        cap = "Image (fallback) ✅"
        if len(content) > 9 * 1024 * 1024:
            await update.message.reply_document(document=content, filename=fname, caption=cap)
        else:
            await update.message.reply_photo(photo=content, caption=cap)

# ========= Bot commands =========
WELCOME = (
    "Pinterest Video Downloader — no official API.\n\n"
    "• Send any **public Pin** link and I’ll fetch the **video** in the best quality I can find.\n"
    "• If no video exists, I’ll return the image as a fallback.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Commands:\n"
    "/start — About\n"
    "/help — Usage\n\n"
    "Developed by @Ghostnosd."
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("Paste a Pinterest Pin link.")
        return
    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("This doesn’t look like a Pinterest Pin link.")
        return
    waiting = await update.message.reply_text("⏳ Fetching…")
    try:
        media_url, media_type = extract_media(url)
        log.info("Found: %s (%s)", media_url, media_type)
        await send_media(update, media_url, media_type)
        await waiting.delete()
    except Exception as e:
        log.exception("Failed")
        await waiting.edit_text(f"Failed to download: {e}\nMake sure the Pin is public.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    log.info("Bot started.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
