#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات ۴ — «رادارِ شایعه» (رادیو بولتن)
هر چند ساعت یک‌بار تازه‌ترین فکت‌چک‌های فکت‌نامه را از صفحه‌ی عمومیِ تلگرامش
(t.me/s/factnameh) می‌خوانَد، آن‌ها را با هوش مصنوعی به یک «هشدارِ شایعه»ی کوتاهِ
فارسی بازنویسی می‌کند (با ذکرِ حکمِ فکت‌نامه و ارجاع به منبع) و در کانال می‌فرستد.
هیچ شایعه‌ای به‌عنوانِ خبرِ درست اعلام نمی‌شود؛ همه با برچسبِ «شایعه/نادرست».
"""

import os
import sys
import json
import html
import requests
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

# ===================== تنظیمات (این بخش را می‌توانی عوض کنی) =====================
BOT_NAME       = "رادار شایعه"
CHANNEL_ID     = "@testbotaii"          # جایی که پست می‌شود
BACKUP_CHANNEL = "@analyzeAisTrb"       # کانالِ گزارشِ فنی
FOOTER         = "\n\n@RadioBulletin | رادیو بولتن"
SOURCE_URL     = "https://t.me/s/factnameh"   # صفحه‌ی عمومیِ کانالِ فکت‌نامه
SOURCE_NAME    = "فکت‌نامه"
MAX_POSTS_PER_RUN = 2                   # حداکثر پست در هر اجرا (۰ تا ۲)

# ===================== ثابت‌ها =====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")

AI_MODEL       = "openai/gpt-4.1"      # بهترین مدلِ رایگان (GPT-5 فقط با پلنِ پولی)
AI_MODEL_CHAIN = [AI_MODEL, "openai/gpt-4o", "openai/gpt-4o-mini"]
AI_ENDPOINT    = "https://models.github.ai/inference/chat/completions"

STATE_FILE = "seen.json"
TEHRAN     = timezone(timedelta(hours=3, minutes=30))
TG_API     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# نشانه‌های یک فکت‌چکِ واقعی (برای فیلترِ اولیه)
FACTCHECK_HINTS = ("نادرست", "گمراه‌کننده", "شاخ‌دار", "نیمه‌درست", "بی‌اساس",
                   "جعلی", "ساختگی", "شایعه", "ادعا", "❌", "❓",
                   "در فکت‌نامه بخوانید")
# پست‌هایی که فکت‌چک نیستند (پادکست/تبلیغ) را رد می‌کنیم
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
def fetch_factnameh():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RadioBulletinBot/1.0)"}
    r = requests.get(SOURCE_URL, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for m in soup.select("div.tgme_widget_message"):
        post = m.get("data-post", "")
        tdiv = m.select_one(".tgme_widget_message_text")
        if not post or not tdiv:
            continue
        text = tdiv.get_text(separator="\n", strip=True)
        items.append({"id": post, "text": text, "link": f"https://t.me/{post}"})
    return items  # به‌ترتیبِ صفحه: قدیمی → جدید


def looks_like_factcheck(text):
    if len(text) < 100:
        return False
    if any(h in text for h in SKIP_HINTS):
        return False
    return any(h in text for h in FACTCHECK_HINTS)


# ===================== بازنویسی با هوش مصنوعی =====================
def ai_rewrite(text):
    system = (
        "You turn a FactNameh (فکت‌نامه) fact-check post into a SHORT anti-misinformation "
        "alert in COLLOQUIAL PERSIAN. FactNameh debunks false or misleading claims that "
        "circulate in Iranian media and social media. Given the raw post text, produce a "
        "short alert with EXACTLY this structure (each on its own line):\n"
        "«⚠️ شایعه:» + a brief, NEUTRAL statement of the claim that is circulating (never "
        "present it as true).\n"
        "«✅ واقعیت طبق فکت‌نامه:» + 1 to 2 short lines with the correct picture and why the "
        "claim is wrong or misleading.\n"
        "«🔖 حکم فکت‌نامه:» + the verdict word found in the post (نادرست / گمراه‌کننده / "
        "شاخ‌دار / نیمه‌درست / بی‌اساس). If none is explicit, use the closest accurate one.\n"
        "RULES: Never present the rumor as true; always frame it as a claim FactNameh checked. "
        "PARAPHRASE in your own words; do NOT copy sentences verbatim. Calm, clear, colloquial. "
        "No hashtags. Keep under 600 characters. "
        "If the post is NOT about checking a specific claim (podcast, promo, announcement), "
        "output exactly: SKIP\n"
        "Output ONLY the Persian alert text, or SKIP."
    )
    user = "FactNameh post:\n" + text[:2000]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Content-Type": "application/json"}
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


def build_post(body, item):
    body = html.escape(body.strip())
    link = item["link"]
    src = (f"\n\n📌 منبع: {SOURCE_NAME} — "
           f"<a href=\"{html.escape(link)}\">مشاهده‌ی اصلِ مطلب</a>")
    return f"{body}{src}{FOOTER}"


# ===================== تلگرام =====================
def send_message(text):
    r = requests.post(f"{TG_API}/sendMessage", timeout=30,
                      data={"chat_id": CHANNEL_ID, "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
    try:
        j = r.json()
    except Exception:
        j = {}
    if not j.get("ok"):
        print("  ❌ ارسالِ تلگرام ناموفق:", r.text[:300])
        return None
    mid = j["result"]["message_id"]
    print(f"  ✅ هشدار فرستاده شد (message_id={mid})")
    return mid


def post_backup(item, model_label, msg_id):
    try:
        now = datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")
        chan = CHANNEL_ID.lstrip("@")
        link = f"https://t.me/{chan}/{msg_id}" if msg_id else "—"
        text = (
            f"🏷 ربات: {BOT_NAME}\n"
            f"🕘 زمان (تهران): {now}\n"
            f"📰 منبع: {SOURCE_NAME}\n"
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

    # تازه‌ها (پست‌نشده + شبیهِ فکت‌چک)، و فقط جدیدترین‌ها
    fresh = [it for it in items
             if it["id"] not in posted_set and looks_like_factcheck(it["text"])]
    to_post = fresh[-MAX_POSTS_PER_RUN:]
    if not to_post:
        print("  ⛔ فکت‌چکِ تازه‌ای نبود.")
        save_state(state)
        return

    count = 0
    for it in to_post:
        body, model_label = ai_rewrite(it["text"])
        if body.strip() == "SKIP":
            print(f"  ⏭ رد شد (فکت‌چکِ مشخصی نبود): {it['id']}")
            posted.append(it["id"])          # دیگر پردازشش نکن
            continue
        caption = build_post(body, it)
        mid = send_message(caption)
        if mid:
            post_backup(it, model_label, mid)
            posted.append(it["id"])
            count += 1

    state["posted_ids"] = posted[-3000:]     # فقط ۳۰۰۰ تای آخر را نگه می‌داریم
    save_state(state)
    print(f"🏁 تمام شد. {count} هشدار منتشر شد.")


if __name__ == "__main__":
    main()
