#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات ۴ — «رادارِ شایعه» (رادیو بولتن)
هر چند ساعت یک‌بار تازه‌ترین فکت‌چک‌های فکت‌نامه را از صفحه‌ی عمومیِ تلگرامش
(t.me/s/factnameh) می‌خوانَد، عکس/ویدیوی شایعه را برمی‌دارد، و در کانال می‌فرستد:
یک تیترِ کوتاهِ بولد (خبر + شایعه‌بودنش) و دلیلش داخلِ کوت، با حکمِ فکت‌نامه و لینکِ منبع.
هیچ شایعه‌ای به‌عنوانِ خبرِ درست اعلام نمی‌شود.
"""

import os
import re
import sys
import json
import html
import requests
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

# ===================== تنظیمات =====================
BOT_NAME       = "رادار شایعه"
CHANNEL_ID     = "@testbotaii"
BACKUP_CHANNEL = "@analyzeAisTrb"
FOOTER         = "\n\n@RadioBulletin | رادیو بولتن"
SOURCE_URL     = "https://t.me/s/factnameh"
SOURCE_NAME    = "فکت‌نامه"
MAX_POSTS_PER_RUN = 2

# ===================== ثابت‌ها =====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")

AI_MODEL       = "openai/gpt-4.1"      # بهترین مدلِ رایگان (GPT-5 فقط با پلنِ پولی)
AI_MODEL_CHAIN = [AI_MODEL, "openai/gpt-4o", "openai/gpt-4o-mini"]
AI_ENDPOINT    = "https://models.github.ai/inference/chat/completions"

STATE_FILE = "seen.json"
TEHRAN     = timezone(timedelta(hours=3, minutes=30))
TG_API     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
UA         = {"User-Agent": "Mozilla/5.0 (compatible; RadioBulletinBot/1.0)"}
MAX_MEDIA_BYTES = 45 * 1024 * 1024     # سقفِ حجمِ مدیا (تلگرام تا ۵۰ مگ)

FACTCHECK_HINTS = ("نادرست", "گمراه‌کننده", "شاخ‌دار", "نیمه‌درست", "بی‌اساس",
                   "جعلی", "ساختگی", "شایعه", "ادعا", "❌", "❓",
                   "در فکت‌نامه بخوانید")
SKIP_HINTS = ("پادکست", "مکتب‌خانه", "اپیزود")


# ===================== وضعیت =====================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("  ⚠️ نتوانستم وضعیت را ذخیره کنم:", e)


# ===================== خواندنِ فکت‌نامه =====================
def _bg_url(style):
    if not style:
        return None
    mt = re.search(r"background-image:url\('([^']+)'\)", style.replace(" ", ""))
    return mt.group(1) if mt else None


def extract_media(msg):
    """ویدیو یا عکسِ خودِ پیام را برمی‌گرداند: (نوع, آدرس)."""
    vid = msg.select_one("video.tgme_widget_message_video")
    if vid and vid.get("src"):
        return ("video", vid["src"])
    ph = msg.select_one("a.tgme_widget_message_photo_wrap")
    if ph:
        u = _bg_url(ph.get("style"))
        if u:
            return ("photo", u)
    vt = msg.select_one(".tgme_widget_message_video_thumb")   # اگر srcِ مستقیمِ ویدیو نبود
    if vt:
        u = _bg_url(vt.get("style"))
        if u:
            return ("photo", u)
    return (None, None)


def fetch_factnameh():
    r = requests.get(SOURCE_URL, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for m in soup.select("div.tgme_widget_message"):
        post = m.get("data-post", "")
        tdiv = m.select_one(".tgme_widget_message_text")
        if not post or not tdiv:
            continue
        text = tdiv.get_text(separator="\n", strip=True)
        mtype, murl = extract_media(m)
        items.append({"id": post, "text": text, "link": f"https://t.me/{post}",
                      "media_type": mtype, "media_url": murl})
    return items  # قدیمی → جدید


def looks_like_factcheck(text):
    if len(text) < 100:
        return False
    if any(h in text for h in SKIP_HINTS):
        return False
    return any(h in text for h in FACTCHECK_HINTS)


# ===================== بازنویسی با هوش مصنوعی =====================
def ai_rewrite(text):
    system = (
        "You convert a FactNameh (فکت‌نامه) fact-check post into a SHORT anti-misinformation "
        "alert in COLLOQUIAL PERSIAN. FactNameh debunks false or misleading claims that "
        "circulate in Iranian media and social media. "
        "Output EXACTLY two labeled parts and NOTHING else:\n"
        "TITLE: a SHORT one-line Persian headline that states the rumor/claim and makes clear "
        "it is a rumor. It MUST start with «⚠️ شایعه:». Under 120 characters. Never present "
        "the claim as true.\n"
        "WHY: 1 to 3 short lines explaining the reality and why the claim is wrong or "
        "misleading, then a final line «🔖 حکم فکت‌نامه:» followed by the verdict found in the "
        "post (نادرست / گمراه‌کننده / شاخ‌دار / نیمه‌درست / بی‌اساس) or the closest accurate one.\n"
        "RULES: Paraphrase in your own words; do NOT copy sentences verbatim. Calm, clear, "
        "colloquial. No hashtags. "
        "If the post is NOT about checking a specific claim (podcast/promo/announcement), "
        "output exactly: SKIP\n"
        "Output only the TITLE/WHY block, or SKIP."
    )
    user = "FactNameh post:\n" + text[:2000]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"}
    used_model = None
    for m in AI_MODEL_CHAIN:
        try:
            payload = {"model": m, "messages": messages}
            if not m.startswith("openai/gpt-5"):
                payload["temperature"] = 0.4
            resp = requests.post(AI_ENDPOINT, headers=headers, timeout=60, json=payload)
            if resp.status_code == 429:
                print(f"  ⏳ سقفِ {m} پر است؛ مدلِ بعدی...")
                continue
            resp.raise_for_status()
            used_model = m
            txt = resp.json()["choices"][0]["message"]["content"].strip()
            if txt:
                if m != AI_MODEL:
                    print(f"  (با مدلِ پشتیبان نوشته شد: {m})")
                return txt, used_model
        except Exception as e:
            print(f"  ⚠️ خطای مدلِ {m}:", e)
            continue
    return "SKIP", (used_model or "fallback")


def parse_title_why(txt):
    t = txt.strip()
    if t == "SKIP":
        return None, None
    m1 = re.search(r"TITLE:\s*(.*?)(?:\nWHY:|\Z)", t, re.S)
    m2 = re.search(r"WHY:\s*(.*)\Z", t, re.S)
    title = (m1.group(1).strip() if m1 else "")
    why = (m2.group(1).strip() if m2 else "")
    if not title:                      # اگر برچسب‌ها نبود، کلِ متن را تیتر کن
        title = t
    return title, why


def build_caption(title, why, item):
    title = html.escape(title.strip())[:300]
    why = html.escape(why.strip())[:700]
    out = f"<b>{title}</b>"
    if why:
        out += f"\n\n<blockquote>{why}</blockquote>"
    link = html.escape(item["link"])
    out += f"\n\n📌 منبع: {SOURCE_NAME} — <a href=\"{link}\">مشاهده‌ی اصلِ مطلب</a>"
    out += FOOTER
    return out


# ===================== دانلود و ارسال =====================
def download_file(url, path):
    with requests.get(url, stream=True, timeout=120, headers=UA) as r:
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > MAX_MEDIA_BYTES:
            raise RuntimeError(f"مدیا بزرگ‌تر از حد است ({cl})")
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise RuntimeError("مدیا از حدِ مجاز گذشت")
    if size < 1000:
        raise RuntimeError("مدیا خیلی کوچک/ناقص است")
    return size


def _msg_id(resp):
    try:
        j = resp.json()
        if j.get("ok"):
            return j["result"]["message_id"]
        print("  ❌ تلگرام:", resp.text[:300])
    except Exception:
        print("  ❌ پاسخِ نامعتبرِ تلگرام:", resp.text[:200])
    return None


def send_media(kind, path, caption):
    endpoint = "sendPhoto" if kind == "photo" else "sendVideo"
    field = "photo" if kind == "photo" else "video"
    with open(path, "rb") as fh:
        files = {field: (os.path.basename(path), fh)}
        data = {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}
        if kind == "video":
            data["supports_streaming"] = "true"
        r = requests.post(f"{TG_API}/{endpoint}", data=data, files=files, timeout=180)
    return _msg_id(r)


def send_message(text):
    r = requests.post(f"{TG_API}/sendMessage", timeout=30,
                      data={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
    return _msg_id(r)


def publish(title, why, item):
    """اول با عکس/ویدیو؛ اگر مدیا نبود یا نشد، متنی."""
    caption = build_caption(title, why, item)
    mtype, murl = item.get("media_type"), item.get("media_url")
    if mtype and murl:
        ext = "mp4" if mtype == "video" else "jpg"
        path = f"media.{ext}"
        try:
            download_file(murl, path)
            mid = send_media(mtype, path, caption)
            if mid:
                return mid
        except Exception as e:
            print("  ⚠️ ارسالِ مدیا نشد، متنی می‌فرستم:", e)
    return send_message(caption)


def post_backup(item, model_label, msg_id):
    try:
        now = datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")
        chan = CHANNEL_ID.lstrip("@")
        link = f"https://t.me/{chan}/{msg_id}" if msg_id else "—"
        media = item.get("media_type") or "بدون مدیا"
        text = (
            f"🏷 ربات: {BOT_NAME}\n"
            f"🕘 زمان (تهران): {now}\n"
            f"📰 منبع: {SOURCE_NAME}\n"
            f"🖼 مدیا: {media}\n"
            f"🔗 اصلِ مطلب: {item.get('link')}\n"
            f"🤖 مدل: {model_label}\n"
            f"📌 پست: {link}"
        )
        requests.post(f"{TG_API}/sendMessage", timeout=30,
                      data={"chat_id": BACKUP_CHANNEL, "text": text,
                            "disable_web_page_preview": "true"})
    except Exception as e:
        print("  ⚠️ گزارشِ پشتیبان ارسال نشد:", e)


# ===================== اجرا =====================
def main():
    print("🛰 شروعِ رادارِ شایعه —", datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M"))
    missing = [k for k, v in [("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                              ("GITHUB_TOKEN", GITHUB_TOKEN)] if not v]
    if missing:
        print("❌ این متغیرها ست نشده‌اند:", ", ".join(missing))
        sys.exit(1)

    state = load_state()
    posted = state.get("posted_ids", [])
    posted_set = set(posted)

    try:
        items = fetch_factnameh()
    except Exception as e:
        print("❌ خواندنِ فکت‌نامه ناموفق:", e)
        return
    print(f"  📥 {len(items)} پست از فکت‌نامه خوانده شد.")

    fresh = [it for it in items
             if it["id"] not in posted_set and looks_like_factcheck(it["text"])]
    to_post = fresh[-MAX_POSTS_PER_RUN:]
    if not to_post:
        print("  ⛔ فکت‌چکِ تازه‌ای نبود.")
        save_state(state)
        return

    count = 0
    for it in to_post:
        raw, model_label = ai_rewrite(it["text"])
        title, why = parse_title_why(raw)
        if title is None:
            print(f"  ⏭ رد شد (فکت‌چکِ مشخصی نبود): {it['id']}")
            posted.append(it["id"])
            continue
        mid = publish(title, why, it)
        if mid:
            post_backup(it, model_label, mid)
            posted.append(it["id"])
            count += 1

    state["posted_ids"] = posted[-3000:]
    save_state(state)
    print(f"🏁 تمام شد. {count} هشدار منتشر شد.")


if __name__ == "__main__":
    main()
