import os
import time
import sqlite3
import re
import html
from datetime import datetime, timezone, timedelta
import feedparser
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.5-flash-lite"
CHANNEL_ID = "@glavnoe_chasa"

MSK_TZ = timezone(timedelta(hours=3))
WORK_START_HOUR = 7
WORK_END_HOUR = 1
DIGEST_HOUR = 21

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}

RSS_FEEDS = [
    {"name": "ТАСС", "url": "https://tass.ru/rss/v2.xml", "weight": 3},
    {"name": "РИА Новости", "url": "https://ria.ru/export/rss2/archive/index.xml", "weight": 3},
    {"name": "РБК", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "weight": 3},
    {"name": "Интерфакс", "url": "https://www.interfax.ru/rss.asp", "weight": 3},
    {"name": "Коммерсантъ", "url": "https://www.kommersant.ru/RSS/news.xml", "weight": 3},
    {"name": "Ведомости", "url": "https://www.vedomosti.ru/rss/news", "weight": 2},
    {"name": "Forbes Russia", "url": "https://www.forbes.ru/newrss.xml", "weight": 2},
    {"name": "Известия", "url": "https://iz.ru/xml/rss/all.xml", "weight": 2},
    {"name": "Газета.Ru", "url": "https://www.gazeta.ru/export/rss/lenta.xml", "weight": 1},
    {"name": "Лента.ру", "url": "https://lenta.ru/rss/news", "weight": 1},
    {"name": "RT на русском", "url": "https://russian.rt.com/rss", "weight": 2}
]

BAD_URL_PARTS = ["/sport/", "rsport.", "/culture/", "/showbiz/", "/lifestyle/", "/lady/", "/afisha/"]

STOP_WORDS = [
    "гороскоп", "погода", "реклама", "промокод", "ставки", "казино", "тест:", "видео:",
    "матч", "сборн", "чемпионат", "лига ", "лиги ", "гол ", "голов", "тренер",
    "футбол", "хокке", "теннис", "турнир", "олимпи", "бокс", "ufc", "мма",
    "кхл", "нхл", "рпл", "уефа", "фифа", "медал", "актер", "актрис", "певиц",
    "певец", "сериал", "шоу-", "свадьб", "развод"
]

BREAKING_WORDS = [
    "срочно", "молния", "экстренно", "чп", "чс", "режим чс", "погиб", "пострадал",
    "жертв", "взрыв", "пожар", "возгорание", "авари", "катастроф", "крушени",
    "обруш", "эвакуац", "теракт", "заложник", "стрельб", "атак", "бпла",
    "беспилотник", "пво", "сбили", "обстрел", "диверси", "землетрясен", "наводнен"
]

IGNORE_WORDS = {"в", "на", "и", "с", "по", "о", "об", "из", "за", "к", "от", "для", "не", "что", "как", "при", "до", "после"}

EDITOR_PROMPT = (
    "Ты — выпускающий редактор новостной ленты. Сожми исходную новость в плотный, читаемый пост для Telegram.\n"
    "Правила:\n"
    "1. Запрещена любая вода, оценки, эпитеты и домыслы. Оставляй ТОЛЬКО конкретные факты: цифры, даты, имена, должности, города и действия.\n"
    "2. Убирай канцелярит.\n"
    "3. Формат:\n"
    "Первая строка — точный заголовок без кавычек.\n"
    "Пустая строка — 1 короткое предложение с главным событием.\n"
    "Пустая строка — 2–4 коротких пункта с деталями через символ «• » (если исходный текст короткий — оформи 1–2 абзаца без воды).\n"
    "4. Длина всего текста: от 250 до 750 символов. Никакого Markdown (** или #) и HTML-тегов."
)

DIGEST_PROMPT = (
    "Ты — редактор новостного Telegram-канала. На основе списка опубликованных за день заголовков составь вечернюю сводку «Итоги дня».\n"
    "Правила:\n"
    "1. Выбери 5–6 самых значимых событий из списка (ничего не выдумывай).\n"
    "2. Оформи каждое событие отдельным пунктом с новой строки, начиная с строгого эмодзи (🔹, ⚡️, 🏛, 📈, 🛡) и короткого заголовка, затем 1 лаконичное предложение с сутью.\n"
    "3. Без воды, без приветствий и прощаний, без Markdown (** или #) и без HTML-тегов. Длина: до 1400 символов."
)


def init_db():
    conn = sqlite3.connect("news_history.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS posted_news (id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT UNIQUE, title TEXT, keywords TEXT, published_at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS daily_digests (date_key TEXT PRIMARY KEY, published_at REAL)")
    conn.commit()
    conn.close()


def extract_keywords(text):
    words = re.findall(r'[а-яёa-z0-9]{3,}', text.lower())
    return {w[:6] for w in words if w not in IGNORE_WORDS}


def calculate_similarity(s1, s2):
    if not s1 or not s2:
        return 0.0
    m = min(len(s1), len(s2))
    return len(s1.intersection(s2)) / m if m > 0 else 0.0


def is_duplicate_in_db(link, keywords):
    conn = sqlite3.connect("news_history.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM posted_news WHERE link = ?", (link,))
    if c.fetchone():
        conn.close()
        return True
    c.execute("SELECT keywords FROM posted_news WHERE published_at > ?", (time.time() - 86400,))
    rows = c.fetchall()
    conn.close()
    for (kw_str,) in rows:
        if kw_str and calculate_similarity(keywords, set(kw_str.split(","))) >= 0.45:
            return True
    return False


def mark_as_posted(link, title, keywords):
    conn = sqlite3.connect("news_history.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_news (link, title, keywords, published_at) VALUES (?, ?, ?, ?)", (link, title, ",".join(keywords), time.time()))
    conn.commit()
    conn.close()


def call_gemini(system_prompt, user_text, max_tokens=500):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            print(f"Ошибка Gemini HTTP {r.status_code}: {r.text[:150]}")
            return None
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        res = "".join(p.get("text", "") for p in parts).strip()
        return re.sub(r'\*\*(.*?)\*\*', r'\1', res)
    except Exception as e:
        print(f"Исключение Gemini: {e}")
        return None


def send_text_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"Ошибка текста: {e}")
        return False


def try_publish_evening_digest(now_msk):
    if now_msk.hour != DIGEST_HOUR or not GEMINI_API_KEY:
        return False
    date_key = now_msk.strftime("%Y-%m-%d")
    conn = sqlite3.connect("news_history.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_digests WHERE date_key = ?", (date_key,))
    if c.fetchone():
        conn.close()
        return False
    c.execute("SELECT title FROM posted_news WHERE published_at > ? ORDER BY id ASC", (time.time() - 57600,))
    titles = [row[0] for row in c.fetchall() if row[0]]
    conn.close()
    if len(titles) < 4:
        return False
    body = call_gemini(DIGEST_PROMPT, "Новости за день:\n" + "\n".join(f"- {t}" for t in titles[:25]), max_tokens=650)
    if not body or len(body) < 200:
        return False
    date_label = f"{now_msk.day} {MONTHS_RU[now_msk.month]} {now_msk.year}"
    full_digest = f"🗞 <b>{date_label} — итоги дня</b>\n\n{html.escape(body)}"[:4090]
    if send_text_message(full_digest):
        conn = sqlite3.connect("news_history.db")
        conn.execute("INSERT OR IGNORE INTO daily_digests (date_key, published_at) VALUES (?, ?)", (date_key, time.time()))
        conn.commit()
        conn.close()
        print(f"[{now_msk.strftime('%H:%M')} МСК] Опубликованы Итоги дня.")
        return True
    return False


def clean_text(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r'<.*?>', '', html.unescape(raw_html))
    text = " ".join(text.split())
    text = re.sub(r'^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+[\.,]\s*\d+\s+[а-яё]+\.?\s*[—–\-]\s*(?:INTERFAX\.RU|Интерфакс|РИА Новости|ТАСС|РБК|ПРАЙМ|ИА REGNUM)\s*[—–\-\.]\s*', '', text, flags=re.I)
    text = re.sub(r'^(?:INTERFAX\.RU|Интерфакс|ТАСС|РИА Новости|РБК|Известия|Ведомости|Коммерсантъ|Лента\.ру|Газета\.Ru)\s*[:\-—–]\s*', '', text, flags=re.I)
    text = re.sub(r'©.*$', '', text)
    text = re.sub(r'(?:\s*\.\.\.)?\s*(?:Читать далее|Подробнее|Читайте также|Полный текст).*$', '', text, flags=re.I)
    for m in ("автономная некоммерческая организация", "«тв-новости»", "главный редактор:", "политика ано", "организации, признанные экстремистскими", "вся информация, размещенная на данном", "сетевое издание", "свидетельство о регистрации", "все права защищены", "нашли опечатку?", "читайте также:", "подписывайтесь на наш"):
        idx = text.lower().find(m)
        if idx != -1:
            text = text[:idx]
    return " ".join(text.split())


def is_bad_content(link, title, summary):
    if any(p in link.lower() for p in BAD_URL_PARTS):
        return True
    full = (title + " " + summary).lower()
    return any(w in full for w in STOP_WORDS)


def is_day_mode():
    h = datetime.now(MSK_TZ).hour
    return (h >= WORK_START_HOUR or h < WORK_END_HOUR) if WORK_START_HOUR > WORK_END_HOUR else (WORK_START_HOUR <= h < WORK_END_HOUR)


def extract_clean_rss_image(entry):
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        if href and ("image" in enc.get("type", "") or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href
    for m in entry.get("media_content", []):
        if m.get("url"):
            return m["url"]
    return None


def decode_html_properly(resp):
    ct = resp.headers.get("Content-Type", "").lower()
    if "windows-1251" in ct or "cp1251" in ct or b"windows-1251" in resp.content[:2048].lower():
        return resp.content.decode("windows-1251", errors="replace")
    return resp.content.decode("utf-8", errors="replace")


def fetch_raw_article_text(entry, url, title):
    raw_full = entry["content"][0].get("value", "") if entry.get("content") else (entry.get("yandex_full-text", "") or entry.get("full-text", ""))
    if raw_full:
        paras = [clean_text(p) for p in re.split(r'</p>|<br\s*/?>|\n+', raw_full, flags=re.I) if len(clean_text(p)) >= 40]
        if len(paras) >= 2:
            return "\n".join(paras[:5])
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}, timeout=12)
        if resp.status_code == 200:
            body = re.sub(r'<(script|style|header|footer|nav|aside)[^>]*>.*?</\1>', '', decode_html_properly(resp), flags=re.S | re.I)
            for pat in (r'<div[^>]+class="[^"]*(?:related|read-more|also-read|recommend|article__footer|social|comments)[^"]*"', r'<section[^>]+class="[^"]*(?:related|read-more|also-read|recommend)[^"]*"'):
                m = re.search(pat, body, flags=re.I)
                if m and m.start() > 500:
                    body = body[:m.start()]
            junk = ("читайте также", "подписывайтесь", "фото:", "источник:", "риа новости", "тасс", "интерфакс", "interfax", "рбк", "все права защищены", "автономная некоммерческая организация", "«тв-новости»", "тв-новости", "сетевое издание", "свидетельство о регистрации", "©")
            good = []
            for p in re.findall(r'<p[^>]*>(.*?)</p>', body, flags=re.S | re.I):
                c = clean_text(p)
                if len(c) >= 40 and "\ufffd" not in c and c.lower() != title.lower() and c not in good and not any(j in c.lower() for j in junk):
                    good.append(c)
                if len(good) >= 5:
                    break
            if len(good) >= 2:
                return "\n".join(good)
    except Exception:
        pass
    return ""


def format_post_html(title, raw_body):
    ai = call_gemini(EDITOR_PROMPT, f"Заголовок: {title}\n\nТекст новости:\n{raw_body[:3000]}", max_tokens=450)
    if ai and len(ai) >= 140:
        lines = [l.strip() for l in ai.splitlines() if l.strip()]
        if len(lines) >= 2:
            out = f"<b>{html.escape(lines[0])}</b>\n\n" + "\n\n".join(html.escape(l) for l in lines[1:])
            if len(out) <= 1010:
                return out
    paras = [p.strip() for p in raw_body.splitlines() if p.strip()]
    return f"<b>{html.escape(title)}</b>\n\n" + "\n\n".join(html.escape(p) for p in paras[:3])


def send_photo_post(caption_html, image_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    if len(caption_html) > 1015:
        cut = caption_html[:1012]
        sp = cut.rfind(" ")
        caption_html = (cut[:sp] if sp > 500 else cut) + "..."
    try:
        r = requests.post(url, json={"chat_id": CHANNEL_ID, "photo": image_url, "caption": caption_html, "parse_mode": "HTML"}, timeout=20)
        if r.status_code == 200:
            return True
        img = requests.get(image_url, timeout=10)
        if img.status_code == 200:
            up = requests.post(url, data={"chat_id": CHANNEL_ID, "caption": caption_html, "parse_mode": "HTML"}, files={"photo": ("image.jpg", img.content, "image/jpeg")}, timeout=25)
            return up.status_code == 200
    except Exception as e:
        print(f"Ошибка фото: {e}")
    return False


def publish_top_news():
    now_msk = datetime.now(MSK_TZ)
    if not is_day_mode():
        print(f"[{now_msk.strftime('%H:%M')} МСК] Ночной режим (сон до {WORK_START_HOUR:02d}:00 МСК).")
        return
    if try_publish_evening_digest(now_msk):
        return
    candidates = []
    for f in RSS_FEEDS:
        try:
            r = requests.get(f["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        for e in feed.entries[:15]:
            link = e.get("link", "").strip()
            title = clean_text(e.get("title", ""))
            summary = clean_text(e.get("summary", e.get("description", "")))
            if not link or not title or "\ufffd" in title or "\ufffd" in summary or is_bad_content(link, title, summary):
                continue
            if len(summary) < 40 or summary.lower() == title.lower():
                continue
            kw = extract_keywords(title)
            if len(kw) < 2 or is_duplicate_in_db(link, kw):
                continue
            bonus = 4 if any(w in (title + " " + summary).lower() for w in BREAKING_WORDS) else 0
            candidates.append({"entry": e, "source": f["name"], "link": link, "title": title, "summary": summary, "image_url": extract_clean_rss_image(e), "keywords": kw, "score": f["weight"] + bonus})
    if not candidates:
        print("Свежих новостей без повторов не найдено.")
        return
    for i in range(len(candidates)):
        for j in range(len(candidates)):
            if i != j and candidates[i]["source"] != candidates[j]["source"] and calculate_similarity(candidates[i]["keywords"], candidates[j]["keywords"]) >= 0.45:
                candidates[i]["score"] += 5
    candidates.sort(key=lambda x: x["score"], reverse=True)
    for best in candidates[:30]:
        if not best["image_url"]:
            continue
        raw_body = fetch_raw_article_text(best["entry"], best["link"], best["title"])
        if len(raw_body) < 120:
            raw_body = best["summary"]
        if len(raw_body) < 110:
            print(f"Пропуск (слишком короткий текст): {best['title']}")
            continue
        post_html = format_post_html(best["title"], raw_body)
        if send_photo_post(post_html, best["image_url"]):
            print(f"[{now_msk.strftime('%H:%M')} МСК] Опубликовано ({len(post_html)} зн.): {best['title']}")
            mark_as_posted(best["link"], best["title"], best["keywords"])
            return


if __name__ == "__main__":
    init_db()
    publish_top_news()
    
