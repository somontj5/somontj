import os
import json
import re
import time
import base64
import requests
from bs4 import BeautifulSoup

# ==== НАСТРОЙКИ ====
SEARCH_URL = os.environ.get(
    "SOMON_URL",
    "https://m.somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/"
)
FULL_PAGE_SCAN = os.environ.get("FULL_PAGE_SCAN", "1").lower() not in {"0", "false", "no"}
SEEN_FILE = "seen_ids.json"
PRICE_HISTORY_FILE = "price_history.jsonl"
SEARCHES_FILE = "searches.json"
OFFSET_FILE = "telegram_offset.json"
MODE_FILE = "search_mode.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

PHOTO_PROMPT = """
Ты — эксперт по оценке смартфонов для анализа рыночной стоимости. Изучи текст объявления и фотографии. Верни ТОЛЬКО корректный JSON без markdown строго такой формы:
{
  "screen": {"scratches": null, "cracks": null, "description": ""},
  "body": {"scratches": null, "dents": null, "cracks": null, "description": ""},
  "back_glass": {"damaged": null, "description": ""},
  "camera": {"damaged": null, "description": ""},
  "battery": {"health_percent": null, "description": ""},
  "face_id_or_fingerprint": {"working": null, "description": ""},
  "charging": {"working": null, "description": ""},
  "repairs_or_replacement": {"detected": null, "description": ""},
  "icloud_or_google_lock": {"locked": null, "description": ""},
  "accessories": "", "overall_visual_condition": "новое|как новое|хорошее|среднее|плохое|неизвестно",
  "visible_defects": [], "positive_features": [], "confidence": 0.0, "valuation_notes": ""
}
Не выдумывай: null означает, что это нельзя определить. Отделяй состояние продавца от состояния на фото. Описывай конкретные дефекты и указывай, что может снизить стоимость. Заполняй battery.health_percent только если процент явно указан.
""".strip()

TRANSLIT_MAP = {
    "iphone": ["айфон"], "samsung": ["самсунг"], "honor": ["хонор"],
    "xiaomi": ["сяоми", "ксиаоми"], "redmi": ["редми"],
    "huawei": ["хуавей"], "google": ["гугл"], "pixel": ["пиксель"],
}
CONDITION_RE = re.compile(r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.I)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.I)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")
MODES = {"all": "все объявления категории", "used": "только Б/у", "params": "только заданные параметры"}


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except json.JSONDecodeError as e: print(f"⚠️ Ошибка в файле {path}: {e}")
    return default


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f: json.dump(seen, f, ensure_ascii=False, indent=2)


def log_listing(item, photo_analysis=None):
    record = {
        "id": item["id"], "title": item["title"], "price": item["price"],
        "condition_seller": item.get("condition"), "memory_gb": item.get("memory"),
        "official_fields": item.get("official_fields", {}), "description": item.get("description", ""),
        "url": item["url"], "photo_analysis": photo_analysis,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f: f.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_telegram(text):
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if not resp.ok: print("Ошибка Telegram:", resp.status_code, resp.text)
    except Exception as e: print("Не удалось отправить в Telegram:", e)


def set_mode(mode):
    with open(MODE_FILE, "w", encoding="utf-8") as f: json.dump({"mode": mode}, f, ensure_ascii=False, indent=2)


def get_mode():
    mode = load_json(MODE_FILE, {}).get("mode")
    if mode in MODES: return mode
    # Старое поведение сохраняется после обновления: полный скан категории.
    return "all" if FULL_PAGE_SCAN else "params"


def check_telegram_commands(searches):
    offset = load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset}, timeout=15)
        resp.raise_for_status(); updates = resp.json().get("result", [])
    except Exception as e:
        print("Не удалось получить команды из Telegram:", e); return searches
    changed = False
    for upd in updates:
        offset = upd["update_id"] + 1
        text = upd.get("message", {}).get("text", "").strip()
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in ("/all", "/used", "/params"):
            mode = {"/all": "all", "/used": "used", "/params": "params"}[command]
            set_mode(mode); send_telegram(f"✅ Режим поиска: {MODES[mode]}.")
        elif command == "/mode":
            mode = argument.lower()
            aliases = {"all": "all", "все": "all", "used": "used", "бу": "used", "б/у": "used", "params": "params", "параметры": "params"}
            if mode in aliases:
                mode = aliases[mode]; set_mode(mode); send_telegram(f"✅ Режим поиска: {MODES[mode]}.")
            else: send_telegram("Формат: /mode all | used | params")
        elif command == "/add":
            try:
                # /add Название | слова | макс_цена | мин_память | состояние
                parts = [p.strip() for p in argument.split("|")]
                if not parts[0]: raise ValueError("не указано название")
                new_search = {"name": parts[0]}
                if len(parts) > 1 and parts[1]: new_search["query"] = [x.strip() for x in parts[1].split(",") if x.strip()]
                if len(parts) > 2 and parts[2]: new_search["max_price"] = int(parts[2])
                if len(parts) > 3 and parts[3]: new_search["min_memory"] = int(parts[3])
                if len(parts) > 4 and parts[4]:
                    requested = normalize_condition(parts[4])
                    if requested not in {"Новый", "Б/у", "Восстановлен"}: raise ValueError("состояние: Новый, Б/у или Восстановлен")
                    new_search["condition"] = requested
                searches = [s for s in searches if s["name"] != new_search["name"]] + [new_search]
                changed = True; send_telegram(f"✅ Параметры «{new_search['name']}» сохранены.")
            except Exception as e:
                send_telegram(f"⚠️ Формат: /add Название | слова | макс_цена | мин_память | состояние\nОшибка: {e}")
        elif command == "/del":
            name = argument.strip(); before = len(searches); searches = [s for s in searches if s["name"] != name]; changed = True
            send_telegram(f"🗑 Удалён «{name}»." if len(searches) < before else f"⚠️ «{name}» не найден.")
        elif command == "/list":
            lines = [f"• {s['name']}: слова={s.get('query', '—')}, до={s.get('max_price', '∞')}, память от={s.get('min_memory', '—')}, состояние={s.get('condition', '—')}" for s in searches]
            send_telegram(f"🔎 Режим: {MODES[get_mode()]}\n" + ("\n".join(lines) if lines else "Параметров нет."))
        elif command == "/help":
            send_telegram("Команды:\n/all — вся категория телефонов\n/used — только Б/у\n/params — только параметры /add\n/mode all|used|params\n/add Название | слова | макс_цена | мин_память | состояние\n/del Название\n/list")
    if changed:
        with open(SEARCHES_FILE, "w", encoding="utf-8") as f: json.dump(searches, f, ensure_ascii=False, indent=2)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f: json.dump({"offset": offset}, f)
    return searches


def normalize_condition(value):
    value = re.sub(r"\s+", "", value.lower())
    if value.startswith("нов"): return "Новый"
    if value.startswith(("б/", "б.")): return "Б/у"
    if value.startswith("восстанов"): return "Восстановлен"
    return value


def clean_title(raw_text):
    text = NOISE_RE.sub("", raw_text); price_match = PRICE_RE.search(text)
    price = int(price_match.group(1).replace(" ", "")) if price_match else None
    if price_match: text = text[price_match.end():]
    cond_match = CONDITION_RE.search(text)
    if cond_match: title, condition, memory = text[:cond_match.start()].strip(), normalize_condition(cond_match.group(1)), int(cond_match.group(2)) if cond_match.group(2) else None
    else: title, condition, memory = text.strip(), None, None
    return title, price, condition, memory


def absolute_url(url):
    if url.startswith("//"): return "https:" + url
    if url.startswith("/"): return "https://m.somon.tj" + url
    return url


def labeled_value(soup, labels):
    """Читает именно строку обязательного поля, а не случайное слово из описания."""
    labels = {x.lower() for x in labels}
    for node in soup.find_all(string=True):
        label = re.sub(r"\s+", " ", node.strip()).strip(" :")
        if label.lower() not in labels: continue
        parent = node.parent
        for container in [parent, parent.parent, parent.parent.parent if parent.parent else None]:
            if not container: continue
            parts = [x.strip() for x in container.get_text("|", strip=True).split("|") if x.strip()]
            for i, part in enumerate(parts):
                if part.lower().strip(" :") in labels and i + 1 < len(parts): return parts[i + 1]
            text = container.get_text(" ", strip=True)
            value = re.sub(re.escape(label), "", text, count=1, flags=re.I).strip(" :|-—")
            if value and len(value) < 120: return value
    return None


def parse_detail(ad_url):
    resp = requests.get(ad_url, headers=HEADERS, timeout=20); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser"); full_text = soup.get_text(" ", strip=True)
    fields = {
        "city": labeled_value(soup, ["Город"]), "color": labeled_value(soup, ["Цвет"]),
        "condition": labeled_value(soup, ["Состояние"]),
        "memory": labeled_value(soup, ["Встроенная память", "Память"]),
        "imei_status": labeled_value(soup, ["IMEI телефона", "IMEI"]),
        "device_model": labeled_value(soup, ["Модель"]),
    }
    condition = normalize_condition(fields["condition"]) if fields["condition"] else None
    memory_match = re.search(r"(\d+)\s*(?:gb|гб)", fields["memory"] or "", re.I)
    memory = int(memory_match.group(1)) if memory_match else None
    if not condition:
        match = CONDITION_RE.search(full_text); condition = normalize_condition(match.group(1)) if match else None
    if not memory:
        match = re.search(r"(?:Новый|Б\s*/\s*у|Восстановлен\w*)\s*[·|,;-]\s*(\d+)\s*gb", full_text, re.I); memory = int(match.group(1)) if match else None
    return condition, memory, full_text, fields


def fetch_listings():
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20); resp.raise_for_status(); soup = BeautifulSoup(resp.text, "html.parser")
    listings, seen_links = [], set()
    for a in soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}")):
        href = absolute_url(a.get("href", ""))
        if not href or href in seen_links: continue
        seen_links.add(href); raw_text = a.get_text(" ", strip=True)
        if len(raw_text) < 5: continue
        title, price, condition, memory = clean_title(raw_text)
        if not title: continue
        try:
            detail_condition, detail_memory, description, fields = parse_detail(href)
            condition, memory = condition or detail_condition, memory or detail_memory
        except Exception as e:
            description, fields = "", {}; print("Не удалось получить карточку", href, ":", e)
        listings.append({"id": re.sub(r"\D", "", href)[-8:] or href, "title": title, "price": price, "condition": condition, "memory": memory, "description": description, "official_fields": fields, "url": href})
    return listings, soup


def fetch_media(ad_url):
    resp = requests.get(ad_url, headers=HEADERS, timeout=20); resp.raise_for_status(); soup = BeautifulSoup(resp.text, "html.parser")
    urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]) and src not in seen: seen.add(src); urls.append(src)
    return urls[:4]


def parse_gemini_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try: return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try: return json.loads(text[start:end + 1])
            except json.JSONDecodeError: pass
    return {"raw_text": text, "overall_visual_condition": "неизвестно", "visible_defects": []}


def analyze_listing(item, photo_urls):
    parts = [{"text": PHOTO_PROMPT + "\n\nТекст объявления:\n" + item.get("description", "")}]
    for url in photo_urls:
        try:
            img = requests.get(url, headers=HEADERS, timeout=15); img.raise_for_status()
            parts.append({"inline_data": {"mime_type": "image/png" if ".png" in url.lower() else "image/jpeg", "data": base64.b64encode(img.content).decode()}})
        except Exception as e: print("Не удалось скачать фото:", url, e)
    if len(parts) == 1: return {"overall_visual_condition": "неизвестно", "visible_defects": [], "raw_text": "Фото не найдены"}
    for _ in range(2):
        try:
            resp = requests.post(GEMINI_URL, json={"contents": [{"parts": parts}]}, timeout=60)
            if resp.ok: return parse_gemini_json(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            print("Ошибка Gemini:", resp.status_code, resp.text)
        except Exception as e: print("Сбой Gemini:", e)
        time.sleep(5)
    return {"overall_visual_condition": "неизвестно", "visible_defects": [], "raw_text": "Анализ не удался"}


def keyword_variants(keyword):
    keyword = keyword.lower(); return [keyword] + TRANSLIT_MAP.get(keyword, [])


def matches_search(item, search):
    title = item["title"].lower(); query = search.get("query")
    if query:
        keywords = query if isinstance(query, list) else [query]
        if not any(any(v in title for v in keyword_variants(k)) for k in keywords): return False
    if search.get("max_price") and item["price"] and item["price"] > search["max_price"]: return False
    if search.get("min_memory") and (not item.get("memory") or item["memory"] < search["min_memory"]): return False
    if search.get("condition") and item.get("condition") != search["condition"]: return False
    return True


def selected(item, searches, mode):
    if mode == "all": return True
    if mode == "used": return item.get("condition") == "Б/у"
    return any(matches_search(item, search) for search in searches)


def main():
    seen = load_json(SEEN_FILE, {}); searches = check_telegram_commands(load_json(SEARCHES_FILE, [])); mode = get_mode()
    try: listings, soup = fetch_listings()
    except Exception as e: print("Не удалось загрузить Somon.tj:", e); return
    if not listings: print("Объявления не найдены:", soup.get_text()[:2000]); return
    print(f"Режим: {MODES[mode]}; поисков: {len(searches)}; объявлений: {len(listings)}")
    for item in listings:
        if not selected(item, searches, mode): continue
        entry = seen.get(item["id"], {}); entry.update({"condition": item.get("condition"), "memory": item.get("memory"), "official_fields": item.get("official_fields", {}), "description": item.get("description", "")})
        if entry.get("photo_ok"): continue
        try:
            analysis = analyze_listing(item, fetch_media(item["url"])); entry["photo_analysis"] = analysis
            defects = ", ".join(analysis.get("visible_defects", [])) or "явные дефекты не определены"
            if not entry.get("notified"):
                text = (f"🔔 Новое объявление ({MODES[mode]})\n\n{item['title']}\n📱 Состояние продавца: {item.get('condition') or 'не указано'}\n"
                        f"💰 {item['price'] or '—'} TJS\n🛠 Видимые дефекты: {defects}\n📊 Визуальное состояние: {analysis.get('overall_visual_condition', 'неизвестно')}\n🔗 {item['url']}\n\n📸 Полный анализ сохранён в {PRICE_HISTORY_FILE}")
                send_telegram(text); entry["notified"] = True
            entry["photo_ok"] = "Анализ не удался" not in analysis.get("raw_text", "")
            log_listing(item, analysis); seen[item["id"]] = entry; save_seen(seen); time.sleep(1.5)
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e); seen[item["id"]] = entry; save_seen(seen)


if __name__ == "__main__": main()
