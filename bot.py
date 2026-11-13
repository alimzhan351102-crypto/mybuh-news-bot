"""
Дублирование новостей с сайта в Telegram-группу/канал.

Как работает:
  1. Открывает страницу со списком новостей (NEWS_URL).
  2. Собирает ссылки на отдельные новости (по шаблону LINK_PATTERN).
  3. Для каждой новой ссылки открывает страницу новости, берёт заголовок,
     описание и картинку из метатегов og:title / og:description / og:image.
  4. Отправляет в Telegram и запоминает ссылку в sent.json, чтобы не было дублей.

При самом первом запуске ничего не отправляет: только запоминает уже
опубликованные новости, чтобы не завалить группу старыми постами.

Проверка без отправки:  python bot.py --check
"""

import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------- Настройки ----------
NEWS_URL = os.environ.get("NEWS_URL", "https://portal.mybuh.kz/news")
# Какие ссылки считать новостями (проверяется путь URL).
LINK_PATTERN = re.compile(os.environ.get("LINK_PATTERN", r"^/news/[^/]+/?$"))
STATE_FILE = os.environ.get("STATE_FILE", "sent.json")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))  # не больше N постов за запуск
READ_MORE = os.environ.get("READ_MORE", "Читать полностью")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; news-to-telegram/1.0)"}
# --------------------------------


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return BeautifulSoup(r.text, "html.parser")


def norm(text):
    return re.sub(r"[^0-9a-zа-яё]+", " ", (text or "").lower()).strip()


def get_news_links():
    """[(ссылка, заголовок)] в порядке, как на странице (обычно сначала свежие)."""
    soup = fetch(NEWS_URL)
    host = urlparse(NEWS_URL).netloc
    order, titles = [], {}
    for a in soup.find_all("a", href=True):
        url = urljoin(NEWS_URL, a["href"]).split("#")[0]
        p = urlparse(url)
        if p.netloc != host or not LINK_PATTERN.search(p.path):
            continue
        if re.search(r"/page/\d+|/(category|tag|tags|rubric)/", p.path):
            continue
        url = url.rstrip("/")
        text = a.get_text(" ", strip=True)
        if url not in titles:
            order.append(url)
            titles[url] = text
        elif len(text) > len(titles[url]):
            titles[url] = text  # берём самый полный текст ссылки (заголовок)
    return [(u, titles[u]) for u in order]


def key(url, title):
    return f"{url} || {norm(title)}"


def already_sent(url, title, sent):
    """Новость считается новой, если у неё новый адрес ИЛИ изменился заголовок.
    Текст ссылки бывает с подзаголовком (большие карточки), поэтому сравниваем по началу."""
    cur = norm(title)
    for s in sent:
        u, _, old = s.partition(" || ")
        if u != url:
            continue
        if not cur or not old or cur.startswith(old) or old.startswith(cur):
            return True
    return False


def meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return None


def get_article(url):
    soup = fetch(url)
    title = meta(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.string.strip() if soup.title else url)
    desc = meta(soup, "og:description", "description", "twitter:description") or ""
    image = meta(soup, "og:image", "twitter:image")
    if image:
        image = urljoin(url, image)
    return {"url": url, "title": title, "desc": desc, "image": image}


def build_text(a, limit):
    head = f"<b>{html.escape(a['title'])}</b>"
    tail = f'<a href="{html.escape(a["url"], quote=True)}">{READ_MORE}</a>'
    room = limit - len(head) - len(tail) - 8
    desc = a["desc"]
    if len(desc) > room:
        desc = desc[: max(room - 1, 0)].rsplit(" ", 1)[0] + "…"
    parts = [head]
    if desc:
        parts.append(html.escape(desc))
    parts.append(tail)
    return "\n\n".join(parts)


def tg(method, data):
    token = os.environ["TG_TOKEN"]
    thread = os.environ.get("TG_THREAD_ID")
    if thread:
        data["message_thread_id"] = thread
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}: {res}")
    return res


def send(a):
    chat = os.environ["TG_CHAT_ID"]
    if a["image"]:
        try:
            return tg("sendPhoto", {"chat_id": chat, "photo": a["image"],
                                    "caption": build_text(a, 1024), "parse_mode": "HTML"})
        except Exception as e:
            print(f"  картинка не отправилась ({e}), отправляю текстом")
    return tg("sendMessage", {"chat_id": chat, "text": build_text(a, 4096),
                              "parse_mode": "HTML", "disable_web_page_preview": "false"})


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(urls):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(urls[-1000:], f, ensure_ascii=False, indent=1)


def check():
    links = get_news_links()
    print(f"Найдено ссылок на новости: {len(links)}")
    for u, t in links[:20]:
        print("  ", t[:80], "|", u)
    if links:
        a = get_article(links[0][0])
        print("\nПервая новость:")
        for k, v in a.items():
            print(f"  {k}: {v}")
        print("\nТак будет выглядеть пост:\n")
        print(build_text(a, 1024))


def main():
    if "--check" in sys.argv:
        return check()

    links = get_news_links()
    if not links:
        sys.exit("Не нашёл ни одной ссылки на новость — проверьте NEWS_URL и LINK_PATTERN (python bot.py --check)")

    sent = load_state()
    if sent is None:
        save_state([key(u, t) for u, t in reversed(links)])
        print(f"Первый запуск: запомнил {len(links)} существующих новостей, ничего не отправлял.")
        return

    # переход со старого формата (только адреса) на «адрес + заголовок»
    if not any(" || " in s for s in sent):
        old = set(sent)
        on_page = {u for u, _ in links}
        sent = [u for u in sent if u not in on_page] + [key(u, t) for u, t in reversed(links) if u in old]
        save_state(sent)
        print("Обновил формат списка отправленных новостей.")

    new = [(u, t) for u, t in links if not already_sent(u, t, sent)][:MAX_PER_RUN]
    if not new:
        print("Новых новостей нет.")
        return

    for url, title in reversed(new):  # отправляем от старых к новым
        try:
            a = get_article(url)
            send(a)
            print(f"Отправлено: {a['title']}")
            sent.append(key(url, title))
            save_state(sent)
            time.sleep(3)
        except Exception as e:
            print(f"Ошибка на {url}: {e}")


if __name__ == "__main__":
    main()
