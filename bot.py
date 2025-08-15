import os
import re
import json
import logging
import asyncio
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ================== Logging ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-bot")

# ================== HTTP ==================
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
HTTP_TIMEOUT = 25
PIN_HOSTS = ("pinterest.com", "www.pinterest.com", "pin.it", "in.pinterest.com", "www.pinterest.co.uk")

# ================== Utils ==================
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

def pin_id_from_url(url: str):
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

def pick_best_video(video_list: dict):
    if not isinstance(video_list, dict):
        return None
    order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for k in order:
        d = video_list.get(k)
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    for d in video_list.values():
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    return None

def deep_find(obj, keys):
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    return v
                found = deep_find(v, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = deep_find(it, keys)
                if found is not None:
                    return found
    except Exception:
        pass
    return None

# ================== Core extraction ==================
def extract_pinterest_video_url(pin_url: str):
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)

    # 1) widgets/pidgets (عام غالبًا)
    pid = pin_id_from_url(url)
    if pid:
        try:
            r = requests.get(
                "https://widgets.pinterest.com/v3/pidgets/pins/info/",
                params={"pin_ids": pid}, headers=HEADERS, timeout=HTTP_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                pins = ((data or {}).get("data") or {}).get("pins") or []
                if pins:
                    vlist = ((pins[0].get("videos") or {}).get("video_list")) or {}
                    vurl = pick_best_video(vlist)
                    if vurl:
                        return vurl
        except Exception:
            pass

    # 2) JSON داخل الصفحة (__PWS_DATA__/Redux أو resourceResponses)
    html = None
    try:
        html = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s
                    break
        if sc and sc.string:
            text = sc.string.strip()
            text = re.sub(r"^[^{]*", "", text)
            text = re.sub(r";?\s*$", "", text)
            data = json.loads(text)

            redux = data
            for k in ("props", "initialReduxState"):
                if isinstance(redux, dict) and k in redux:
                    redux = redux[k]

            vlist = deep_find(redux, ("video_list", "videos"))
            if isinstance(vlist, dict) and "video_list" not in vlist:
                vlist = vlist.get("video_list", vlist)
            if isinstance(vlist, dict):
                vurl = pick_best_video(vlist)
                if vurl:
                    return vurl

            rr = deep_find(data, ("resourceResponses",))
            if rr:
                vlist = deep_find(rr, ("video_list", "videos"))
                if isinstance(vlist, dict) and "video_list" not in vlist:
                    vlist = vlist.get("video_list", vlist)
                if isinstance(vlist, dict):
                    vurl = pick_best_video(vlist)
                    if vurl:
                        return vurl
    except Exception:
        pass

    # 3) og:video / twitter:player:stream
    try:
        if html is None:
            html = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url") \
             or soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"]
    except Exception:
        pass

    # 4) Regex مباشر لأي mp4 من pinimg
    try:
        if html is None:
            html = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT).text
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, flags=re.I)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None

def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False

# ================== Telegram Bot ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WELCOME = "Hi! Send me a public Pinterest Pin link and I will fetch the video for you. Developed by @Ghostnosd."
HELP = "Just paste a Pin URL (pin.it or pinterest.com). Private pins are not supported."

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("Please send a Pinterest Pin link.")
        return
    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("This doesn't look like a Pinterest Pin link.")
        return

    status = await update.message.reply_text("Working…")
    try:
        vurl = extract_pinterest_video_url(url)
        if not vurl:
            await status.edit_text("Failed: No video found on this Pin (or it is private).")
            return

        r = requests.get(vurl, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True)
        r.raise_for_status()
        content = r.content

        # Telegram limits – if big, send as document
        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=content,
                filename="pinterest_video.mp4",
                caption="Downloaded ✅ (sent as file due to size)"
            )
        else:
            await update.message.reply_video(
                video=content,
                filename="pinterest_video.mp4",
                caption="Downloaded ✅"
            )
        await status.delete()
    except Exception as e:
        log.exception("send failed")
        await status.edit_text(f"Error: {e}")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env variable.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot started.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
