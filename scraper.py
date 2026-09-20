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
    "https://m.somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/sostoyanie---1/?ordering=newest&location=185,187,195,204,205,230,210,180"
)
MAX_NEW_ITEMS_PER_RUN = int(os.environ.get("MAX_NEW_ITEMS_PER_RUN", "5"))
MAX_SOLD_CHECKS_PER_RUN = int(os.environ.get("MAX_SOLD_CHECKS_PER_RUN", "5"))
SIMILAR_EXAMPLES_LIMIT = 8
GEMINI_DAILY_LIMIT_PER_COMBO = int(os.environ.get("GEMINI_DAILY_LIMIT_PER_COMBO", "450"))
TAVILY_MONTHLY_LIMIT_PER_KEY = int(os.environ.get("TAVILY_MONTHLY_LIMIT_PER_KEY", "950"))
GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY = int(os.environ.get("GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY", "90"))
FALLBACK_USD_TJS_RATE = float(os.environ.get("FALLBACK_USD_TJS_RATE", "10.5"))
ALERT_GAP_MINUTES = int(os.environ.get("ALERT_GAP_MINUTES", "40"))
ALERT_FAILURE_THRESHOLD = int(os.environ.get("ALERT_FAILURE_THRESHOLD", "3"))
DEFAULT_MIN_PROFIT = int(os.environ.get("DEFAULT_MIN_PROFIT", "0"))

SEEN_FILE = "seen_ids.json"
PRICE_HISTORY_FILE = "price_history.jsonl"
REJECTED_FILE = "rejected_lots.jsonl"
SEARCHES_FILE = "searches.json"
OFFSET_FILE = "telegram_offset.json"
MODE_FILE = "search_mode.json"
SUBSCRIBERS_FILE = "subscribers.json"
GEMINI_DAILY_FILE = "gemini_daily_usage.json"
SEARCH_USAGE_FILE = "search_provider_usage.json"
EXCHANGE_RATE_FILE = "exchange_rate.json"
HEALTH_FILE = "health_status.json"
MIN_PROFIT_FILE = "min_profit.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GEMINI_KEYS = []
if os.environ.get("GEMINI_API_KEY"):
    GEMINI_KEYS.append({"name": "key1", "key": os.environ["GEMINI_API_KEY"]})
if os.environ.get("GEMINI_API_KEY_2"):
    GEMINI_KEYS.append({"name": "key2", "key": os.environ["GEMINI_API_KEY_2"]})

GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
GEMINI_COMBOS = [
    {"id": f"{k['name']}::{m}", "key": k["key"], "model": m}
    for k in GEMINI_KEYS
    for m in GEMINI_MODELS
]

GOOGLE_SEARCH_CX = os.environ.get("GOOGLE_SEARCH_CX", "")

SEARCH_PROVIDERS = []
for i in range(1, 4):
    key = os.environ.get(f"TAVILY_API_KEY{'' if i == 1 else '_' + str(i)}", "")
    if key:
        SEARCH_PROVIDERS.append({
            "id": f"tavily{i}", "type": "tavily", "key": key,
            "period": "month", "limit": TAVILY_MONTHLY_LIMIT_PER_KEY,
        })
for i in range(1, 3):
    key = os.environ.get(f"GOOGLE_SEARCH_API_KEY{'' if i == 1 else '_' + str(i)}", "")
    if key and GOOGLE_SEARCH_CX:
        SEARCH_PROVIDERS.append({
            "id": f"google{i}", "type": "google", "key": key, "cx": GOOGLE_SEARCH_CX,
            "period": "day", "limit": GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY,
        })

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

TRANSLIT_MAP = {
    "iphone": ["айфон"], "samsung": ["самсунг"], "honor": ["хонор"],
    "xiaomi": ["сяоми", "ксиаоми"], "redmi": ["редми"],
    "huawei": ["хуавей"], "google": ["гугл"], "pixel": ["пиксель"],
    "realme": ["реалми"], "oneplus": ["ванплюс"],
}
KNOWN_BRANDS = ["iphone", "apple", "samsung", "xiaomi", "redmi", "honor",
                "huawei", "tecno", "infinix", "nokia", "google", "pixel", "oppo", "vivo",
                "realme", "itel", "oneplus", "vertu", "galaxy",
                "xioami", "xiаomi", "infinx", "realmi", "оppo", "poco"]
BRAND_ALIASES = {
    "xioami": "xiaomi", "xiаomi": "xiaomi", "infinx": "infinix",
    "realmi": "realme", "оppo": "oppo", "galaxy": "samsung", "poco": "poco",
}
NOISE_WORDS = {"vietnam", "global", "version", "black", "white", "gold", "silver",
               "blue", "green", "pink", "gray", "grey", "new", "оригинал"}

CONDITION_RE = re.compile(r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.I)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.I)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")
DESC_RE = re.compile(r"Описание\s*(.*?)\s*(?:Показать телефон|Начать чат|Пожаловаться|$)", re.S)

IMEI_WHITE_RE = re.compile(r"IMEI[^.]{0,60}в\s+бел\w*\s+списк", re.I)
IMEI_BLACK_RE = re.compile(r"IMEI[^.]{0,60}в\s+ч[её]рн\w*\s+списк", re.I)
IMEI_NOT_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}не\s+внес", re.I)
IMEI_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}(?<!не\s)внес", re.I)

SOLD_RE = re.compile(r"(?<!не\s)\bПродано\b", re.I)
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
    words = re.findall(r"[a-zа-я0-9]+", title.lower())
    brand = next((w for w in words if w in KNOWN_BRANDS), None)
    if not brand:
        return None
    canonical_brand = BRAND_ALIASES.get(brand, brand)
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
    return f"{canonical_brand} {' '.join(model_words[:3])}".strip()


def log_market_point(item):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "market_point", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "vip": item.get("vip", False),
            "model_key": extract_model_key(item["title"]),
            "date": time.strftime("%Y-%m-%d"),
        }, ensure_ascii=False) + "\n")


def log_full_analysis(item, analysis, verdict, data_sources):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "full_analysis", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "description": item.get("description", ""),
            "model_key": extract_model_key(item["title"]),
            "visible_defects": analysis.get("visible_defects", []),
            "positive_features": analysis.get("positive_features", []),
            "overall_visual_condition": analysis.get("overall_visual_condition"),
            "market_verdict": verdict,
            "confidence": analysis.get("confidence"),
            "estimated_repair_cost": analysis.get("estimated_repair_cost"),
            "estimated_customs_cost": item.get("estimated_customs_cost"),
            "imei_status": item.get("imei_status"),
            "estimated_total_cost": analysis.get("estimated_total_cost"),
            "estimated_resale_price": analysis.get("estimated_resale_price"),
            "questions_for_seller": analysis.get("questions_for_seller", []),
            "data_sources": data_sources,
            "used_web_search": analysis.get("used_web_search", False),
            "url": item["url"], "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_rejected(item, analysis, note=None):
    with open(REJECTED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": item["id"], "title": item["title"], "price": item["price"],
            "url": item["url"], "verdict": analysis.get("market_verdict"),
            "reasoning": analysis.get("reasoning"),
            "estimated_repair_cost": analysis.get("estimated_repair_cost"),
            "note": note,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_manual_note(text):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "manual_note", "text": text,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_confirmed_sale(record):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "confirmed_sale", "id": record["id"], "title": record["title"],
            "price": record["price"], "condition": record.get("condition"),
            "model_key": record.get("model_key"),
            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_confirmed_good_call(record):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "confirmed_good_call", "id": record["id"], "title": record["title"],
            "price": record["price"], "condition": record.get("condition"),
            "model_key": record.get("model_key"),
            "visible_defects": record.get("visible_defects", []),
            "reasoning": record.get("reasoning", ""),
            "url": record.get("url"),
            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def get_manual_notes(model_key, limit=5):
    if not model_key or not os.path.exists(PRICE_HISTORY_FILE):
        return []
    words = model_key.split()
    notes = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "manual_note":
                continue
            text_low = rec.get("text", "").lower()
            if all(w in text_low for w in words if len(w) > 2):
                notes.append(rec)
    notes.sort(key=lambda r: r.get("added_at", ""), reverse=True)
    return notes[:limit]


def get_confirmed_good_calls(model_key, limit=3):
    if not model_key or not os.path.exists(PRICE_HISTORY_FILE):
        return []
    matches = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "confirmed_good_call" and rec.get("model_key") == model_key:
                matches.append(rec)
    matches.sort(key=lambda r: r.get("confirmed_at", ""), reverse=True)
    return matches[:limit]


def get_confirmed_sales(model_key, limit=5):
    if not model_key or not os.path.exists(PRICE_HISTORY_FILE):
        return []
    matches = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "confirmed_sale" and rec.get("model_key") == model_key:
                matches.append(rec)
    matches.sort(key=lambda r: r.get("confirmed_at", ""), reverse=True)
    return matches[:limit]


def find_latest_analysis_by_url(url):
    if not os.path.exists(PRICE_HISTORY_FILE):
        return None
    found = None
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "full_analysis" and rec.get("url", "").rstrip("/") == url.rstrip("/"):
                found = rec
    return found


def similar_full_analyses(model_key, condition, exclude_id, limit=SIMILAR_EXAMPLES_LIMIT):
    if not model_key or not os.path.exists(PRICE_HISTORY_FILE):
        return []
    matches = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "full_analysis" or rec.get("id") == exclude_id:
                continue
            if rec.get("model_key") != model_key:
                continue
            if condition and rec.get("condition") and rec.get("condition") != condition:
                continue
            matches.append(rec)
    matches.sort(key=lambda r: r.get("collected_at", ""), reverse=True)
    return matches[:limit]


def market_stats_for(model_key, condition, exclude_id):
    """Только для команды /price — Gemini эту сводку не получает."""
    if not model_key or not os.path.exists(PRICE_HISTORY_FILE):
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
            if rec.get("model_key") != model_key:
                continue
            prices.append(rec["price"])
    if len(prices) < 3:
        return None
    return {"count": len(prices), "min": min(prices), "median": round(statistics.median(prices))}


# ---------- Минимальная выгода ----------

def get_min_profit():
    return load_json(MIN_PROFIT_FILE, {}).get("value", DEFAULT_MIN_PROFIT)


def set_min_profit(value):
    with open(MIN_PROFIT_FILE, "w", encoding="utf-8") as f:
        json.dump({"value": value}, f)


# ---------- Курс USD/TJS ----------

def get_usd_tjs_rate():
    cached = load_json(EXCHANGE_RATE_FILE, {})
    today = time.strftime("%Y-%m-%d")
    if cached.get("date") == today and cached.get("rate"):
        return cached["rate"]
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rate = data.get("rates", {}).get("TJS")
        if rate:
            with open(EXCHANGE_RATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today, "rate": rate}, f)
            return rate
    except Exception as e:
        print("Не удалось получить курс USD/TJS:", e)
    print(f"Использую резервный курс: {FALLBACK_USD_TJS_RATE}")
    return cached.get("rate", FALLBACK_USD_TJS_RATE)


def estimate_customs_cost(price_tjs, usd_rate):
    if not price_tjs or not usd_rate:
        return None
    price_usd = price_tjs / usd_rate
    duty_usd = price_usd * 0.20
    vat_usd = price_usd * 0.14
    fee_usd = 0 if price_usd < 100 else 10
    broker_tjs = 50
    total_tjs = (price_usd + duty_usd + vat_usd + fee_usd) * usd_rate + broker_tjs
    return round(total_tjs)


def detect_imei_status(full_text):
    """Три реальных статуса на Somon.tj + запасной общий случай."""
    if IMEI_BLACK_RE.search(full_text):
        return "black"
    if IMEI_WHITE_RE.search(full_text):
        return "white"
    if IMEI_NOT_REGISTERED_RE.search(full_text):
        return "not_registered"
    if IMEI_REGISTERED_RE.search(full_text):
        return "registered"
    return None


# ---------- Дневные/месячные квоты Gemini ----------

def get_daily_usage(path, keys):
    data = load_json(path, {})
    today = time.strftime("%Y-%m-%d")
    if data.get("date") != today:
        data = {"date": today}
    for k in keys:
        data.setdefault(k, 0)
    return data


def save_daily_usage(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def pick_available_combo(usage):
    for combo in GEMINI_COMBOS:
        if usage.get(combo["id"], 0) < GEMINI_DAILY_LIMIT_PER_COMBO:
            return combo
    return None


# ---------- Поиск: Tavily + Google ----------

def get_search_usage():
    data = load_json(SEARCH_USAGE_FILE, {})
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    if data.get("day") != today:
        for p in SEARCH_PROVIDERS:
            if p["period"] == "day":
                data[p["id"]] = 0
        data["day"] = today
    if data.get("month") != month:
        for p in SEARCH_PROVIDERS:
            if p["period"] == "month":
                data[p["id"]] = 0
        data["month"] = month
    for p in SEARCH_PROVIDERS:
        data.setdefault(p["id"], 0)
    return data


def save_search_usage(data):
    with open(SEARCH_USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def pick_search_provider(usage):
    for p in SEARCH_PROVIDERS:
        if usage.get(p["id"], 0) < p["limit"]:
            return p
    return None


def tavily_search(key, query, num=3):
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": num, "search_depth": "basic"},
            timeout=15,
        )
        if not resp.ok:
            print("Ошибка Tavily:", resp.status_code, resp.text[:300])
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        return "\n".join(f"- {r.get('title', '')}: {r.get('content', '')[:300]}" for r in results[:num])
    except Exception as e:
        print("Сбой Tavily:", e)
        return None


def google_custom_search(key, cx, query, num=3):
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": key, "cx": cx, "q": query, "num": num},
            timeout=15,
        )
        if not resp.ok:
            print("Ошибка Google Search API:", resp.status_code, resp.text[:300])
            return None
        items = resp.json().get("items", [])
        if not items:
            return None
        return "\n".join(f"- {it.get('title', '')}: {it.get('snippet', '')}" for it in items[:num])
    except Exception as e:
        print("Сбой Google Search API:", e)
        return None


def web_search_lookup(query):
    usage = get_search_usage()
    provider = pick_search_provider(usage)
    if not provider:
        return None
    if provider["type"] == "tavily":
        result = tavily_search(provider["key"], query)
    else:
        result = google_custom_search(provider["key"], provider["cx"], query)
    usage[provider["id"]] += 1
    save_search_usage(usage)
    return result


# ---------- Telegram: подписчики, отправка текста и файлов ----------

def load_subscribers():
    subs = load_json(SUBSCRIBERS_FILE, None)
    if subs is None:
        subs = [TELEGRAM_CHAT_ID]
        save_subscribers(subs)
    return subs


def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


def send_telegram(chat_id, text):
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              data={"chat_id": chat_id, "text": text}, timeout=15)
        if not resp.ok:
            print("Ошибка Telegram:", chat_id, resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить в Telegram:", chat_id, e)


def send_telegram_document(chat_id, filename, content_str, caption=""):
    try:
        files = {"document": (filename, content_str.encode("utf-8"))}
        data = {"chat_id": chat_id, "caption": caption[:1024]}
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                              data=data, files=files, timeout=30)
        if not resp.ok:
            print("Ошибка отправки файла в Telegram:", chat_id, resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить файл в Telegram:", chat_id, e)


def broadcast_telegram(subscribers, text):
    for chat_id in subscribers:
        send_telegram(chat_id, text)
        time.sleep(0.3)


def alert_owner(text):
    send_telegram(TELEGRAM_CHAT_ID, f"⚠️ {text}")


# ---------- Самопроверка здоровья бота ----------

def check_health():
    health = load_json(HEALTH_FILE, {})
    now = time.time()
    last_run_at = health.get("last_run_at")
    if last_run_at:
        gap_minutes = (now - last_run_at) / 60
        if gap_minutes > ALERT_GAP_MINUTES:
            alert_owner(
                f"Бот не запускался {round(gap_minutes)} минут (ожидалось не больше {ALERT_GAP_MINUTES}). "
                f"Проверь планировщик cron-job.org и токен доступа к GitHub API."
            )
    health["last_run_at"] = now
    with open(HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(health, f)
    return health


def report_fetch_result(health, success):
    if success:
        if health.get("consecutive_failures", 0) > 0:
            alert_owner("Somon.tj снова загружается нормально — проблема, о которой я писал раньше, ушла.")
        health["consecutive_failures"] = 0
    else:
        health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
        if health["consecutive_failures"] == ALERT_FAILURE_THRESHOLD:
            alert_owner(
                f"Не удаётся получить объявления с Somon.tj уже {ALERT_FAILURE_THRESHOLD} прогона подряд. "
                f"Возможно, сайт изменил структуру страницы или заблокировал доступ — стоит проверить вручную."
            )
    with open(HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(health, f)


# ---------- Отслеживание проданных объявлений ----------

def check_sold_status(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if not resp.ok:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        full_text = soup.get_text(" ", strip=True)
        return bool(SOLD_RE.search(full_text))
    except Exception as e:
        print("Не удалось проверить статус объявления:", url, e)
        return None


def process_disappeared_ads(seen, current_ids):
    to_check = [
        (ad_id, entry) for ad_id, entry in seen.items()
        if entry.get("photo_ok") and entry.get("url") and ad_id not in current_ids
        and not entry.get("sold_checked")
    ][:MAX_SOLD_CHECKS_PER_RUN]

    for ad_id, entry in to_check:
        is_sold = check_sold_status(entry["url"])
        if is_sold is True:
            record = {
                "id": ad_id, "title": entry.get("title"), "price": entry.get("price"),
                "condition": entry.get("condition"), "model_key": entry.get("model_key"),
            }
            log_confirmed_sale(record)
            alert_owner(f"✅ Подтверждена продажа: «{entry.get('title')}» за {entry.get('price')} TJS — база пополнилась реальным фактом.")
        if is_sold is not None:
            entry["sold_checked"] = True
        seen[ad_id] = entry
    return seen


# ---------- Красивый HTML-отчёт по отклонённым лотам ----------

def build_rejected_report_html():
    rows = []
    if os.path.exists(REJECTED_FILE):
        with open(REJECTED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: r.get("checked_at", ""), reverse=True)

    verdict_colors = {
        "справедливая цена": "#4a90d9", "переоценено": "#d94a4a",
        "недостаточно данных": "#999999", "недооценено": "#3fa34d",
    }

    cards = []
    for r in rows:
        color = verdict_colors.get(r.get("verdict"), "#777777")
        repair = r.get("estimated_repair_cost")
        repair_line = f"<div>🔧 Оценка ремонта: {repair} TJS</div>" if repair else ""
        note_line = f"<div class='note'>ℹ️ {r.get('note')}</div>" if r.get("note") else ""
        cards.append(f"""
        <div class="card">
          <div class="title">{r.get('title', '—')}</div>
          <div class="badge" style="background:{color}">{r.get('verdict', '—')}</div>
          <div>💰 Цена: {r.get('price', '—')} TJS</div>
          {repair_line}
          {note_line}
          <div class="reason">{r.get('reasoning', '')}</div>
          <div class="meta">🕒 {r.get('checked_at', '')} · <a href="{r.get('url', '#')}">открыть объявление</a></div>
        </div>
        """)

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Отклонённые лоты</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#f4f4f4; margin:0; padding:16px; }}
  h1 {{ font-size: 20px; }}
  .summary {{ color:#555; margin-bottom: 20px; }}
  .card {{ background:#fff; border-radius:12px; padding:14px 16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
  .title {{ font-weight:600; font-size:15px; margin-bottom:6px; }}
  .badge {{ display:inline-block; color:#fff; font-size:12px; padding:3px 10px; border-radius:20px; margin-bottom:8px; }}
  .reason {{ color:#444; font-size:14px; margin-top:6px; }}
  .note {{ color:#a06a00; font-size:13px; margin-top:4px; }}
  .meta {{ color:#888; font-size:12px; margin-top:8px; }}
  a {{ color:#4a90d9; }}
</style></head>
<body>
  <h1>📋 Отклонённые лоты</h1>
  <div class="summary">Всего в списке: <b>{len(rows)}</b></div>
  {''.join(cards) if cards else '<p>Пока пусто.</p>'}
</body></html>"""
    return html, len(rows)


# ---------- Режим и команды ----------

def set_mode(mode):
    with open(MODE_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f, ensure_ascii=False, indent=2)


def get_mode():
    mode = load_json(MODE_FILE, {}).get("mode")
    return mode if mode in MODES else "params"


def check_telegram_commands(searches, subscribers):
    offset = load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                             params={"offset": offset}, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        print("Не удалось получить команды из Telegram:", e)
        return searches, subscribers

    searches_changed = False
    subs_changed = False

    for upd in updates:
        offset = upd["update_id"] + 1
        message = upd.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()
        if not chat_id or not text:
            continue

        if chat_id not in subscribers:
            subscribers = subscribers + [chat_id]
            subs_changed = True
            send_telegram(chat_id, "✅ Вы подписаны на уведомления о выгодных объявлениях. /help — список команд.")

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command in ("/all", "/used", "/params"):
            mode = command[1:]
            set_mode(mode)
            send_telegram(chat_id, f"✅ Режим поиска: {MODES[mode]}.")
        elif command == "/mode":
            aliases = {"all": "all", "все": "all", "used": "used", "бу": "used", "б/у": "used",
                       "params": "params", "параметры": "params"}
            mode = aliases.get(argument.lower())
            if mode:
                set_mode(mode)
                send_telegram(chat_id, f"✅ Режим поиска: {MODES[mode]}.")
            else:
                send_telegram(chat_id, "Формат: /mode all | used | params")
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
                searches_changed = True
                send_telegram(chat_id, f"✅ Параметры «{new_search['name']}» сохранены.")
            except Exception as e:
                send_telegram(chat_id, f"⚠️ Формат: /add Название | слова | макс_цена | мин_память\nОшибка: {e}")
        elif command == "/del":
            name = argument.strip()
            before = len(searches)
            searches = [s for s in searches if s["name"] != name]
            searches_changed = True
            send_telegram(chat_id, f"🗑 Удалён «{name}»." if len(searches) < before else f"⚠️ «{name}» не найден.")
        elif command == "/list":
            lines = [f"• {s['name']}: {s.get('query', '—')}, до {s.get('max_price', '∞')}" for s in searches]
            send_telegram(chat_id, f"🔎 Режим: {MODES[get_mode()]}\n" + ("\n".join(lines) if lines else "Параметров нет."))
        elif command == "/minprofit":
            arg = argument.strip()
            if arg.lstrip("-").isdigit():
                set_min_profit(int(arg))
                send_telegram(chat_id, f"✅ Минимальная выгода для уведомлений: {arg} TJS.")
            else:
                send_telegram(chat_id, f"Текущий порог: {get_min_profit()} TJS.\nФормат: /minprofit 300")
        elif command == "/subscribers":
            send_telegram(chat_id, f"👥 Подписчиков: {len(subscribers)}")
        elif command == "/stop":
            if chat_id in subscribers:
                subscribers = [s for s in subscribers if s != chat_id]
                subs_changed = True
                send_telegram(chat_id, "🔕 Вы отписаны от уведомлений.")
        elif command == "/dbadd":
            note = argument.strip()
            if note:
                log_manual_note(note)
                send_telegram(chat_id, "✅ Запись добавлена в базу — Gemini будет учитывать её при похожих разборах.")
            else:
                send_telegram(chat_id, "Напишите текст после команды, например:\n/dbadd iPhone 13 128GB, замена экрана ~350 TJS, батарея ~150 TJS")
        elif command == "/bought":
            url = argument.strip()
            if not url:
                send_telegram(chat_id, "Формат: /bought ссылка_на_объявление")
            else:
                record = find_latest_analysis_by_url(url)
                if record:
                    log_confirmed_good_call(record)
                    send_telegram(chat_id, f"✅ Запомнил! «{record.get('title')}» — подтверждённая удачная рекомендация, буду учитывать это при похожих разборах в будущем.")
                else:
                    send_telegram(chat_id, "Не нашёл разбор по этой ссылке в базе — проверьте, что ссылка точная и это объявление, которое я присылал.")
        elif command == "/rejected":
            html, count = build_rejected_report_html()
            send_telegram(chat_id, f"📋 Всего отклонённых лотов: {count}. Отправляю файл...")
            send_telegram_document(chat_id, "rejected_lots.html", html, caption=f"Отклонённые лоты: {count}")
        elif command == "/price":
            query = argument.strip()
            if not query:
                send_telegram(chat_id, "Напишите модель, например: /price iPhone 13")
            else:
                model_key = extract_model_key(query) or query.lower().strip()
                stats = market_stats_for(model_key, None, exclude_id=None)
                if stats:
                    send_telegram(chat_id,
                        f"💰 {query}\nПо базе ({stats['count']} похожих объявлений):\n"
                        f"Минимальная цена: {stats['min']} TJS\nМедианная цена: {stats['median']} TJS")
                else:
                    send_telegram(chat_id, f"По «{query}» в базе пока меньше 3 похожих объявлений.")
        elif command == "/stats":
            gem_usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
            search_usage = get_search_usage()
            db_count = sum(1 for _ in open(PRICE_HISTORY_FILE, encoding="utf-8")) if os.path.exists(PRICE_HISTORY_FILE) else 0
            rej_count = sum(1 for _ in open(REJECTED_FILE, encoding="utf-8")) if os.path.exists(REJECTED_FILE) else 0
            lines = ["📊 Статистика", "", f"💵 Минимальная выгода: {get_min_profit()} TJS", "", "Gemini сегодня:"]
            lines += [f"  {c['id']}: {gem_usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS]
            lines += ["", "Поиск в сети:"]
            lines += [f"  {p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']} ({'мес' if p['period']=='month' else 'день'})" for p in SEARCH_PROVIDERS]
            lines += ["", f"📁 Записей в базе: {db_count}", f"🚫 Отклонённых лотов: {rej_count}", f"👥 Подписчиков: {len(subscribers)}"]
            send_telegram(chat_id, "\n".join(lines))
        elif command == "/help":
            send_telegram(chat_id, "Команды:\n/all /used /params — режим поиска\n"
                                    "/add Название | слова | макс_цена | мин_память\n"
                                    "/del Название\n/list — список поисков\n"
                                    "/minprofit число — минимальная выгода для уведомлений (TJS)\n"
                                    "/price Модель — грубая сводка цен по базе\n"
                                    "/dbadd текст — добавить что угодно в базу вручную\n"
                                    "/bought ссылка — подтвердить удачную покупку по рекомендации бота\n"
                                    "/rejected — файл со всеми отклонёнными лотами\n"
                                    "/stats — расход лимитов и размер базы\n"
                                    "/subscribers — сколько подписчиков\n/stop — отписаться")

    if searches_changed:
        with open(SEARCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(searches, f, ensure_ascii=False, indent=2)
    if subs_changed:
        save_subscribers(subscribers)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    return searches, subscribers


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
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    condition = normalize_condition(labeled_value(soup, ["Состояние"]))
    memory_raw = labeled_value(soup, ["Встроенная память", "Память"])
    memory_match = re.search(r"(\d+)\s*(?:gb|гб)", memory_raw or "", re.I)
    memory = int(memory_match.group(1)) if memory_match else None
    imei_status = detect_imei_status(full_text)

    desc_match = DESC_RE.search(full_text)
    description = desc_match.group(1).strip() if desc_match else full_text[:500]

    photo_urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]) and src not in seen:
            seen.add(src)
            photo_urls.append(src)

    return condition, memory, description, photo_urls[:4], imei_status


# ---------- Gemini: анализ ----------

def format_similar_examples(examples):
    if not examples:
        return "Похожих проверенных лотов этой модели из нашей базы пока нет."
    lines = ["Похожие проверенные лоты этой модели из нашей базы (от новых к старым) — "
             "судьи сам по каждому, чем этот лот отличается по дефектам и цене:"]
    for i, rec in enumerate(examples, 1):
        defects = ", ".join(rec.get("visible_defects") or []) or "не обнаружены"
        positives = ", ".join(rec.get("positive_features") or []) or "—"
        repair = rec.get("estimated_repair_cost")
        repair_text = f", оценка ремонта тогда: {repair} TJS" if repair else ""
        lines.append(
            f"{i}. Цена {rec.get('price', '—')} TJS, состояние по фото: "
            f"{rec.get('overall_visual_condition', 'неизвестно')}, "
            f"дефекты: {defects}, плюсы: {positives}{repair_text} — вердикт тогда: {rec.get('market_verdict', '—')}"
        )
    return "\n".join(lines)


def format_confirmed_good_calls(records):
    if not records:
        return ""
    lines = ["✅ ПОДТВЕРЖДЕНО ВЛАДЕЛЬЦЕМ БОТА — это реальные покупки по прошлым рекомендациям, максимально доверяй им:"]
    for r in records:
        defects = ", ".join(r.get("visible_defects") or []) or "не обнаружены"
        lines.append(f"- Куплен «{r.get('title')}» за {r.get('price')} TJS, дефекты тогда: {defects}. Причина рекомендации: {r.get('reasoning', '')}")
    return "\n".join(lines)


def format_confirmed_sales(records):
    if not records:
        return ""
    lines = ["✅ ПОДТВЕРЖДЁННЫЕ РЕАЛЬНЫЕ ПРОДАЖИ этой модели (объявление реально было продано за эту цену, это не догадка):"]
    for r in records:
        lines.append(f"- Продано за {r.get('price')} TJS, состояние: {r.get('condition') or 'не указано'}")
    return "\n".join(lines)


def format_manual_notes(notes):
    if not notes:
        return ""
    lines = ["Заметки, добавленные вручную владельцем бота (доверяй им как надёжному источнику):"]
    for n in notes:
        lines.append(f"- {n['text']}")
    return "\n".join(lines)


def format_imei_block(item, usd_rate):
    """Формирует и текст для Gemini, и что уже ИЗВЕСТНО (чтобы не задавал лишних вопросов)."""
    status = item.get("imei_status")
    if status == "white" or status == "registered":
        return "\nIMEI уже зарегистрирован (легально растаможен) — дополнительных таможенных расходов и рисков не будет. НЕ спрашивай продавца про растаможку/IMEI, это уже известно."
    if status == "not_registered":
        return (
            f"\nВАЖНО: IMEI этого телефона НЕ зарегистрирован в Таджикистане (обычная, не критичная растаможка "
            f"ещё не оплачена). Точный расчёт по формуле (пошлина 20% + НДС 14% от таможенной стоимости + сбор "
            f"+ услуги оформления, курс {usd_rate} TJS за $1) уже посчитан программой: примерно "
            f"{item.get('estimated_customs_cost')} TJS. Обязательно прибавь эту сумму к итоговой стоимости. "
            f"НЕ спрашивай продавца про растаможку/IMEI, это уже известно и посчитано."
        )
    if status == "black":
        return (
            "\nВАЖНО И СЕРЬЁЗНО: IMEI этого телефона в ЧЁРНОМ СПИСКЕ — это не просто неоплаченная растаможка, "
            "а отдельный, более рискованный статус (возможен штраф, сложности или невозможность легальной "
            "растаможки в принципе). Это весомый минус, который может сделать сделку невыгодной или рискованной "
            "даже при низкой цене — учти это в вердикте и снизь уверенность, если не уверен в масштабе риска. "
            "НЕ спрашивай продавца про сам факт чёрного списка, это уже известно — если хочешь, можешь спросить "
            "только про конкретные детали (например, можно ли вообще легализовать такой IMEI)."
        )
    return "\nСтатус IMEI на странице определить не удалось — если это важно, можешь спросить у продавца напрямую про растаможку."


def build_prompt(item, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate):
    examples_text = format_similar_examples(similar_examples)
    good_calls_text = format_confirmed_good_calls(confirmed_good_calls)
    sales_text = format_confirmed_sales(confirmed_sales)
    web_text = (
        f"Результаты веб-поиска по этой модели (реальные страницы из интернета):\n{web_results}"
        if web_results else "Веб-поиск не дал результатов в этот раз."
    )
    manual_text = format_manual_notes(manual_notes)
    imei_text = format_imei_block(item, usd_rate)

    return f"""
Ты — эксперт по оценке б/у смартфонов для перепродажи в Таджикистане. Изучи текст объявления и фото.
Используй не только присланные данные ниже, но и свои собственные знания о рынке смартфонов.

Объявление: {item['title']}
Цена: {item['price']} TJS
Состояние по словам продавца: {item.get('condition') or 'не указано'}
Описание продавца: {item.get('description', '')[:800]}
{imei_text}

{good_calls_text}

{sales_text}

{examples_text}

{manual_text}

{web_text}

Сравни дефекты ЭТОГО лота с дефектами похожих лотов выше по существу, не через одно среднее число —
рассуждай по каждому примеру отдельно, как эксперт. Подтверждённые покупки и продажи выше — самые
надёжные данные, опирайся на них в первую очередь, если они есть.

Сам реши и посчитай: стоимость возможного ремонта, итоговую цену (лот + ремонт + все указанные выше
расходы/риски по IMEI), и по какой цене реально продать телефон после этого. Считай "недооценено"
только если после всех расходов телефон реально можно продать дороже итоговой цены с заметным
запасом. Честно оцени свою уверенность в диапазоне 0.0-1.0 — не завышай её искусственно, и снижай
её при серьёзных рисках (например, чёрный список IMEI).

ВАЖНО про вопросы продавцу: не спрашивай о том, что уже прямо указано в данных выше (состояние,
память, статус IMEI — если он определён). Задавай вопросы только о том, что реально неизвестно
и важно для решения (например, состояние аккумулятора, есть ли скрытые повреждения, комплектация).

Верни ТОЛЬКО JSON без markdown, строго такой формы:
{{
  "overall_visual_condition": "новое|как новое|хорошее|среднее|плохое|неизвестно",
  "visible_defects": [],
  "positive_features": [],
  "estimated_repair_cost": null,
  "estimated_total_cost": null,
  "estimated_resale_price": null,
  "market_verdict": "недооценено|справедливая цена|переоценено|недостаточно данных",
  "reasoning": "коротко: на чём основан вывод — с какими конкретно примерами сравнивал",
  "confidence": 0.0,
  "questions_for_seller": []
}}
Если данных совсем мало — verdict "недостаточно данных", не выдумывай цифры.
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


def analyze_listing(item, photo_urls, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate, usage):
    combo = pick_available_combo(usage)
    if not combo:
        return {"market_verdict": "недостаточно данных",
                "reasoning": "дневной лимит Gemini исчерпан на всех сочетаниях ключ+модель, анализ отложен",
                "visible_defects": []}

    parts = [{"text": build_prompt(item, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate)}]
    for url in photo_urls:
        try:
            img = requests.get(url, headers=HEADERS, timeout=15)
            img.raise_for_status()
            mime = "image/png" if ".png" in url.lower() else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img.content).decode()}})
        except Exception as e:
            print("Не удалось скачать фото:", url, e)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{combo['model']}:generateContent?key={combo['key']}"
    )
    try:
        resp = requests.post(url, json={"contents": [{"parts": parts}]}, timeout=60)
        usage[combo["id"]] = usage.get(combo["id"], 0) + 1
        save_daily_usage(GEMINI_DAILY_FILE, usage)

        if resp.ok:
            result = parse_gemini_json(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            result["used_web_search"] = bool(web_results)
            return result

        if resp.status_code == 429:
            print(f"Gemini: {combo['id']} исчерпал квоту, больше не используется сегодня")
            usage[combo["id"]] = GEMINI_DAILY_LIMIT_PER_COMBO
            save_daily_usage(GEMINI_DAILY_FILE, usage)
        print("Ошибка Gemini:", combo["id"], resp.status_code, resp.text[:800])
    except Exception as e:
        print("Сбой Gemini:", combo["id"], e)

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


# ---------- Сборка списка источников ----------

def build_data_sources(similar_examples, confirmed_good_calls, confirmed_sales, manual_notes, web_results, item):
    sources = []
    if confirmed_good_calls:
        sources.append(f"подтверждённые покупки ({len(confirmed_good_calls)})")
    if confirmed_sales:
        sources.append(f"подтверждённые продажи ({len(confirmed_sales)})")
    if similar_examples:
        sources.append(f"прошлые полные разборы похожих лотов ({len(similar_examples)})")
    if manual_notes:
        sources.append(f"ваши заметки вручную ({len(manual_notes)})")
    if web_results:
        sources.append("веб-поиск")
    if item.get("imei_status") in ("not_registered", "black"):
        sources.append("статус IMEI со страницы")
    if not sources:
        sources.append("только фото и текст объявления, без доп. данных")
    return sources


# ---------- Главная логика ----------

def main():
    health = check_health()

    seen = load_json(SEEN_FILE, {})
    subscribers = load_subscribers()
    searches, subscribers = check_telegram_commands(load_json(SEARCHES_FILE, []), subscribers)
    mode = get_mode()
    usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
    usd_rate = get_usd_tjs_rate()
    min_profit = get_min_profit()

    try:
        listings, soup = fetch_listings()
        report_fetch_result(health, success=True)
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        report_fetch_result(health, success=False)
        return

    if not listings:
        print("Объявления не найдены:", soup.get_text()[:2000])
        return

    current_ids = {item["id"] for item in listings}
    seen = process_disappeared_ads(seen, current_ids)

    usage_str = ", ".join(f"{c['id']}: {usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS)
    search_usage = get_search_usage()
    search_usage_str = ", ".join(f"{p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']}" for p in SEARCH_PROVIDERS)
    print(f"Режим: {MODES[mode]}; поисков: {len(searches)}; объявлений: {len(listings)}; "
          f"подписчиков: {len(subscribers)}; мин. выгода: {min_profit} TJS; курс USD/TJS: {usd_rate}; "
          f"Gemini сегодня — {usage_str}; поиск в сети — {search_usage_str}")

    for item in listings:
        entry = seen.get(item["id"], {})
        if not entry.get("logged"):
            log_market_point(item)
            entry["logged"] = True
            seen[item["id"]] = entry
    save_seen(seen)

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
            condition, memory, description, photo_urls, imei_status = fetch_detail(item["url"])
            item["condition"] = condition or item.get("condition")
            item["memory"] = memory or item.get("memory")
            item["description"] = description
            item["imei_status"] = imei_status
            if imei_status == "not_registered":
                item["estimated_customs_cost"] = estimate_customs_cost(item["price"], usd_rate)

            model_key = extract_model_key(item["title"])
            similar_examples = similar_full_analyses(model_key, item.get("condition"), item["id"])
            confirmed_good_calls = get_confirmed_good_calls(model_key)
            confirmed_sales = get_confirmed_sales(model_key)
            manual_notes = get_manual_notes(model_key)

            web_results = None
            if model_key:
                memory_part = f"{item.get('memory')}gb " if item.get("memory") else ""
                condition_part = item.get("condition") or "б/у"
                query = f"{model_key} {memory_part}{condition_part} цена Таджикистан Somon"
                web_results = web_search_lookup(query)

            data_sources = build_data_sources(similar_examples, confirmed_good_calls, confirmed_sales, manual_notes, web_results, item)

            analysis = analyze_listing(item, photo_urls, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate, usage)
            verdict = analysis.get("market_verdict", "недостаточно данных")

            log_full_analysis(item, analysis, verdict, data_sources)

            resale = analysis.get("estimated_resale_price")
            total = analysis.get("estimated_total_cost")
            profit = None
            if isinstance(resale, (int, float)) and isinstance(total, (int, float)):
                profit = round(resale - total)

            passes_profit = profit is not None and profit >= min_profit

            if verdict == "недооценено" and passes_profit:
                defects = ", ".join(analysis.get("visible_defects", [])) or "не обнаружены"
                repair = analysis.get("estimated_repair_cost")
                customs = item.get("estimated_customs_cost")
                confidence = analysis.get("confidence")
                questions = analysis.get("questions_for_seller", [])

                cost_lines = ""
                if repair:
                    cost_lines += f"🔧 Примерный ремонт: {repair} TJS\n"
                if item.get("imei_status") == "black":
                    cost_lines += "🚫 IMEI в чёрном списке — серьёзный риск, см. пояснение ниже\n"
                if customs:
                    cost_lines += f"🛃 Примерная растаможка (IMEI не оформлен): {customs} TJS\n"
                if total:
                    cost_lines += f"🧮 Итоговая цена (лот + расходы): {total} TJS\n"
                if resale:
                    cost_lines += f"📈 Продать можно примерно за: {resale} TJS\n"
                cost_lines += f"💵 Примерная выгода: {profit} TJS\n"
                if isinstance(confidence, (int, float)):
                    cost_lines += f"🎯 Уверенность Gemini: {round(confidence * 100)}%\n"

                questions_lines = ""
                if questions:
                    questions_lines = "❓ Вопросы продавцу:\n" + "\n".join(f"  • {q}" for q in questions) + "\n"

                text = (
                    f"🔥 Потенциально выгодное объявление\n\n{item['title']}\n"
                    f"💰 Цена лота: {item['price'] or '—'} TJS\n"
                    f"📊 Визуальное состояние: {analysis.get('overall_visual_condition', 'неизвестно')}\n"
                    f"🛠 Дефекты: {defects}\n"
                    f"{cost_lines}"
                    f"{questions_lines}"
                    f"📚 На основе: {', '.join(data_sources)}\n"
                    f"💡 {analysis.get('reasoning', '')}\n"
                    f"🔗 {item['url']}"
                )
                broadcast_telegram(subscribers, text)
                entry["notified"] = True
            else:
                note = None
                if verdict == "недооценено" and not passes_profit:
                    note = f"Gemini счёл недооценённым, но выгода ({profit if profit is not None else 'не посчитана'} TJS) ниже порога {min_profit} TJS"
                log_rejected(item, analysis, note=note)

            entry["photo_ok"] = True
            entry["url"] = item["url"]
            entry["title"] = item["title"]
            entry["price"] = item["price"]
            entry["condition"] = item.get("condition")
            entry["model_key"] = model_key
            seen[item["id"]] = entry
            time.sleep(2)
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e)
        finally:
            save_seen(seen)


if __name__ == "__main__":
    main()