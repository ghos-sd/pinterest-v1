# -*- coding: utf-8 -*-
import os
import re
import json
import logging
import asyncio
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-bot")

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
HTTP_TIMEOUT = 25

PIN_HOSTS = (
    "pinterest.com", "www.pinterest.com", "in.pinterest.com",
    "www.pinterest.co.uk", "pin.it"
)

# ---------- HTTP helpers ----------
def expand_url(url: str) -> str:
    """Follow redirects (pin.it) + try canonical/og:url to reach the real /pin/ page."""
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

# ---------- JSON helpers ----------
def deep_find(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    """Recursive search for any of 'keys' inside nested dict/list."""
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    return v
                found = deep_find(v, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = deep_find(item, keys)
                if found is not None:
                    return found
    except Exception:
        pass
    return None

def pick_best_video(video_list: Dict[str, Any]) -> Optional[str]:
    """Choose the highest quality url from Pinterest 'video_list'."""
    if not isinstance(video_list, dict):
        return None
    order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for key in order:
        d = video_list.get(key)
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    for d in video_list.values():
        if isinstance(d, dict) and d.get("url"):
            return d["url"]
    return None

def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

# ---------- Core extractor ----------
def extract_pinterest_video(pin_url: str) -> Optional[str]:
    """
    Try several robust ways to get a direct mp4 URL from a public Pin:
      1) widgets.pinterest.com/v3/pidgets/pins/info/ (legacy but still works for many pins)
      2) Parse __PWS_DATA__ / initialReduxState JSON on the page
      3) og:video / twitter:player:stream meta tags
      4) Regex sweep for *.pinimg.com/*.mp4 in page source
    """
    url = expand_url(pin_url)
    log.info("Final URL: %s", url)

    # 1) pidgets API (no auth, public only)
    pid = pin_id_from_url(url)
    if pid:
        try:
            r = requests.get(
                "https://widgets.pinterest.com/v3/pidgets/pins/info/",
                params={"pin_ids": pid},
                headers=HEADERS, timeout=HTTP_TIMEOUT,
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

    # 2) Parse page JSON
    html = ""
    try:
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        data_script = soup.find("script", id="__PWS_DATA__")
        if not data_script or not data_script.string:
            for sc in soup.find_all("script"):
                if sc.string and ("initialReduxState" in sc.string or "__PWS_DATA__" in sc.string):
                    data_script = sc
                    break
        if data_script and data_script.string:
            text = data_script.string.strip()
            text = re.sub(r"^[^{]*", "", text)
            text = re.sub(r";?\s*$", "", text)
            data = json.loads(text)

            # Sometimes nested under props.initialReduxState
            redux = data
            for key in ("props", "initialReduxState"):
                if isinstance(redux, dict) and key in redux:
                    redux = redux[key]

            video_list = deep_find(redux, ("video_list", "videos"))
            if isinstance(video_list, dict) and "video_list" not in video_list:
                video_list = video_list.get("video_list", video_list)
            if isinstance(video_list, dict):
                vurl = pick_best_video(video_list)
                if vurl:
                    return vurl

            rr = deep_find(data, ("resourceResponses",))
            if rr:
                video_list = deep_find(rr, ("video_list", "videos"))
                if isinstance(video_list, dict) and "video_list" not in video_list:
                    video_list = video_list.get("video_list", video_list)
                if isinstance(video_list, dict):
                    vurl = pick_best_video(video_list)
                    if vurl:
                        return vurl
    except Exception:
        pass

    # 3) Meta fallbacks
    try:
        if not html:
            html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"]
    except Exception:
        pass

    # 4) Regex for direct pinimg mp4 in source
    try:
        if not html:
            html = get_html(url)
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, flags=re.I)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None

# ---------- Telegram bot ----------
WELCOME = (
    "Hey! 👋\n"
    "Send me a **Pinterest Pin** link and I'll fetch the **video** for you (no API).\n\n"
    "Supported: `pinterest.com` / `pin.it` / regional domains.\n"
    "Built & maintained by @Ghostnosd.\n"
    "— If a pin is private or image-only, I’ll let you know."
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, disable_web_page_preview=True)

def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", msg)
    if not m:
        await update.message.reply_text("Send a Pinterest **Pin** link.")
        return

    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("That doesn’t look like a Pinterest **Pin** link.")
        return

    status = await update.message.reply_text("⏳ Working on it...")
    try:
        vurl = await asyncio.to_thread(extract_pinterest_video, url)
        if not vurl:
            await status.edit_text("Failed: No video found on this Pin (or it is private).")
            return

        # Download content to forward to Telegram reliably
        def _download() -> bytes:
            r = requests.get(vurl, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True)
            r.raise_for_status()
            return r.content

        content = await asyncio.to_thread(_download)
        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=content, filename="pinterest_video.mp4",
                caption="✅ Downloaded (sent as document due to size)."
            )
        else:
            await update.message.reply_video(
                video=content, filename="pinterest_video.mp4",
                caption="✅ Downloaded."
            )
        await status.delete()
    except Exception as e:
        log.exception("Send failed")
        await status.edit_text(f"Error: {e}\nTry a different **public** Pin link.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot is running (polling).")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()    ""
    يحاول استخراج رابط الفيديو المباشر من Pinterest (عام فقط).
    يتدرج عبر عدّة طرق قوية وحديثة.
    """
    url = _expand_url(pin_url)
    log.info("Expanded URL: %s", url)

    # 1) واجهة Pidgets العامة (غير موثّقة لكنها ما زالت تعمل لكثير من الـ Pins)
    pid = _pin_id_from_url(url)
    if pid:
        try:
            r = requests.get(
                "https://widgets.pinterest.com/v3/pidgets/pins/info/",
                params={"pin_ids": pid},
                headers=HEADERS,
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                pins = ((data or {}).get("data") or {}).get("pins") or []
                if pins:
                    video_list = ((pins[0].get("videos") or {}).get("video_list")) or {}
                    v = _pick_best_video(video_list)
                    if v:
                        return v
        except Exception as e:
            log.warning("Pidgets path failed: %s", e)

    # 2) تحليل HTML والـ JSON الداخلي (__PWS_DATA__/initialReduxState/resourceResponses)
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
            txt = sc.string.strip()
            # نظّف أي أحرف قبل { وأي ; في النهاية
            txt = re.sub(r"^[^{]*", "", txt)
            txt = re.sub(r";?\s*$", "", txt)
            data = json.loads(txt)

            redux = data
            for key in ("props", "initialReduxState"):
                if isinstance(redux, dict) and key in redux:
                    redux = redux[key]

            # ابحث عن video_list أو videos
            vlist = _deep_find(redux, ("video_list", "videos"))
            if isinstance(vlist, dict) and "video_list" not in vlist:
                vlist = vlist.get("video_list", vlist)
            if isinstance(vlist, dict):
                v = _pick_best_video(vlist)
                if v:
                    return v

            rr = _deep_find(data, ("resourceResponses",))
            if rr:
                vlist = _deep_find(rr, ("video_list", "videos"))
                if isinstance(vlist, dict) and "video_list" not in vlist:
                    vlist = vlist.get("video_list", vlist)
                if isinstance(vlist, dict):
                    v = _pick_best_video(vlist)
                    if v:
                        return v
    except Exception as e:
        log.warning("Parsing __PWS_DATA__ failed: %s", e)

    # 3) meta fallback (og:video / twitter:player:stream)
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") \
             or soup.find("meta", property="og:video:url") \
             or soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"]
    except Exception as e:
        log.warning("Meta fallback failed: %s", e)

    # 4) Regex sweep عن روابط pinimg .mp4 داخل الصفحة
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT).text
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, flags=re.I)
        if m:
            return m.group(0)
    except Exception as e:
        log.warning("Regex sweep failed: %s", e)

    return None


def _looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False


# ================== تيليجرام ==================
WELCOME = (
    "Welcome 👋\n"
    "Send a public Pinterest **Pin** link that contains a **video**, and I'll fetch it for you.\n"
    "• Works with pin.it and pinterest.com links\n"
    "• Videos only (image Pins are ignored)\n"
    "\nDeveloped by @Ghostnosd"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("Please send a Pinterest Pin link that contains a video.")
        return

    url = m.group(1)
    if not _looks_like_pin(url):
        await update.message.reply_text("This doesn't look like a Pinterest Pin link.")
        return

    status = await update.message.reply_text("⏳ Processing…")
    try:
        vurl = find_video_url(url)
        if not vurl:
            await status.edit_text("Failed: No video found on this Pin (or it is private).")
            return

        log.info("Video URL: %s", vurl)
        # نزّل المحتوى لنتفادى حظر تيليجرام لبعض الروابط المباشرة
        with requests.get(vurl, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            data = r.content

        # لو كبير جدًا أرسله كمستند
        if len(data) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=data, filename="pinterest_video.mp4",
                caption="✅ Downloaded (sent as document due to size)"
            )
        else:
            await update.message.reply_video(
                video=data, filename="pinterest_video.mp4",
                caption="✅ Downloaded"
            )
        await status.delete()
    except Exception as e:
        log.exception("Send failed")
        await status.edit_text(f"Error: {e}")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()                return None

            redux = data
            for k in ("props", "initialReduxState"):
                if isinstance(redux, dict) and k in redux:
                    redux = redux[k]

            vlist = deep_find(redux, ("video_list", "videos"))
            if isinstance(vlist, dict) and "video_list" not in vlist:
                vlist = vlist.get("video_list", vlist)
            if isinstance(vlist, dict):
                v = pick_best_v(vlist)
                if v: return v

            rr = deep_find(data, ("resourceResponses",))
            if rr:
                vlist = deep_find(rr, ("video_list", "videos"))
                if isinstance(vlist, dict) and "video_list" not in vlist:
                    vlist = vlist.get("video_list", vlist)
                if isinstance(vlist, dict):
                    v = pick_best_v(vlist)
                    if v: return v
    except Exception:
        pass

    # 3) meta fallback
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"]
    except Exception:
        pass

    # 4) regex sweep for pinimg mp4
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, flags=re.I)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None

# ========= أدوات مساعدة =========
def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False

HELP = (
    "Send me a Pinterest Pin URL (video pins only) and I'll fetch the video.\n\n"
    "• Works with pin.it and pinterest.com links\n"
    "• Public pins only\n\n"
    "Developed by @Ghostnosd"
)

# ========= Handlers =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hi 👋\n{HELP}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        await update.message.reply_text("Send a Pinterest Pin URL (video).")
        return

    url = m.group(1)
    if not looks_like_pin(url):
        await update.message.reply_text("This doesn't look like a Pinterest pin URL.")
        return

    status = await update.message.reply_text("⏳ Processing…")
    try:
        vurl = find_video_url(url)
        if not vurl:
            await status.edit_text("Failed: No video found on this Pin (or it is private).")
            return

        log.info("Video URL: %s", vurl)
        r = requests.get(vurl, headers=HEADERS, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        content = r.content

        # أرسل كـ فيديو إن أمكن، وإلا مستند
        bio = BytesIO(content); bio.name = "pinterest_video.mp4"
        ct = r.headers.get("Content-Type", "")
        if "video" in ct or len(content) <= 45 * 1024 * 1024:
            await update.message.reply_video(video=bio, caption="✅ Downloaded")
        else:
            await update.message.reply_document(document=bio, caption="✅ Downloaded")

        await status.delete()
    except Exception as e:
        log.exception("send failed")
        await status.edit_text(f"Error: {e}")

# ========= تشغيل البوت =========
def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var")

    # مهم جدًا: امسح أي Webhook قديم قبل الـ polling
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",
            timeout=10
        )
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()        pass

    # 3) meta fallback
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content"):
            return mv["content"]
    except Exception:
        pass

    # 4) regex sweep for pinimg mp4
    try:
        if "html" not in locals():
            html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, flags=re.I)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None
