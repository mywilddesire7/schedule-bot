"""Бот расписания Реал Бетис."""
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
            if result < ref - timedelta(days=60):
                result = date(ref.year + 1, mo, d)
            return result
        except ValueError:
            continue
    return None


def parse_time(line):
    m = RE_TIME.search(line) or RE_TIME2.search(line)
    return f"{int(m[1]):02d}:{m[2]}" if m else ""


def parse_ltleague_post(post, keywords, debug=False):
    """Гибкий парсер для формата LT League."""
    text = post["text"]
    results = []
    
    # Ищем все пары команд
    matches = list(re.finditer(r'«([^»]+)»\s*[-—–]\s*«([^»]+)»', text))
    
    for i, m_teams in enumerate(matches):
        teams = f"«{m_teams.group(1)}» — «{m_teams.group(2)}»"
        
        # Проверяем, есть ли ключевое слово в названиях команд
        if not any(k in teams.lower() for k in keywords):
            continue
            
        # Определяем блок текста, относящийся к этому матчу
        start_pos = m_teams.start()
        end_pos = matches[i+1].start() if i + 1 < len(matches) else len(text)
        block = text[start_pos:end_pos]
        
        if 'отпись' in block.lower() or 'отпись' in teams.lower():
            continue
            
        # Ищем время
        m_time = re.search(r'(\d{2}:\d{2})', block)
        time_str = m_time.group(1) if m_time else ""
        
        # Ищем стадион
        m_stadium = re.search(r'🏟[^\w]*\s*(.+)', block)
        stadium = m_stadium.group(1).strip() if m_stadium else ""
        
        # Ищем дату (если нет в блоке, берем дату публикации поста)
        match_date = parse_date(block, post["posted"].date()) or post["posted"].date()
        
        display_text = teams
        if stadium:
            display_text += f"\n🏟 {stadium}"
            
        results.append({
            "date": match_date,
            "time": time_str,
            "text": display_text,
            "raw_teams": teams,
            "posted": post["posted"],
            "link": post["link"]
        })
    return results


def fetch_public(channel):
    r = requests.get(
        f"https://t.me/s/{channel}",
        timeout=30,
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


def collect(debug=False):
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
                parsed_matches = parse_ltleague_post(p, keywords, debug)
                for m in parsed_matches:
                    # Небольшое послабление: берем матчи с сегодняшнего дня (или вчерашние, если пост вышел ночью)
                    if today - timedelta(days=1) <= m["date"] <= limit:
                        key = (m["date"], src, m["raw_teams"])
                        prev = best.get(key)
                        if not prev or m["posted"] > prev["posted"]:
                            best[key] = m
                    elif debug:
                        print(f"    [DEBUG] Пропущен матч (дата {m['date']} вне диапазона {today} - {limit})")
                            
    return sorted(best.values(), key=lambda m: (m["date"], m["time"] or "99:99"))


def render(matches):
    head = f"📅 <b>Расписание: {html.escape(TITLE)}</b>"
    if not matches:
        return head + "\n\nБлижайших матчей не найдено."
    out = [head, ""]
    
    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    
    for m in matches:
        d = m["date"]
        
        # Красивое отображение дат
        if d == today:
            when = f"Сегодня, {d:%d.%m}"
        elif d == tomorrow:
            when = f"Завтра, {d:%d.%m}"
        else:
            when = f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"
            
        when += f" в {m['time']}" if m["time"] else " (время уточняется)"
        
        safe_text = html.escape(m['text']).replace('\n', '<br>')
        out.append(f"• <b>{when}</b>\n  {safe_text} (<a href=\"{m['link']}\">пост</a>)")
    return "\n".join(out)

# ... Остальные функции (send, load_state, save_state, main) без изменений ...
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
    debug = "--debug" in sys.argv
    text = render(collect(debug=debug))

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
