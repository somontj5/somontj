# -*- coding: utf-8 -*-
"""Воркер: только НАХОДИТ объявления и складывает их в свою ветку.

ВАЖНО: скрипт намеренно НЕ импортирует scraper.py. В scraper.py секреты читаются
на уровне модуля (TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]), поэтому
импорт требовал бы токенов бота и ключей Gemini. Воркеру они не нужны и не должны
быть ему доступны: он умеет ровно одно — читать страницу выдачи. Свой разбор
карточек здесь компактный (заголовок, цена, состояние, память, город, время).

Что делает
----------
1. Каждые WORKER_INTERVAL секунд берёт страницу выдачи.
2. Новые id (которых нет в worker-seen-N.json) пишет в worker-findings-N.jsonl
   и пушит в СВОЮ ветку worker-N.
3. Если нашёл новое — будит главный процесс (monitor.yml) через workflow_dispatch.
   Если главный уже работает, его concurrency оставит вызов в очереди и запустит
   сразу после окончания текущего разбора: находка не теряется.

Чего НЕ делает: не вызывает Gemini, не пишет в Telegram, не трогает main.
Поэтому 3 воркера и 30 воркеров одинаково безопасны для общего состояния.
"""
import json
import os
import re
import subprocess
import time

import requests
from bs4 import BeautifulSoup

SEARCH_URL = os.environ.get("SOMON_URL") or (
    "https://m.somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/"
    "sostoyanie---1/?ordering=newest&location=185,187,195,204,205,230,210,180")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
WORKER_INDEX = int(os.environ.get("WORKER_INDEX", "1"))
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "3"))
WORKER_INTERVAL = float(os.environ.get("WORKER_INTERVAL", "15"))
WORKER_BRANCH = os.environ.get("WORKER_BRANCH") or f"worker-{WORKER_INDEX}"
WORKER_MAX_RUNTIME = float(os.environ.get("WORKER_MAX_RUNTIME", "20400"))  # 5 ч 40 мин
FINDINGS_FILE = f"worker-findings-{WORKER_INDEX}.jsonl"
SEEN_FILE = f"worker-seen-{WORKER_INDEX}.json"
GH_REPO = os.environ.get("GH_REPO", "")
GH_PAT = os.environ.get("GH_PAT", "")
MAIN_WORKFLOW = os.environ.get("MAIN_WORKFLOW", "monitor.yml")
WAKE_MIN_GAP = float(os.environ.get("WAKE_MIN_GAP", "120"))
# Сколько запросов выдачи в минуту делает ОДИН воркер.
REQUESTS_PER_MINUTE = 60.0 / max(WORKER_INTERVAL, 1.0)

PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*(?:c\.|с\.|TJS|сомони)", re.I)
MEMORY_RE = re.compile(r"(\d{2,4})\s*(?:gb|гб)", re.I)
CITY_RE = re.compile(r"Город:\s*([А-ЯЁ][А-Яа-яЁё\- ]{2,30}?)(?=\s+\d|\s*$)")
TIME_RE = re.compile(r"(\d+\s*(?:минут|час|дн\w*|секунд)\w*\s*назад|Сегодня|Вчера)", re.I)


def absolute_url(href):
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://m.somon.tj" + href
    return href


def parse_cards(html):
    """Карточки со страницы выдачи: id, заголовок, цена, память, город, время.

    Разбор намеренно простой: у воркера задача — как можно раньше заметить новый
    id, а точные детали (IMEI, состояние, описание, фото) читает главный процесс
    уже со страницы объявления.
    """
    soup = BeautifulSoup(html, "html.parser")
    out, seen_links = [], set()
    for a in soup.find_all("a", href=re.compile(r"/adv/|/item/")):
        href = absolute_url(a.get("href", ""))
        if not href or href in seen_links:
            continue
        card = a
        for _ in range(3):                       # поднимаемся до контейнера карточки
            if card.parent is None:
                break
            card = card.parent
            if len(card.get_text(" ", strip=True)) > 40:
                break
        text = card.get_text(" ", strip=True)
        # Заголовок берём из самой ссылки, а не из всей карточки: в тексте карточки
        # рядом лежат цена, город и время, и они попадали в название лота.
        title = a.get_text(" ", strip=True)[:120] or text.split(",")[0].strip()[:120]
        if not title:
            continue
        seen_links.add(href)
        price_m = PRICE_RE.search(text)
        mem_m = MEMORY_RE.search(text)
        city_m = CITY_RE.search(text)
        time_m = TIME_RE.search(text)
        out.append({
            "id": re.sub(r"\D", "", href)[-8:] or href,
            "title": title,
            "price": int(re.sub(r"\D", "", price_m.group(1))) if price_m else None,
            "memory": int(mem_m.group(1)) if mem_m else None,
            "condition": "Новый" if re.search(r"\bНов(ый|ое)\b", text) else None,
            "url": href,
            "city_list": city_m.group(1).strip() if city_m else None,
            "published_list": time_m.group(1) if time_m else None,
        })
    return out


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ {path} не читается ({e}) — начинаю с пустого")
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip()[:300])
    if r.returncode != 0 and r.stderr.strip():
        print(f"⚠️ {cmd} -> {r.stderr.strip()[:250]}")
    return r.returncode


def commit_and_push():
    """Записать находки в свою ветку. Конфликтов нет: ветка только у этого воркера."""
    run(f"git add {FINDINGS_FILE} {SEEN_FILE}")
    if run("git diff --cached --quiet") == 0:
        return False
    run(f'git commit -q -m "worker {WORKER_INDEX}: findings"')
    for i in (1, 2, 3):
        if run(f"git pull --rebase --autostash origin {WORKER_BRANCH}") == 0 and \
           run(f"git push origin HEAD:{WORKER_BRANCH}") == 0:
            print(f"✅ находки в ветке {WORKER_BRANCH} (попытка {i})")
            return True
        run("git rebase --abort")
        time.sleep(5 * i)
    print("❌ не удалось запушить находки")
    return False


def wake_main(reason):
    """Разбудить главный процесс (workflow_dispatch).

    Нужен свой токен (secrets.GH_PAT с правом actions: write): встроенный
    GITHUB_TOKEN вызывает workflow_dispatch не всегда. Если токена нет — воркер
    продолжает работать, а главный подхватит находки по своему расписанию.
    """
    if not (GH_REPO and GH_PAT):
        print("ℹ️ GH_PAT не задан — главный процесс подхватит находки по расписанию")
        return False
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{MAIN_WORKFLOW}/dispatches"
    try:
        r = requests.post(url, timeout=15,
                          headers={"Authorization": f"Bearer {GH_PAT}",
                                   "Accept": "application/vnd.github+json"},
                          json={"ref": "main"})
        if r.status_code in (200, 201, 204):
            print(f"🔔 главный процесс разбужен ({reason})")
            return True
        print(f"⚠️ dispatch -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ dispatch не удался: {e}")
    return False


def slot_sleep():
    """Ждать до своего слота.

    Фиксированный sleep здесь не годится: раннеры стартуют вразнобой (20-60 с),
    поэтому у каждого воркера свой слот внутри общего периода — воркер N просыпается
    на отметке (N-1) * WORKER_INTERVAL / WORKER_COUNT от начала каждых
    WORKER_INTERVAL секунд. Так WORKER_COUNT воркеров с WORKER_INTERVAL=15 дают
    запрос раз в 15/WORKER_COUNT секунд, не сталкиваясь.
    """
    step = max(WORKER_INTERVAL / max(WORKER_COUNT, 1), 1.0)
    offset = (WORKER_INDEX - 1) * step
    now = time.time()
    next_tick = now - (now % WORKER_INTERVAL) + offset
    while next_tick <= now:
        next_tick += WORKER_INTERVAL
    wait = next_tick - now
    print(f"⏳ слот {WORKER_INDEX}: жду {wait:.1f} с (шаг {step:.1f} с)")
    time.sleep(wait)


def main():
    started = time.time()
    seen = load_json(SEEN_FILE, {})
    print(f"=== Worker {WORKER_INDEX}/{WORKER_COUNT} | ветка {WORKER_BRANCH} | "
          f"интервал {WORKER_INTERVAL} с (~{REQUESTS_PER_MINUTE:.1f} запросов в минуту) ===")
    last_wake, cycles, total_new = 0.0, 0, 0

    while time.time() - started < WORKER_MAX_RUNTIME:
        slot_sleep()
        cycles += 1
        try:
            resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            found = parse_cards(resp.text)
        except Exception as e:
            print(f"⚠️ выдача не загрузилась: {type(e).__name__}: {str(e)[:150]}")
            continue
        new = [it for it in found if it["id"] not in seen]
        for it in new:
            it["found_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            it["worker"] = WORKER_INDEX
            seen[it["id"]] = it
        if new:
            with open(FINDINGS_FILE, "a", encoding="utf-8") as f:
                for it in new:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            save_json(SEEN_FILE, seen)
            total_new += len(new)
            print(f"🔍 цикл {cycles}: новых {len(new)} (всего {total_new}); карточек {len(found)}")
            commit_and_push()
            if time.time() - last_wake >= WAKE_MIN_GAP and wake_main(f"новая находка {new[0]['id']}"):
                last_wake = time.time()
        else:
            print(f"цикл {cycles}: новых нет (карточек {len(found)}, в памяти {len(seen)})")
        if len(seen) > 5000:            # воркеру хватает последних 5000 id
            for k in list(seen.keys())[:len(seen) - 5000]:
                seen.pop(k, None)

    print(f"=== Worker {WORKER_INDEX} завершил: {cycles} опросов, находок {total_new} ===")
    save_json(SEEN_FILE, seen)
    commit_and_push()


if __name__ == "__main__":
    main()
