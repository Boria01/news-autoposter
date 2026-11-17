import os
import json
import sqlite3
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL = os.environ.get("CHANNEL_USERNAME", "@glavnoe_chasa")

MSK_TZ = timezone(timedelta(hours=3))
DB_PATH = "analytics.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date_str TEXT PRIMARY KEY,
            subscribers INTEGER,
            posts_count INTEGER,
            avg_views REAL,
            err_percent REAL,
            er_percent REAL,
            report_sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def send_telegram_report(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"[Telegram Send Error]: {e}")


def format_post_preview(raw_text):
    if not raw_text:
        return "Медиа-публикация без текста"
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    title = lines[0] if lines else raw_text.strip()
    if len(title) > 68:
        title = title[:65].rstrip(".,:;!?- ") + "..."
    return title


async def collect_and_report():
    now_msk = datetime.now(MSK_TZ)
    today_str = now_msk.strftime("%Y-%m-%d")
    yesterday_str = (now_msk - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff_24h = now_msk - timedelta(hours=24)

    conn = init_db()
    cur = conn.cursor()

    client = TelegramClient(
        StringSession(TG_SESSION),
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S24",
        system_version="Android 14",
        app_version="11.4.2",
        lang_code="ru",
        system_lang_code="ru-RU"
    )

    async with client:
        entity = await client.get_entity(CHANNEL)
        full = await client(GetFullChannelRequest(channel=entity))
        subs = full.full_chat.participants_count or 0

        posts = []
        async for msg in client.iter_messages(entity, limit=50):
            if not msg.date:
                continue
            msg_date_msk = msg.date.astimezone(MSK_TZ)
            if msg_date_msk < cutoff_24h:
                break
            if msg.views is None:
                continue

            reactions_count = 0
            if msg.reactions and msg.reactions.results:
                reactions_count = sum(r.count for r in msg.reactions.results)

            forwards_count = msg.forwards or 0
            posts.append({
                "id": msg.id,
                "text": format_post_preview(msg.message),
                "views": msg.views,
                "reactions": reactions_count,
                "forwards": forwards_count
            })

    posts_count = len(posts)
    if posts_count > 0:
        avg_views = round(sum(p["views"] for p in posts) / posts_count, 1)
        total_reactions = sum(p["reactions"] for p in posts)
        total_forwards = sum(p["forwards"] for p in posts)
        err_percent = round((avg_views / subs * 100), 1) if subs > 0 else 0.0
        avg_eng = (total_reactions + total_forwards) / posts_count
        er_percent = round((avg_eng / subs * 100), 2) if subs > 0 else 0.0
    else:
        avg_views = 0.0
        total_reactions = 0
        total_forwards = 0
        err_percent = 0.0
        er_percent = 0.0

    cur.execute("SELECT subscribers FROM daily_stats WHERE date_str = ?", (yesterday_str,))
    prev_row = cur.fetchone()
    if prev_row and prev_row[0] is not None:
        diff = subs - prev_row[0]
        diff_str = f"+{diff}" if diff >= 0 else f"{diff}"
    else:
        diff_str = "+0"

    cur.execute("SELECT report_sent FROM daily_stats WHERE date_str = ?", (today_str,))
    sent_row = cur.fetchone()
    already_sent = sent_row[0] if sent_row else 0

    cur.execute("""
        INSERT OR REPLACE INTO daily_stats
        (date_str, subscribers, posts_count, avg_views, err_percent, er_percent, report_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (today_str, subs, posts_count, avg_views, err_percent, er_percent, already_sent))
    conn.commit()

    # Отправляем отчёт в окне с 20:50 до 21:59 МСК один раз за день (или при ручном запуске)
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    in_report_window = (now_msk.hour == 20 and now_msk.minute >= 50) or (now_msk.hour >= 21)

    if is_manual or (in_report_window and not already_sent):
        top_posts = sorted(posts, key=lambda x: (x["views"], x["reactions"]), reverse=True)[:3]

        top_lines = []
        for idx, p in enumerate(top_posts, 1):
            post_err = round((p["views"] / subs * 100), 1) if subs > 0 else 0.0
            top_lines.append(
                f"<b>{idx}. {p['text']}</b>\n"
                f"    👁 {p['views']} ({post_err}%)  •  ❤️ {p['reactions']}  •  🔁 {p['forwards']}"
            )

        top_block = "\n\n".join(top_lines) if top_lines else "<i>За последние 24 часа публикаций не было</i>"

        report = (
            f"📊 <b>Сводка {CHANNEL}</b>  |  <code>{now_msk.strftime('%d.%m.%Y')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 <b>Подписчики:</b> {subs} (<b>{diff_str}</b> за день)\n"
            f"📰 <b>Постов за 24ч:</b> {posts_count}\n"
            f"👁 <b>Средний охват:</b> {int(avg_views)} (ERR: <b>{err_percent}%</b>)\n"
            f"🔥 <b>Активность:</b> {total_reactions} ❤️  |  {total_forwards} 🔁 (ER: <b>{er_percent}%</b>)\n\n"
            f"🏆 <b>Топ-3 публикации дня</b>\n\n"
            f"{top_block}"
        )

        send_telegram_report(report)
        cur.execute("UPDATE daily_stats SET report_sent = 1 WHERE date_str = ?", (today_str,))
        conn.commit()

    conn.close()


if __name__ == "__main__":
    asyncio.run(collect_and_report())
    
