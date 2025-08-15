# -*- coding: utf-8 -*-
"""
Pinterest Video Downloader Bot — Railway-ready
Focus: Video only (no images)
Author credit: @Ghostnosd (Developed by)
"""

import os
import re
import json
import logging
from typing import Optional, Tuple, Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-video-bot")

# -------------------- Constants --------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
HTTP_TIMEOUT = 25

PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "pin.it",
    "in.pinterest.com", "www.pinterest.co.uk"
}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


# -------------------- HTTP helpers --------------------
def expand_url(url: str) -> str:
    """Expand pin.it short URLs and return final Pinterest pin URL if possible."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        final_url = r.url or url
        # if page has canonical/og:url, use it
        if "/pin/" not in final_url:
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


# -------------------- JSON utilities --------------------
def _find_in_dict(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    """Deep search for any key in 'keys' and return first match."""
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
    """Choose best quality url from pinterest 'video_list' dict."""
    if not isinstance(video_list, dict):
        return None
    quality_order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
    for q in quality_order:
        rec = video_list.get(q)
        if isinstance(rec, dict):
            u = rec.get("url")
            if u:
                return u
    # any first url
    for rec in video_list.values():
        if isinstance(rec, dict):
            u = rec.get("url")
            if u:
                return u
    return None


# -------------------- Extractors (video only) --------------------
def pin_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None


def try_pidgets(pin_url: str) -> Optional[str]:
    """Legacy widgets API (often works unauthenticated)."""
    pid = pin_id_from_url(pin_url)
    if not pid:
        return None
    try:
        r = requests.get(
            "https://widgets.pinterest.com/v3/pidgets/pins/info/",
            params={"pin_ids": pid},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        pins = ((data or {}).get("data") or {}).get("pins") or []
        if not pins:
            return None
        pin = pins[0]
        vlist = ((pin.get("videos") or {}).get("video_list")) or {}
        return _pick_best_video(vlist)
    except Exception:
        return None


def try_page_json(pin_url: str) -> Optional[str]:
    """Parse __PWS_DATA__ / initialReduxState from the HTML."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        data_script = soup.find("script", id="__PWS_DATA__")
        if not data_script or not data_script.string:
            # look through other scripts
            for sc in soup.find_all("script"):
                if sc.string and ("initialReduxState" in sc.string or "__PWS_DATA__" in sc.string):
                    data_script = sc
                    break
        if not data_script or not data_script.string:
            return None

        text = data_script.string.strip()
        # keep only JSON body
        text = re.sub(r"^[^{]*", "", text)
        text = re.sub(r";?\s*$", "", text)
        data = json.loads(text)

        # walk into redux
        redux = data
        for key in ("props", "initialReduxState"):
            if isinstance(redux, dict) and key in redux:
                redux = redux[key]

        video_list = _find_in_dict(redux, ("video_list", "videos"))
        if isinstance(video_list, dict) and "video_list" not in video_list:
            video_list = video_list.get("video_list", video_list)

        if video_list:
            return _pick_best_video(video_list)

        # sometimes under resourceResponses
        rr = _find_in_dict(data, ("resourceResponses",))
        if rr:
            video_list = _find_in_dict(rr, ("video_list", "videos"))
            if isinstance(video_list, dict) and "video_list" not in video_list:
                video_list = video_list.get("video_list", video_list)
            if video_list:
                return _pick_best_video(video_list)

        return None
    except Exception:
        return None


def try_meta_video(pin_url: str) -> Optional[str]:
    """Read og:video / twitter:player:stream from the pin page."""
    try:
        html = get_html(pin_url)
        soup = BeautifulSoup(html, "html.parser")
        cand = (
            soup.find("meta", property="og:video")
            or soup.find("meta", property="og:video:url")
            or soup.find("meta", property="twitter:player:stream")
        )
        if cand and cand.get("content"):
            return cand["content"]
        return None
    except Exception:
        return None


def extract_video_url(pin_url: str) -> str:
    """
    Return direct video URL or raise ValueError if none.
    """
    url = expand_url(pin_url)
    log.info("Expanded URL: %s", url)

    for extractor in (try_pidgets, try_page_json, try_meta_video):
        v = extractor(url)
        if v:
            return v

    raise ValueError("No video found on this Pin (or it is private).")


# -------------------- Telegram Bot --------------------
WELCOME = (
    "Hello! 👋\n"
    "Send me a **Pinterest Pin** link and I’ll fetch the **video** for you — no API required.\n\n"
    "• Example:\n"
    "https://www.pinterest.com/pin/123456789/\n\n"
    "Developed by @Ghostnosd · Fast, simple, and privacy-friendly."
)

HELP = (
    "Just paste a Pinterest **Pin** link that contains a video. "
    "If the Pin is private or requires login, I won't be able to download it."
)


def looks_like_pin(url: str) -> bool:
    try:
        u = urlparse(url)
        return (u.netloc in PIN_HOSTS) or ("pinterest.com/pin/" in url)
    except Exception:
        return False


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, disable_web_page_preview=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    m = re.search(r"(https?://\S+)", text)
    if not m or not looks_like_pin(m.group(1)):
        await update.message.reply_text("Please send a valid **Pinterest Pin** link that has a video.")
        return

    url = m.group(1)
    status = await update.message.reply_text("⏳ Working…")

    try:
        video_url = extract_video_url(url)
        log.info("Video URL: %s", video_url)

        # Prefer uploading bytes (safer for redirects/content-types)
        with requests.get(video_url, headers=HEADERS, timeout=HTTP_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            content = r.content

        if len(content) > 45 * 1024 * 1024:
            await update.message.reply_document(
                document=content,
                filename="pinterest_video.mp4",
                caption="Downloaded ✅ (sent as document due to size)"
            )
        else:
            await update.message.reply_video(
                video=content,
                filename="pinterest_video.mp4",
                caption="Downloaded ✅"
            )

        await status.delete()

    except Exception as e:
        log.exception("Download failed")
        await status.edit_text(f"Failed: {e}\nNo video found or Pin is private.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
