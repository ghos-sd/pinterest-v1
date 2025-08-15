# -*- coding: utf-8 -*-
import os, re, json, logging
from typing import Optional, Tuple, Any
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# -------- Logging --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("pin-video-bot")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
HTTP_TIMEOUT = 25
PIN_HOSTS = ("pinterest.com","www.pinterest.com","pin.it","in.pinterest.com","www.pinterest.co.uk")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# -------- Helpers --------
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

def _find_in(obj: Any, keys: Tuple[str, ...]):
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

# -------- Video extractors (video-only) --------
def try_html_mp4(pin_url: str):
    try:
        html = get_html(pin_url)
        hits = re.findall(r'https://v\.pinimg\.com/[^"\s]+?\.mp4', html)
        if hits:
            hits.sort(key=len, reverse=True)
            return hits[0]
    except Exception:
        pass
    return None

def try_pws_json(pin_url: str):
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s; break
        if not sc or not sc.string:
            return None
        text = sc.string.strip()
        text = re.sub(r"^[^{]*", "", text); text = re.sub(r";?\s*$", "", text)
        data = json.loads(text)

        redux = data
        for key in ("props", "initialReduxState"):
            if isinstance(redux, dict) and key in redux: redux = redux[key]

        video_list = _find_in(redux, ("video_list","videos"))
        if isinstance(video_list, dict) and "video_list" not in video_list:
            video_list = video_list.get("video_list", video_list)
        if not video_list:
            rr = _find_in(data, ("resourceResponses",))
            if rr:
                video_list = _find_in(rr, ("video_list","videos"))
        if video_list:
            return _pick_best_video(video_list)
    except Exception:
        pass
    return None

def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

def try_pidgets(pin_url: str):
    pid = pin_id_from_url(pin_url)
    if not pid: return None
    try:
        r = requests.get("https://widgets.pinterest.com/v3/pidgets/pins/info/",
                         params={"pin_ids": pid}, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200: return None
        data = r.json()
        pins = (((data or {}).get("data") or {}).get("pins") or [])
        if not pins: return None
        pin = pins[0]
        vlist = (((pin.get("videos") or {}).get("video_list")) or {})
        return _pick_best_video(vlist)
    except Exception:
        return None

def try_meta_video(pin_url: str):
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"): return mv["content"]
    except Exception:
        pass
    return None

def extract_video(pin_url: str) -> str:
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)
    for fn in (try_html_mp4, try_pws_json, try_pidgets, try_meta_video):
        u = fn(url)
        if u: return u
    raise ValueError("No video found on this Pin (it might be private).")

def sniff_is_video(url: str) -> bool:
    try:
        h = requests.head(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        ct = (h.headers.get("Content-Type") or "").lower()
        if ct.startswith("video/"): return True
    except Exception:
        pass
    p = urlparse(url).path.lower()
    return p.endswith(".mp4") or ".mp4?" in p

# -------- Telegram Bot --------
WELCOME = (
    "Pinterest **Video** Downloader (no official API)\n\n"
    "Send any **public Pin** link and I’ll fetch the **video** in the best quality.\n"
    "If the Pin has no video, you’ll get a clear notice.\n\n"
    "Example:\nhttps://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, disable_web_page_preview=True)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("Paste a Pinterest Pin link (video Pins only).")
        return
    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("This doesn’t look like a Pinterest Pin link.")
        return

    waiting = await update.message.reply_text("⏳ Fetching video…")
    try:
        video_url = extract_video(url)
        log.info("Candidate video: %s", video_url)

        # تأكيد إنه فيديو قبل الإرسال
        if not sniff_is_video(video_url):
            raise ValueError("Found media isn’t a video.")

        with requests.get(video_url, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            content = r.content

        fname = os.path.basename(urlparse(video_url).path)
        if not fname.lower().endswith(".mp4"):
            fname += ".mp4"

        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(document=content, filename=fname,
                                                caption="Downloaded ✅ (sent as document due to size)")
        else:
            await update.message.reply_video(video=content, filename=fname, caption="Downloaded ✅")
        await waiting.delete()
    except Exception as e:
        log.exception("Download failed")
        await waiting.edit_text(f"Failed: {e}\nNo video found or Pin is private.")

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
