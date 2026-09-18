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

# Анализ намеренно структурированный: эти данные сохраняются в JSONL и затем
# могут использоваться для сравнения состояния и цены разных телефонов.
PHOTO_PROMPT = """
Ты — эксперт по оценке смартфонов для анализа рыночной стоимости. Изучи текст
объявления и фотографии. Верни ТОЛЬКО корректный JSON без markdown и пояснений
строго такой формы:
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
  "accessories": "",
  "overall_visual_condition": "новое|как новое|хорошее|среднее|плохое|неизвестно",
  "visible_defects": [],
  "positive_features": [],
  "confidence": 0.0,
  "valuation_notes": ""
}

Правила:
- null означает, что по тексту и фото это определить нельзя; ничего не выдумывай.
- Отделяй состояние, указанное продавцом, от состояния, видимого на фото.
- Описывай конкретно: где царапина, есть ли трещина, скол, вмятина, следы ремонта.
- Если дефект виден неуверенно, укажи это в description и снизь confidence.
- battery.health_percent заполняй только если процент явно виден в тексте или на фото.
- accessories заполняй только по тексту объявления или явно видимым предметам.
- valuation_notes должны кратко объяснять, какие дефекты могут снизить стоимость.
""".strip()

TRANSLIT_MAP = {
    "iphone": ["айфон"], "samsung": ["самсунг"], "honor": ["хонор"],
    "xiaomi": ["сяоми", "ксиаоми"], "redmi": ["редми"],
    "huawei": ["хуавей"], "google": ["гугл"], "pixel": ["пиксель"],
}
CONDITION_RE = re.compile(
    r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b"
    r"(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.IGNORECASE
)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.IGNORECASE)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"⚠️ Ошибка в файле {path}: {e}. Использую значение по умолчанию.")
    return default


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def log_listing(item, photo_analysis=None):
    # Одна неизменяемая строка на каждый собранный лот — удобный формат для
    # последующего импорта в DuckDB, pandas или отдельную модель оценки цены.
    record = {
        "id": item["id"], "title": item["title"], "price": item["price"],
        "condition_seller": item.get("condition"), "memory_gb": item.get("memory"),
        "description": item.get("description", ""), "url": item["url"],
        "photo_analysis": photo_analysis, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_telegram(text):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15,
        )
        if not resp.ok:
            print("Ошибка Telegram:", resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить в Telegram:", e)


def check_telegram_commands(searches):
    offset = load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": offset}, timeout=15,
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        print("Не удалось получить команды из Telegram:", e)
        return searches

    changed = False
    for upd in updates:
        offset = upd["update_id"] + 1
        text = upd.get("message", {}).get("text", "")
        if text.startswith("/add "):
            try:
                parts = [p.strip() for p in text[5:].split("|")]
                name = parts[0]
                keywords = [k.strip() for k in parts[1].split(",")] if len(parts) > 1 and parts[1] else None
                max_price = int(parts[2]) if len(parts) > 2 and parts[2] else None
                min_memory = int(parts[3]) if len(parts) > 3 and parts[3] else None
                new_search = {"name": name}
                if keywords: new_search["query"] = keywords
                if max_price: new_search["max_price"] = max_price
                if min_memory: new_search["min_memory"] = min_memory
                searches = [s for s in searches if s["name"] != name] + [new_search]
                changed = True
                send_telegram(f"✅ Поиск «{name}» сохранён.")
            except Exception as e:
                send_telegram(f"⚠️ Формат: /add Название | слово1,слово2 | макс_цена | мин_память\nОшибка: {e}")
        elif text.startswith("/del "):
            name = text[5:].strip()
            before = len(searches)
            searches = [s for s in searches if s["name"] != name]
            changed = True
            send_telegram(f"🗑 Удалён «{name}»." if len(searches) < before else f"⚠️ «{name}» не найден.")
        elif text.strip() == "/list":
            send_telegram("📋 Поиски:\n" + "\n".join(
                f"• {s['name']}: {s.get('query', '—')}, до {s.get('max_price', '∞')} TJS" for s in searches
            ) if searches else "Поисков пока нет.")

    if changed:
        with open(SEARCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(searches, f, ensure_ascii=False, indent=2)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    return searches


def normalize_condition(value):
    value = re.sub(r"\s+", "", value.lower())
    if value.startswith("нов"): return "Новый"
    if value.startswith(("б/", "б.")): return "Б/у"
    if value.startswith("восстанов"): return "Восстановлен"
    return value


def clean_title(raw_text):
    text = NOISE_RE.sub("", raw_text)
    price_match = PRICE_RE.search(text)
    price = int(price_match.group(1).replace(" ", "")) if price_match else None
    if price_match: text = text[price_match.end():]
    cond_match = CONDITION_RE.search(text)
    if cond_match:
        title = text[:cond_match.start()].strip()
        condition = normalize_condition(cond_match.group(1))
        memory = int(cond_match.group(2)) if cond_match.group(2) else None
    else:
        title, condition, memory = text.strip(), None, None
    return title, price, condition, memory


def absolute_url(url):
    if url.startswith("//"): return "https:" + url
    if url.startswith("/"): return "https://m.somon.tj" + url
    return url


def parse_detail(ad_url):
    """Получает обязательное состояние и полный текст описания из карточки."""
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text(" ", strip=True)
    condition_match = CONDITION_RE.search(full_text)
    condition = normalize_condition(condition_match.group(1)) if condition_match else None
    memory = int(condition_match.group(2)) if condition_match and condition_match.group(2) else None
    return condition, memory, full_text


def fetch_listings():
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}"))
    listings, seen_links = [], set()
    for a in candidates:
        href = absolute_url(a.get("href", ""))
        if not href or href in seen_links: continue
        seen_links.add(href)
        raw_text = a.get_text(" ", strip=True)
        if len(raw_text) < 5: continue
        title, price, condition, memory = clean_title(raw_text)
        if not title: continue
        try:
            detail_condition, detail_memory, description = parse_detail(href)
            condition = condition or detail_condition
            memory = memory or detail_memory
        except Exception as e:
            description = ""
            print("Не удалось получить карточку", href, ":", e)
        ad_id = re.sub(r"\D", "", href)[-8:] or href
        listings.append({
            "id": ad_id, "title": title, "price": price, "condition": condition,
            "memory": memory, "description": description, "url": href,
        })
    return listings, soup


def fetch_media(ad_url):
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
            if src not in seen: seen.add(src); urls.append(src)
    return urls[:4]


def parse_gemini_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
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
            img = requests.get(url, headers=HEADERS, timeout=15)
            img.raise_for_status()
            mime = "image/png" if ".png" in url.lower() else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img.content).decode()}})
        except Exception as e:
            print("Не удалось скачать фото:", url, e)
    if len(parts) == 1:
        return {"overall_visual_condition": "неизвестно", "visible_defects": [], "raw_text": "Фото не найдены"}
    for attempt in range(2):
        try:
            resp = requests.post(GEMINI_URL, json={"contents": [{"parts": parts}]}, timeout=60)
            if resp.ok:
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return parse_gemini_json(text)
            print("Ошибка Gemini:", resp.status_code, resp.text)
        except Exception as e:
            print("Сбой Gemini:", e)
        time.sleep(5)
    return {"overall_visual_condition": "неизвестно", "visible_defects": [], "raw_text": "Анализ не удался"}


def keyword_variants(keyword):
    keyword = keyword.lower()
    return [keyword] + TRANSLIT_MAP.get(keyword, [])


def matches_search(item, search):
    title = item["title"].lower()
    query = search.get("query")
    if query:
        keywords = query if isinstance(query, list) else [query]
        if not any(any(v in title for v in keyword_variants(k)) for k in keywords): return False
    if search.get("max_price") and item["price"] and item["price"] > search["max_price"]: return False
    if search.get("min_memory") and (not item.get("memory") or item["memory"] < search["min_memory"]): return False
    return True


def main():
    seen = load_json(SEEN_FILE, {})
    searches = check_telegram_commands(load_json(SEARCHES_FILE, []))
    try:
        listings, soup = fetch_listings()
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        return
    if not listings:
        print("Объявления не найдены:", soup.get_text()[:2000])
        return

    print(f"Поисков загружено: {len(searches)}; найдено объявлений: {len(listings)}")
    for item in listings:
        entry = seen.get(item["id"], {})
        entry.update({"condition": item.get("condition"), "memory": item.get("memory"), "description": item.get("description", "")})
        matched = [s["name"] for s in searches if matches_search(item, s)]
        if FULL_PAGE_SCAN: matched = matched or ["вся страница телефонов"]
        elif not matched: continue
        if entry.get("photo_ok"): continue
        try:
            analysis = analyze_listing(item, fetch_media(item["url"]))
            entry["photo_analysis"] = analysis
            entry["logged"] = True
            if not entry.get("notified"):
                condition = item.get("condition") or "не указано"
                memory = f" | {item['memory']} GB" if item.get("memory") else ""
                defects = ", ".join(analysis.get("visible_defects", [])) or "явные дефекты не определены"
                text = (
                    f"🔔 Новое объявление ({', '.join(matched)})\n\n{item['title']}\n"
                    f"📱 Состояние продавца: {condition}{memory}\n"
                    f"💰 {item['price'] or '—'} TJS\n🔗 {item['url']}\n"
                    f"\n🛠 Видимые дефекты: {defects}\n"
                    f"📊 Визуальное состояние: {analysis.get('overall_visual_condition', 'неизвестно')}\n"
                    f"📸 Полный анализ сохранён в {PRICE_HISTORY_FILE}"
                )
                send_telegram(text)
                entry["notified"] = True
            entry["photo_ok"] = "Анализ не удался" not in analysis.get("raw_text", "")
            log_listing(item, analysis)
            seen[item["id"]] = entry
            save_seen(seen)
            time.sleep(1.5)
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e)
            seen[item["id"]] = entry
            save_seen(seen)


if __name__ == "__main__":
    main()
