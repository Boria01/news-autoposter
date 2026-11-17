import os
import json
import sqlite3
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetExportedChatInvitesRequest

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
CHANNEL_USERNAME = "glavnoe_chasa"
CHANNEL_ID = f"@{CHANNEL_USERNAME}"

MSK_TZ = timezone(timedelta(hours=3))
REPORT_HOUR = 21
DB_PATH = "analytics.db"
LINKS_CONFIG = "links.json"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            date_msk TEXT,
            hour_msk INTEGER,
            count INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            source_name TEXT PRIMARY KEY,
            invite_link TEXT,
            total_joins INTEGER DEFAULT 0,
            last_checked TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posts_metrics (
            msg_id INTEGER PRIMARY KEY,
            post_date TEXT,
            text_preview TEXT,
            views INTEGER DEFAULT 0,
            forwards INTEGER DEFAULT 0,
            replies INTEGER DEFAULT 0,
            reactions_total INTEGER DEFAULT 0,
            reactions_detail TEXT,
            err_percent REAL DEFAULT 0.0,
            er_percent REAL DEFAULT 0.0,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date TEXT PRIMARY KEY,
            sent_at TEXT
        )
    """)
    conn.commit()
    return conn


def tg_bot_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload or {}, timeout=15).json()
    if not resp.get("ok"):
        print(f"[BotAPI Warning] {method}: {resp}")
    return resp


def sync_subscribers_and_links(conn, now_msk):
    cur = conn.cursor()
    res = tg_bot_api("getChatMemberCount", {"chat_id": CHANNEL_ID})
    subs_count = res.get("result", 0)
    if subs_count > 0:
        cur.execute(
            "INSERT INTO subscribers_log (ts, date_msk, hour_msk, count) VALUES (?, ?, ?, ?)",
            (now_msk.isoformat(), now_msk.strftime("%Y-%m-%d"), now_msk.hour, subs_count)
        )

    sources = []
    if os.path.exists(LINKS_CONFIG):
        with open(LINKS_CONFIG, "r", encoding="utf-8") as f:
            sources = json.load(f).get("sources", [])

    for src in sources:
        cur.execute("SELECT invite_link FROM invite_links WHERE source_name = ?", (src,))
        if not cur.fetchone():
            created = tg_bot_api("createChatInviteLink", {
                "chat_id": CHANNEL_ID,
                "name": src[:32]
            })
            link = created.get("result", {}).get("invite_link")
            if link:
                cur.execute(
                    "INSERT INTO invite_links (source_name, invite_link, total_joins, last_checked) VALUES (?, ?, 0, ?)",
                    (src, link, now_msk.isoformat())
                )

    conn.commit()
    return subs_count


async def collect_mtproto_metrics(conn, subs_count, now_msk):
    cur = conn.cursor()
    client = TelegramClient(
        StringSession(TG_SESSION),
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S23",
        system_version="Android 14",
        app_version="11.4.2",
        lang_code="ru",
        system_lang_code="ru-RU"
    )
    async with client:
        entity = await client.get_entity(CHANNEL_USERNAME)

        async for msg in client.iter_messages(entity, limit=40):
            if not msg.message and not msg.media:
                continue

            views = msg.views or 0
            forwards = msg.forwards or 0
            replies = msg.replies.replies if msg.replies else 0

            reactions_total = 0
            reactions_map = []
            if msg.reactions and msg.reactions.results:
                for r in msg.reactions.results:
                    cnt = r.count or 0
                    reactions_total += cnt
                    emoticon = getattr(r.reaction, "emoticon", "custom")
                    reactions_map.append(f"{emoticon}:{cnt}")

            err = round((views / subs_count * 100), 2) if subs_count > 0 else 0.0
            er = round(((reactions_total + forwards + replies) / views * 100), 2) if views > 0 else 0.0

            preview = (msg.message or "[Медиа-пост]").replace("\n", " ").strip()[:65]
            post_dt_msk = msg.date.astimezone(MSK_TZ).strftime("%Y-%m-%d %H:%M")

            cur.execute("""
                INSERT INTO posts_metrics (
                    msg_id, post_date, text_preview, views, forwards, replies,
                    reactions_total, reactions_detail, err_percent, er_percent, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(msg_id) DO UPDATE SET
                    views=excluded.views,
                    forwards=excluded.forwards,
                    replies=excluded.replies,
                    reactions_total=excluded.reactions_total,
                    reactions_detail=excluded.reactions_detail,
                    err_percent=excluded.err_percent,
                    er_percent=excluded.er_percent,
                    updated_at=excluded.updated_at
            """, (
                msg.id, post_dt_msk, preview, views, forwards, replies,
                reactions_total, " ".join(reactions_map), err, er, now_msk.isoformat()
            ))

        try:
            invites = await client(GetExportedChatInvitesRequest(
                peer=entity,
                admin_id=await client.get_me(),
                limit=50
            ))
            for inv in invites.invites:
                title = getattr(inv, "title", None)
                usage = getattr(inv, "usage", 0) or 0
                if title:
                    cur.execute(
                        "UPDATE invite_links SET total_joins = ?, last_checked = ? WHERE source_name = ?",
                        (usage, now_msk.isoformat(), title)
                    )
        except Exception as e:
            print(f"[Invites check skipped]: {e}")

    conn.commit()


def send_daily_report_if_needed(conn, subs_count, now_msk):
    today_str = now_msk.strftime("%Y-%m-%d")
    if now_msk.hour != REPORT_HOUR:
        return

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM daily_reports WHERE report_date = ?", (today_str,))
    if cur.fetchone():
        return

    cur.execute("SELECT count FROM subscribers_log WHERE date_msk = ? ORDER BY id ASC LIMIT 1", (today_str,))
    first_row = cur.fetchone()
    start_subs = first_row[0] if first_row else subs_count
    delta = subs_count - start_subs
    delta_str = f"+{delta}" if delta >= 0 else str(delta)

    yesterday_str = (now_msk - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    cur.execute("""
        SELECT COUNT(*), AVG(views), AVG(err_percent), AVG(er_percent), SUM(reactions_total), SUM(forwards)
        FROM posts_metrics WHERE post_date >= ?
    """, (yesterday_str,))
    posts_cnt, avg_views, avg_err, avg_er, sum_react, sum_fwd = cur.fetchone()

    cur.execute("""
        SELECT text_preview, views, reactions_total, forwards, err_percent, reactions_detail
        FROM posts_metrics WHERE post_date >= ?
        ORDER BY (reactions_total * 2 + forwards * 3 + views * 0.05) DESC LIMIT 3
    """, (yesterday_str,))
    top_posts = cur.fetchall()

    cur.execute("SELECT source_name, total_joins, invite_link FROM invite_links ORDER BY total_joins DESC")
    links_rows = cur.fetchall()

    lines = [
        f"📊 <b>Аналитика @{CHANNEL_USERNAME} за {now_msk.strftime('%d.%m.%Y')}</b>\n",
        f"👥 <b>Подписчики:</b> {subs_count} ({delta_str} за день)",
        f"📰 <b>Постов за 24ч:</b> {posts_cnt or 0}",
        f"👁 <b>Средний охват:</b> {int(avg_views or 0)} (ERR: {round(avg_err or 0, 1)}%)",
        f"🔥 <b>Активность:</b> {sum_react or 0} реакций | {sum_fwd or 0} репостов (ER: {round(avg_er or 0, 1)}%)\n",
        "🏆 <b>Топ-3 публикации дня:</b>"
    ]

    if top_posts:
        for i, (prev, v, r, f, err, r_det) in enumerate(top_posts, 1):
            lines.append(f"{i}. <i>{prev}...</i>\n   👁 {v} ({err}%) | ❤️ {r} ({r_det or '-'}) | 🔁 {f}")
    else:
        lines.append("— Нет данных за последние 24 часа")

    lines.append("\n🔗 <b>Источники подписчиков (инвайт-ссылки):</b>")
    if links_rows:
        for name, joins, url in links_rows:
            lines.append(f"• <b>{name}:</b> {joins} вступлений — <code>{url}</code>")
    else:
        lines.append("— Ссылки ещё не добавлены в links.json")

    tg_bot_api("sendMessage", {
        "chat_id": ADMIN_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })

    cur.execute("INSERT INTO daily_reports (report_date, sent_at) VALUES (?, ?)", (today_str, now_msk.isoformat()))
    conn.commit()


def main():
    now_msk = datetime.now(MSK_TZ)
    print(f"[{now_msk.strftime('%H:%M МСК')}] Запуск сбора аналитики...")
    conn = init_db()
    subs_count = sync_subscribers_and_links(conn, now_msk)
    asyncio.run(collect_mtproto_metrics(conn, subs_count, now_msk))
    send_daily_report_if_needed(conn, subs_count, now_msk)
    conn.close()
    print(f"[{now_msk.strftime('%H:%M МСК')}] Готово. Подписчиков: {subs_count}")


if __name__ == "__main__":
    main()
