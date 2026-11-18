"""Бот расписания Реал Бетис."""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

# Берем токен из переменных окружения или из config.json
BOT_TOKEN = os.environ.get("BOT_TOKEN", CFG.get("bot_token", ""))
CHAT_ID = str(CFG["chat_id"])
TZ = ZoneInfo(CFG.get("timezone", "Europe/Moscow"))
POST_HOUR = int(CFG.get("post_hour", 10))
DAYS_AHEAD = int(CFG.get("days_ahead", 7))
TITLE = CFG.get("title", "Расписание команды")
STATE_FILE = os.path.join(BASE, "state.json")
ALIASES = CFG.get("aliases", {})

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

RE_WORD_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
RE_NUM_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
RE_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RE_TIME2 = re.compile(r"\bв\s*([01]?\d|2[0-3])[.\-]([0-5]\d)(?!\d)", re.I)


def parse_date(line, ref):
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
            if result < ref - timedelta(days=60):
                result = date(ref.year + 1, mo, d)
            return result
        except ValueError:
            continue
    return None


def parse_time(line):
    m = RE_TIME.search(line) or RE_TIME2.search(line)
    return f"{int(m[1]):02d}:{m[2]}" if m else ""


def apply_alias(team_name):
    """Проверяет, есть ли команда в словаре переименований."""
    clean_name = team_name.strip()
    alias = ALIASES.get(clean_name)
    if alias:
        return f"{clean_name} ({alias})"
    return clean_name


def parse_ltleague_post(post, keywords):
    text = post["text"]
    results = []
    matches = list(re.finditer(r'«([^»]+)»\s*[-—–]\s*«([^»]+)»', text))
    
    for i, m_teams in enumerate(matches):
        t1, t2 = m_teams.group(1), m_teams.group(2)
        teams = f"«{apply_alias(t1)}» — «{apply_alias(t2)}»"
        
        if not any(k in teams.lower() for k in keywords):
            continue
            
        start_pos = m_teams.start()
        end_pos = matches[i+1].start() if i + 1 < len(matches) else len(text)
        block = text[start_pos:end_pos]
        
        if 'отпись' in block.lower() or 'отпись' in teams.lower():
            continue
            
        m_time = re.search(r'(\d{2}:\d{2})', block)
        time_str = m_time.group(1) if m_time else ""
        match_date = parse_date(block, post["posted"].date()) or post["posted"].date()
            
        results.append({
            "date": match_date, "time": time_str, "text": teams,
            "raw_teams": teams, "posted": post["posted"], "link": post["link"]
        })
    return results


def parse_epl_post(post, keywords):
    """Парсер для канала eplmatchesss."""
    text = post["text"]
    results = []
    
    m_date = re.search(r'📆\s*(\d{2}\.\d{2}\.\d{4})', text)
    post_date = parse_date(m_date.group(1), post["posted"].date()) if m_date else post["posted"].date()
    
    lines = text.split('\n')
    for i, line in enumerate(lines):
        clean_line = line.replace('🏴󠁧󠁢󠁥󠁮󠁧󠁿', '').strip()
        if ' - ' in clean_line:
            parts = clean_line.split(' - ')
            if len(parts) == 2:
                t1, t2 = parts[0].strip(), parts[1].strip()
                teams = f"«{apply_alias(t1)}» — «{apply_alias(t2)}»"
                
                if not any(k in teams.lower() for k in keywords):
                    continue
                    
                time_str = ""
                if i + 1 < len(lines):
                    m_time = re.search(r'⌛️\s*(\d{2}:\d{2})', lines[i+1])
                    if m_time:
                        time_str = m_time.group(1)
                        
                results.append({
                    "date": post_date, "time": time_str, "text": teams,
                    "raw_teams": teams, "posted": post["posted"], "link": post["link"]
                })
    return results


def fetch_public(channel):
    r = requests.get(
        f"https://t.me/s/{channel}", timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
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


def collect():
    today = datetime.now(TZ).date()
    limit = today + timedelta(days=DAYS_AHEAD)
    best = {}
    
    for ch in CFG["channels"]:
        src = ch["source"]
        keywords = [k.lower() for k in ch["keywords"]]
        parser_type = ch.get("parser", "default")
            
        try:
            posts = fetch_public(src)
        except Exception as e:
            print(f"[{src[:6]}...] ошибка: {e}", file=sys.stderr)
            continue
            
        for p in posts:
            post_day = p["posted"].date()
            if post_day < today - timedelta(days=14):
                continue
                
            if parser_type == "ltleague":
                parsed = parse_ltleague_post(p, keywords)
            elif parser_type == "epl":
                parsed = parse_epl_post(p, keywords)
            else:
                parsed = []
                
            for m in parsed:
                if today - timedelta(days=1) <= m["date"] <= limit:
                    key = (m["date"], src, m["raw_teams"])
                    prev = best.get(key)
                    if not prev or m["posted"] > prev["posted"]:
                        best[key] = m
                            
    return sorted(best.values(), key=lambda m: (m["date"], m["time"] or "99:99"))


def render(matches):
    head = f"📅 <b>Расписание: {html.escape(TITLE)}</b>"
    if not matches:
        return head + "\n\nБлижайших матчей не найдено."
    out = [head, ""]
    
    for m in matches:
        # Только время, без даты
        when = f"в {m['time']}" if m['time'] else "Время уточняется"
        safe_text = html.escape(m['text']).replace('\n', '<br>')
        out.append(f"• <b>{when}</b>\n  {safe_text} (<a href=\"{m['link']}\">пост</a>)")
    return "\n".join(out)


def send(text, target_chat=None):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": target_chat or CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
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

def listen():
    """Слушаем команду /raspa. Показывает все входящие сообщения для диагностики."""
    # Удаляем возможный webhook, иначе getUpdates не работает
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
    except Exception:
        pass

    # Проверяем, что бот существует
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        if not me.get("ok"):
            print(f"Ошибка авторизации бота: {me}")
            return
        print(f"✅ Бот: @{me['result']['username']}")
    except Exception as e:
        print(f"Не удалось подключиться к Telegram API: {e}")
        return

    print("👂 Слушаю команду /raspa. Пиши её в группе...")
    offset = -1  # Пропускаем старые накопившиеся апдейты

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40
            )
            data = r.json()

            if not data.get("ok"):
                print(f"❌ Ошибка Telegram API: {data.get('description')}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message") or {}
                text = msg.get("text", "")
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                chat_title = chat.get("title") or chat.get("username") or "личка"

                if text:
                    print(f"📩 [{chat_title}] {text}")

                if text and text.startswith("/raspa"):
                    print(f"✅ Получена команда /raspa от {chat_title}")
                    try:
                        schedule_text = render(collect())
                        send(schedule_text, target_chat=chat_id)
                        print("📤 Расписание отправлено")
                    except Exception as e:
                        print(f"❌ Ошибка при отправке: {e}")
        except Exception as e:
            print(f"⚠️ Ошибка соединения: {e}")
            time.sleep(5)

def main():
    if "--listen" in sys.argv:
        if not BOT_TOKEN:
            sys.exit("Не задан BOT_TOKEN")
        listen()
        return

    text = render(collect())

    if "--dry" in sys.argv:
        print("\n--- РЕЗУЛЬТАТ ---")
        print(text)
        return
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN")

    now = datetime.now(TZ)
    today = now.date().isoformat()
    state = load_state()
    digest = hashlib.md5(text.encode()).hexdigest()

    if "--force" in sys.argv or (now.hour >= POST_HOUR and state.get("last_daily") != today):
        send(text)
        state.update(last_daily=today, last_hash=digest)
    elif now.hour >= POST_HOUR and digest != state.get("last_hash"):
        send("🔄 <b>Расписание изменилось</b>\n\n" + text)
        state["last_hash"] = digest
    else:
        return
    save_state(state)


if __name__ == "__main__":
    main()
