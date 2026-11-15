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
CHANNEL_ID = "@glavnoe_chasa"

MSK_TZ = timezone(timedelta(hours=3))
WORK_START_HOUR = 9   # со скольки утра по МСК начинать постить (например, с 09:00)
WORK_END_HOUR = 1     # до скольки ночи по МСК постить (с 01:00 до 09:00 — полный сон)

RSS_FEEDS = [
    # Федеральная база (высший приоритет)
    {"name": "ТАСС", "url": "https://tass.ru/rss/v2.xml", "weight": 3},
    {"name": "РИА Новости", "url": "https://ria.ru/export/rss2/archive/index.xml", "weight": 3},
    {"name": "РБК", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "weight": 3},
    {"name": "Интерфакс", "url": "https://www.interfax.ru/rss.asp", "weight": 3},

    # Деловая и аналитическая пресса (эксклюзивы, законы, экономика)
    {"name": "Коммерсантъ", "url": "https://www.kommersant.ru/RSS/news.xml", "weight": 3},
    {"name": "Ведомости", "url": "https://www.vedomosti.ru/rss/news", "weight": 2},
    {"name": "Forbes Russia", "url": "https://www.forbes.ru/newrss.xml", "weight": 2},

    # Оперативные новости, происшествия и резонанс
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


def clean_text(raw_html):
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = re.sub(r'<.*?>', '', text)
    text = " ".join(text.split())

    # 1. Срезаем стандартные информагентства в начале строки
    text = re.sub(
        r'^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+[\.,]\s*\d+\s+[а-яё]+\.?\s*[—–\-]\s*(?:INTERFAX\.RU|Интерфакс|РИА Новости|ТАСС|РБК|ПРАЙМ|ИА REGNUM)\s*[—–\-\.]\s*',
        '',
        text,
        flags=re.IGNORECASE
    )
    # 2. Срезаем прямые префиксы в начале
    text = re.sub(
        r'^(?:INTERFAX\.RU|Интерфакс|ТАСС|РИА Новости|РБК|Известия|Ведомости|Коммерсантъ|Лента\.ру|Газета\.Ru)\s*[:\-—–]\s*',
        '',
        text,
        flags=re.IGNORECASE
    )

    # 3. Отрезаем любые знаки копирайта © до конца строки
    text = re.sub(r'©.*$', '', text)

    # 4. Обрезаем подвальные маркеры, если они прилипли в конец абзаца
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


def fetch_full_article_paragraphs(url, fallback_summary, title):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return [fallback_summary] if fallback_summary else []

        raw_html = decode_html_properly(resp)
        html_body = re.sub(
            r'<(script|style|header|footer|nav|aside)[^>]*>.*?</\1>',
            '',
            raw_html,
            flags=re.DOTALL | re.IGNORECASE
        )

        # Отрезаем хвост страницы с блоками "Читайте также / Похожие новости / Подвал статьи"
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
            "тасс", "интерфакс", "interfax", "рбк", "все права защищены", "использование материалов",
            "зарегистрировано", "роскомнадзор", "cookie", "нашли опечатку",
            "материал дополняется", "подробнее в нашем", "наш телеграм",
            "главный редактор:", "адрес редакции:", "политика ано",
            "автономная некоммерческая организация", "«тв-новости»", "тв-новости",
            "организации, признанные экстремистскими", "обработки персональных данных",
            "сетевое издание", "учредитель:", "свидетельство о регистрации",
            "вся информация, размещенная на данном", "перепечатка материалов",
            "редакция не несет ответственности", "сообщает корреспондент", "©"
        )

        total_len = 0
        for p in raw_paragraphs:
            cleaned = clean_text(p)
            if len(cleaned) < 45:
                continue
            low = cleaned.lower()
            if any(jp in low for jp in junk_phrases):
                continue
            if "\ufffd" in cleaned:
                continue
            if cleaned.lower() == title.lower():
                continue
            if cleaned in good_paragraphs:
                continue

            # Берём максимум 4 основных абзаца статьи, чтобы не захватить чужие врезки снизу
            if len(good_paragraphs) >= 4 or total_len + len(cleaned) > 1800:
                break
            good_paragraphs.append(cleaned)
            total_len += len(cleaned) + 2

        if good_paragraphs:
            return good_paragraphs
    except Exception:
        pass

    return [fallback_summary] if fallback_summary else []


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


def send_photo_request(caption_html, image_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": caption_html,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return "OK"
        if "caption is too long" in r.text.lower():
            return "TOO_LONG"
        img_resp = requests.get(image_url, timeout=10)
        if img_resp.status_code == 200:
            files = {"photo": ("image.jpg", img_resp.content, "image/jpeg")}
            data = {"chat_id": CHANNEL_ID, "caption": caption_html, "parse_mode": "HTML"}
            up_r = requests.post(url, data=data, files=files, timeout=25)
            if up_r.status_code == 200:
                return "OK"
            if "caption is too long" in up_r.text.lower():
                return "TOO_LONG"
    except Exception as e:
        print(f"Ошибка при отправке фото: {e}")
    return "FAIL"


def publish_full_article(title, paragraphs, image_url=None):
    title_escaped = f"<b>{html.escape(title)}</b>"
    escaped_paras = [html.escape(p) for p in paragraphs if p.lower() != title.lower()]

    full_post = title_escaped
    if escaped_paras:
        full_post += "\n\n" + "\n\n".join(escaped_paras)

    if not image_url:
        return send_text_message(full_post[:4090])

    if len(full_post) <= 1020:
        return send_photo_request(full_post, image_url) == "OK"

    if send_photo_request(full_post[:4090], image_url) == "OK":
        return True

    part1_paras = []
    current_len = len(title_escaped)
    split_idx = 0

    for idx, ep in enumerate(escaped_paras):
        if current_len + len(ep) + 2 <= 1015:
            part1_paras.append(ep)
            current_len += len(ep) + 2
            split_idx = idx + 1
        else:
            break

    caption_part = title_escaped
    if part1_paras:
        caption_part += "\n\n" + "\n\n".join(part1_paras)

    if send_photo_request(caption_part, image_url) == "OK":
        remaining_paras = escaped_paras[split_idx:]
        if remaining_paras:
            time.sleep(0.5)
            send_text_message("\n\n".join(remaining_paras)[:4090])
        return True

    return False


def publish_top_news():
    now_msk = datetime.now(MSK_TZ)

    # Полный ночной сон (с WORK_END_HOUR до WORK_START_HOUR по МСК) — никаких постов ночью
    if not is_day_mode():
        print(f"[{now_msk.strftime('%H:%M')} МСК] Ночной режим (сон до {WORK_START_HOUR:02d}:00 МСК). Пропуск.")
        return

    candidates = []

    for feed_info in RSS_FEEDS:
        source_name = feed_info["name"]
        feed_url = feed_info["url"]
        base_weight = feed_info["weight"]

        try:
            rss_resp = requests.get(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8
            )
            if rss_resp.status_code != 200:
                print(f"Пропуск {source_name}: статус {rss_resp.status_code}")
                continue
            feed = feedparser.parse(rss_resp.content)
        except Exception as e:
            print(f"Таймаут или ошибка {source_name}: {e}")
            continue

        for entry in feed.entries[:15]:
            link = entry.get("link", "").strip()
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))

            if not link or not title:
                continue
            if "\ufffd" in title or "\ufffd" in summary:
                continue
            if is_bad_content(link, title, summary):
                continue
            if len(summary) < 40 or summary.lower() == title.lower():
                continue

            keywords = extract_keywords(title)
            if len(keywords) < 2:
                continue
            if is_duplicate_in_db(link, keywords):
                continue

            full_text_lower = (title + " " + summary).lower()
            breaking_bonus = 4 if any(w in full_text_lower for w in BREAKING_WORDS) else 0
            image_url = extract_clean_rss_image(entry)

            candidates.append({
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

        paragraphs = fetch_full_article_paragraphs(best_news["link"], best_news["summary"], best_news["title"])

        if publish_full_article(best_news["title"], paragraphs, image_url):
            total_chars = sum(len(p) for p in paragraphs)
            print(f"[{now_str} МСК] Опубликовано ({total_chars} зн., рейтинг {best_news['score']}): {best_news['title']}")
            mark_as_posted(best_news["link"], best_news["title"], best_news["keywords"])
            return


if __name__ == "__main__":
    init_db()
    publish_top_news()
        
