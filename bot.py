# -*- coding: utf-8 -*-
import os, re, json, logging, requests
from io import BytesIO
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ========= إعدادات عامة =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pinterest-bot")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
TIMEOUT = 25

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PIN_HOSTS = ("pinterest.com","www.pinterest.com","pin.it","in.pinterest.com","www.pinterest.co.uk")

# ========= دالة استخراج رابط الفيديو (من رسالتك) =========
def find_video_url(pinterest_url):
    import json, re
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse
    import requests

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    TIMEOUT = 25

    def expand(u):
        try:
            r = requests.get(u, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            final_url = r.url or u
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
            return u

    def pin_id(u):
        m = re.search(r"/pin/(\d+)", u)
        return m.group(1) if m else None

    def pick_best_v(vlist):
        if not isinstance(vlist, dict): return None
        order = ["V_720P", "V_640P", "V_480P", "V_360P", "V_240P", "V_EXP4"]
        for k in order:
            if isinstance(vlist.get(k), dict) and vlist[k].get("url"):
                return vlist[k]["url"]
        for k, d in vlist.items():
            if isinstance(d, dict) and d.get("url"): return d["url"]
        return None

    url = expand(pinterest_url)

    # 1) pidgets (public)
    pid = pin_id(url)
    if pid:
        try:
            r = requests.get(
                "https://widgets.pinterest.com/v3/pidgets/pins/info/",
                params={"pin_ids": pid}, headers=HEADERS, timeout=TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                pins = ((data or {}).get("data") or {}).get("pins") or []
                if pins:
                    v = pick_best_v(((pins[0].get("videos") or {}).get("video_list")) or {})
                    if v: return v
        except Exception:
            pass

    # 2) Parse page JSON
    try:
        html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s; break
        if sc and sc.string:
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
