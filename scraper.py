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


def log_market_point(item):
    """Дешёвая запись для базы цен — для ВСЕХ объявлений, включая VIP."""
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "market_point", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "vip": item.get("vip", False),
            "date": time.strftime("%Y-%m-%d"),
        }, ensure_ascii=False) + "\n")


def log_full_analysis(item, analysis, verdict):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "full_analysis", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "description": item.get("description", ""),
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
    """Медиана/минимум по похожим прошлым объявлениям ТОГО ЖЕ состояния (VIP тоже учитываются)."""
    key_words = [w for w in re.findall(r"[a-zа-я0-9]+", title.lower()) if len(w) > 2][:3]
    if not key_words or not os.path.exists(PRICE_HISTORY_FILE):
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
            t = rec.get("title", "").lower()
            if all(w in t for w in key_words[:2]):
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
            mode = c