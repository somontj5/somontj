import os
import json
import re
import time
import base64
import requests
from bs4 import BeautifulSoup

# ==== НАСТРОЙКИ — проверьте и поправьте после первого запуска ====
SEARCH_URL = os.environ.get("SOMON_URL", "https://somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/")
SEEN_FILE = "seen_ids.json"
PRICE_HISTORY_FILE = "price_history.jsonl"
SEARCHES_FILE = "searches.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

PHOTO_PROMPT = (
    "Ты — эксперт по оценке состояния б/у смартфонов. Осмотри фото объявления и опиши "
    "на русском: 1) царапины/трещины на экране; 2) повреждения корпуса; "
    "3) общее состояние (как новое/хорошее/среднее/плохое). Если не видно чётко — так и напиши. "
    "Кратко, 3-5 строк."
)


# ---------- Хранилище ----------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"⚠️ Ошибка в файле {path}: {e}. Использую значение по умолчанию.")
            return default
    return default


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def log_price(item):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": item["id"], "title": item["title"],
            "price": item["price"], "date": time.strftime("%Y-%m-%d"),
        }, ensure_ascii=False) + "\n")


# ---------- Telegram ----------

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if not resp.ok:
            print("Ошибка Telegram:", resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить в Telegram:", e)


# ---------- Somon.tj ----------

CONDITION_RE = re.compile(r"(Новый|Б/у|Б\.у\.|Восстановлен\w*)\s*·\s*(\d+)\s*gb", re.IGNORECASE)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.IGNORECASE)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс].")


def clean_title(raw_text):
    text = NOISE_RE.sub("", raw_text)
    price_match = PRICE_RE.search(text)
    price = int(price_match.group(1).replace(" ", "")) if price_match else None
    if price_match:
        text = text[price_match.end():]

    cond_match = CONDITION_RE.search(text)
    if cond_match:
        title = PRICE_RE.sub("", text[:cond_match.start()]).strip()
        condition = cond_match.group(1)
        memory = int(cond_match.group(2))
    else:
        title = text.strip()
        condition = None
        memory = None

    return title, price, condition, memory


def fetch_listings():
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidate_links = soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}"))

    listings, seen_links = [], set()
    for a in candidate_links:
        href = a.get("href")
        if not href or href in seen_links:
            continue
        seen_links.add(href)
        if href.startswith("/"):
            href = "https://somon.tj" + href

        raw_text = a.get_text(strip=True)
        if not raw_text or len(raw_text) < 5:
            continue

        title, price, condition, memory = clean_title(raw_text)
        if not title:
            continue

        ad_id = re.sub(r"\D", "", href)[-8:] or href
        listings.append({
            "id": ad_id, "title": title, "price": price,
            "condition": condition, "memory": memory, "url": href,
        })

    return listings, soup


def fetch_photo_urls(ad_url):
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    urls, seen = [], set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://somon.tj" + src
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
            if src not in seen:
                seen.add(src)
                urls.append(src)
    return urls[:4]


# ---------- Gemini ----------

def analyze_photos(photo_urls):
    if not photo_urls:
        return "Фото не найдены для анализа."

    parts = [{"text": PHOTO_PROMPT}]
    for url in photo_urls:
        try:
            img = requests.get(url, headers=HEADERS, timeout=15)
            img.raise_for_status()
            b64 = base64.b64encode(img.content).decode("utf-8")
            mime = "image/png" if ".png" in url.lower() else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        except Exception as e:
            print("Не удалось скачать фото:", url, e)

    if len(parts) == 1:
        return "Не удалось скачать ни одного фото."

    payload = {"contents": [{"parts": parts}]}
    for attempt in range(2):
        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=60)
            if resp.ok:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"Ошибка Gemini (попытка {attempt + 1}):", resp.status_code, resp.text)
        except Exception as e:
            print(f"Сбой запроса к Gemini (попытка {attempt + 1}):", e)
        time.sleep(5)

    return "⚠️ Анализ фото не удался."


# ---------- Поиск ----------

def matches_search(item, search):
    title = item["title"].lower()
    if search["query"].lower() not in title:
        return False
    if search.get("max_price") and item["price"] and item["price"] > search["max_price"]:
        return False
    if search.get("min_memory"):
        if not item.get("memory") or item["memory"] < search["min_memory"]:
            return False
    return True


# ---------- Главная логика ----------

def main():
    seen = load_json(SEEN_FILE, {})
    searches = load_json(SEARCHES_FILE, [])

    try:
        listings, soup = fetch_listings()
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        return

    if not listings:
        print("Объявления не найдены. Начало страницы для диагностики:")
        print(soup.get_text()[:2000])
        return

    print(f"Поисков загружено: {len(searches)}")
    print(f"Найдено объявлений на странице: {len(listings)}")
    for item in listings[:10]:
        print(f" - {item['title']!r} | цена: {item['price']} | id: {item['id']}")

    for item in listings:
        matched = [s["name"] for s in searches if matches_search(item, s)]
        if not matched:
            continue

        entry = seen.get(item["id"])
        if entry and entry.get("photo_ok"):
            continue

        try:
            if entry is None:
                log_price(item)
                photo_urls = fetch_photo_urls(item["url"])
                photo_analysis = analyze_photos(photo_urls)
                text = (
                    f"🔔 Новое объявление ({', '.join(matched)})\n\n{item['title']}\n"
                    f"💰 {item['price'] if item['price'] else '—'} TJS\n"
                    f"🔗 {item['url']}\n\n📸 Анализ фото:\n{photo_analysis}"
                )
                send_telegram(text)
                time.sleep(1.5)
            else:
                photo_urls = fetch_photo_urls(item["url"])
                photo_analysis = analyze_photos(photo_urls)
                if "не удался" not in photo_analysis:
                    send_telegram(f"📸 Обновление по объявлению:\n{item['url']}\n\n{photo_analysis}")
                    time.sleep(1.5)

            photo_ok = "не удался" not in photo_analysis
            seen[item["id"]] = {"notified": True, "photo_ok": photo_ok}
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e)
        finally:
            save_seen(seen)  # сохраняем после каждого объявления, а не в конце


if __name__ == "__main__":
    main()
