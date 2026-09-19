import os
import json
import re
import time
import base64
import statistics
import requests
from bs4 import BeautifulSoup

# ==== НАСТРОЙКИ ====
SEARCH_URL = os.environ.get(
    "SOMON_URL",
    "https://m.somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/sostoyanie---1/?ordering=newest"
)
MAX_NEW_ITEMS_PER_RUN = int(os.environ.get("MAX_NEW_ITEMS_PER_RUN", "5"))

SEEN_FILE = "seen_ids.json"
PRICE_HISTORY_FILE = "price_history.jsonl"
REJECTED_FILE = "rejected_lots.jsonl"
SEARCHES_FILE = "searches.json"
OFFSET_FILE = "telegram_offset.json"
MODE_FILE = "search_mode.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

TRANSLIT_MAP = {
    "iphone": ["айфон"], "samsung": ["самсунг"], "honor": ["хонор"],
    "xiaomi": ["сяоми", "ксиаоми"], "redmi": ["редми"],
    "huawei": ["хуавей"], "google": ["гугл"], "pixel": ["пиксель"],
}
KNOWN_BRANDS = ["iphone", "apple", "samsung", "xiaomi", "redmi", "honor",
                "huawei", "tecno", "infinix", "nokia", "google", "pixel", "oppo", "vivo"]
NOISE_WORDS = {"vietnam", "global", "version", "black", "white", "gold", "silver",
               "blue", "green", "pink", "gray", "grey", "new", "оригинал"}

CONDITION_RE = re.compile(r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.I)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.I)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")
DESC_RE = re.compile(r"Описание\s*(.*?)\s*(?:Показать телефон|Начать чат|Пожаловаться|$)", re.S)
MODES = {"all": "все объявления категории", "used": "только Б/у", "params": "только заданные параметры"}


# ---------- Хранилище ----------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"⚠️ Ошибка в файле {path}: {e}")
    return default


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def extract_model_key(title):
    """Бренд + основная модель, без цвета/региона/мусора — надёжный ключ для сравнения."""
    words = re.findall(r"[a-zа-я0-9]+", title.lower())
    brand = next((w for w in words if w in KNOWN_BRANDS), None)
    if not brand:
        return None
    model_words = []
    started = False
    for w in words:
        if w == brand:
            started = True
            continue
        if not started:
            continue
        if w in NOISE_WORDS or (w.isdigit() and int(w) >= 32) or w.endswith("gb"):
            break
        model_words.append(w)
    return f"{brand} {' '.join(model_words[:3])}".strip()


def log_market_point(item):
    """Дешёвая запись для базы цен — для ВСЕХ объявлений, включая VIP."""
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "market_point", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "vip": item.get("vip", False),
            "model_key": extract_model_key(item["title"]),
            "date": time.strftime("%Y-%m-%d"),
        }, ensure_ascii=False) + "\n")


def log_full_analysis(item, analysis, verdict):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "full_analysis", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "description": item.get("description", ""),
            "model_key": extract_model_key(item["title"]),
            "photo_analysis": analysis, "market_verdict": verdict,
            "url": item["url"], "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_rejected(item, analysis):
    with open(REJECTED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": item["id"], "title": item["title"], "price": item["price"],
            "url": item["url"], "verdict": analysis.get("market_verdict"),
            "reasoning": analysis.get("reasoning"), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def market_stats_for(title, condition, exclude_id):
    """Медиана/минимум по похожим прошлым объявлениям — сравнение по бренду+модели, не по словам."""
    key = extract_model_key(title)
    if not key or not os.path.exists(PRICE_HISTORY_FILE):
        return None

    prices = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == exclude_id or not rec.get("price"):
                continue
            if condition and rec.get("condition") and rec.get("condition") != condition:
                continue
            if rec.get("model_key") != key:
                continue
            prices.append(rec["price"])

    if len(prices) < 3:
        return None
    return {"count": len(prices), "min": min(prices), "median": round(statistics.median(prices))}


# ---------- Telegram: отправка ----------

def send_telegram(text):
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if not resp.ok:
            print("Ошибка Telegram:", resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить в Telegram:", e)


# ---------- Режим и команды ----------

def set_mode(mode):
    with open(MODE_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f, ensure_ascii=False, indent=2)


def get_mode():
    mode = load_json(MODE_FILE, {}).get("mode")
    return mode if mode in MODES else "params"


def check_telegram_commands(searches):
    offset = load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                             params={"offset": offset}, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        print("Не удалось получить команды из Telegram:", e)
        return searches

    changed = False
    for upd in updates:
        offset = upd["update_id"] + 1
        text = upd.get("message", {}).get("text", "").strip()
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command in ("/all", "/used", "/params"):
            mode = command[1:]
            set_mode(mode)
            send_telegram(f"✅ Режим поиска: {MODES[mode]}.")
        elif command == "/mode":
            aliases = {"all": "all", "все": "all", "used": "used", "бу": "used", "б/у": "used",
                       "params": "params", "параметры": "params"}
            mode = aliases.get(argument.lower())
            if mode:
                set_mode(mode)
                send_telegram(f"✅ Режим поиска: {MODES[mode]}.")
            else:
                send_telegram("Формат: /mode all | used | params")
        elif command == "/add":
            try:
                parts = [p.strip() for p in argument.split("|")]
                if not parts[0]:
                    raise ValueError("не указано название")
                new_search = {"name": parts[0]}
                if len(parts) > 1 and parts[1]:
                    new_search["query"] = [x.strip() for x in parts[1].split(",") if x.strip()]
                if len(parts) > 2 and parts[2]:
                    new_search["max_price"] = int(parts[2])
                if len(parts) > 3 and parts[3]:
                    new_search["min_memory"] = int(parts[3])
                searches = [s for s in searches if s["name"] != new_search["name"]] + [new_search]
                changed = True
                send_telegram(f"✅ Параметры «{new_search['name']}» сохранены.")
            except Exception as e:
                send_telegram(f"⚠️ Формат: /add Название | слова | макс_цена | мин_память\nОшибка: {e}")
        elif command == "/del":
            name = argument.strip()
            before = len(searches)
            searches = [s for s in searches if s["name"] != name]
            changed = True
            send_telegram(f"🗑 Удалён «{name}»." if len(searches) < before else f"⚠️ «{name}» не найден.")
        elif command == "/list":
            lines = [f"• {s['name']}: {s.get('query', '—')}, до {s.get('max_price', '∞')}" for s in searches]
            send_telegram(f"🔎 Режим: {MODES[get_mode()]}\n" + ("\n".join(lines) if lines else "Параметров нет."))
        elif command == "/help":
            send_telegram("Команды:\n/all /used /params — режим\n/add Название | слова | макс_цена | мин_память\n/del Название\n/list")

    if changed:
        with open(SEARCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(searches, f, ensure_ascii=False, indent=2)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    return searches


# ---------- Разбор Somon.tj ----------

def normalize_condition(value):
    value = re.sub(r"\s+", "", (value or "").lower())
    if value.startswith("нов"):
        return "Новый"
    if value.startswith(("б/", "б.")):
        return "Б/у"
    if value.startswith("восстанов"):
        return "Восстановлен"
    return value or None


def clean_title(raw_text):
    is_vip = bool(re.search(r"\bVIP\b", raw_text, re.I))
    text = NOISE_RE.sub("", raw_text)
    price_match = PRICE_RE.search(text)
    price = int(price_match.group(1).replace(" ", "")) if price_match else None
    if price_match:
        text = text[price_match.end():]
    cond_match = CONDITION_RE.search(text)
    if cond_match:
        title = PRICE_RE.sub("", text[:cond_match.start()]).strip()
        condition = normalize_condition(cond_match.group(1))
        memory = int(cond_match.group(2)) if cond_match.group(2) else None
    else:
        title, condition, memory = text.strip(), None, None
    return title, price, condition, memory, is_vip


def absolute_url(url):
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://m.somon.tj" + url
    return url


def labeled_value(soup, labels):
    labels = {x.lower() for x in labels}
    for node in soup.find_all(string=True):
        label = re.sub(r"\s+", " ", node.strip()).strip(" :")
        if label.lower() not in labels:
            continue
        parent = node.parent
        for container in [parent, parent.parent, parent.parent.parent if parent.parent else None]:
            if not container:
                continue
            parts = [x.strip() for x in container.get_text("|", strip=True).split("|") if x.strip()]
            for i, part in enumerate(parts):
                if part.lower().strip(" :") in labels and i + 1 < len(parts):
                    return parts[i + 1]
    return None


def fetch_listings():
    """Лёгкий проход по странице категории (уже отфильтрованной по Б/у на уровне сайта)."""
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings, seen_links = [], set()
    for a in soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}")):
        href = absolute_url(a.get("href", ""))
        if not href or href in seen_links:
            continue
        seen_links.add(href)
        raw_text = a.get_text(" ", strip=True)
        if len(raw_text) < 5:
            continue
        title, price, condition, memory, is_vip = clean_title(raw_text)
        if not title:
            continue
        listings.append({
            "id": re.sub(r"\D", "", href)[-8:] or href,
            "title": title, "price": price, "condition": condition,
            "memory": memory, "url": href, "vip": is_vip,
        })
    return listings, soup


def fetch_detail(ad_url):
    """Дорогой шаг — вызывается только для кандидатов на полный анализ."""
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    condition = normalize_condition(labeled_value(soup, ["Состояние"]))
    memory_raw = labeled_value(soup, ["Встроенная память", "Память"])
    memory_match = re.search(r"(\d+)\s*(?:gb|гб)", memory_raw or "", re.I)
    memory = int(memory_match.group(1)) if memory_match else None

    desc_match = DESC_RE.search(full_text)
    description = desc_match.group(1).strip() if desc_match else full_text[:500]

    photo_urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]) and src not in seen:
            seen.add(src)
            photo_urls.append(src)

    return condition, memory, description, photo_urls[:4]


# ---------- Gemini ----------

def build_prompt(item, stats):
    stats_text = (
        f"По базе похожих объявлений: минимальная цена {stats['min']} TJS, "
        f"медианная {stats['median']} TJS, найдено {stats['count']} похожих."
        if stats else "Своих данных по похожим объявлениям пока недостаточно."
    )
    return f"""
Ты — эксперт по оценке б/у смартфонов для перепродажи. Изучи текст объявления и фото.

Объявление: {item['title']}
Цена: {item['price']} TJS
Состояние по словам продавца: {item.get('condition') or 'не указано'}
Описание продавца: {item.get('description', '')[:800]}

{stats_text}

Если своих данных по рынку недостаточно или хочешь свериться — используй поиск Google, чтобы проверить
актуальную цену такой модели б/у в Таджикистане и примерную стоимость ремонта видимых повреждений
(например, замена экрана), и учти это в вердикте.

Верни ТОЛЬКО JSON без markdown, строго такой формы:
{{
  "overall_visual_condition": "новое|как новое|хорошее|среднее|плохое|неизвестно",
  "visible_defects": [],
  "positive_features": [],
  "market_verdict": "недооценено|справедливая цена|переоценено|недостаточно данных",
  "estimated_resale_price": null,
  "reasoning": "коротко почему такой вердикт, с учётом цены, состояния, рынка и, если искал — данных из интернета",
  "confidence": 0.0
}}
Считай "недооценено" только если после вычета возможного ремонта телефон реально можно перепродать дороже с запасом, а не просто "дешевле среднего на глаз". Если данных мало — verdict "недостаточно данных", не выдумывай.
""".strip()


def parse_gemini_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {"market_verdict": "недостаточно данных", "reasoning": "не удалось разобрать ответ", "visible_defects": []}


def analyze_listing(item, photo_urls, stats):
    parts = [{"text": build_prompt(item, stats)}]
    for url in photo_urls:
        try:
            img = requests.get(url, headers=HEADERS, timeout=15)
            img.raise_for_status()
            mime = "image/png" if ".png" in url.lower() else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img.content).decode()}})
        except Exception as e:
            print("Не удалось скачать фото:", url, e)

    try:
        resp = requests.post(GEMINI_URL, json={
            "contents": [{"parts": parts}],
        }, timeout=60)
        if resp.ok:
            return parse_gemini_json(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        print("Ошибка Gemini:", resp.status_code, resp.text[:800])
    except Exception as e:
        print("Сбой Gemini:", e)
    return {"market_verdict": "недостаточно данных", "reasoning": "анализ не удался", "visible_defects": []}


# ---------- Фильтрация ----------

def keyword_variants(keyword):
    keyword = keyword.lower()
    return [keyword] + TRANSLIT_MAP.get(keyword, [])


def matches_search(item, search):
    title = item["title"].lower()
    query = search.get("query")
    if query:
        keywords = query if isinstance(query, list) else [query]
        if not any(any(v in title for v in keyword_variants(k)) for k in keywords):
            return False
    if search.get("max_price") and item["price"] and item["price"] > search["max_price"]:
        return False
    if search.get("min_memory") and (not item.get("memory") or item["memory"] < search["min_memory"]):
        return False
    return True


def selected(item, searches, mode):
    if mode == "all":
        return True
    if mode == "used":
        return item.get("condition") == "Б/у"
    return any(matches_search(item, search) for search in searches)


# ---------- Главная логика ----------

def main():
    seen = load_json(SEEN_FILE, {})
    searches = check_telegram_commands(load_json(SEARCHES_FILE, []))
    mode = get_mode()

    try:
        listings, soup = fetch_listings()
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        return

    if not listings:
        print("Объявления не найдены:", soup.get_text()[:2000])
        return

    print(f"Режим: {MODES[mode]}; поисков: {len(searches)}; объявлений: {len(listings)}")

    # Шаг 1: дёшево логируем ВСЕ объявления в базу цен (включая VIP, без сети, без Gemini)
    for item in listings:
        entry = seen.get(item["id"], {})
        if not entry.get("logged"):
            log_market_point(item)
            entry["logged"] = True
            seen[item["id"]] = entry
    save_seen(seen)

    # Шаг 2: кандидаты на полный анализ — без VIP, максимум MAX_NEW_ITEMS_PER_RUN за раз
    candidates = [
        item for item in listings
        if selected(item, searches, mode)
        and not item.get("vip")
        and not seen.get(item["id"], {}).get("photo_ok")
    ][:MAX_NEW_ITEMS_PER_RUN]

    print(f"На полный анализ в этом прогоне: {len(candidates)}")

    for item in candidates:
        entry = seen.get(item["id"], {})
        try:
            condition, memory, description, photo_urls = fetch_detail(item["url"])
            item["condition"] = condition or item.get("condition")
            item["memory"] = memory or item.get("memory")
            item["description"] = description

            stats = market_stats_for(item["title"], item.get("condition"), item["id"])
            analysis = analyze_listing(item, photo_urls, stats)
            verdict = analysis.get("market_verdict", "недостаточно данных")

            log_full_analysis(item, analysis, verdict)

            if verdict == "недооценено":
                defects = ", ".join(analysis.get("visible_defects", [])) or "не обнаружены"
                text = (
                    f"🔥 Потенциально выгодное объявление\n\n{item['title']}\n"
                    f"💰 {item['price'] or '—'} TJS\n"
                    f"📊 Визуальное состояние: {analysis.get('overall_visual_condition', 'неизвестно')}\n"
                    f"🛠 Дефекты: {defects}\n"
                    f"💡 {analysis.get('reasoning', '')}\n"
                    f"🔗 {item['url']}"
                )
                send_telegram(text)
                entry["notified"] = True
            else:
                log_rejected(item, analysis)

            entry["photo_ok"] = True
            seen[item["id"]] = entry
            time.sleep(2)
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e)
        finally:
            save_seen(seen)


if __name__ == "__main__":
    main()