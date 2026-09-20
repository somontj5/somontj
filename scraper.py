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
SIMILAR_EXAMPLES_LIMIT = 5
GEMINI_DAILY_LIMIT_PER_COMBO = int(os.environ.get("GEMINI_DAILY_LIMIT_PER_COMBO", "450"))
TAVILY_MONTHLY_LIMIT_PER_KEY = int(os.environ.get("TAVILY_MONTHLY_LIMIT_PER_KEY", "950"))
GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY = int(os.environ.get("GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY", "90"))
FALLBACK_USD_TJS_RATE = float(os.environ.get("FALLBACK_USD_TJS_RATE", "10.5"))

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
}
KNOWN_BRANDS = ["iphone", "apple", "samsung", "xiaomi", "redmi", "honor",
                "huawei", "tecno", "infinix", "nokia", "google", "pixel", "oppo", "vivo"]
NOISE_WORDS = {"vietnam", "global", "version", "black", "white", "gold", "silver",
               "blue", "green", "pink", "gray", "grey", "new", "оригинал"}

CONDITION_RE = re.compile(r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.I)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|IMEI\s*проверен", re.I)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")
DESC_RE = re.compile(r"Описание\s*(.*?)\s*(?:Показать телефон|Начать чат|Пожаловаться|$)", re.S)
IMEI_NOT_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}(?:не\s+внес|не\s+в\s+бел\w*\s+списк)", re.I)
IMEI_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}(?:в\s+бел\w*\s+списк|(?<!не\s)внес)", re.I)
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
            "visible_defects": analysis.get("visible_defects", []),
            "positive_features": analysis.get("positive_features", []),
            "overall_visual_condition": analysis.get("overall_visual_condition"),
            "market_verdict": verdict,
            "estimated_repair_cost": analysis.get("estimated_repair_cost"),
            "estimated_customs_cost": item.get("estimated_customs_cost"),
            "imei_registered": item.get("imei_registered"),
            "estimated_total_cost": analysis.get("estimated_total_cost"),
            "estimated_resale_price": analysis.get("estimated_resale_price"),
            "used_web_search": analysis.get("used_web_search", False),
            "url": item["url"], "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_rejected(item, analysis):
    with open(REJECTED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": item["id"], "title": item["title"], "price": item["price"],
            "url": item["url"], "verdict": analysis.get("market_verdict"),
            "reasoning": analysis.get("reasoning"),
            "estimated_repair_cost": analysis.get("estimated_repair_cost"),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_manual_note(text):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "manual_note",
            "text": text,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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


def market_stats_for(model_key, condition, exclude_id):
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


# ---------- Курс USD/TJS — проверяется раз в день, кэшируется ----------

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
    """Растаможка по формуле пользователя: пошлина 20% + НДС 14% от таможенной стоимости,
    сбор $10 (если цена < $100 — сбор не берётся), услуги оформления ~50 сомони."""
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
    if IMEI_NOT_REGISTERED_RE.search(full_text):
        return False
    if IMEI_REGISTERED_RE.search(full_text):
        return True
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
        "справедливая цена": "#4a90d9",
        "переоценено": "#d94a4a",
        "недостаточно данных": "#999999",
    }

    cards = []
    for r in rows:
        color = verdict_colors.get(r.get("verdict"), "#777777")
        repair = r.get("estimated_repair_cost")
        repair_line = f"<div>🔧 Оценка ремонта: {repair} TJS</div>" if repair else ""
        cards.append(f"""
        <div class="card">
          <div class="title">{r.get('title', '—')}</div>
          <div class="badge" style="background:{color}">{r.get('verdict', '—')}</div>
          <div>💰 Цена: {r.get('price', '—')} TJS</div>
          {repair_line}
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
        elif command == "/rejected":
            html, count = build_rejected_report_html()
            send_telegram(chat_id, f"📋 Всего отклонённых лотов: {count}. Отправляю файл...")
            send_telegram_document(chat_id, "rejected_lots.html", html, caption=f"Отклонённые лоты: {count}")
        elif command == "/stats":
            gem_usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
            search_usage = get_search_usage()
            db_count = sum(1 for _ in open(PRICE_HISTORY_FILE, encoding="utf-8")) if os.path.exists(PRICE_HISTORY_FILE) else 0
            rej_count = sum(1 for _ in open(REJECTED_FILE, encoding="utf-8")) if os.path.exists(REJECTED_FILE) else 0
            lines = ["📊 Статистика", "", "Gemini сегодня:"]
            lines += [f"  {c['id']}: {gem_usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS]
            lines += ["", "Поиск в сети:"]
            lines += [f"  {p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']} ({'мес' if p['period']=='month' else 'день'})" for p in SEARCH_PROVIDERS]
            lines += ["", f"📁 Записей в базе: {db_count}", f"🚫 Отклонённых лотов: {rej_count}", f"👥 Подписчиков: {len(subscribers)}"]
            send_telegram(chat_id, "\n".join(lines))
        elif command == "/help":
            send_telegram(chat_id, "Команды:\n/all /used /params — режим поиска\n"
                                    "/add Название | слова | макс_цена | мин_память\n"
                                    "/del Название\n/list — список поисков\n"
                                    "/dbadd текст — добавить что угодно в базу вручную\n"
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
    imei_registered = detect_imei_status(full_text)

    desc_match = DESC_RE.search(full_text)
    description = desc_match.group(1).strip() if desc_match else full_text[:500]

    photo_urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]) and src not in seen:
            seen.add(src)
            photo_urls.append(src)

    return condition, memory, description, photo_urls[:4], imei_registered


# ---------- Gemini: анализ ----------

def format_similar_examples(examples):
    if not examples:
        return "Похожих проверенных лотов этой модели из нашей базы пока нет."
    lines = ["Похожие проверенные лоты этой модели из нашей базы (от новых к старым):"]
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


def format_manual_notes(notes):
    if not notes:
        return ""
    lines = ["Заметки, добавленные вручную владельцем бота (доверяй им как надёжному источнику):"]
    for n in notes:
        lines.append(f"- {n['text']}")
    return "\n".join(lines)


def build_prompt(item, stats, similar_examples, web_results, manual_notes, usd_rate):
    stats_text = (
        f"Числовая сводка по нашей базе: минимальная цена {stats['min']} TJS, "
        f"медианная {stats['median']} TJS, всего похожих объявлений {stats['count']}."
        if stats else "Числовых данных по нашей базе пока недостаточно."
    )
    examples_text = format_similar_examples(similar_examples)
    web_text = (
        f"Результаты веб-поиска по этой модели (реальные страницы из интернета):\n{web_results}"
        if web_results else "Веб-поиск не дал результатов в этот раз — суди по своей базе и знаниям."
    )
    manual_text = format_manual_notes(manual_notes)

    customs_text = ""
    if item.get("imei_registered") is False:
        customs_text = (
            f"\nВАЖНО: IMEI этого телефона НЕ зарегистрирован в Таджикистане. Точный расчёт растаможки "
            f"(пошлина 20% + НДС 14% от таможенной стоимости + сбор + услуги оформления, курс {usd_rate} TJS "
            f"за $1) уже посчитан программой: примерно {item.get('estimated_customs_cost')} TJS. "
            f"Обязательно прибавь эту сумму к итоговой стоимости (estimated_total_cost), помимо ремонта."
        )
    elif item.get("imei_registered") is True:
        customs_text = "\nIMEI уже зарегистрирован — дополнительных таможенных расходов не будет."

    return f"""
Ты — эксперт по оценке б/у смартфонов для перепродажи в Таджикистане. Изучи текст объявления и фото.

Объявление: {item['title']}
Цена: {item['price']} TJS
Состояние по словам продавца: {item.get('condition') or 'не указано'}
Описание продавца: {item.get('description', '')[:800]}
{customs_text}

{stats_text}

{examples_text}

{manual_text}

{web_text}

Сравни дефекты ЭТОГО лота с дефектами похожих лотов из нашей базы выше. Если дефектов меньше или
они мельче при той же или более низкой цене — сигнал "недооценено". Если больше/серьёзнее — наоборот.

Посчитай: цена лота + примерная стоимость ремонта (если есть дефекты) + растаможка (если указана выше)
= итоговая цена. Сравни итоговую цену с реальной рыночной ценой исправного телефона такой модели
(база, заметки, веб-поиск выше). Считай "недооценено" только если после всех расходов телефон реально
можно продать дороже итоговой цены с заметным запасом, а не на 100-200 TJS.

Верни ТОЛЬКО JSON без markdown, строго такой формы:
{{
  "overall_visual_condition": "новое|как новое|хорошее|среднее|плохое|неизвестно",
  "visible_defects": [],
  "positive_features": [],
  "estimated_repair_cost": null,
  "estimated_total_cost": null,
  "estimated_resale_price": null,
  "market_verdict": "недооценено|справедливая цена|переоценено|недостаточно данных",
  "reasoning": "коротко: что дал веб-поиск/заметки (если были), как считал ремонт, растаможку и итоговую цену",
  "confidence": 0.0
}}
estimated_repair_cost — только если есть дефекты, иначе null. estimated_total_cost = цена лота + ремонт + растаможка (если применимо). estimated_resale_price — по какой цене реально продать после всех расходов. Если данных совсем мало — verdict "недостаточно данных", не выдумывай цифры.
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


def analyze_listing(item, photo_urls, stats, similar_examples, web_results, manual_notes, usd_rate, usage):
    combo = pick_available_combo(usage)
    if not combo:
        return {"market_verdict": "недостаточно данных",
                "reasoning": "дневной лимит Gemini исчерпан на всех сочетаниях ключ+модель, анализ отложен",
                "visible_defects": []}

    parts = [{"text": build_prompt(item, stats, similar_examples, web_results, manual_notes, usd_rate)}]
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


# ---------- Главная логика ----------

def main():
    seen = load_json(SEEN_FILE, {})
    subscribers = load_subscribers()
    searches, subscribers = check_telegram_commands(load_json(SEARCHES_FILE, []), subscribers)
    mode = get_mode()
    usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
    usd_rate = get_usd_tjs_rate()

    try:
        listings, soup = fetch_listings()
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        return

    if not listings:
        print("Объявления не найдены:", soup.get_text()[:2000])
        return

    usage_str = ", ".join(f"{c['id']}: {usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS)
    search_usage = get_search_usage()
    search_usage_str = ", ".join(f"{p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']}" for p in SEARCH_PROVIDERS)
    print(f"Режим: {MODES[mode]}; поисков: {len(searches)}; объявлений: {len(listings)}; "
          f"подписчиков: {len(subscribers)}; курс USD/TJS: {usd_rate}; "
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
            condition, memory, description, photo_urls, imei_registered = fetch_detail(item["url"])
            item["condition"] = condition or item.get("condition")
            item["memory"] = memory or item.get("memory")
            item["description"] = description
            item["imei_registered"] = imei_registered
            if imei_registered is False:
                item["estimated_customs_cost"] = estimate_customs_cost(item["price"], usd_rate)

            model_key = extract_model_key(item["title"])
            stats = market_stats_for(model_key, item.get("condition"), item["id"])
            similar_examples = similar_full_analyses(model_key, item.get("condition"), item["id"])
            manual_notes = get_manual_notes(model_key)

            web_results = None
            if model_key:
                memory_part = f"{item.get('memory')}gb " if item.get("memory") else ""
                condition_part = item.get("condition") or "б/у"
                query = f"{model_key} {memory_part}{condition_part} цена Таджикистан Somon"
                web_results = web_search_lookup(query)

            analysis = analyze_listing(item, photo_urls, stats, similar_examples, web_results, manual_notes, usd_rate, usage)
            verdict = analysis.get("market_verdict", "недостаточно данных")

            log_full_analysis(item, analysis, verdict)

            if verdict == "недооценено":
                defects = ", ".join(analysis.get("visible_defects", [])) or "не обнаружены"
                repair = analysis.get("estimated_repair_cost")
                total = analysis.get("estimated_total_cost")
                resale = analysis.get("estimated_resale_price")
                customs = item.get("estimated_customs_cost")

                cost_lines = ""
                if repair:
                    cost_lines += f"🔧 Примерный ремонт: {repair} TJS\n"
                if customs:
                    cost_lines += f"🛃 Примерная растаможка (IMEI не оформлен): {customs} TJS\n"
                if total:
                    cost_lines += f"🧮 Итоговая цена (лот + расходы): {total} TJS\n"
                if resale:
                    cost_lines += f"📈 Продать можно примерно за: {resale} TJS\n"

                text = (
                    f"🔥 Потенциально выгодное объявление\n\n{item['title']}\n"
                    f"💰 Цена лота: {item['price'] or '—'} TJS\n"
                    f"📊 Визуальное состояние: {analysis.get('overall_visual_condition', 'неизвестно')}\n"
                    f"🛠 Дефекты: {defects}\n"
                    f"{cost_lines}"
                    f"💡 {analysis.get('reasoning', '')}\n"
                    f"🔗 {item['url']}"
                )
                broadcast_telegram(subscribers, text)
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