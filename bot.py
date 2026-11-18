"""Бот расписания Реал Бетис.

Читает каналы (публичные - через t.me/s/<канал>, закрытые - через Telethon),
ищет строки с названием команды, достаёт дату и время и публикует
расписание в беседу.

  python bot.py          - обычный режим (его вызывает планировщик)
  python bot.py --dry    - только показать расписание в консоли
  python bot.py --force  - отправить расписание в беседу прямо сейчас
"""
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = str(CFG["chat_id"])
TZ = ZoneInfo(CFG.get("timezone", "Europe/Berlin"))
POST_HOUR = int(CFG.get("post_hour", 10))
DAYS_AHEAD = int(CFG.get("days_ahead", 7))
TITLE = CFG.get("title", "Расписание команды")
STATE_FILE = os.path.join(BASE, "state.json")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

RE_WORD_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
RE_NUM_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
RE_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RE_TIME2 = re.compile(r"\bв\s*([01]?\d|2[0-3])[.\-]([0-5]\d)(?!\d)", re.I)


def parse_date(line, ref):
    """Ищет дату в строке: '22.11', '22.11.2026', '22 ноября'."""
    cands = []
    for m in RE_WORD_DATE.finditer(line):
        cands.append((int(m[1]), MONTHS[m[2].lower()], m[3]))
    for m in RE_NUM_DATE.finditer(line):
        cands.append((int(m[1]), int(m[2]), m[3]))
    for d, mo, y in cands:
        try:
            if y:
                y = int(y)
                return date(y + 2000 if y < 100 else y, mo, d)
            result = date(ref.year, mo, d)
            if result < ref - timedelta(days=60):  # переход через Новый год
                result = date(ref.year + 1, mo, d)
            return result
        except ValueError:
            continue
    return None


def parse_time(line):
    m = RE_TIME.search(line) or RE_TIME2.search(line)
    return f"{int(m[1]):02d}:{m[2]}" if m else ""


def fetch_public(channel):
    r = requests.get(
        f"https://t.me/s/{channel}",
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for wrap in soup.select(".tgme_widget_message"):
        body = wrap.select_one(".tgme_widget_message_text")
        stamp = wrap.select_one(".tgme_widget_message_date time")
        if not body or not stamp:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        posts.append({
            "text": body.get_text(),
            "posted": datetime.fromisoformat(stamp["datetime"]).astimezone(TZ),
            "link": "https://t.me/" + wrap.get("data-post", ""),
        })
    return posts


def fetch_private(invite_hash):
    """Закрытый канал по invite-ссылке. Нужен ваш Telegram-аккаунт (Telethon),
    и этот аккаунт должен уже состоять в канале."""
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
    from telethon.tl.functions.messages import CheckChatInviteRequest
    from telethon.tl.types import ChatInviteAlready

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    session = os.environ.get("TG_SESSION")
    if not (api_id and api_hash and session):
        raise RuntimeError("не заданы TG_API_ID / TG_API_HASH / TG_SESSION")

    posts = []
    with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        info = client(CheckChatInviteRequest(invite_hash))
        if not isinstance(info, ChatInviteAlready):
            raise RuntimeError("аккаунт не состоит в этом канале - сначала вступите в него")
        for msg in client.iter_messages(info.chat, limit=40):
            if msg.message:
                posts.append({
                    "text": msg.message,
                    "posted": msg.date.astimezone(TZ),
                    "link": f"https://t.me/c/{info.chat.id}/{msg.id}",
                })
    return posts


def collect():
    """Возвращает список матчей. Для каждой пары (дата, канал) остаётся
    самый свежий пост - так подхватываются переносы времени.
    Если в строке/посте нет даты, берётся дата публикации поста
    (каналы выкладывают расписание на день)."""
    today = datetime.now(TZ).date()
    limit = today + timedelta(days=DAYS_AHEAD)
    best = {}
    for ch in CFG["channels"]:
        src = ch["source"]
        keywords = [k.lower() for k in ch["keywords"]]
        try:
            posts = fetch_private(src[1:]) if src.startswith("+") else fetch_public(src)
        except Exception as e:  # один сбойный канал не должен ронять всё
            print(f"[{src[:6]}...] ошибка: {e}", file=sys.stderr)
            continue
        for p in posts:
            post_day = p["posted"].date()
            if post_day < today - timedelta(days=2):
                continue
            current = post_day
            for raw in p["text"].split("\n"):
                line = raw.strip()
                if not line:
                    continue
                found = parse_date(line, post_day)
                if found:
                    current = found
                if today <= current <= limit and any(k in line.lower() for k in keywords):
                    key = (current, src)
                    prev = best.get(key)
                    if not prev or p["posted"] > prev["posted"]:
                        best[key] = {
                            "date": current,
                            "time": parse_time(line),
                            "text": line,
                            "posted": p["posted"],
                            "link": p["link"],
                        }
    return sorted(best.values(), key=lambda m: (m["date"], m["time"] or "99:99"))


def render(matches):
    head = f"📅 <b>Расписание: {html.escape(TITLE)}</b>"
    if not matches:
        return head + "\n\nБлижайших матчей не найдено."
    out = [head, ""]
    for m in matches:
        d = m["date"]
        when = f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"
        when += f" в {m['time']}" if m["time"] else " (время уточняется)"
        out.append(f"• <b>{when}</b>\n  {html.escape(m['text'])} (<a href=\"{m['link']}\">пост</a>)")
    return "\n".join(out)


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    text = render(collect())

    if "--dry" in sys.argv:
        print(text)
        return
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN")

    now = datetime.now(TZ)
    today = now.date().isoformat()
    state = load_state()
    digest = hashlib.md5(text.encode()).hexdigest()

    if "--force" in sys.argv or (now.hour >= POST_HOUR and state.get("last_daily") != today):
        send(text)  # ежедневная публикация
        state.update(last_daily=today, last_hash=digest)
    elif now.hour >= POST_HOUR and digest != state.get("last_hash"):
        send("🔄 <b>Расписание изменилось</b>\n\n" + text)  # обновление в течение дня
        state["last_hash"] = digest
    else:
        return
    save_state(state)


if __name__ == "__main__":
    main()
