"""
Напоминания о вебинарах в Telegram.

Читает календарь из webinars.txt и публикует в тему «Вебинары»:
  - напоминание за 1 день до начала;
  - напоминание за 1 час до начала.
Что уже отправлено, запоминает в reminded.json (без дублей).

Проверка без отправки:  python webinars.py --check
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

CAL_FILE = os.environ.get("WEBINARS_FILE", "webinars.txt")
STATE_FILE = os.environ.get("WEBINARS_STATE", "reminded.json")
TZ = timezone(timedelta(hours=5))  # Астана
THREAD = os.environ.get("TG_WEBINARS_THREAD_ID", "4")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
LINE = re.compile(
    r"^\s*(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(.+?)\s+(\d{1,2}):(\d{2})\s*(.*)$",
    re.IGNORECASE,
)

# (ключ, за сколько до начала, заголовок поста)
REMINDERS = [
    ("1d", timedelta(days=1), "📅 Завтра вебинар"),
    ("1h", timedelta(hours=1), "⏰ Через час начинаем вебинар"),
]


def parse(text, now):
    events = []
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        day, month, title, hh, mm, rest = m.groups()
        month = MONTHS[month.lower()]
        year = now.year
        start = datetime(year, month, int(day), int(hh), int(mm), tzinfo=TZ)
        if start < now - timedelta(days=60):  # декабрь → январь следующего года
            start = start.replace(year=year + 1)
        online = "онлайн" in rest.lower()
        speaker = re.sub(r"\(?\s*онлайн\s*\)?", "", rest, flags=re.IGNORECASE).strip(" ,-—")
        events.append({"start": start, "title": title.strip(), "speaker": speaker, "online": online})
    return events


def key(e):
    return e["start"].strftime("%Y-%m-%d %H:%M") + " | " + e["title"]


RU_MONTHS_GEN = [None] + list(MONTHS)


def build_text(e, heading):
    s = e["start"]
    lines = [
        f"<b>{heading}</b>",
        "",
        f"<b>«{html.escape(e['title'])}»</b>",
        "",
        f"🗓 {s.day} {RU_MONTHS_GEN[s.month]}, {s:%H:%M} (Астана)",
    ]
    if e["speaker"]:
        lines.append(f"🎤 Спикер: {html.escape(e['speaker'])}")
    if e["online"]:
        lines.append("💻 Формат: онлайн")
    return "\n".join(lines)


def due(e, now):
    """Какое напоминание пора отправить сейчас (только самое позднее из наступивших)."""
    if now >= e["start"]:
        return None
    hit = None
    for k, before, heading in REMINDERS:
        if now >= e["start"] - before:
            hit = (k, heading)
    return hit


def send(text):
    token = os.environ["TG_TOKEN"]
    data = {"chat_id": os.environ["TG_CHAT_ID"], "text": text, "parse_mode": "HTML"}
    if THREAD:
        data["message_thread_id"] = THREAD
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram: {res}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    now = datetime.now(TZ)
    with open(CAL_FILE, encoding="utf-8") as f:
        events = parse(f.read(), now)

    if "--check" in sys.argv:
        print(f"Сейчас: {now:%d.%m.%Y %H:%M}. Найдено вебинаров: {len(events)}")
        for e in events:
            print(f"  {e['start']:%d.%m.%Y %H:%M} | {e['title']} | {e['speaker']} | онлайн={e['online']}")
        if events:
            print("\nПример поста:\n")
            print(build_text(events[0], REMINDERS[0][2]))
        return

    state = load_state()
    sent_any = False
    for e in events:
        hit = due(e, now)
        if not hit:
            continue
        k, heading = hit
        done = state.get(key(e), [])
        if k in done:
            continue
        try:
            send(build_text(e, heading))
            print(f"Отправлено ({k}): {e['title']}")
            # если отправили напоминание «за час», «за день» уже не нужно
            state[key(e)] = sorted(set(done) | {r[0] for r in REMINDERS[: [r[0] for r in REMINDERS].index(k) + 1]})
            sent_any = True
        except Exception as ex:
            print(f"Ошибка ({e['title']}): {ex}")
    save_state(state)
    if not sent_any:
        print("Напоминаний сейчас нет.")


if __name__ == "__main__":
    main()
