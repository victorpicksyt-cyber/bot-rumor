#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات ۴ — «رادارِ شایعه» (رادیو بولتن)
روزی دو بار: تازه‌ترین فکت‌چک‌ها را از صفحه‌ی عمومیِ تلگرامِ فکت‌نامه می‌خوانَد،
هوش مصنوعی مهم‌ترین شایعه را انتخاب و تیتر+دلیل را با زبانِ خودش می‌نویسد، سپس:
  • اگر پست عکس داشت: عکسی را که واترمارک ندارد (با تشخیصِ تصویریِ هوش مصنوعی)
    انتخاب می‌کند، کیفیتش را کمی بهتر و مهرِ «شایعه»ی رادیو بولتن را رویش می‌زند و می‌فرستد.
    اگر هیچ عکسِ بی‌واترمارکی نبود → فقط متنی می‌فرستد.
  • اگر پست ویدیو داشت: ویدیو را سالم و کامل می‌فرستد؛ اگر از ۵۰ مگ بیشتر بود،
    چند فریم از همان ویدیو را به‌صورتِ عکس می‌فرستد.
هیچ واترمارکی پاک یا پوشانده نمی‌شود؛ فقط نسخه‌ی بی‌واترمارک انتخاب می‌شود.
"""

import os
import re
import sys
import json
import html
import base64
import subprocess
import requests
from io import BytesIO
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance

# ===================== تنظیمات =====================
BOT_NAME       = "رادار شایعه"
CHANNEL_ID     = "@testbotaii"
BACKUP_CHANNEL = "@analyzeAisTrb"
FOOTER         = "\n\n@RadioBulletin | رادیو بولتن"
SOURCE_URL     = "https://t.me/s/factnameh"
SOURCE_NAME    = "فکت‌نامه"
CANDIDATES     = 12          # چند شایعه‌ی تازه به هوش مصنوعی بدهیم تا مهم‌ترین را انتخاب کند (هر اجرا ۱ پست)
STAMP_PATH     = "stamp.png" # مهرِ «شایعه»ی رادیو بولتن (پس‌زمینه‌اش در کد شفاف می‌شود)
STAMP_WIDTH_RATIO = 0.34     # عرضِ مهر نسبت به عرضِ عکس
FRAME_COUNT    = 4           # تعداد فریم برای ویدیوهای بزرگ‌تر از ۵۰ مگ
MAX_WHOLE_VIDEO = 50 * 1024 * 1024     # تا این حجم، ویدیو کامل می‌رود؛ بیشتر → فریم‌ها
DOWNLOAD_CAP    = 120 * 1024 * 1024    # سقفِ دانلودِ ویدیو برای جلوگیری از مصرفِ بی‌رویه

# ===================== ثابت‌ها =====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")

AI_MODEL       = "openai/gpt-4.1"      # هم متن، هم دیدِ تصویری
AI_MODEL_CHAIN = [AI_MODEL, "openai/gpt-4o", "openai/gpt-4o-mini"]
AI_ENDPOINT    = "https://models.github.ai/inference/chat/completions"

STATE_FILE = "seen.json"
TEHRAN     = timezone(timedelta(hours=3, minutes=30))
TG_API     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
UA         = {"User-Agent": "Mozilla/5.0 (compatible; RadioBulletinBot/1.0)"}

FACTCHECK_HINTS = ("نادرست", "گمراه‌کننده", "شاخ‌دار", "نیمه‌درست", "بی‌اساس",
                   "جعلی", "ساختگی", "شایعه", "ادعا", "❌", "❓",
                   "در فکت‌نامه بخوانید")
SKIP_HINTS = ("پادکست", "مکتب‌خانه", "اپیزود")

_STAMP_CACHE = None


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


def extract_all_media(msg):
    """همه‌ی ویدیوها و عکس‌های یک پیام را برمی‌گرداند: (videos[], photos[])."""
    videos, photos = [], []
    for v in msg.select("video.tgme_widget_message_video"):
        if v.get("src"):
            videos.append(v["src"])
    for ph in msg.select("a.tgme_widget_message_photo_wrap"):
        u = _bg_url(ph.get("style"))
        if u:
            photos.append(u)
    if not videos and not photos:   # تصویرِ بندانگشتیِ ویدیو (اگر srcِ مستقیم نبود)
        for vt in msg.select(".tgme_widget_message_video_thumb"):
            u = _bg_url(vt.get("style"))
            if u:
                photos.append(u)
    # حذفِ تکراری‌ها با حفظِ ترتیب
    videos = list(dict.fromkeys(videos))
    photos = list(dict.fromkeys(photos))
    return videos, photos


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
        videos, photos = extract_all_media(m)
        items.append({"id": post, "text": text, "link": f"https://t.me/{post}",
                      "videos": videos, "photos": photos})
    return items  # قدیمی → جدید


def looks_like_factcheck(text):
    if len(text) < 100:
        return False
    if any(h in text for h in SKIP_HINTS):
        return False
    return any(h in text for h in FACTCHECK_HINTS)


# ===================== هوش مصنوعی =====================
def _call_ai(messages, temperature=0.5):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"}
    for m in AI_MODEL_CHAIN:
        try:
            payload = {"model": m, "messages": messages}
            if not m.startswith("openai/gpt-5"):
                payload["temperature"] = temperature
            resp = requests.post(AI_ENDPOINT, headers=headers, timeout=90, json=payload)
            if resp.status_code == 429:
                print(f"  ⏳ سقفِ {m} پر است؛ مدلِ بعدی...")
                continue
            resp.raise_for_status()
            txt = resp.json()["choices"][0]["message"]["content"].strip()
            if txt:
                if m != AI_MODEL:
                    print(f"  (با مدلِ پشتیبان: {m})")
                return txt, m
        except Exception as e:
            print(f"  ⚠️ خطای مدلِ {m}:", e)
            continue
    return None, AI_MODEL_CHAIN[-1]


def ai_select_and_write(cands):
    """همه را می‌خواند، مهم‌ترین را انتخاب می‌کند، تیتر+دلیل را خودش می‌نویسد."""
    lines = []
    for i, it in enumerate(cands):
        mt = "ویدیو" if it["videos"] else ("عکس" if it["photos"] else "متن")
        snippet = it["text"][:600].replace("\n", " ")
        lines.append(f"[{i}] (نوعِ مدیا: {mt}) {snippet}")
    listing = "\n\n".join(lines)
    system = (
        "You are the editor of an anti-misinformation Telegram channel writing in COLLOQUIAL "
        "PERSIAN. You receive several candidate fact-check items.\n"
        "STEP 1 — Read ALL candidates carefully. Choose the SINGLE most important / most serious "
        "rumor to warn people about. VERY IMPORTANT: only choose an item whose verdict is FALSE, "
        "misleading, baseless, fabricated, OUTDATED, or a rumor (نادرست / گمراه‌کننده / شاخ‌دار / "
        "نیمه‌درست / بی‌اساس / قدیمی / جعلی / ساختگی). NEVER choose an item that the fact-check "
        "confirms as TRUE / correct / verified (درست / تأییدشده) — a confirmed-true item is NOT a "
        "rumor. If you are not sure an item is actually false/misleading, do not pick it.\n"
        "STEP 2 — Write a fully ORIGINAL alert for the chosen item in YOUR OWN words. Do NOT copy "
        "the original headline or sentences; rephrase freshly and engagingly.\n"
        "Output EXACTLY this and nothing else:\n"
        "PICK: the index number (just the digit) of the chosen candidate\n"
        "TITLE: a Persian headline you write yourself. Decide intelligently what the rumor IS and "
        "start with the matching word: if it is a fake/old/misleading VIDEO → «ویدیویی که …»; if "
        "it is a fake/misleading PHOTO or image → «عکسی که …»; if it is a textual news claim → "
        "«خبری که …». Then clearly say it is a rumor and untrue — e.g. «ویدیویی که … شایعه است و "
        "واقعیت ندارد». Engaging, never a copy. Under 140 characters.\n"
        "WHY: in popular, easy, engaging Persian, explain FULLY and clearly why the claim is "
        "false or misleading — cover all the key points, in your own simple words (a few short "
        "lines). End with a final line «🔖 حکم:» followed by the verdict "
        "(نادرست / گمراه‌کننده / شاخ‌دار / نیمه‌درست / بی‌اساس / قدیمی) or the closest. Keep WHY under 600 chars.\n"
        "RULES: Never present the rumor as true. Do NOT mention or name ANY source, website, "
        "organization, channel or fact-checker anywhere (no «فکت‌نامه», no links, no «بخوانید»). "
        "Paraphrase; never copy verbatim. No hashtags.\n"
        "If NONE of the candidates is a genuine FALSE/misleading rumor (e.g. all are true or "
        "none is a specific claim), output exactly: SKIP\n"
        "Output only PICK/TITLE/WHY, or SKIP."
    )
    txt, model = _call_ai([{"role": "system", "content": system},
                           {"role": "user", "content": "Candidate items:\n\n" + listing}],
                          temperature=0.6)
    if not txt or txt.strip() == "SKIP":
        return None, None, None, model
    t = txt.strip()
    mp = re.search(r"PICK:\s*\[?\s*(\d+)\s*\]?", t)
    mt = re.search(r"TITLE:\s*(.*?)(?:\nWHY:|\Z)", t, re.S)
    mw = re.search(r"WHY:\s*(.*)\Z", t, re.S)
    if not mp:
        return None, None, None, model
    idx = int(mp.group(1))
    if idx < 0 or idx >= len(cands):
        return None, None, None, model
    title = mt.group(1).strip() if mt else ""
    why = mw.group(1).strip() if mw else ""
    if not title:
        return None, None, None, model
    return idx, title, why, model


# ===================== کارِ تصویر =====================
def _img_to_b64(raw, max_side=700):
    try:
        im = Image.open(BytesIO(raw)).convert("RGB")
        if max(im.size) > max_side:
            s = max_side / max(im.size)
            im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def looks_true_in_image(photo_url):
    """اگر روی تصویر آشکارا «درست/تأییدشده» خورده باشد True (یعنی شایعه نیست و نباید پخش شود)."""
    try:
        raw = requests.get(photo_url, headers=UA, timeout=30).content
    except Exception:
        return False
    b64 = _img_to_b64(raw)
    if not b64:
        return False
    content = [
        {"type": "text", "text":
         "این تصویر مربوط به یک خبرِ راستی‌آزمایی‌شده است. فقط به برچسب/مهرِ حکمِ روی تصویر نگاه کن. "
         "اگر آشکارا نشان می‌دهد خبر «درست» یا «تأییدشده» است (یعنی شایعه و نادرست نیست)، فقط بنویس TRUE. "
         "اگر حکم نادرست/گمراه‌کننده/قدیمی/جعلی/شایعه است یا نامشخص است، فقط بنویس RUMOR."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    txt, _ = _call_ai([{"role": "user", "content": content}], temperature=0.0)
    return bool(txt) and "TRUE" in txt.upper() and "RUMOR" not in txt.upper()

def load_stamp():
    global _STAMP_CACHE
    if _STAMP_CACHE is not None:
        return _STAMP_CACHE
    im = Image.open(STAMP_PATH).convert("RGBA")
    out = [(r, g, b, 0) if (r > 235 and g > 235 and b > 235) else (r, g, b, a)
           for (r, g, b, a) in im.getdata()]
    im.putdata(out)
    _STAMP_CACHE = im
    return im


def enhance(img):
    if max(img.size) < 1000:
        s = 1000 / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.05)
    return img


def make_banner(raw_bytes, out_path):
    base = Image.open(BytesIO(raw_bytes)).convert("RGB")
    base = enhance(base).convert("RGBA")
    W, H = base.size
    stamp = load_stamp()
    tw = max(120, int(W * STAMP_WIDTH_RATIO))
    ratio = tw / stamp.width
    stamp = stamp.resize((tw, int(stamp.height * ratio)), Image.LANCZOS)
    margin = int(W * 0.03)
    pos = (W - stamp.width - margin, margin)           # بالا-راست
    base.alpha_composite(stamp, pos)
    base.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


# ===================== دانلود/ویدیو =====================
def download_file(url, path, cap=DOWNLOAD_CAP):
    with requests.get(url, stream=True, timeout=180, headers=UA) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
                    if size > cap:
                        break
    return os.path.getsize(path)


def extract_frames(video_path, n=FRAME_COUNT):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path])
        dur = float(json.loads(out)["format"].get("duration", 0))
    except Exception:
        dur = 0
    paths = []
    DN = subprocess.DEVNULL
    for i in range(n):
        t = (dur * (i + 1) / (n + 1)) if dur > 0 else (i * 2 + 1)
        p = f"frame_{i}.jpg"
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
                            "-frames:v", "1", "-q:v", "3", p], stdout=DN, stderr=DN)
            if os.path.exists(p) and os.path.getsize(p) > 1000:
                paths.append(p)
        except Exception:
            continue
    return paths


# ===================== تلگرام =====================
def _msg_id(resp):
    try:
        j = resp.json()
        if j.get("ok"):
            res = j["result"]
            if isinstance(res, list):
                return res[0]["message_id"]
            return res["message_id"]
        print("  ❌ تلگرام:", resp.text[:300])
    except Exception:
        print("  ❌ پاسخِ نامعتبرِ تلگرام:", resp.text[:200])
    return None


def send_photo(path, caption):
    with open(path, "rb") as fh:
        r = requests.post(f"{TG_API}/sendPhoto", timeout=120,
                          data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                          files={"photo": (os.path.basename(path), fh, "image/jpeg")})
    return _msg_id(r)


def send_video(path, caption):
    with open(path, "rb") as fh:
        r = requests.post(f"{TG_API}/sendVideo", timeout=300,
                          data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML",
                                "supports_streaming": "true"},
                          files={"video": (os.path.basename(path), fh, "video/mp4")})
    return _msg_id(r)


def send_media_group(paths, caption):
    media, files, handles = [], {}, []
    for i, p in enumerate(paths[:10]):
        key = f"f{i}"
        fh = open(p, "rb")
        handles.append(fh)
        files[key] = (os.path.basename(p), fh, "image/jpeg")
        item = {"type": "photo", "media": f"attach://{key}"}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
    try:
        r = requests.post(f"{TG_API}/sendMediaGroup", timeout=300,
                          data={"chat_id": CHANNEL_ID, "media": json.dumps(media)}, files=files)
        return _msg_id(r)
    finally:
        for fh in handles:
            fh.close()


def send_message(text):
    r = requests.post(f"{TG_API}/sendMessage", timeout=30,
                      data={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
    return _msg_id(r)


def build_caption(title, why):
    title = html.escape(title.strip())[:200]
    why = html.escape(why.strip())[:760]
    out = f"<b>⚠️ {title}</b>"
    if why:
        out += f"\n\n<blockquote>{why}</blockquote>"
    out += FOOTER
    return out


def publish(title, why, item):
    caption = build_caption(title, why)

    # --- ویدیو: کامل بفرست؛ اگر بزرگ بود، فریم‌ها ---
    if item["videos"]:
        url = item["videos"][0]
        vpath = "video.mp4"
        try:
            sz = download_file(url, vpath)
            if sz <= MAX_WHOLE_VIDEO:
                print(f"  🎬 ویدیوی کامل ({sz // 1024 // 1024} مگ)")
                mid = send_video(vpath, caption)
                if mid:
                    return mid
            else:
                print(f"  🎞 ویدیو بزرگ است ({sz // 1024 // 1024} مگ)؛ فریم‌ها فرستاده می‌شوند.")
                frames = extract_frames(vpath)
                if frames:
                    mid = send_media_group(frames, caption)
                    if mid:
                        return mid
        except Exception as e:
            print("  ⚠️ کارِ ویدیو نشد، متنی می‌فرستم:", e)
        return send_message(caption)

    # --- عکس: نسخه‌ی اصل را با مهرِ شایعه بگذار (دست‌نخورده) ---
    if item["photos"]:
        try:
            raw = requests.get(item["photos"][0], headers=UA, timeout=30).content
            banner = make_banner(raw, "banner.jpg")
            print("  🖼 عکسِ اصل با مهرِ شایعه آماده شد.")
            mid = send_photo(banner, caption)
            if mid:
                return mid
        except Exception as e:
            print("  ⚠️ ساختِ بنر نشد، متنی می‌فرستم:", e)
        return send_message(caption)

    # --- بدونِ مدیا ---
    return send_message(caption)


def post_backup(item, model_label, msg_id, mode):
    try:
        now = datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")
        chan = CHANNEL_ID.lstrip("@")
        link = f"https://t.me/{chan}/{msg_id}" if msg_id else "—"
        text = (
            f"🏷 ربات: {BOT_NAME}\n"
            f"🕘 زمان (تهران): {now}\n"
            f"📰 منبع: {SOURCE_NAME}\n"
            f"🖼 حالت: {mode}\n"
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
    print(f"  📥 {len(items)} پست خوانده شد.")

    fresh = [it for it in items
             if it["id"] not in posted_set and looks_like_factcheck(it["text"])]
    if not fresh:
        print("  ⛔ شایعه‌ی تازه‌ای نبود.")
        save_state(state)
        return

    cands = fresh[-CANDIDATES:]
    print(f"  🔎 {len(cands)} شایعه‌ی تازه؛ مهم‌ترین انتخاب می‌شود...")
    idx, title, why, model_label = ai_select_and_write(cands)
    if idx is None:
        print("  ⏭ موردِ مناسبی نبود؛ اجرای بعدی دوباره بررسی می‌شود.")
        save_state(state)
        return

    chosen = cands[idx]
    mode = "ویدیو" if chosen["videos"] else ("عکس" if chosen["photos"] else "متن")
    print(f"  🎯 انتخاب: {chosen['id']} — {title[:60]}")

    # گاردِ تصویری: اگر روی عکس «درست/تأییدشده» خورده باشد، شایعه نیست → پخش نکن
    if chosen["photos"] and looks_true_in_image(chosen["photos"][0]):
        print("  🛑 این مورد روی تصویرش «درست/تأییدشده» خورده، نه شایعه؛ پخش نمی‌شود.")
        posted.append(chosen["id"])
        state["posted_ids"] = posted[-3000:]
        save_state(state)
        return

    mid = publish(title, why, chosen)
    if mid:
        post_backup(chosen, model_label, mid, mode)
        posted.append(chosen["id"])
        state["posted_ids"] = posted[-3000:]
        save_state(state)
        print("🏁 تمام شد. ۱ هشدار منتشر شد.")
    else:
        print("❌ ارسال ناموفق بود؛ چیزی ثبت نشد (اجرای بعدی دوباره تلاش می‌شود).")


if __name__ == "__main__":
    main()
