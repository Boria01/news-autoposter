import os
import time
import sqlite3
import re
import html
from datetime import datetime, timezone, timedelta
import feedparser
import requests

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.5-flash-lite"
CHANNEL_ID = "@glavnoe_chasa"

MSK_TZ = timezone(timedelta(hours=3))
WORK_START_HOUR = 9    # начало работы утром по МСК (09:00)
WORK_END_HOUR = 1      # конец работы ночью по МСК (с 01:00 до 09:00 — полный сон)
DIGEST_HOUR = 21       # час публикации вечерних итогов дня по МСК (21:00–21:59)

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
    "Жесткие правила:\n"
    "1. Запрещена любая вода, оценки, эпитеты, вводные слова и домыслы. Оставляй ТОЛЬКО конкретные факты из текста: цифры, даты, имена, должности, города и действия.\n"
    "2. Убирай канцелярит (слова «в течение прошедшего времени», «осуществляется», «данный», «в рамках»).\n"
    "3. Формат:\n"
    "Первая строка — точный заголовок без кавычек.\n"
    "Пустая строка — 1 короткое предложение с главным событием.\n"
    "Пустая строка — 2–4 коротких пункта с деталями через символ «• ».\n"
    "4. Длина всего текста: строго от 350 до 750 символов. Никакого Markdown (** или #) и HTML-тегов."
)

DIGEST_PROMPT = (
    "Ты — редактор новостного Telegram-канала. На основе списка опубликованных за день заголовков и фактов составь вечернюю сводку «Итоги дня».\n"
    "Правила:\n"
    "1. Выбери 5–6 самых значимых событий из переданного списка (не придумывай ничего от себя).\n"
    "2. Оформи каждое событие отдельным пунктом с новой строки, начиная с подходящего строгого эмодзи (например: 🔹, ⚡️, 🏛, 📈, 🛡) и короткого заголовка, затем 1 лаконичное предложение с сутью.\n"
    "3. Без воды, без приветствий и прощаний, без Markdown (** или #) и без HTML-тегов.\n"
    "4. Общая длина текста: до 1400 символов."
)
# =============================================


def init_db():
    conn = sqlite3.connect("news_history.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posted_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT UNIQUE,
            title TEXT,
            keywords TEXT,
            published_at REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_digests (
            date_key TEXT PRIMARY KEY,
            published_at REAL
        )
    """)
    conn.commit()
    conn.close()


def extract_keywords(text):
    words = re.findall(r'[а-яёa-z0-9]{3,}', text.lower())
    return {w[:6] for w in words if w not in IGNORE_WORDS}


def calculate_similarity(set1, set2):
    if not set1 or not set2:
        return 0.0
    intersection = len(set1.intersection(set2))
    min_len = min(len(set1), len(set2))
    return intersection / min_len if min_len > 0 else 0.0


def is_duplicate_in_db(link, keywords):
    conn = sqlite3.connect("news_history.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM posted_news WHERE link = ?", (link,))
    if cursor.fetchone():
        conn.close()
        return True

    cutoff_time = time.time() - 86400
    cursor.execute("SELECT keywords FROM posted_news WHERE published_at > ?", (cutoff_time,))
    rows = cursor.fetchall()
    conn.close()

    for (saved_kw_str,) in rows:
        if not saved_kw_str:
            continue
        saved_kw = set(saved_kw_str.split(","))
        if calculate_similarity(keywords, saved_kw) >= 0.45:
            return True
    return False


def mark_as_posted(link, title, keywords):
    conn = sqlite3.connect("news_history.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO posted_news (link, title, keywords, published_at) VALUES (?, ?, ?, ?)",
        (link, title, ",".join(keywords), time.time())
    )
    conn.commit()
    conn.close()


def call_gemini(system_prompt, user_text, max_tokens=500):
    if not GEMINI_API_KEY:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": user_text}]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code != 200:
            print(f"Ошибка Gemini HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        result = "".join(p.get("text", "") for p in parts).strip()
        result = re.sub(r'\*\*(.*?)\*\*', r'\1', result)
        return result
    except Exception as e:
        print(f"Исключение Gemini: {e}")
        return None


def send_text_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки текста: {e}")
        return False


def try_publish_evening_digest(now_msk):
    if now_msk.hour != DIGEST_HOUR or not GEMINI_API_KEY:
        return False

    date_key = now_msk.strftime("%Y-%m-%d")
    conn = sqlite3.connect("news_history.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM daily_digests WHERE date_key = ?", (date_key,))
    if cursor.fetchone():
        conn.close()
        return False

    cutoff = time.time() - 16 * 3600
    cursor.execute("SELECT title FROM posted_news WHERE published_at > ? ORDER BY id ASC", (cutoff,))
    titles = [row[0] for row in cursor.fetchall() if row[0]]
    conn.close()

    if len(titles) < 4:
        return False

    titles_block = "\n".join(f"- {t}" for t in titles[:25])
    digest_body = call_gemini(DIGEST_PROMPT, f"Новости за день:\n{titles_block}", max_tokens=650)
    if not digest_body or len(digest_body) < 200:
        return False

    date_label = f"{now_msk.day} {MONTHS_RU[now_msk.month]} {now_msk.year}"
    header = f"🗞 <b>{date_label} — итоги дня</b>"
    escaped_body = html.escape(digest_body)
    full_digest = f"{header}\n\n{escaped_body}"[:4090]

    if send_text_message(full_digest):
        conn = sqlite3.connect("news_history.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO daily_digests (date_key, published_at) VALUES (?, ?)",
            (date_key, time.time())
        )
        conn.commit()
        conn.close()
        print(f"[{now_msk.strftime('%H:%M')} МСК] Опубликована вечерняя сводка «Итоги дня».")
        return True
    return False


def clean_text(raw_html):
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = re.sub(r'<.*?>', '', text)
    text = " ".join(text.split())

    text = re.sub(
        r'^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+[\.,]\s*\d+\s+[а-яё]+\.?\s*[—–\-]\s*(?:INTERFAX\.RU|Интерфакс|РИА Новости|ТАСС|РБК|ПРАЙМ|ИА REGNUM)\s*[—–\-\.]\s*',
        '',
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r'^(?:INTERFAX\.RU|Интерфакс|ТАСС|РИА Новости|РБК|Известия|Ведомости|Коммерсантъ|Лента\.ру|Газета\.Ru)\s*[:\-—–]\s*',
        '',
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(r'©.*$', '', text)
    text = re.sub(
        r'(?:\s*\.\.\.)?\s*(?:Читать далее|Подробнее|Читайте также|Полный текст).*$',
        '',
        text,
        flags=re.IGNORECASE
    )

    footer_markers = (
        "автономная некоммерческая организация",
        "«тв-новости»",
        "главный редактор:",
        "политика ано",
        "организации, признанные экстремистскими",
        "вся информация, размещенная на данном веб-сайте",
        "сетевое издание",
        "свидетельство о регистрации",
        "все права защищены",
        "нашли опечатку?",
        "читайте также:",
        "подписывайтесь на наш"
    )
    for marker in footer_markers:
        idx = text.lower().find(marker)
        if idx != -1:
            text = text[:idx]

    return " ".join(text.split())


def is_bad_content(link, title, summary):
    link_lower = link.lower()
    if any(part in link_lower for part in BAD_URL_PARTS):
        return True
    full_lower = (title + " " + summary).lower()
    return any(word in full_lower for word in STOP_WORDS)


def is_day_mode():
    now_msk = datetime.now(MSK_TZ)
    hour = now_msk.hour
    if WORK_START_HOUR > WORK_END_HOUR:
        return hour >= WORK_START_HOUR or hour < WORK_END_HOUR
    return WORK_START_HOUR <= hour < WORK_END_HOUR


def extract_clean_rss_image(entry):
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        enc_type = enc.get("type", "")
        if href and ("image" in enc_type or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url:
            return url
    return None


def decode_html_properly(resp):
    content_type = resp.headers.get("Content-Type", "").lower()
    if "windows-1251" in content_type or "cp1251" in content_type:
        return resp.content.decode("windows-1251", errors="replace")
    if "utf-8" in content_type:
        return resp.content.decode("utf-8", errors="replace")

    head_sample = resp.content[:2048].lower()
    if b"windows-1251" in head_sample or b"cp1251" in head_sample:
        return resp.content.decode("windows-1251", errors="replace")

    if resp.apparent_encoding:
        try:
            return resp.content.decode(resp.apparent_encoding, errors="replace")
        except Exception:
            pass
    return resp.content.decode("utf-8", errors="replace")


def extract_rss_full_text(entry):
    raw_full = ""
    if "content" in entry and entry["content"]:
        raw_full = entry["content"][0].get("value", "")
    if not raw_full:
        raw_full = entry.get("yandex_full-text", "") or entry.get("full-text", "")
    if not raw_full:
        return []

    parts = re.split(r'</p>|<br\s*/?>|\n+', raw_full, flags=re.IGNORECASE)
    paras = []
    for p in parts:
        cleaned = clean_text(p)
        if len(cleaned) >= 40 and cleaned not in paras:
            paras.append(cleaned)
        if len(paras) >= 5:
            break
    return paras


def fetch_raw_article_text(entry, url, title):
    rss_paras = extract_rss_full_text(entry)
    if len(rss_paras) >= 2:
        return "\n".join(rss_paras)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9"
        }
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            raw_html = decode_html_properly(resp)
            html_body = re.sub(
                r'<(script|style|header|footer|nav|aside)[^>]*>.*?</\1>',
                '',
                raw_html,
                flags=re.DOTALL | re.IGNORECASE
            )
            cut_patterns = [
                r'<div[^>]+class="[^"]*(?:related|read-more|also-read|recommend|article__footer|social|comments)[^"]*"',
                r'<section[^>]+class="[^"]*(?:related|read-more|also-read|recommend)[^"]*"'
            ]
            for pat in cut_patterns:
                m = re.search(pat, html_body, flags=re.IGNORECASE)
                if m and m.start() > 500:
                    html_body = html_body[:m.start()]

            raw_paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_body, flags=re.DOTALL | re.IGNORECASE)
            good_paragraphs = []
            junk_phrases = (
                "читайте также", "подписывайтесь", "фото:", "источник:", "риа новости",
                "тасс", "интерфакс", "interfax", "рбк", "все права защищены",
                "автономная некоммерческая организация", "«тв-новости»", "тв-новости",
                "сетевое издание", "свидетельство о регистрации", "©"
            )
            for p in raw_paragraphs:
                cleaned = clean_text(p)
                if len(cleaned) < 40 or "\ufffd" in cleaned:
                    continue
                if any(jp in cleaned.lower() for jp in junk_phrases):
                    continue
                if cleaned.lower() == title.lower() or cleaned in good_paragraphs:
                    continue
                good_paragraphs.append(cleaned)
                if len(good_paragraphs) >= 5:
                    break

            if len(good_paragraphs) >= 2:
                return "\n".join(good_paragraphs)
    except Exception:
        pass

    return ""


def format_post_html(title, raw_body):
    ai_text = call_gemini(
        EDITOR_PROMPT,
        f"Заголовок: {title}\n\nТекст новости:\n{raw_body[:3000]}",
        max_tokens=450
    )
    if ai_text and len(ai_text) >= 180:
        lines = [line.strip() for line in ai_text.splitlines() if line.strip()]
        if len(lines) >= 2:
            head = f"<b>{html.escape(lines[0])}</b>"
            body = "\n\n".join(html.escape(line) for line in lines[1:])
            formatted = f"{head}\n\n{body}"
            if len(formatted) <= 1010:
                return formatted

    paras = [p.strip() for p in raw_body.splitlines() if p.strip()]
    head = f"<b>{html.escape(title)}</b>"
    body = "\n\n".join(html.escape(p) for p in paras[:3])
    return f"{head}\n\n{body}"


def send_photo_post(caption_html, image_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    if len(caption_html) > 1015:
        cut = caption_html[:1012]
        last_space = cut.rfind(" ")
        if last_space > 500:
            cut = cut[:last_space]
        caption_html = cut + "..."

    payload = {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": caption_html,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        img_resp = requests.get(image_url, timeout=10)
        if img_resp.status_code == 200:
            files = {"photo": ("image.jpg", img_resp.content, "image/jpeg")}
            data = {"chat_id": CHANNEL_ID, "caption": caption_html, "parse_mode": "HTML"}
            up_r = requests.post(url, data=data, files=files, timeout=25)
            return up_r.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки фото: {e}")
    return False


def publish_top_news():
    now_msk = datetime.now(MSK_TZ)

    if not is_day_mode():
        print(f"[{now_msk.strftime('%H:%M')} МСК] Ночной режим (сон до {WORK_START_HOUR:02d}:00 МСК). Пропуск.")
        return

    # В 21:00–21:59 МСК один раз выпускаем вечернюю сводку «Итоги дня»
    if try_publish_evening_digest(now_msk):
        return

    candidates = []

    for feed_info in RSS_FEEDS:
        source_name = feed_info["name"]
        feed_url = feed_info["url"]
        base_weight = feed_info["weight"]

        try:
            rss_resp = requests.get(feed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if rss_resp.status_code != 200:
                continue
            feed = feedparser.parse(rss_resp.content)
        except Exception:
            continue

        for entry in feed.entries[:15]:
            link = entry.get("link", "").strip()
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))

            if not link or not title or "\ufffd" in title or "\ufffd" in summary:
                continue
            if is_bad_content(link, title, summary):
                continue
            if len(summary) < 40 or summary.lower() == title.lower():
                continue

            keywords = extract_keywords(title)
            if len(keywords) < 2 or is_duplicate_in_db(link, keywords):
                continue

            full_text_lower = (title + " " + summary).lower()
            breaking_bonus = 4 if any(w in full_text_lower for w in BREAKING_WORDS) else 0
            image_url = extract_clean_rss_image(entry)

            candidates.append({
                "entry": entry,
                "source": source_name,
                "link": link,
                "title": title,
                "summary": summary,
                "image_url": image_url,
                "keywords": keywords,
                "score": base_weight + breaking_bonus
            })

    if not candidates:
        print("Подходящих свежих новостей без повторов не найдено.")
        return

    for i in range(len(candidates)):
        for j in range(len(candidates)):
            if i != j and candidates[i]["source"] != candidates[j]["source"]:
                if calculate_similarity(candidates[i]["keywords"], candidates[j]["keywords"]) >= 0.45:
                    candidates[i]["score"] += 5

    candidates.sort(key=lambda x: x["score"], reverse=True)
    now_str = now_msk.strftime("%H:%M")

    for best_news in candidates[:15]:
        image_url = best_news["image_url"]
        if not image_url:
            continue

        raw_body = fetch_raw_article_text(
            best_news["entry"],
            best_news["link"],
            best_news["title"]
        )

        if len(raw_body) < 250:
            print(f"Пропуск (полный те
