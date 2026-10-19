import os
import json
import re
import time
import base64
import hashlib
import statistics
import requests
from difflib import SequenceMatcher
from bs4 import BeautifulSoup

# ==== НАСТРОЙКИ ====
SEARCH_URL = os.environ.get(
    "SOMON_URL",
    "https://m.somon.tj/telefonyi-i-svyaz/mobilnyie-telefonyi/sostoyanie---1/?ordering=newest&location=185,187,195,204,205,230,210,180"
)
MAX_NEW_ITEMS_PER_RUN = int(os.environ.get("MAX_NEW_ITEMS_PER_RUN", "5"))
MAX_SOLD_CHECKS_PER_RUN = int(os.environ.get("MAX_SOLD_CHECKS_PER_RUN", "5"))
# Сколько похожих лотов из базы уходит в Gemini. Владелец просил присылать всю
# найденную информацию, а не скупую выборку — лимиты подняты, от переполнения
# защищает PROMPT_MAX_CHARS.
SIMILAR_EXAMPLES_LIMIT = int(os.environ.get("SIMILAR_EXAMPLES_LIMIT", "40"))
SIMILAR_MARKET_POINTS_LIMIT = int(os.environ.get("SIMILAR_MARKET_POINTS_LIMIT", "60"))
# Полный текст объявления (описание продавца) — раньше обрезался до 800 символов.
DESCRIPTION_MAX_CHARS = int(os.environ.get("DESCRIPTION_MAX_CHARS", "4000"))
# Потолок размера промпта в символах: если база по модели огромная, лишние примеры
# отбрасываются с явной пометкой, а не молча.
PROMPT_MAX_CHARS = int(os.environ.get("PROMPT_MAX_CHARS", "90000"))
# Сколько прогонов в час делает workflow (cron "*/5" = 12) — нужно, чтобы
# равномерно расходовать суточную квоту Gemini.
RUNS_PER_HOUR = int(os.environ.get("RUNS_PER_HOUR", "60"))
FUZZY_MATCH_THRESHOLD = float(os.environ.get("FUZZY_MATCH_THRESHOLD", "0.78"))
PRICE_COMMAND_LIMIT = int(os.environ.get("PRICE_COMMAND_LIMIT", "15"))
STALE_EXAMPLE_DAYS = int(os.environ.get("STALE_EXAMPLE_DAYS", "45"))
ANOMALY_RATIO_THRESHOLD = float(os.environ.get("ANOMALY_RATIO_THRESHOLD", "3.0"))
ANOMALY_MIN_COUNT = int(os.environ.get("ANOMALY_MIN_COUNT", "3"))
ANOMALY_MIN_PRICE = int(os.environ.get("ANOMALY_MIN_PRICE", "100"))
# Во сколько раз должна отличаться цена, чтобы считать, что это уже ДРУГОЕ
# устройство, а не разброс по состоянию/комплектации внутри одной модели.
ANOMALY_CLUSTER_GAP = float(os.environ.get("ANOMALY_CLUSTER_GAP", "1.6"))
GEMINI_DAILY_LIMIT_PER_COMBO = int(os.environ.get("GEMINI_DAILY_LIMIT_PER_COMBO", "450"))
GEMINI_RETRIES_PER_COMBO = int(os.environ.get("GEMINI_RETRIES_PER_COMBO", "2"))   # попыток на одно сочетание ключ+модель при 5xx/сети
GEMINI_RETRY_DELAY = int(os.environ.get("GEMINI_RETRY_DELAY", "3"))               # пауза между попытками, сек (×номер попытки)
TAVILY_MONTHLY_LIMIT_PER_KEY = int(os.environ.get("TAVILY_MONTHLY_LIMIT_PER_KEY", "950"))
GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY = int(os.environ.get("GOOGLE_SEARCH_DAILY_LIMIT_PER_KEY", "90"))
CUSTOMS_DUE_STATUSES = ("not_registered", "gray")
FALLBACK_USD_TJS_RATE = float(os.environ.get("FALLBACK_USD_TJS_RATE", "10.5"))
ALERT_GAP_MINUTES = int(os.environ.get("ALERT_GAP_MINUTES", "40"))
ALERT_FAILURE_THRESHOLD = int(os.environ.get("ALERT_FAILURE_THRESHOLD", "3"))
DEFAULT_MIN_PROFIT = int(os.environ.get("DEFAULT_MIN_PROFIT", "0"))
DEFAULT_MIN_CONFIDENCE = float(os.environ.get("DEFAULT_MIN_CONFIDENCE", "0"))
DEFAULT_MAX_PRICE = int(os.environ.get("DEFAULT_MAX_PRICE", "0"))
# Бюджет времени прогона в секундах: разбор новых лотов прекращается, если время
# вышло (текущий лот доводится до конца). Нужен потому, что запуски идут каждую
# минуту, а concurrency в workflow пропускает их по одному: если прогон идёт 11
# минут, следующие запуски ждут в очереди, и уведомление приходит по объявлению,
# которому уже 11 минут. Короткий прогон = уведомление почти сразу.
RUN_TIME_BUDGET_SECONDS = float(os.environ.get("RUN_TIME_BUDGET_SECONDS", "240"))
# Входящие от воркеров: merge_workers.py складывает сюда находки всех
# worker-N, а этот прогон их разбирает. Воркеры сами пишут только в свои ветки.
WORKER_INBOX_FILE = os.environ.get("WORKER_INBOX", "worker_inbox.jsonl")
RUN_START_TS = time.time()

def run_budget_exhausted():
    """True, если бюджет времени прогона исчерпан."""
    return (time.time() - RUN_START_TS) > RUN_TIME_BUDGET_SECONDS
# Пауза между лотами внутри одного прогона (была жёстко 2 сек).
INTER_ITEM_DELAY = float(os.environ.get("INTER_ITEM_DELAY", "0.5"))

# Порог "активных объявлений" продавца, начиная с которого он однозначно похож на
# магазин/перекупщика (жёсткая эвристика — блокируем сразу, без похода к Gemini).
RESELLER_MIN_ACTIVE_ADS = int(os.environ.get("RESELLER_MIN_ACTIVE_ADS", "10"))
# Более мягкий порог: срабатывает, только если ИМЯ продавца ещё и похоже на
# название точки продаж (см. SHOP_NAME_RE ниже).
RESELLER_SOFT_ACTIVE_ADS = int(os.environ.get("RESELLER_SOFT_ACTIVE_ADS", "5"))
# Доля объявлений продавца, которые должны быть похожи на телефоны/планшеты, чтобы
# банить его по одному только количеству объявлений. Если продавец торгует всем
# подряд (доска объявлений, барахолка), большое число объявлений само по себе
# ничего не значит — банить нельзя.
RESELLER_PHONE_RATIO = float(os.environ.get("RESELLER_PHONE_RATIO", "0.8"))
# Минимум объявлений у продавца, которые вообще удалось разобрать с его страницы,
# чтобы доверять посчитанной доле (иначе 1 из 1 "похожих" — не показатель).
RESELLER_MIN_SAMPLE = int(os.environ.get("RESELLER_MIN_SAMPLE", "3"))
# Слова-маркеры телефона/планшета в заголовке — вместе с известными брендами
# (extract_model_key) используются, чтобы оценить долю телефонных объявлений
# на странице продавца без похода к Gemini.
PHONE_WORD_RE = re.compile(
    r"телефон|смартфон|планшет|айфон|iphone|ipad|планш\w*|gb\b|гб\b",
    re.I,
)

CITY_WHITELIST = []  # заполняется настоящим списком городов со страницы поиска
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
MIN_CONFIDENCE_FILE = "min_confidence.json"
MAX_PRICE_FILE = "max_price.json"
ANOMALY_STATE_FILE = "known_anomalies.json"
ANOMALY_CORRECTIONS_FILE = "anomaly_corrections.json"   # правки полей после разбора аномалий (Gemini)
ANOMALY_REASONS_FILE = "anomaly_reasons.jsonl"           # причины аномалий и подсказки для кода
SELLER_BLACKLIST_FILE = "seller_blacklist.json"
REMIGRATE_LIVE_STATE_FILE = "remigrate_live_state.json"
REMIGRATE_LIVE_BATCH_SIZE = int(os.environ.get("REMIGRATE_LIVE_BATCH_SIZE", "20"))

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
REVERSE_TRANSLIT_MAP = {}
for _en, _ru_list in TRANSLIT_MAP.items():
    for _ru in _ru_list:
        REVERSE_TRANSLIT_MAP.setdefault(_ru, []).append(_en)
KNOWN_BRANDS = ["iphone", "apple", "samsung", "xiaomi", "redmi", "honor",
                "huawei", "tecno", "infinix", "nokia", "google", "pixel", "oppo", "vivo",
                "realme", "itel", "oneplus", "vertu", "galaxy",
                "xioami", "xiаomi", "infinx", "realmi", "оppo", "poco"]
BRAND_ALIASES = {
    "xioami": "xiaomi", "xiаomi": "xiaomi", "infinx": "infinix",
    "realmi": "realme", "оppo": "oppo", "galaxy": "samsung", "poco": "poco",
    "iphone": "apple",
}
SPECIFIC_LINE_WORDS = {"redmi", "poco", "pixel"}
PARENT_ECHO_WORDS = {"xiaomi", "xioami", "xiаomi", "google"}
NOISE_WORDS = {"vietnam", "global", "version", "black", "white", "gold", "silver",
               "blue", "green", "pink", "gray", "grey", "new", "оригинал", "5g", "4g"}
STORAGE_SIZES = {16, 32, 64, 128, 256, 512, 1024, 2048}
GB_WORDS = {"gb", "гб"}

CONDITION_RE = re.compile(r"\b(Новый|Б\s*/\s*у|Б\s*\.\s*у\.?|Восстановлен\w*)\b(?:\s*[·|,;—-]\s*(\d+)\s*gb)?", re.I)
NOISE_RE = re.compile(r"Еще\s*\d+\s*фото|VIP|ТОП\b|TOP\b|IMEI\s*проверен", re.I)
IMEI_VERIFIED_BADGE_RE = re.compile(r"IMEI\s*проверен", re.I)
TOP_BADGE_RE = re.compile(r"(?<![а-яa-z])ТОП(?![а-яa-z])|\bTOP\b", re.I)
# Признак того, что аккаунт продавца назван как магазин/точка продаж, а не как
# имя частного человека — используется только как ДОПОЛНИТЕЛЬНЫЙ сигнал вместе
# с количеством активных объявлений, никогда сам по себе.
SHOP_NAME_RE = re.compile(
    r"phone|mobile|gsm|shop|store|market|tex|tech|электрон|магазин|салон|маркет|"
    r"техник|связ|apple\s*store|iphone\s*store",
    re.I,
)
PRICE_RE = re.compile(r"(\d[\d\s]{2,})\s*[cс]\.")
DESC_RE = re.compile(r"Описание\s*(.*?)\s*(?:Показать телефон|Начать чат|Пожаловаться|$)", re.S)

IMEI_WHITE_RE = re.compile(r"IMEI[^.]{0,60}в\s+бел\w*\s+списк", re.I)
IMEI_GRAY_RE = re.compile(r"IMEI[^.]{0,60}в\s+сер\w*\s+списк", re.I)
IMEI_BLACK_RE = re.compile(r"IMEI[^.]{0,60}в\s+ч[её]рн\w*\s+списк", re.I)
IMEI_NOT_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}не\s+внес", re.I)
IMEI_REGISTERED_RE = re.compile(r"IMEI[^.]{0,60}(?<!не\s)внес", re.I)
PUBLISHED_RE = re.compile(r"(Сегодня|Вчера|\d+\s*(?:минут\w*|час\w*|день|дн\w*|недел\w*)\s*назад)", re.I)
CITY_RE = re.compile(r"Город\s*:?\s*([А-ЯЁ][А-ЯЁа-яё\-]{1,30})")

# --- Города -----------------------------------------------------------------
# «Город: —» в уведомлениях был потому, что на Somon.tj города почти нигде нет
# как подписи «Город:». На странице объявления он написан строкой
# "№17361978 in Худжанд", а в списке — в конце карточки, после времени
# ("14 минут назад  Худжанд"). Ни один из вариантов старый код не разбирал.
DEFAULT_CITIES = [
    "Душанбе", "Худжанд", "Бободжон Гафуров", "Бустон", "Гулистон", "Зафарабад",
    "Истаравшан", "Канибадам", "Спитамен", "Кайраккум", "Чкаловск", "Вахдат",
    "Турсунзаде", "Куляб", "Бохтар", "Курган-Тюбе", "Хорог", "Пенджикент", "Нурек",
    "Рашт", "Дангара", "Фархор", "Восе", "Хамадони", "Джиргаталь", "Шахритус",
    "Кубодиён", "Айни", "Джалолиддини Балхи", "Левакант", "Исфара", "Ашт",
]
# Время публикации на карточке: "14 минут назад", "2 часа назад", "Вчера",
# "3 дня назад", "2 недели назад", "Сегодня".
CARD_TIME_RE = re.compile(
    r"\b(Сегодня|Вчера)\b|\b(\d+)\s*(минут\w*|мин\.?|час\w*|ч\.)\s*назад\b|"
    r"\b(\d+)\s*(день|дня|дней|недел\w*|месяц\w*)\s*назад\b",
    re.I,
)


def parse_city_whitelist(page_text):
    """Список городов прямо со страницы поиска: Somon в заголовке пишет
    «Купить мобильный телефон Худжанд, Бободжон Гафуров, Бустон (Чкаловск), …» —
    это ровно те города, по которым идёт поиск в текущем URL (location=…)."""
    cities = set(DEFAULT_CITIES)
    m = re.search(r"Купить[^\n]{0,40}телефон\s+(.{0,400})", page_text or "", re.I)
    if m:
        for part in m.group(1).split(","):
            name = re.sub(r"\s+", " ", part.split("(")[0].strip(" .;|\t"))
            if 3 <= len(name) <= 40 and not re.search(r"\d", name):
                cities.add(name)
    return sorted(cities, key=len, reverse=True)


def find_city(text, whitelist=None):
    """Первое упоминание города из белого списка в тексте."""
    if not text:
        return None
    for city in (whitelist or DEFAULT_CITIES):
        pattern = (r"(?<![А-Яа-яЁё0-9])" + re.escape(city) +
                   r"(?:\s*\([^)]{0,25}\))?(?![А-Яа-яЁё])")
        if re.search(pattern, text, re.I):
            return city
    return None


def split_card_meta(raw_text, whitelist=None):
    """Разбирает хвост карточки объявления из списка.

    Карточка выглядит как «Apple iPhone 13, 128 gb, Midnight 2 часа назад
    Гулистон (Кайраккум)» — время и город дописаны в конец заголовка. Без этого
    город не попадал в item["city"] вовсе (в Telegram печаталось «—»), а время и
    город засоряли заголовок, уходящий в Gemini и в model_key.
    Возвращает (заголовок без хвоста, время публикации, город).
    """
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    city = None
    for candidate in (whitelist or DEFAULT_CITIES):
        pattern = re.compile(r"[,\-–|\s]*" + re.escape(candidate) +
                             r"(?:\s*\([^)]{0,25}\))?\s*$", re.I)
        m = pattern.search(text)
        if m:
            city = candidate
            text = text[:m.start()].strip(" ,-–|")
            break
    published = None
    m = CARD_TIME_RE.search(text)
    if m and m.end() >= len(text) - 2:
        if m.group(1):
            published = m.group(1).capitalize()
        elif m.group(2):
            published = f"{m.group(2)} {m.group(3)} назад"
        else:
            published = f"{m.group(4)} {m.group(5)} назад"
        text = text[:m.start()].strip(" ,-–|")
    return (text or None), published, city


def card_age_minutes(published):
    """Возраст объявления в минутах по тексту с карточки — чтобы свежие лоты
    уходили на анализ первыми, даже если верх страницы занят платными VIP/ТОП."""
    if not published:
        return 10 ** 9
    p = published.lower()
    if p.startswith("сегодня"):
        return 0
    if p.startswith("вчера"):
        return 24 * 60
    m = re.match(r"(\d+)\s*(минут|час|день|дня|дней|недел|месяц)", p)
    if not m:
        return 10 ** 9
    value, unit = int(m.group(1)), m.group(2)
    if unit.startswith("минут"):
        return value
    if unit.startswith("час"):
        return value * 60
    if unit.startswith("недел"):
        return value * 7 * 24 * 60
    if unit.startswith("месяц"):
        return value * 30 * 24 * 60
    return value * 24 * 60

SOLD_RE = re.compile(r"(?<!не\s)\bПродано\b", re.I)
MODES = {"all": "все объявления категории", "used": "только Б/у", "params": "только заданные параметры"}

CLONE_RE = re.compile(r"копи[яи]\w*|реплик\w*|дублика\w*|новодел\w*|подделк\w*|\bfake\b|\bclone\b", re.I)

RESERVATION_RE = re.compile(
    r"\bпред\s*-?\s*заказ\w*|\bпредоплат\w*|\bбронь\b|\bзабронир\w*|\bброни\w*\s+(?:за|под)\b"
    r"|(?<!без\s)\bзалог\w*|\bдепозит\w*|\bзадатк\w*|\bрезерв\w*",
    re.I,
)

NEVER_PROFITABLE_RE = re.compile(r"(?:iphone|айфон|apple).{0,80}?\b(?:mini|мини)\b", re.I)

# Маркеры аксессуара/запчасти в заголовке. Такие лоты нельзя сравнивать как
# полноценный телефон: цена чехла (380 TJS), дисплея или аккумулятора ломала
# статистику группы и давала ложные «разбросы цен» вида 380 … 14600 TJS.
ACCESSORY_RE = re.compile(
    r"\bчехол\w*|\bстекл\w*|плёнк\w*|пленк\w*|защитн\w*\s+(?:стекл\w*|плёнк\w*|пленк\w*)|"
    r"\bкабель\w*|\bшнур\w*|\bзарядк\w*|зарядное\s+устройств\w*|\bадаптер\w*|блок\s+питания|"
    r"\bнаушник\w*|\bгарнитур\w*|\bремешок\w*|\bбрелок\w*|\bбампер\w*|\bнакладк\w*|"
    r"\bкрышк\w*|\bматеринск\w*\s+плат\w*|\bдисплей\w*|\bматриц\w*|\bаккумулятор\w*|"
    r"\bбатаре\w*|\bзапчаст\w*|\bпереходник\w*|\bдержател\w*|\bподставк\w*|"
    r"\bkeychain\b|\bscreen\s+protector\b|\bcase\b|\bcharger\b|\bbattery\b", re.I)

# Строка "Модель" в блоке проверки IMEI — официальное название устройства из
# государственной базы IMEI. Живые примеры со страниц Somon.tj:
#   "Модель: Xiaomi Redmi 5 Plus (MEE7, MET7)"
#   "МодельApple iPhone XR (A2105)"
# То есть полное название без опечаток, а в скобках может быть 1, 2 или больше
# кодов варианта устройства через запятую.
VERIFIED_MODEL_CODES_RE = re.compile(r"\(([^)]*)\)")
VERIFIED_MODEL_CODE_RE = re.compile(r"\(([A-Za-z0-9+\-]+)\)\s*$")

# IMEI со страницы объявления. Somon.tj показывает его ЗАМАСКИРОВАННЫМ:
#   "IMEI: 8682080XXXXXX4" / "IMEI 8904909XXXXXX4"
# Первые 8 цифр (TAC) видны, хвост закрыт буквами X. Полностью проверить номер
# по контрольной сумме (Luhn) можно только когда видны все 15 цифр.
IMEI_NUMBER_RE = re.compile(r"IMEI[^0-9]{0,8}([0-9Xx][0-9Xx\s\-]{8,24})")


def parse_imei_number(text):
    """Возвращает (номер как на странице, masked). (None, False), если IMEI нет."""
    m = IMEI_NUMBER_RE.search(text or "")
    if not m:
        return None, False
    raw = re.sub(r"[^0-9Xx]", "", m.group(1)).upper()
    # Либо маскированный номер (есть X), либо полный 15-значный. Иначе это не IMEI
    # (например, телефон/просмотры рядом со словом IMEI), и подставлять его нельзя.
    if not ("X" in raw or (raw.isdigit() and len(raw) == 15)):
        return None, False
    return raw, ("X" in raw)


def luhn_ok(digits):
    """Контрольная сумма IMEI (Luhn). None — проверить нельзя (номер маскирован)."""
    if not digits or not digits.isdigit() or len(digits) != 15:
        return None
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_meaningful_model_text(text):
    """labeled_value(soup, ["Модель"]) иногда находит слово "Модель" не в зелёном
    блоке проверки IMEI, а где-то ещё на странице, и подставляет вместо значения
    случайный обрывок текста вроде одного ":" — тогда verified_model технически
    непустой, но бессмысленный. Раньше такой мусор превращался в валидный на вид
    ключ "imei:" и склеивал в одну группу совершенно разные модели телефонов.
    Отбрасываем строки без хотя бы 2 буквенно-цифровых символов."""
    if not text:
        return False
    return len(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", text)) >= 2


def parse_verified_model(raw_model_text):
    """Разбирает строку "Модель" из блока проверки IMEI.

    "Xiaomi Redmi 5 Plus (MEE7, MET7)" -> ("Xiaomi Redmi 5 Plus", ["mee7", "met7"])
    "Apple iPhone XR (A2105)"          -> ("Apple iPhone XR", ["a2105"])
    Старый код брал ТОЛЬКО код в скобках и делал ключ "imei:mee7" — поэтому лот с
    проверенным IMEI никогда не сравнивался с лотом той же модели без проверки
    (у того ключ из заголовка): одна модель распадалась на два множества.
    """
    if not is_meaningful_model_text(raw_model_text):
        return None, []
    text = re.sub(r"\s+", " ", raw_model_text).strip()
    name = VERIFIED_MODEL_CODES_RE.sub(" ", text)
    name = re.sub(r"^модель\s*:?\s*", "", name, flags=re.I).strip(" :,-")
    name = re.sub(r"\s+", " ", name).strip()
    codes = []
    for group in VERIFIED_MODEL_CODES_RE.findall(text):
        for part in group.split(","):
            code = re.sub(r"[^0-9A-Za-z+_-]", "", part).lower()
            if is_meaningful_model_text(code):
                codes.append(code)
    return (name if is_meaningful_model_text(name) else None), codes


def official_model_key(verified_model, title=None):
    """model_key с приоритетом официальной модели из блока IMEI.

    Порядок: официальное название (полное, без опечаток продавца) → заголовок
    (так бывает, когда блока IMEI на странице нет вовсе) → код варианта устройства.
    """
    name, codes = parse_verified_model(verified_model)
    if name:
        key = extract_model_key(name)
        if key:
            return key
    key = extract_model_key(title or "")
    if key:
        return key
    if codes:
        return f"imei:{codes[0]}"
    return None


def record_model_codes(rec):
    """Коды варианта устройства у записи — чтобы находить лот той же модели, когда
    у одного IMEI проверен, а у другого нет."""
    codes = rec.get("model_codes")
    if isinstance(codes, list) and codes:
        return [str(c).lower() for c in codes]
    return parse_verified_model(rec.get("verified_model"))[1]


def derive_verified_model_key(verified_model):
    """Превращает строку "Модель" из блока проверки IMEI в надёжный model_key.
    Приоритет — коду в скобках (SM-S936B/A2399/MGA-LX9N): это официальный номер
    конкретного варианта устройства, куда точнее любого разбора текста заголовка,
    который пишет продавец. Если скобок нет (как в "CELIO 707C"), используем всю
    строку целиком. Префикс "imei:" метит такие ключи как точные идентификаторы —
    model_keys_match() требует для них полного совпадения, без нечёткости."""
    if not is_meaningful_model_text(verified_model):
        return None
    match = VERIFIED_MODEL_CODE_RE.search(verified_model)
    if match:
        code = match.group(1)
        if not is_meaningful_model_text(code):
            return None
        return f"imei:{code.lower()}"
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", " ", verified_model).strip().lower()
    if not is_meaningful_model_text(normalized):
        # Мусор вроде одного ":" давал ключ "imei::" — под него в одну группу
        # сбивались Redmi Note 9, iPhone 14 Pro, Galaxy S21 и т.д.
        return None
    return f"imei:{normalized}"


def skip_analysis_reason(title):
    if NEVER_PROFITABLE_RE.search(title or ""):
        return "iPhone mini — заведомо невыгодная модель, не анализируется и не уведомляет"
    return None


def junk_title_reason(title):
    """Причина, по которой лот нельзя сравнивать как обычный экземпляр модели, или None."""
    if ACCESSORY_RE.search(title or ""):
        return ("В заголовке маркер аксессуара/запчасти (чехол, стекло, дисплей, батарея и т.п.) — "
                "это не телефон целиком, в статистику цены не идёт")
    if CLONE_RE.search(title or ""):
        return "В заголовке маркер копии/реплики/дубликата — не анализируется как оригинал"
    if RESERVATION_RE.search(title or ""):
        return "В заголовке маркер брони/предзаказа/залога — цена, скорее всего, не полная стоимость устройства"
    return None


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


def fuzzy_ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def model_keys_match(key_a, key_b, threshold=FUZZY_MATCH_THRESHOLD):
    """Сравнивает два model_key нечётко, но ПОСЛОВНО, а не как одну строку целиком."""
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    # Ключи с префиксом "imei:" получены из официального кода модели (проверка
    # IMEI на сайте) — точный идентификатор устройства, где ОДИН отличающийся
    # символ означает совсем другую модель (SM-S911B/SM-S916B — S23/S23+).
    # Для них допускаем только точное совпадение (уже не подошло выше — разные).
    if key_a.startswith("imei:") or key_b.startswith("imei:"):
        return False
    words_a, words_b = key_a.split(), key_b.split()
    if len(words_a) != len(words_b):
        return False
    return all(fuzzy_ratio(wa, wb) >= threshold for wa, wb in zip(words_a, words_b))


BAD_MODEL_KEY_RE = re.compile(r"^imei:\s*$|^imei::+$")


def is_valid_model_key(key):
    """Годен ли model_key для группировки лотов.

    Отсекает исторический мусор: пустой ключ "imei:"/"imei::" (получался, когда
    labeled_value цеплял слово «Модель» не в блоке IMEI) и ключи из одного бренда
    без модели — под ними в одну «группу» сбивались совершенно разные телефоны.
    """
    if not key or not isinstance(key, str):
        return False
    key = key.strip()
    if not key or BAD_MODEL_KEY_RE.match(key):
        return False
    if key.startswith("imei:"):
        return is_meaningful_model_text(key[5:])
    return len(key.split()) >= 2


def effective_model_key(rec):
    """model_key записи, пригодный для группировки.

    В price_history.jsonl ключ хранится таким, каким был посчитан В МОМЕНТ записи,
    и сам не обновляется (старые записи содержат "imei::", "samsung" и т.п.).
    Здесь берём сохранённый ключ, только если он валиден, иначе пересчитываем по
    текущей формуле: официальная модель из базы IMEI → разбор заголовка.
    Если и так модели не видно — None: сравнивать такой лот нельзя.
    """
    title = rec.get("title") or ""
    # Заголовок, который и сегодня распознаётся как мусорный (аксессуар,
    # копия, бронь) или заведомо невыгодный (iPhone mini) — записи не
    # место в ценовой статистике вовсе.
    if junk_title_reason(title) or skip_analysis_reason(title):
        return None
    key = rec.get("model_key")
    if is_valid_model_key(key):
        return key
    recomputed = official_model_key(rec.get("verified_model"), rec.get("title") or "")
    return recomputed if is_valid_model_key(recomputed) else None


def word_in_text(word, text, text_words, threshold=FUZZY_MATCH_THRESHOLD):
    if word in text:
        return True
    return any(fuzzy_ratio(word, w) >= threshold for w in text_words)


def days_since(date_str):
    if not date_str:
        return None
    try:
        dt = time.strptime(date_str[:10], "%Y-%m-%d")
        return max(0, int((time.time() - time.mktime(dt)) / 86400))
    except (ValueError, OverflowError):
        return None


def extract_model_key(title):
    # Ключ нужен только для СРАВНЕНИЯ лота с другими лотами той же модели.
    # Поэтому None отдаём и мусорным заголовкам (копия/бронь/аксессуар), и
    # «заведомо невыгодным» iPhone mini — они не должны попадать в статистику.
    if junk_title_reason(title) or skip_analysis_reason(title):
        return None
    title = re.sub(r"(\w)\+(?=\s|$)", r"\1 plus", title)
    title = re.sub(r"(\d{2})e\b", r"\1 e", title, flags=re.I)
    title = re.sub(r"\d+\s*/\s*\d+\s*(?:gb|гб)", " ", title, flags=re.I)
    words = re.findall(r"[a-zа-я0-9]+", title.lower())

    brand = (next((w for w in words if w in SPECIFIC_LINE_WORDS), None)
             or next((w for w in words if w in KNOWN_BRANDS), None))
    if not brand:
        return None
    canonical_brand = BRAND_ALIASES.get(brand, brand)

    model_words = []
    started = False
    for idx, w in enumerate(words):
        if w == brand:
            started = True
            continue
        if not started:
            continue
        if BRAND_ALIASES.get(w, w) == canonical_brand or (brand in SPECIFIC_LINE_WORDS and w in PARENT_ECHO_WORDS):
            continue
        if w in NOISE_WORDS or w.endswith("gb") or w.endswith("гб"):
            break
        if w.isdigit() and int(w) in STORAGE_SIZES:
            next_w = words[idx + 1] if idx + 1 < len(words) else ""
            if next_w in GB_WORDS:
                break
        model_words.append(w)
    if not model_words:
        # В заголовке есть только бренд («Телефон Samsung», «Телефон Redmi») —
        # модели нет. Раньше такие лоты сбивались в один ключ "samsung" / "redmi"
        # и давали «разброс» 450 … 3700 TJS между разными аппаратами.
        return None
    return f"{canonical_brand} {' '.join(model_words[:5])}".strip()


def is_plausible_price(price):
    return isinstance(price, (int, float)) and price >= ANOMALY_MIN_PRICE


def record_memory(rec):
    mem = rec.get("memory_gb")
    if isinstance(mem, (int, float)) and mem:
        return int(mem)
    m = re.search(r"(\d+)\s*(gb|гб|tb|тб)\b", rec.get("title") or "", re.I)
    if not m:
        return None
    value = int(m.group(1))
    return value * 1024 if m.group(2).lower() in ("tb", "тб") else value


def log_market_point(item, model_key=None):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "market_point", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "vip": item.get("vip", False), "top": item.get("top", False),
            "model_key": model_key if model_key is not None else extract_model_key(item["title"]),
            "verified_model": item.get("verified_model"),
            "model_codes": item.get("model_codes") or [],
            "city": item.get("city") or item.get("city_list"),
            "published_at": item.get("published_at") or item.get("published_list"),
            "url": item.get("url"),
            "date": time.strftime("%Y-%m-%d"),
        }, ensure_ascii=False) + "\n")


def log_full_analysis(item, analysis, verdict, data_sources, model_key):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "full_analysis", "id": item["id"], "title": item["title"],
            "price": item["price"], "condition": item.get("condition"),
            "memory_gb": item.get("memory"), "description": item.get("description", ""),
            "model_key": model_key,
            "verified_model": item.get("verified_model"),
            "model_codes": item.get("model_codes") or [],
            "imei_number": item.get("imei_number"),
            "imei_masked": item.get("imei_masked"),
            "seller_name": item.get("seller_name"),
            "visible_defects": analysis.get("visible_defects", []),
            "positive_features": analysis.get("positive_features", []),
            "overall_visual_condition": analysis.get("overall_visual_condition"),
            "market_verdict": verdict,
            "confidence": analysis.get("confidence"),
            "estimated_repair_cost": analysis.get("estimated_repair_cost"),
            "estimated_customs_cost": item.get("estimated_customs_cost"),
            "imei_status": item.get("imei_status"),
            "city": item.get("city"),
            "published_at": item.get("published_at"),
            "estimated_total_cost": analysis.get("estimated_total_cost"),
            "estimated_resale_price": analysis.get("estimated_resale_price"),
            "questions_for_seller": analysis.get("questions_for_seller", []),
            "data_sources": data_sources,
            # Числа выше — мнение ИИ, а не факт: в промпт следующего анализа они
            # не передаются (sanitize_prior_analysis / PROMPT_FORBIDDEN_FIELDS).
            "estimates_are_ai_opinion": True,
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
            "verified_model": record.get("verified_model"),
            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def log_confirmed_good_call(record):
    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "confirmed_good_call", "id": record["id"], "title": record["title"],
            "price": record["price"], "condition": record.get("condition"),
            "model_key": record.get("model_key"),
            "verified_model": record.get("verified_model"),
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
            if rec.get("type") == "confirmed_good_call" and model_keys_match(rec.get("model_key"), model_key):
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
            if rec.get("type") == "confirmed_sale" and model_keys_match(rec.get("model_key"), model_key):
                if not is_plausible_price(rec.get("price")):
                    continue
                matches.append(rec)
    matches.sort(key=lambda r: r.get("confirmed_at", ""), reverse=True)
    return matches[:limit]


def get_all_confirmed_sales(limit=300):
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    sales = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "confirmed_sale":
                sales.append(rec)
    sales.sort(key=lambda r: r.get("confirmed_at", ""), reverse=True)
    return sales[:limit]


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


def similar_full_analyses(model_key, condition, exclude_id, limit=SIMILAR_EXAMPLES_LIMIT,
                          model_codes=None):
    """Все похожие лоты этой модели из базы — их получает Gemini.

    Совпадение = тот же model_key ЛИБО общий код варианта устройства (MEE7/A2105).
    Так лот с проверенным IMEI находится рядом с лотом той же модели без проверки:
    раньше ключ строился только из кода в скобках ("imei:mee7"), и такие лоты вообще
    не видели друг друга — одна модель распадалась на два непересекающихся множества.
    Лоты того же состояния, что и оцениваемый, идут первыми (их цена сопоставима
    напрямую), внутри группы — по свежести.
    """
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    codes_wanted = {str(c).lower() for c in (model_codes or [])}
    matches = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "full_analysis" or rec.get("id") == exclude_id:
                continue
            if not is_plausible_price(rec.get("price")):
                continue
            same_key = bool(model_key) and model_keys_match(effective_model_key(rec), model_key)
            same_code = bool(codes_wanted) and bool(codes_wanted & set(record_model_codes(rec)))
            if not (same_key or same_code):
                continue
            matches.append(rec)
    matches.sort(key=lambda r: r.get("collected_at", ""), reverse=True)
    matches.sort(key=lambda r: 0 if (condition and r.get("condition") == condition) else 1)
    return matches[:limit]


def similar_market_points(model_key, exclude_id, limit=SIMILAR_MARKET_POINTS_LIMIT,
                          model_codes=None):
    """Все рыночные точки этой модели (включая лоты, по которым полного анализа не
    было) — компактная таблица цен, которую тоже отправляем в Gemini."""
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    codes_wanted = {str(c).lower() for c in (model_codes or [])}
    points, seen_ids = [], set()
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "market_point" or rec.get("id") == exclude_id:
                continue
            if not is_plausible_price(rec.get("price")):
                continue
            same_key = bool(model_key) and model_keys_match(effective_model_key(rec), model_key)
            same_code = bool(codes_wanted) and bool(codes_wanted & set(record_model_codes(rec)))
            if not (same_key or same_code):
                continue
            rid = rec.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            points.append(rec)
    points.sort(key=lambda r: r.get("date", ""), reverse=True)
    return points[:limit]


def remigrate_model_keys():
    """extract_model_key()/derive_verified_model_key() менялись несколько раз — но
    model_key в уже записанных строках price_history.jsonl остаётся таким, каким
    был посчитан В МОМЕНТ записи, и сам себя не обновляет. Эта функция один раз
    перечитывает всю базу и пересчитывает model_key по ТЕКУЩЕЙ версии формулы —
    приоритет verified_model (официальная модель по базе IMEI), если он сохранён
    в записи, иначе разбор текста заголовка. Вызывается только вручную, /remigrate."""
    if not os.path.exists(PRICE_HISTORY_FILE):
        return 0, 0
    lines_out = []
    changed = total = 0
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                lines_out.append(line)
                continue
            if rec.get("type") in ("market_point", "full_analysis", "confirmed_sale", "confirmed_good_call") and rec.get("title"):
                total += 1
                # «model_key": None» — принудительно пересчитываем ключ с нуля.
                new_key = effective_model_key({**rec, "model_key": None})
                if new_key != rec.get("model_key"):
                    rec["model_key"] = new_key
                    changed += 1
            lines_out.append(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(PRICE_HISTORY_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines_out)
    if os.path.exists(ANOMALY_STATE_FILE):
        os.remove(ANOMALY_STATE_FILE)
    return changed, total


def fetch_verified_model_from_url(url):
    """Лёгкий запрос страницы объявления только за строкой "Модель" из блока
    проверки IMEI (без фото, без остального разбора) — используется для
    /remigrate_live, чтобы досчитать verified_model у старых market_point,
    у которых страница объявления никогда не открывалась при сборе. Возвращает
    строку (может быть None, если блока проверки IMEI на странице нет — значит,
    IMEI не найден в базе) или спец-значение "__gone__", если объявление больше
    не открывается (снято/удалено) — такие URL повторно не проверяем."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code in (404, 410):
            return "__gone__"
        if not resp.ok:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        raw_value = labeled_value(soup, ["Модель"])
        return raw_value if is_meaningful_model_text(raw_value) else None
    except Exception as e:
        print("Не удалось дозагрузить объявление для /remigrate_live:", url, e)
        return None


def _remigrate_live_relevant_types():
    return ("market_point", "full_analysis", "confirmed_sale", "confirmed_good_call")


def start_remigrate_live():
    """Готовит список уникальных URL, у которых ещё нет verified_model, для
    досбора его напрямую со страницы объявления (см. fetch_verified_model_from_url).
    Если процесс уже идёт — ничего не трогает, чтобы не терять прогресс."""
    existing = load_json(REMIGRATE_LIVE_STATE_FILE, None)
    if existing and not existing.get("done"):
        return existing, False

    urls_needed = []
    seen_urls = set()
    if os.path.exists(PRICE_HISTORY_FILE):
        with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") not in _remigrate_live_relevant_types():
                    continue
                url = rec.get("url")
                if not url or url in seen_urls or rec.get("verified_model"):
                    continue
                seen_urls.add(url)
                urls_needed.append(url)

    state = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "urls": urls_needed,
        "cursor": 0,
        "results": {},
        "done": False,
    }
    with open(REMIGRATE_LIVE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return state, True


def finish_remigrate_live(state):
    """Финальный проход: переписывает model_key каждой записи в базе, используя
    дособранный verified_model там, где он нашёлся, иначе — старый разбор
    заголовка (как в remigrate_model_keys)."""
    url_to_model = {
        url: result for url, result in state.get("results", {}).items()
        if result and result != "__gone__"
    }
    changed = total = 0
    if os.path.exists(PRICE_HISTORY_FILE):
        lines_out = []
        with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    lines_out.append(line)
                    continue
                if rec.get("type") in _remigrate_live_relevant_types() and rec.get("title"):
                    total += 1
                    verified_model = rec.get("verified_model") or url_to_model.get(rec.get("url"))
                    if verified_model and not rec.get("verified_model"):
                        rec["verified_model"] = verified_model
                    new_key = official_model_key(verified_model, rec["title"])
                    if new_key != rec.get("model_key"):
                        rec["model_key"] = new_key
                        changed += 1
                lines_out.append(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(PRICE_HISTORY_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines_out)
    if os.path.exists(ANOMALY_STATE_FILE):
        os.remove(ANOMALY_STATE_FILE)
    return changed, total


def process_remigrate_live_batch(subscribers):
    """Вызывается на каждом прогоне бота. Если фоновый досбор verified_model
    (/remigrate_live) активен — обрабатывает следующую пачку URL и, если это
    была последняя пачка, выполняет финальную переписку model_key и оповещает
    владельца. Ничего не делает, если процесс не запущен."""
    state = load_json(REMIGRATE_LIVE_STATE_FILE, None)
    if not state or state.get("done"):
        return

    urls = state.get("urls", [])
    cursor = state.get("cursor", 0)
    batch = urls[cursor: cursor + REMIGRATE_LIVE_BATCH_SIZE]
    for url in batch:
        state["results"][url] = fetch_verified_model_from_url(url)
        time.sleep(1)
    state["cursor"] = cursor + len(batch)

    if state["cursor"] >= len(urls):
        changed, total = finish_remigrate_live(state)
        found = sum(1 for v in state["results"].values() if v and v != "__gone__")
        alert_owner(
            f"/remigrate_live завершён: проверено объявлений {len(urls)}, найдена модель по IMEI у {found}, "
            f"обновлено model_key {changed} из {total} записей в базе. Список аномалий сброшен — "
            f"проверь /anomalies заново."
        )
        state["done"] = True

    with open(REMIGRATE_LIVE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _cluster_prices(sorted_prices, gap_ratio):
    """Разбивает ОТСОРТИРОВАННЫЕ цены на кластеры по «разрывам».

    Разрыв между соседними ценами больше gap_ratio раз означает, что это, скорее
    всего, уже другое устройство (или приманка), а не разброс цен одной модели.
    """
    clusters, current = [], []
    for price in sorted_prices:
        if current and price > current[-1] * gap_ratio:
            clusters.append(current)
            current = []
        current.append(price)
    if current:
        clusters.append(current)
    return clusters


def load_corrections():
    """Правки лотов, снятые разбором аномалий (anomaly_review.py).

    Отдельный файл, а не правка журнала: price_history.jsonl — сырая история,
    по ней видно, что парсер считал раньше. Ключ — id лота из базы.
    """
    data = load_json(ANOMALY_CORRECTIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_corrections(corrections):
    with open(ANOMALY_CORRECTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(corrections, f, ensure_ascii=False, indent=2, sort_keys=True)


def log_anomaly_reason(entry):
    """Причина аномалии и подсказка для кода — в отдельный журнал.

    Журнал читается командой /anomalyreasons: по нему правят сам парсер, чтобы
    те же группы не приходилось разбирать снова.
    """
    with open(ANOMALY_REASONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_anomaly_reasons(limit=10):
    if not os.path.exists(ANOMALY_REASONS_FILE):
        return []
    rows = []
    with open(ANOMALY_REASONS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def find_price_anomalies(ratio_threshold=ANOMALY_RATIO_THRESHOLD, min_count=ANOMALY_MIN_COUNT, corrections=None):
    """Ищет группы, где в один model_key склеились РАЗНЫЕ устройства.

    Ситуация разбирается по кластерам цен и разделяется на три случая:
      * kind="merged"  — в группе несколько полноценных кластеров (в каждом
        >= min_count лотов): ключ действительно смешал разные модели;
      * kind="outlier" — один крупный кластер + единичные нетипичные лоты
        (приманка, копия, запчасть): ключ чистый, пересчитывать его не нужно;
      * kind="wide"    — один кластер, но разброс внутри него больше порога.

    Новое: слой исправлений ANOMALY_CORRECTIONS_FILE читается ПЕРВЫМ. Это правки,
    которые внёс разбор аномалий через Gemini (anomaly_review.py), поэтому
    разобранная группа больше не выглядит аномальной, а журнал price_history.jsonl
    остаётся неизменным:
      * {"action": "exclude"} — лот-аксессуар/приманка: в статистику не идёт;
      * "model_key" / "memory_gb" / "price" — поле подменяется исправленным.

    В каждой группе теперь лежит список lots (id, цена, ссылка, город, память,
    состояние) — по нему разбор открывает страницы объявлений.
    """
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    corrections = corrections if corrections is not None else load_corrections()
    groups = {}
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("market_point", "full_analysis"):
                continue
            lot_id = str(rec.get("id") or "")
            corr = corrections.get(lot_id) or {}
            if corr.get("action") == "exclude":
                continue
            price = corr.get("price") or rec.get("price")
            if not price or price < ANOMALY_MIN_PRICE:
                continue
            try:
                price = int(price)
            except (TypeError, ValueError):
                continue
            model_key = corr.get("model_key") or effective_model_key(rec)
            if corr.get("model_key") and not is_valid_model_key(corr["model_key"]):
                model_key = effective_model_key(rec)
            if not model_key:
                # «Телефон Samsung», лот без модели, аксессуар — группировать не с чем.
                continue
            memory = corr.get("memory_gb") or record_memory(rec)
            groups.setdefault((model_key, memory), []).append({
                "id": lot_id, "price": price, "title": rec.get("title") or "",
                "url": rec.get("url") or "", "memory": memory,
                "condition": rec.get("condition"),
                "city": rec.get("city"), "published_at": rec.get("published_at"),
                "corrected": bool(corr),
            })

    anomalies = []
    for (model_key, memory), lots in groups.items():
        if len(lots) < min_count:
            continue
        lots_sorted = sorted(lots, key=lambda l: l["price"])
        prices = [l["price"] for l in lots_sorted]
        clusters = _cluster_prices(prices, ANOMALY_CLUSTER_GAP)
        main = max(clusters, key=len)
        full = [c for c in clusters if len(c) >= min_count]
        ratio_core = main[-1] / main[0] if main[0] > 0 else 1.0
        ratio_all = prices[-1] / prices[0] if prices[0] > 0 else 0.0

        if len(full) >= 2:
            kind = "merged"
        elif len(full) == 1 and len(clusters) > 1 and ratio_all >= ratio_threshold:
            kind = "outlier"
        elif len(full) == 1 and ratio_core >= ratio_threshold:
            kind = "wide"
        else:
            continue
        if kind == "merged" and ratio_core < 1.0:
            continue

        anomalies.append({
            "kind": kind,
            "model_key": model_key, "memory": memory,
            "label": f"{model_key} {memory}GB" if memory else f"{model_key} (память не указана)",
            "group_key": f"{model_key}|{memory or '?'}",
            "count": len(lots),
            "ratio": round(ratio_all if kind != "wide" else ratio_core, 2),
            "clusters": clusters,
            "cluster_sizes": [len(c) for c in clusters],
            "core_min": main[0], "core_max": main[-1],
            "min_price": prices[0], "min_title": lots_sorted[0]["title"],
            "max_price": prices[-1], "max_title": lots_sorted[-1]["title"],
            "min_id": lots_sorted[0]["id"], "max_id": lots_sorted[-1]["id"],
            "corrected": sum(1 for l in lots if l["corrected"]),
            "lots": lots_sorted,
        })
    anomalies.sort(key=lambda a: ({"merged": 0, "outlier": 1, "wide": 2}[a["kind"]], -a["ratio"]))
    return anomalies
def market_stats_for(query, condition, exclude_id):
    """Только для команды /price — Gemini эту сводку не получает. Принимает сырой
    текст запроса (а не готовый model_key): часть записей теперь имеет ключ вида
    "imei:sm-s936b" (по официальной модели с базы IMEI), который не совпадёт с
    придуманным пользователем текстовым запросом через обычное сравнение — нужен
    тот же текстовый фолбэк по заголовку, что и в find_market_listings(), иначе
    такие записи молча выпадут из статистики min/median."""
    if not query or not os.path.exists(PRICE_HISTORY_FILE):
        return None
    model_key = extract_model_key(query) or query.lower().strip()
    query_words = [w for w in re.findall(r"[a-zа-я0-9]+", query.lower()) if len(w) > 2]
    prices = []
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == exclude_id or not is_plausible_price(rec.get("price")):
                continue
            if condition and rec.get("condition") and rec.get("condition") != condition:
                continue

            title = rec.get("title", "")
            title_low = title.lower()
            title_words = re.findall(r"[a-zа-я0-9]+", title_low)
            match = model_keys_match(effective_model_key(rec), model_key)
            if not match and query_words:
                match = all(
                    any(word_in_text(v, title_low, title_words) for v in keyword_variants(w))
                    for w in query_words
                )
            if not match:
                continue
            prices.append(rec["price"])
    if len(prices) < 3:
        return None
    return {"count": len(prices), "min": min(prices), "median": round(statistics.median(prices))}


def find_market_listings(query, limit=PRICE_COMMAND_LIMIT):
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    model_key = extract_model_key(query) or query.lower().strip()
    query_words = [w for w in re.findall(r"[a-zа-я0-9]+", query.lower()) if len(w) > 2]

    latest_by_id = {}
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("market_point", "full_analysis") or not rec.get("price"):
                continue

            title = rec.get("title", "")
            title_low = title.lower()
            title_words = re.findall(r"[a-zа-я0-9]+", title_low)

            match = model_keys_match(effective_model_key(rec), model_key)
            if not match and query_words:
                match = all(
                    any(word_in_text(v, title_low, title_words) for v in keyword_variants(w))
                    for w in query_words
                )
            if not match:
                continue

            rid = rec.get("id")
            sort_key = rec.get("collected_at") or rec.get("date", "")
            if rid not in latest_by_id or sort_key >= latest_by_id[rid]["_sort_key"]:
                latest_by_id[rid] = {
                    "title": title, "price": rec.get("price"),
                    "url": rec.get("url"), "_sort_key": sort_key,
                }

    items = sorted(latest_by_id.values(), key=lambda r: r["_sort_key"], reverse=True)
    return items[:limit]


# ---------- Минимальная выгода / уверенность / максимальная цена ----------

def gemini_budget_remaining(usage):
    """Сколько анализов ещё можно потратить сегодня по всем сочетаниям ключ+модель."""
    total_limit = GEMINI_DAILY_LIMIT_PER_COMBO * len(GEMINI_COMBOS)
    used = sum(int(usage.get(combo["id"], 0) or 0) for combo in GEMINI_COMBOS)
    return max(0, total_limit - used)


def allowed_analyses_per_run(usage):
    """Сколько лотов анализировать в этом прогоне.

    Прогоны идут каждые 5 минут, поэтому нужен лимит на прогон, иначе суточная
    квота Gemini выгорит за пару часов: остаток бюджета делим на число прогонов
    до конца суток. Новые объявления разбираются почти сразу, а квота тратится
    равномерно.
    """
    left = gemini_budget_remaining(usage)
    if left <= 0:
        return 0
    hours_left = max(1, 24 - time.gmtime().tm_hour)
    runs_left = max(1, hours_left * RUNS_PER_HOUR)
    return max(1, min(MAX_NEW_ITEMS_PER_RUN, left // runs_left))


def get_min_profit():
    return load_json(MIN_PROFIT_FILE, {}).get("value", DEFAULT_MIN_PROFIT)


def set_min_profit(value):
    with open(MIN_PROFIT_FILE, "w", encoding="utf-8") as f:
        json.dump({"value": value}, f)


def get_min_confidence():
    return load_json(MIN_CONFIDENCE_FILE, {}).get("value", DEFAULT_MIN_CONFIDENCE)


def set_min_confidence(value):
    with open(MIN_CONFIDENCE_FILE, "w", encoding="utf-8") as f:
        json.dump({"value": value}, f)


def get_max_price():
    """Максимальная цена лота (TJS), который бот вообще обрабатывает (тратит на него
    Gemini/фото/веб-поиск). 0 = выключено (обрабатываем любую цену, как раньше)."""
    return load_json(MAX_PRICE_FILE, {}).get("value", DEFAULT_MAX_PRICE)


def set_max_price(value):
    with open(MAX_PRICE_FILE, "w", encoding="utf-8") as f:
        json.dump({"value": value}, f)


# ---------- Курс USD/TJS ----------

def seller_key_from_url(seller_url):
    """Стабильный ключ продавца: последний элемент ссылки на его профиль/все
    объявления (https://m.somon.tj/items/author/322889/ -> '322889',
    https://m.somon.tj/c/KhushangPhones/ -> 'khushangphones').

    Для одного человека ссылка приходит в разных формах (/c/<слаг>/ и
    /items/author/<номер>/), поэтому ключ нормализуем: убираем query/fragment,
    приводим к нижнему регистру. Числовой ключ — внутренний id продавца, он
    стабилен; именно он и печатается в уведомлениях, когда у страницы нет
    человеческого имени аккаунта.
    """
    if not seller_url:
        return None
    cleaned = seller_url.split("?")[0].split("#")[0].rstrip("/")
    slug = cleaned.rsplit("/", 1)[-1].strip().lower()
    if not slug or slug in ("items", "author", "c", "adv"):
        return None
    return slug


def load_seller_blacklist(strict=False):
    """Читает чёрный список продавцов.

    strict=True (режим записи): при повреждённом или нечитаемом файле НЕ отдаём
    пустой словарь. Код, который сохраняет список сразу после чтения, записал бы
    поверх файла одну новую запись и уничтожил все прежние — именно так чёрный
    список и «похудел» с сотен записей до полусотни (в журнале объявлений 150
    событий «добавлен в чёрный список», а в файле осталось 59 записей, и ни одной
    за 26–27 сентября). Битый файл сохраняем под другим именем и говорим владельцу.
    """
    if not os.path.exists(SELLER_BLACKLIST_FILE):
        return {}
    try:
        with open(SELLER_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("ожидался JSON-объект {ключ: запись}")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"⚠️ Ошибка в файле {SELLER_BLACKLIST_FILE}: {e}")
        if not strict:
            return {}
        backup = f"{SELLER_BLACKLIST_FILE}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(SELLER_BLACKLIST_FILE, backup)
            alert_owner(f"⚠️ Файл чёрного списка продавцов повреждён ({e}).\n"
                        f"Копия сохранена как {backup} — записи не потеряны, разбери файл вручную.\n"
                        f"Бот продолжает работать и ведёт список дальше.")
        except OSError as backup_error:
            print(f"⚠️ Не удалось сохранить копию битого файла: {backup_error}")
        return {}


def save_seller_blacklist(blacklist):
    """Атомарная запись (временный файл + os.replace): обрыв раннера не может
    оставить недописанный JSON, из-за которого следующий прогон начал бы с нуля."""
    tmp_path = f"{SELLER_BLACKLIST_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(blacklist, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SELLER_BLACKLIST_FILE)


def is_seller_blacklisted(seller_key):
    if not seller_key:
        return None
    return load_seller_blacklist().get(seller_key)


def blacklist_seller(seller_key, name, profile_url, reason, ad_url, extra=None):
    """Добавляет продавца в чёрный список. Возвращает (запись, is_new).

    is_new=False означает, что продавец УЖЕ был в списке: обновляем причину и
    историю объявлений, но повторное уведомление владельцу не шлём — иначе на
    одного и того же продавца прилетают десятки одинаковых «🚫 добавлен в чёрный
    список» (у одного и того же продавца ещё и не меняется внутренний номер,
    который печатался вместо имени, потому что name был None).
    """
    blacklist = load_seller_blacklist(strict=True)
    existing = blacklist.get(seller_key)
    is_new = existing is None
    entry = dict(existing or {})
    entry.update({
        "name": name or entry.get("name"),
        "profile_url": profile_url or entry.get("profile_url"),
        "reason": reason,
        "blocked_by_ad_url": ad_url,
        "blocked_at": entry.get("blocked_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    if extra:
        entry.update(extra)
    entry["ban_count"] = int(entry.get("ban_count") or 0) + 1
    ads = [u for u in (entry.get("ban_ad_urls") or []) if u != ad_url]
    if ad_url:
        ads.append(ad_url)
    entry["ban_ad_urls"] = ads[-10:]
    blacklist[seller_key] = entry
    save_seller_blacklist(blacklist)
    return entry, is_new


def unblacklist_seller(seller_key):
    blacklist = load_seller_blacklist(strict=True)
    removed = blacklist.pop(seller_key, None)
    if removed is not None:
        save_seller_blacklist(blacklist)
    return removed


def looks_like_phone_title(title):
    """Дешёвая проверка без Gemini: известный бренд телефона (extract_model_key)
    ИЛИ характерные слова в заголовке (телефон/смартфон/gb и т.п.)."""
    return bool(extract_model_key(title)) or bool(PHONE_WORD_RE.search(title))


def fetch_seller_titles(seller_url, limit=40):
    """Заголовки активных объявлений продавца с его страницы (m.somon.tj/c/<slug>/).
    При любой ошибке — пустой список, вызывающий код должен относиться к этому
    как к "не удалось проверить", а не как к "объявлений о телефонах нет"."""
    if not seller_url:
        return []
    try:
        resp = requests.get(seller_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"Не удалось загрузить страницу продавца {seller_url}: {e}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    titles, seen_links = [], set()
    for a in soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}")):
        href = absolute_url(a.get("href", ""))
        if not href or href in seen_links:
            continue
        seen_links.add(href)
        raw_text = a.get_text(" ", strip=True)
        if len(raw_text) < 5:
            continue
        title = clean_title(raw_text)[0]
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def seller_phone_ratio(seller_url):
    """Возвращает (доля_телефонных, сколько_всего_разобрано). Доля None, если
    со страницы продавца не удалось разобрать ни одного заголовка — тогда
    решение по бану принимать нельзя, только по фото через Gemini."""
    titles = fetch_seller_titles(seller_url)
    if not titles:
        return None, 0
    phone_like = sum(1 for t in titles if looks_like_phone_title(t))
    return phone_like / len(titles), len(titles)


def reseller_heuristic_reason(item, phone_ratio=None, phone_sample=0):
    """Эвристика по данным со страницы объявления (без обращения к Gemini за анализ
    фото): много активных объявлений САМО ПО СЕБЕ ничего не значит — на Somon.tj
    так выглядит и обычная барахолка на 15 разных вещей. Баним только если вдобавок
    почти все объявления продавца — телефоны/планшеты (phone_ratio), т.е. это
    действительно точка продажи телефонов, а не человек с одним объявлением
    в куче другого хлама."""
    active_ads = item.get("seller_active_ads")
    name = item.get("seller_name") or ""
    if active_ads is None:
        return None

    enough_ads = active_ads >= RESELLER_MIN_ACTIVE_ADS
    soft_enough_ads = active_ads >= RESELLER_SOFT_ACTIVE_ADS and SHOP_NAME_RE.search(name)
    if not (enough_ads or soft_enough_ads):
        return None

    if phone_ratio is None or phone_sample < RESELLER_MIN_SAMPLE:
        # Не смогли проверить, чем ещё торгует продавец — по одному количеству
        # объявлений не баним, это может быть и обычная барахолка.
        return None
    if phone_ratio < RESELLER_PHONE_RATIO:
        print(f"Продавец «{name or item.get('seller_key')}»: {active_ads} активных объявлений, "
              f"но лишь {round(phone_ratio * 100)}% похожи на телефоны из {phone_sample} — не баню")
        return None

    ratio_str = f"{round(phone_ratio * 100)}% из {phone_sample} объявлений — телефоны"
    if enough_ads:
        return f"у продавца {active_ads} активных объявлений, {ratio_str} — похоже на магазин/перекупщика"
    return (f"у продавца {active_ads} активных объявлений, а имя «{name}» похоже на название "
            f"магазина/точки продаж, {ratio_str} — похоже на перекупщика")


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
    if IMEI_BLACK_RE.search(full_text):
        return "black"
    if IMEI_GRAY_RE.search(full_text):
        return "gray"
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


def pick_available_combo(usage, exclude=()):
    for combo in GEMINI_COMBOS:
        if combo["id"] in exclude:
            continue
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


def send_telegram(chat_id, text, reply_markup=None):
    """Отправка сообщения.

    reply_markup — готовая JSON-строка клавиатуры. Если её не передали, к сообщению
    автоматически прикрепляется нижняя клавиатура разделов: раньше она уходила только
    при /start, поэтому навигация пропадала из виду. Так кнопки остаются на месте в
    любом ответе бота — и в уведомлениях о находках тоже.
    """
    try:
        data = {"chat_id": chat_id, "text": text}
        data["reply_markup"] = reply_markup if reply_markup else persistent_keyboard_markup()
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              data=data, timeout=15)
        if not resp.ok:
            print("Ошибка Telegram:", chat_id, resp.status_code, resp.text)
    except Exception as e:
        print("Не удалось отправить в Telegram:", chat_id, e)


def answer_callback_query(callback_id, text=""):
    """Обязательный ответ на нажатие кнопки — иначе Telegram крутит «часики»."""
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                       data={"callback_query_id": callback_id, "text": text[:200]}, timeout=15)
    except Exception as e:
        print("Не удалось ответить на нажатие кнопки:", e)


REPLY_BUTTONS = [
    ["🏠 Меню", "📖 Все команды"],
    ["🔍 Аномалии", "✅ Продажи"],
    ["⚙️ Режим", "💰 Пороги"],
    ["🚫 Чёрный список", "📊 Статистика"],
]

# Нажатие текстовой кнопки приходит обычным сообщением, а не callback: переводим
# подпись кнопки в команду или в раздел меню.
TEXT_BUTTON_ACTIONS = {
    "🏠 Меню": ("section", "main"),
    "📖 Все команды": ("cmd", "/help"),
    "🔍 Аномалии": ("cmd", "/anomalies"),
    "✅ Продажи": ("cmd", "/sold"),
    "⚙️ Режим": ("section", "mode"),
    "💰 Пороги": ("section", "thresholds"),
    "🚫 Чёрный список": ("cmd", "/blacklist"),
    "📊 Статистика": ("cmd", "/stats"),
}


def persistent_keyboard_markup():
    """Постоянная нижняя клавиатура: только разделы, по две кнопки в ряд.

    Раньше вся навигация была одной инлайн-клавиатурой из 7 рядов по 3 кнопки с
    длинными подписями — текст не помещался, а разделы были не видны, пока не
    нажмёшь /start. Здесь подписи короткие (до 17 знаков), поэтому помещаются.
    """
    keyboard = [[{"text": label} for label in row] for row in REPLY_BUTTONS]
    return json.dumps({"keyboard": keyboard, "resize_keyboard": True,
                       "input_field_placeholder": "Команда или кнопка ниже"},
                      ensure_ascii=False)


def main_menu_markup():
    """Инлайн-меню разделов. Имя сохранено прежним: на него опираются все
    существующие вызовы reply_markup=main_menu_markup()."""
    return section_markup("main")


def section_markup(section):
    """Клавиатура одного раздела: подписи короткие, у каждого есть «Назад»."""
    back = {"text": "⬅️ Назад", "callback_data": "nav:main"}
    if section == "mode":
        rows = [[{"text": "🌐 Все", "callback_data": "mode:all"}],
                [{"text": "🚶 Только Б/у", "callback_data": "mode:used"}],
                [{"text": "⚙️ По параметрам", "callback_data": "mode:params"}],
                [back]]
    elif section == "thresholds":
        profit, conf, ceiling = get_min_profit(), get_min_confidence(), get_max_price()
        rows = [[{"text": f"💰 Выгода: {profit} TJS", "callback_data": "settings:profit"}],
                [{"text": "−100", "callback_data": "profit:-100"},
                 {"text": "+100", "callback_data": "profit:+100"}],
                [{"text": f"🎯 Уверенность: {round(conf * 100)}%", "callback_data": "settings:conf"}],
                [{"text": "−5%", "callback_data": "conf:-0.05"},
                 {"text": "+5%", "callback_data": "conf:+0.05"}],
                [{"text": f"📉 Потолок: {ceiling or 'нет'}", "callback_data": "settings:ceiling"}],
                [{"text": "−500", "callback_data": "ceiling:-500"},
                 {"text": "+500", "callback_data": "ceiling:+500"}],
                [back]]
    elif section == "db":
        rows = [[{"text": "🔄 Пересчитать ключи", "callback_data": "remigrate"}],
                [{"text": "🧩 Дособрать по IMEI", "callback_data": "remigrate_live"}],
                [{"text": "📁 Отклонённые лоты", "callback_data": "rejected"}],
                [{"text": "🩺 Разобрать аномалии", "callback_data": "anomaly_review"}],
                [{"text": "📝 Причины аномалий", "callback_data": "anomaly_reasons"}],
                [back]]
    else:
        rows = [[{"text": "🔍 Аномалии", "callback_data": "anomalies"},
                 {"text": "✅ Продажи", "callback_data": "sold"}],
                [{"text": "🚫 Чёрный список", "callback_data": "blacklist"},
                 {"text": "📊 Статистика", "callback_data": "stats"}],
                [{"text": "⚙️ Режим", "callback_data": "sec:mode"},
                 {"text": "💰 Пороги", "callback_data": "sec:thresholds"}],
                [{"text": "🗂 База", "callback_data": "sec:db"},
                 {"text": "📖 Все команды", "callback_data": "help"}]]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


SECTION_TITLE = {"mode": "⚙️ Режим поиска", "thresholds": "💰 Пороги уведомлений",
                 "db": "🗂 Работа с базой", "main": "🏠 Разделы меню"}


def send_section(chat_id, section):
    """Показать раздел: заголовок + его кнопки."""
    title = SECTION_TITLE.get(section, "🏠 Разделы меню")
    if section == "thresholds":
        body = (f"{title}\n\n"
                f"Выгода: {get_min_profit()} TJS · Уверенность: {round(get_min_confidence() * 100)}% · "
                f"Потолок: {get_max_price() or 'не задан'} TJS")
    elif section == "mode":
        body = f"{title}\n\nСейчас: {MODES[get_mode()]}"
    elif section == "db":
        body = f"{title}\n\nПересчёт ключей и досбор модели по IMEI идут по всей базе — это долго."
    else:
        body = f"{title}\n\nРазделы ниже. Все команды — кнопка «📖 Все команды» или /help."
    send_telegram(chat_id, body, reply_markup=section_markup(section))


def send_persistent_keyboard(chat_id):
    """Включить нижнюю клавиатуру. Отправляется как отдельное сообщение, потому что
    в одном сообщении Telegram допускает только одну клавиатуру: инлайн-меню разделов
    и нижние кнопки вместе не отправить."""
    send_telegram(chat_id, "⌨️ Нижние кнопки включены — они всегда под рукой.",
                  reply_markup=persistent_keyboard_markup())


def send_help(chat_id):
    send_telegram(chat_id, HELP_TEXT, reply_markup=section_markup("main"))


MENU_ACK = {"anomalies": "Смотрю базу…", "sold": "Собираю продажи…",
            "blacklist": "Открываю чёрный список…", "remigrate": "Пересчитываю ключи…",
            "remigrate_live": "Собираю модели по IMEI…", "rejected": "Готовлю файл…",
            "help": "Открываю список команд…", "menu": "Открываю меню…",
            "stats": "Считаю статистику…", "nav:main": "Открываю меню…",
            "sec:mode": "Открываю режим…", "sec:thresholds": "Открываю пороги…",
            "sec:db": "Открываю работу с базой…",
            "mode:all": "Ставлю режим «Все»…", "mode:used": "Ставлю режим «Только Б/у»…",
            "mode:params": "Ставлю режим «По параметрам»…"}



def send_main_menu(chat_id, note=None):
    text = (note or "Управление ботом — кнопки ниже.\n"
            "Бот запускается по расписанию GitHub Actions, поэтому ответ на нажатие "
            "приходит в течение нескольких минут.")
    send_telegram(chat_id, text, reply_markup=main_menu_markup())


def handle_menu_settings(chat_id, data):
    """Нажатия кнопок: разделы, «Назад» и пороги. Возвращает True, если нажатие
    обработано здесь и в цепочку команд идти не нужно."""
    if data == "nav:main":
        send_section(chat_id, "main")
        return True
    if data.startswith("sec:"):
        send_section(chat_id, data.split(":", 1)[1])
        return True
    if data == "settings:profit":
        send_telegram(chat_id, f"💰 Минимальная выгода: {get_min_profit()} TJS.\n"
                               "Шаги −100 / +100 — кнопками ниже.",
                      reply_markup=section_markup("thresholds"))
        return True
    if data == "settings:conf":
        send_telegram(chat_id, f"🎯 Минимальная уверенность Gemini: {round(get_min_confidence() * 100)}%.\n"
                               "Шаги −5% / +5% — кнопками ниже.",
                      reply_markup=section_markup("thresholds"))
        return True
    if data == "settings:ceiling":
        ceiling = get_max_price()
        shown = f"{ceiling} TJS" if ceiling else "не задан (без ограничения)"
        send_telegram(chat_id, f"📉 Потолок цены: {shown}.\nШаги −500 / +500 — кнопками ниже.",
                      reply_markup=section_markup("thresholds"))
        return True
    for label, prefix, getter, setter, is_percent in (
            ("выгода", "profit:", get_min_profit, set_min_profit, False),
            ("уверенность", "conf:", get_min_confidence, set_min_confidence, True),
            ("потолок цены", "ceiling:", get_max_price, set_max_price, False)):
        if not data.startswith(prefix):
            continue
        try:
            delta = float(data[len(prefix):])
        except ValueError:
            return True
        value = (getter() or 0) + delta
        if is_percent:
            value = round(min(max(value, 0.0), 1.0), 2)
            shown = f"{round(value * 100)}%"
        else:
            value = int(min(max(value, 0), 10 ** 7))
            shown = f"{value} TJS"
        setter(value)
        print(f"Настройка «{label}» изменена на {shown}")
        send_telegram(chat_id, f"✅ {label.capitalize()} теперь {shown}.",
                      reply_markup=section_markup("thresholds"))
        return True
    return False

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


def backfill_entry_from_history(ad_id, entry):
    needed = ("url", "title", "price", "condition", "model_key")
    if all(entry.get(f) for f in needed):
        return entry
    if not os.path.exists(PRICE_HISTORY_FILE):
        return entry

    best, best_date = None, ""
    with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") != ad_id or rec.get("type") not in ("market_point", "full_analysis"):
                continue
            date_key = rec.get("collected_at") or rec.get("date", "")
            if best is None or date_key >= best_date:
                best, best_date = rec, date_key
    if not best:
        return entry

    updated = dict(entry)
    for f in needed:
        if not updated.get(f) and best.get(f):
            updated[f] = best[f]
    if not updated.get("verified_model") and best.get("verified_model"):
        updated["verified_model"] = best["verified_model"]
    return updated


MAX_SOLD_CHECK_ATTEMPTS = int(os.environ.get("MAX_SOLD_CHECK_ATTEMPTS", "3"))


def process_disappeared_ads(seen, current_ids):
    disappeared = [
        (ad_id, entry) for ad_id, entry in seen.items()
        if entry.get("photo_ok") and ad_id not in current_ids
        and not entry.get("sold_checked")
    ]
    for ad_id, entry in disappeared:
        seen[ad_id] = backfill_entry_from_history(ad_id, entry)

    to_check = [
        (ad_id, entry) for ad_id, entry in disappeared
        if seen[ad_id].get("url")
    ][:MAX_SOLD_CHECKS_PER_RUN]

    for ad_id, entry in to_check:
        is_sold = check_sold_status(seen[ad_id]["url"])
        if is_sold is True:
            record = {
                "id": ad_id, "title": seen[ad_id].get("title"), "price": seen[ad_id].get("price"),
                "condition": seen[ad_id].get("condition"), "model_key": seen[ad_id].get("model_key"),
                "verified_model": seen[ad_id].get("verified_model"),
            }
            log_confirmed_sale(record)
        if is_sold is not None:
            seen[ad_id]["sold_checked"] = True
        else:
            attempts = seen[ad_id].get("sold_check_attempts", 0) + 1
            seen[ad_id]["sold_check_attempts"] = attempts
            if attempts >= MAX_SOLD_CHECK_ATTEMPTS:
                seen[ad_id]["sold_checked"] = True
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


BOT_COMMANDS = [('start', 'Начать работу, показать меню'), ('menu', 'Меню управления кнопками'), ('help', 'Полный список всех команд'), ('all', 'Режим: все объявления'), ('used', 'Режим: только б/у'), ('params', 'Режим: поиск по параметрам'), ('mode', 'Режим поиска (служебная)'), ('add', 'Добавить поиск: Название | слова | цена | память'), ('del', 'Удалить поиск по названию'), ('list', 'Список моих поисков'), ('minprofit', 'Минимальная выгода, TJS'), ('minconfidence', 'Минимальная уверенность Gemini, %'), ('maxprice', 'Потолок цены объявления, TJS'), ('price', 'Сводка цен по модели'), ('sold', 'Проданные телефоны по моделям'), ('anomalies', 'Проверить базу на разбросы цен'), ('fixanomalies', 'Разбор аномалий через Gemini: причины и правки'), ('anomalyreasons', 'Журнал причин аномалий и подсказок для кода'), ('remigrate', 'Пересчитать ключи всей базы'), ('remigrate_live', 'Дособрать модель по IMEI и пересчитать'), ('blacklist', 'Продавцы в чёрном списке'), ('blacklist_add', 'Заблокировать продавца вручную'), ('blacklist_del', 'Убрать продавца из чёрного списка'), ('stats', 'Расход лимитов и размер базы'), ('manual', 'Проверить объявление без фото'), ('bought', 'Подтвердить удачную покупку'), ('rejected', 'Файл со всеми отклонёнными лотами'), ('dbadd', 'Добавить запись в базу вручную'), ('subscribers', 'Сколько подписчиков'), ('stop', 'Отписаться от уведомлений')]

HELP_TEXT = """📖 Все команды — по разделам.

⌨️ Клавиатура внизу всегда под рукой: разделы открываются кнопками, а любая команда ниже работает и текстом.

▫️ Режим поиска
/all /used /params — все / только б-у / по параметрам
/mode — служебная запись режима

▫️ Мои поиски
/add Название | слова | макс_цена | мин_память — добавить
/del Название — удалить
/list — список поисков

▫️ Пороги уведомлений
/minprofit число — минимальная выгода, TJS (0 — выкл.)
/minconfidence число — минимальная уверенность Gemini, % (0 — выкл.)
/maxprice число — потолок цены объявления, TJS (0 — выкл.)

▫️ База и цены
/price Модель — грубая сводка цен по базе
/sold — фактически проданные телефоны (цена, дата)
/anomalies — проверить базу на разбросы цен
/remigrate — пересчитать model_key всей базы
/remigrate_live — дособрать модель по IMEI и пересчитать
/dbadd текст — добавить что угодно в базу вручную
/rejected — файл со всеми отклонёнными лотами
/fixanomalies — разбор аномалий: Gemini читает страницы лотов, правит поля и пишет причину
/anomalyreasons — журнал причин аномалий и подсказок для кода

▫️ Продавцы
/blacklist — список продавцов (причина + объявление-повод)
/blacklist_add ссылка | причина — заблокировать вручную
/blacklist_del слаг_или_ссылка — убрать из чёрного списка

▫️ Проверка объявления
/manual Название | Цена | Состояние | Описание — без фото, в базу не пишется
/bought ссылка — подтвердить удачную покупку по рекомендации

▫️ Прочее
/stats — расход лимитов и размер базы
/subscribers — сколько подписчиков
/stop — отписаться
/menu — меню кнопками, /help — этот список"""

BOT_SETUP_FILE = "bot_setup.json"
# Полный список команд для /help (см. HELP_TEXT) и для setMyCommands.


def telegram_api(method, payload=None):
    """Вызов Bot API. Возвращает разобранный ответ или словарь с ошибкой."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                          data=payload or {}, timeout=30)
        try:
            body = r.json()
        except ValueError:
            return {"ok": False, "status": r.status_code, "text": r.text[:200]}
        if not r.ok:
            body.setdefault("status", r.status_code)
        return body
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def register_bot_commands(force=False):
    """Раз в сутки: снять webhook и опубликовать список команд для кнопки «/».

    Если у бота когда-то был установлен webhook, метод getUpdates перестаёт отдавать
    обновления — и бот «не слушает» команды вообще, хотя код исправен. Поэтому при
    каждом запуске проверяем getWebhookInfo и снимаем webhook, а setMyCommands делает
    все команды видимыми в списке Telegram (раньше их не было в навигации).
    """
    fingerprint = hashlib.md5(json.dumps(BOT_COMMANDS, ensure_ascii=False).encode()).hexdigest()
    state = load_json(BOT_SETUP_FILE, {})
    today = time.strftime("%Y-%m-%d")
    if not force and state.get("fingerprint") == fingerprint and state.get("checked_on") == today:
        return
    info = telegram_api("getWebhookInfo")
    had_webhook = ""
    if isinstance(info, dict) and info.get("ok"):
        had_webhook = ((info.get("result") or {}).get("url") or "").strip()
    if had_webhook:
        print(f"⚠️ У бота стоял webhook ({had_webhook}) — getUpdates не отдавал команды. Снимаю.")
        telegram_api("deleteWebhook", {"drop_pending_updates": "false"})
    res = telegram_api("setMyCommands", {"commands": json.dumps(
        [{"command": c, "description": d} for c, d in BOT_COMMANDS], ensure_ascii=False)})
    ok = bool(isinstance(res, dict) and res.get("ok"))
    print(("✅ " if ok else "⚠️ ") + f"Список команд для Telegram: {len(BOT_COMMANDS)} шт. — "
          + ("опубликован" if ok else str(res)[:200]))
    state.update({"fingerprint": fingerprint, "checked_on": today,
                  "commands": len(BOT_COMMANDS), "had_webhook": had_webhook,
                  "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    with open(BOT_SETUP_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_telegram_commands(searches, subscribers):
    register_bot_commands()
    # Постоянный слушатель bot.py забирает команды себе: иначе два процесса
    # (прогон и слушатель) делили бы один offset и часть команд терялась.
    if os.environ.get("SKIP_TELEGRAM_POLL") == "1":
        print("ℹ️ Опрос Telegram пропущен: команды обрабатывает bot.py")
        return searches, subscribers
    offset = load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    try:
        params = {"offset": offset}
        # В режиме слушателя включаем длинный опрос: запрос висит до 50 секунд и
        # возвращается сразу с командой, поэтому реакция мгновенная, а обращений к
        # API в десятки раз меньше.
        if os.environ.get("TELEGRAM_LONG_POLL") == "1":
            params["timeout"] = 50
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                             params=params, timeout=60)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        print("Не удалось получить команды из Telegram:", e)
        return searches, subscribers

    searches_changed = False
    subs_changed = False

    for upd in updates:
        offset = upd["update_id"] + 1

        # Нажатия инлайн-кнопок приходят в тот же getUpdates, что и сообщения: у
        # бота нет постоянно работающего процесса (запуск по расписанию GitHub
        # Actions), поэтому кнопки обрабатываются в каждом прогоне, а offset
        # сохраняется ниже — повторно то же нажатие не выполнится.
        callback = upd.get("callback_query")
        command, argument = "", ""
        if callback:
            data = (callback.get("data") or "").strip()
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            if not chat_id:
                continue
            answer_callback_query(callback.get("id"), MENU_ACK.get(data, "Выполняю…"))
            if chat_id not in subscribers:
                subscribers = subscribers + [chat_id]
                subs_changed = True
            if handle_menu_settings(chat_id, data):
                continue
            if data.startswith("unbl:"):
                command, argument = "/blacklist_del", data.split(":", 1)[1]
            elif data.startswith("mode:"):
                command = "/" + data.split(":", 1)[1]
            else:
                command = {"anomalies": "/anomalies", "sold": "/sold",
                           "blacklist": "/blacklist", "remigrate": "/remigrate",
                           "remigrate_live": "/remigrate_live", "rejected": "/rejected",
                           "stats": "/stats", "anomaly_review": "/fixanomalies",
                           "anomaly_reasons": "/anomalyreasons",
                           "help": "/help"}.get(data, "")
            if not command:
                send_main_menu(chat_id, "Обновляю меню.")
                continue
        else:
            message = upd.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "").strip()
            if not chat_id or not text:
                continue

            if chat_id not in subscribers:
                subscribers = subscribers + [chat_id]
                subs_changed = True
                send_telegram(chat_id, "✅ Вы подписаны на уведомления о выгодных объявлениях. "
                                       "Управление — кнопками: /menu",
                              reply_markup=main_menu_markup())

            action = TEXT_BUTTON_ACTIONS.get(text)
            if action and action[0] == "section":
                send_section(chat_id, action[1])
                continue
            if action and action[0] == "cmd":
                command, _, argument = action[1].partition(" ")
            else:
                command, _, argument = text.partition(" ")
                command = command.split("@", 1)[0].lower()

        if command in ("/start", "/menu"):
            send_persistent_keyboard(chat_id)
            send_section(chat_id, "main")
            continue

        if command == "/help":
            send_help(chat_id)
            continue

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
        elif command == "/minconfidence":
            raw = argument.strip().replace(",", ".").rstrip("%").strip()
            try:
                value = float(raw) if raw else None
            except ValueError:
                value = None
            if value is not None:
                if value > 1:
                    value /= 100
                if 0 <= value <= 1:
                    set_min_confidence(round(value, 2))
                    send_telegram(chat_id, f"✅ Минимальная уверенность Gemini для уведомлений: {round(value * 100)}%"
                                           + (" (фильтр выключен)." if value == 0 else "."))
                else:
                    send_telegram(chat_id, "⚠️ Значение должно быть от 0 до 100 (%), например: /minconfidence 60")
            else:
                send_telegram(chat_id, f"Текущий порог уверенности: {round(get_min_confidence() * 100)}%.\n"
                                       "Формат: /minconfidence 60  (0 — выключить)")
        elif command == "/maxprice":
            arg = argument.strip().replace(" ", "")
            if arg == "" :
                current = get_max_price()
                send_telegram(chat_id, (f"Текущий потолок цены: {current} TJS." if current else
                                        "Текущий потолок цены: не задан (обрабатываются любые цены).")
                              + "\nФормат: /maxprice 15000  (0 — выключить потолок)")
            elif arg.lstrip("-").isdigit() and int(arg) >= 0:
                value = int(arg)
                set_max_price(value)
                if value == 0:
                    send_telegram(chat_id, "✅ Потолок цены снят — обрабатываются объявления с любой ценой.")
                else:
                    send_telegram(chat_id, f"✅ Потолок цены: {value} TJS. Объявления дороже не будут анализироваться "
                                           "и не потратят бюджет Gemini/веб-поиска (но останутся в базе для /price).")
            else:
                send_telegram(chat_id, "⚠️ Нужно целое число TJS, например: /maxprice 15000  (0 — выключить)")
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
        elif command == "/manual":
            parts = [p.strip() for p in argument.split("|")]
            price_ok = len(parts) > 1 and parts[1].replace(" ", "").isdigit()
            if len(parts) < 2 or not parts[0] or not price_ok:
                send_telegram(
                    chat_id,
                    "Формат: /manual Название | Цена | Состояние | Описание\n"
                    "Состояние и описание необязательны, но чем подробнее описание "
                    "(дефекты, IMEI, память), тем точнее будет оценка.\n\n"
                    "Например:\n/manual iPhone 13 128GB | 2500 | б/у | Экран без сколов, "
                    "батарея 82%, IMEI зарегистрирован, коробки нет",
                )
            else:
                title = parts[0]
                price = int(parts[1].replace(" ", ""))
                condition = normalize_condition(parts[2]) if len(parts) > 2 and parts[2] else None
                description = parts[3] if len(parts) > 3 else ""
                send_telegram(chat_id, "🧪 Проверяю гипотетическое объявление без фото — это может занять до минуты...")
                manual_usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
                manual_usd_rate = get_usd_tjs_rate()
                result_text = run_manual_check(title, price, condition, description, manual_usd_rate, manual_usage)
                send_telegram(chat_id, result_text)
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
                stats = market_stats_for(query, None, exclude_id=None)
                listings = find_market_listings(query)
                if not listings:
                    send_telegram(chat_id, f"По «{query}» в базе пока ничего не нашлось.")
                else:
                    lines = [f"💰 {query} — найдено объявлений: {len(listings)}"]
                    if stats:
                        lines.append(f"Минимальная цена: {stats['min']} TJS | Медианная: {stats['median']} TJS")
                    lines.append("")
                    for it in listings:
                        price_str = f"{it['price']} TJS" if it.get("price") else "цена не указана"
                        line = f"• {it['title']} — {price_str}"
                        if it.get("url"):
                            line += f"\n  {it['url']}"
                        lines.append(line)
                    text = "\n".join(lines)
                    if len(text) > 3800:
                        send_telegram(chat_id, "\n".join(lines[:3]) + f"\n\nСписок длинный — отправляю файлом.")
                        send_telegram_document(chat_id, "price_search.txt", text, caption=f"Результаты по «{query}»")
                    else:
                        send_telegram(chat_id, text)
        elif command == "/sold":
            sales = get_all_confirmed_sales()
            if not sales:
                send_telegram(chat_id, "Подтверждённых продаж пока нет.")
            else:
                groups = {}
                for r in sales:
                    key = effective_model_key(r) or (r.get("title") or "").lower() or "—"
                    groups.setdefault(key, []).append(r)
                ordered = sorted(groups.values(), key=lambda recs: max(r.get("confirmed_at", "") for r in recs), reverse=True)

                lines = [f"✅ Подтверждённых продаж: {len(sales)}, моделей: {len(ordered)}"]
                for recs in ordered:
                    title = recs[0].get("title") or recs[0].get("model_key") or "—"
                    prices = [r["price"] for r in recs if r.get("price")]
                    avg = round(sum(prices) / len(prices)) if prices else None
                    lines.append(f"\n📱 {title} ({len(recs)})" + (f" — средняя цена {avg} TJS" if avg else ""))
                    for r in recs[:10]:
                        date = (r.get("confirmed_at") or "")[:10] or "дата неизвестна"
                        cond = f", {r['condition']}" if r.get("condition") else ""
                        lines.append(f"  • {r.get('price', '—')} TJS{cond} — {date}")
                    if len(recs) > 10:
                        lines.append(f"  …и ещё {len(recs) - 10}")

                text = "\n".join(lines)
                if len(text) > 3800:
                    send_telegram(chat_id, "\n".join(lines[:2]) + "\n\nСписок длинный — отправляю файлом.")
                    send_telegram_document(chat_id, "sold_phones.txt", text, caption=f"Подтверждённые продажи: {len(sales)}")
                else:
                    send_telegram(chat_id, text)
        elif command == "/anomalies":
            anomalies = find_price_anomalies()
            merged = [a for a in anomalies if a["kind"] == "merged"]
            outliers = [a for a in anomalies if a["kind"] == "outlier"]
            wide = [a for a in anomalies if a["kind"] == "wide"]
            if not anomalies:
                send_telegram(chat_id, "Подозрительных групп не найдено — model_key выглядят чистыми.")
            else:
                lines = [
                    f"🔍 Склейка ключа (разные модели в одном ключе): {len(merged)}; "
                    f"одиночные выбросы: {len(outliers)}; обычный широкий разброс: {len(wide)}",
                    "Разбирать нужно только первую группу — остальное не ошибка ключа.",
                    "Причины и правки по страницам лотов — /fixanomalies, журнал — /anomalyreasons.",
                ]
                for a in merged[:10]:
                    lines.append(
                        f"\n⚠️ СКЛЕЙКА: {a['label']} ({a['count']} лотов)\n"
                        f"  основной коридор: {a['core_min']}–{a['core_max']} TJS ({a['cluster_sizes'][0]} лотов)\n"
                        + "".join(f"  отдельный коридор: {c[0]}–{c[-1]} TJS ({len(c)} лотов)\n"
                                  for c in a["clusters"][1:])
                        + f"  крайние: {a['min_price']} TJS «{a['min_title']}» … "
                          f"{a['max_price']} TJS «{a['max_title']}»"
                    )
                for a in outliers[:5]:
                    lines.append(
                        f"\n• Выброс (ключ, скорее всего, чистый): {a['label']} "
                        f"({a['count']} лотов, ×{a['ratio']:.1f})\n"
                        f"  основной коридор: {a['core_min']}–{a['core_max']} TJS\n"
                        f"  выбивается: {a['min_price']} TJS «{a['min_title']}»"
                    )
                if wide:
                    lines.append("\n📊 Широкий, но обычный разброс внутри одной модели (не ошибка ключа):")
                    for a in wide[:10]:
                        lines.append(f"• {a['label']} — {a['core_min']}–{a['core_max']} TJS, "
                                     f"{a['count']} лотов, ×{a['ratio']:.1f}")
                text = "\n".join(lines)
                if len(text) > 3800:
                    send_telegram(chat_id, "\n".join(lines[:3]) + "\n\nСписок длинный — отправляю файлом.")
                    send_telegram_document(chat_id, "anomalies.txt", text,
                                           caption=f"Подозрительные model_key: {len(anomalies)}")
                else:
                    send_telegram(chat_id, text)
        elif command == "/fixanomalies":
            if chat_id != TELEGRAM_CHAT_ID:
                send_telegram(chat_id, "⛔ Разбор аномалий доступен только владельцу бота.")
            else:
                send_telegram(chat_id, "🩺 Разбираю аномалии: скачиваю страницы лотов и отдаю текст Gemini…")
                try:
                    import anomaly_review
                    report = anomaly_review.run_once(
                        limit=anomaly_review.ANOMALY_REVIEW_PER_RUN,
                        notify=lambda text: send_telegram(chat_id, text))
                    if not report["reviewed"]:
                        send_telegram(chat_id, f"Разбирать нечего: аномальных групп нужных типов нет "
                                               f"(всего найдено {report['found']}).")
                    else:
                        lines = [f"🩺 Разобрано групп: {len(report['reviewed'])} "
                                 f"(найдено аномалий {report['found']}, правок в базе {report['corrections_total']})."]
                        for r in report["reviewed"]:
                            lines.append(f"\n• {r['label']} — {r['verdict']}"
                                         f" (правок {len(r['applied'])}, исключено {len(r['excluded'])})")
                            if r.get("summary"):
                                lines.append(f"  {r['summary']}")
                            if r.get("code_hint"):
                                lines.append(f"  💡 {r['code_hint']}")
                        if report["errors"]:
                            lines.append("\n⚠️ " + "; ".join(report["errors"][:3]))
                        lines.append(f"\nЖурнал причин: {ANOMALY_REASONS_FILE}, правки: {ANOMALY_CORRECTIONS_FILE}")
                        send_telegram(chat_id, "\n".join(lines))
                except Exception as e:
                    send_telegram(chat_id, f"⚠️ Разбор аномалий не удался: {type(e).__name__}: {str(e)[:200]}")
        elif command == "/anomalyreasons":
            rows = recent_anomaly_reasons(10)
            if not rows:
                send_telegram(chat_id, f"Журнал причин аномалий пуст ({ANOMALY_REASONS_FILE}). "
                                       f"Запусти разбор: /fixanomalies")
            else:
                lines = [f"📝 Последние разборы аномалий ({len(rows)} из журнала):"]
                for r in reversed(rows):
                    lines.append(f"\n• {r.get('label') or r.get('group_key')} — {r.get('verdict')}"
                                 f" ({r.get('reviewed_at')})")
                    if r.get("summary"):
                        lines.append(f"  {r['summary']}")
                    if r.get("code_hint"):
                        lines.append(f"  💡 {r['code_hint']}")
                    if r.get("applied") or r.get("excluded"):
                        lines.append(f"  правок: {len(r.get('applied') or [])}, "
                                     f"исключено: {len(r.get('excluded') or [])}")
                text = "\n".join(lines)
                if len(text) > 3800:
                    send_telegram(chat_id, "\n".join(lines[:6]) + "\n\nЖурнал длинный — отправляю файлом.")
                    send_telegram_document(chat_id, "anomaly_reasons.txt", text, caption="Причины аномалий")
                else:
                    send_telegram(chat_id, text)
        elif command == "/remigrate":
            if chat_id != TELEGRAM_CHAT_ID:
                send_telegram(chat_id, "⛔ Команда доступна только владельцу бота.")
            else:
                changed, total = remigrate_model_keys()
                send_telegram(
                    chat_id,
                    f"✅ Пересчитаны model_key по текущей формуле: обновлено {changed} из {total} записей.\n"
                    f"Список известных аномалий сброшен — проверь /anomalies заново.",
                )
        elif command == "/remigrate_live":
            if chat_id != TELEGRAM_CHAT_ID:
                send_telegram(chat_id, "⛔ Команда доступна только владельцу бота.")
            else:
                state = load_json(REMIGRATE_LIVE_STATE_FILE, None)
                if state and not state.get("done"):
                    urls, cursor = state.get("urls", []), state.get("cursor", 0)
                    send_telegram(
                        chat_id,
                        f"⏳ Досбор моделей по IMEI уже идёт: проверено {cursor} из {len(urls)} объявлений "
                        f"(по {REMIGRATE_LIVE_BATCH_SIZE} за прогон бота). Подожди — пришлю сообщение, когда закончится.",
                    )
                else:
                    new_state, started = start_remigrate_live()
                    total_urls = len(new_state.get("urls", []))
                    if total_urls == 0:
                        send_telegram(chat_id, "У всех записей в базе уже есть verified_model — обновлять нечего. "
                                                "Если нужно всё равно пересчитать model_key, используй /remigrate.")
                    else:
                        send_telegram(
                            chat_id,
                            f"🔄 Запущен досбор модели по IMEI напрямую со страниц объявлений: {total_urls} "
                            f"уникальных URL без сохранённой verified_model. Буду проверять по "
                            f"{REMIGRATE_LIVE_BATCH_SIZE} за каждый прогон бота — это может занять несколько "
                            f"прогонов. Когда закончу, пересчитаю model_key всей базы и пришлю итог сюда."
                        )
        elif command == "/blacklist":
            blacklist = load_seller_blacklist()
            if not blacklist:
                send_telegram(chat_id, "🚫 Чёрный список продавцов пуст.")
            else:
                lines = [f"🚫 В чёрном списке продавцов: {len(blacklist)}"]
                for key, bl_entry in sorted(blacklist.items(), key=lambda kv: kv[1].get("blocked_at", ""), reverse=True):
                    lines.append(
                        f"\n👤 {bl_entry.get('name') or key}"
                        + (f"\n  профиль: {bl_entry['profile_url']}" if bl_entry.get("profile_url") else "")
                        + f"\n  причина: {bl_entry.get('reason', '—')}"
                        + f"\n  объявление-повод: {bl_entry.get('blocked_by_ad_url', '—')}"
                        + f"\n  заблокирован: {(bl_entry.get('blocked_at') or '—')[:10]}"
                    )
                text = "\n".join(lines)
                if len(text) > 3800:
                    send_telegram(chat_id, lines[0] + "\n\nСписок длинный — отправляю файлом.")
                    send_telegram_document(chat_id, "seller_blacklist.txt", text,
                                            caption=f"Чёрный список продавцов: {len(blacklist)}")
                else:
                    send_telegram(chat_id, text)
        elif command == "/blacklist_add":
            raw = argument.strip()
            if not raw:
                send_telegram(chat_id, "Формат: /blacklist_add ссылка_на_объявление | причина (необязательно)")
            else:
                url_part, _, reason_part = raw.partition("|")
                ad_url = url_part.strip()
                reason = reason_part.strip() or "добавлено вручную владельцем"
                try:
                    detail = fetch_detail(ad_url)
                    seller_name_m, seller_url_m, seller_active_ads_m = detail[8], detail[9], detail[10]
                    seller_key_m = seller_key_from_url(seller_url_m)
                    if not seller_key_m:
                        send_telegram(chat_id, "Не нашёл продавца на этой странице объявления — проверьте ссылку.")
                    else:
                        entry_m, is_new_m = blacklist_seller(
                            seller_key_m, seller_name_m, seller_url_m, reason, ad_url,
                            extra={"active_ads_at_block": seller_active_ads_m})
                        if is_new_m:
                            send_telegram(chat_id, f"✅ Продавец «{seller_name_m or seller_key_m}» добавлен в чёрный список.")
                        else:
                            send_telegram(chat_id, f"ℹ️ Продавец «{entry_m.get('name') or seller_key_m}» уже был "
                                                   f"в чёрном списке — обновил причину и добавил объявление "
                                                   f"(всего поводов: {entry_m.get('ban_count')}).")
                except Exception as e:
                    send_telegram(chat_id, f"⚠️ Не удалось обработать ссылку: {e}")
        elif command == "/blacklist_del":
            arg = argument.strip()
            if not arg:
                send_telegram(chat_id, "Формат: /blacklist_del слаг_продавца ИЛИ ссылка на его профиль")
            else:
                key = seller_key_from_url(arg) if arg.startswith("http") else arg.lower()
                removed = unblacklist_seller(key)
                if removed:
                    send_telegram(chat_id, f"✅ Продавец «{removed.get('name') or key}» удалён из чёрного списка.")
                else:
                    send_telegram(chat_id, "Такого продавца нет в чёрном списке.")
        elif command == "/stats":
            gem_usage = get_daily_usage(GEMINI_DAILY_FILE, [c["id"] for c in GEMINI_COMBOS])
            search_usage = get_search_usage()
            db_count = sum(1 for _ in open(PRICE_HISTORY_FILE, encoding="utf-8")) if os.path.exists(PRICE_HISTORY_FILE) else 0
            rej_count = sum(1 for _ in open(REJECTED_FILE, encoding="utf-8")) if os.path.exists(REJECTED_FILE) else 0
            max_price_val = get_max_price()
            lines = ["📊 Статистика", "", f"💵 Минимальная выгода: {get_min_profit()} TJS",
                     f"🎯 Минимальная уверенность: {round(get_min_confidence() * 100)}%",
                     f"🚧 Потолок цены: {max_price_val if max_price_val else 'не задан'} TJS", "", "Gemini сегодня:"]
            lines += [f"  {c['id']}: {gem_usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS]
            lines += ["", "Поиск в сети:"]
            lines += [f"  {p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']} ({'мес' if p['period']=='month' else 'день'})" for p in SEARCH_PROVIDERS]
            bl_count = len(load_seller_blacklist())
            lines += ["", f"📁 Записей в базе: {db_count}", f"🚫 Отклонённых лотов: {rej_count}",
                      f"🚫 Продавцов в чёрном списке: {bl_count}", f"👥 Подписчиков: {len(subscribers)}"]
            send_telegram(chat_id, "\n".join(lines))
        elif command == "/help":
            send_telegram(chat_id, "Команды:\n/all /used /params — режим поиска\n"
                                    "/add Название | слова | макс_цена | мин_память\n"
                                    "/del Название\n/list — список поисков\n"
                                    "/minprofit число — минимальная выгода для уведомлений (TJS)\n"
                                    "/minconfidence число — минимальная уверенность Gemini для уведомлений (%, 0 — выкл.)\n"
                                    "/maxprice число — потолок цены объявления, которое вообще обрабатывается (TJS, 0 — выкл.)\n"
                                    "/price Модель — грубая сводка цен по базе\n"
                                    "/sold — фактически проданные телефоны (цена, дата), по моделям\n"
                                    "/menu — меню управления кнопками (режим, пороги, продавцы, аномалии)\n"
                                    "/anomalies — проверить базу на подозрительные разбросы цен (склеенные модели)\n"
                                    "/remigrate — пересчитать model_key всей базы по текущей формуле (только владелец)\n"
                                    "/remigrate_live — дособрать модель по IMEI со страниц старых объявлений и "
                                    "пересчитать model_key всей базы на её основе (только владелец)\n"
                                    "/dbadd текст — добавить что угодно в базу вручную\n"
                                    "/manual Название | Цена | Состояние | Описание — гипотетическая "
                                    "проверка объявления без фото (не сохраняется в базу)\n"
                                    "/bought ссылка — подтвердить удачную покупку по рекомендации бота\n"
                                    "/rejected — файл со всеми отклонёнными лотами\n/fixanomalies — разбор аномалий: Gemini читает страницы лотов, правит поля и пишет причину\n/anomalyreasons — журнал причин аномалий и подсказок для кода\n"
                                    "/blacklist — список продавцов в чёрном списке (причина + ссылка на "
                                    "объявление-повод)\n"
                                    "/blacklist_add ссылка_на_объявление | причина — заблокировать продавца вручную\n"
                                    "/blacklist_del слаг_или_ссылка_на_профиль — убрать продавца из чёрного списка\n"
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


NEW_TEXT_CUES = ("запечатан", "нераспечатан", "не вскрыт", "не пользовал", "с биркой",
                  "новый, коробка", "не был в использовании", "новый в коробке")
USED_TEXT_CUES = ("царапин", "потёрт", "потерт", "скол", "трещин", "след использования",
                   "мелкие дефект", "потёрто", "потерто")


def condition_signal(item):
    desc = (item.get("description") or "").lower()
    has_new_cue = any(c in desc for c in NEW_TEXT_CUES)
    has_used_cue = any(c in desc for c in USED_TEXT_CUES)
    return "new" if (has_new_cue and not has_used_cue) else "used"


def clean_title(raw_text):
    is_vip = bool(re.search(r"\bVIP\b", raw_text, re.I))
    is_top = bool(TOP_BADGE_RE.search(raw_text))
    # Бейдж "IMEI проверен" виден прямо на карточке в списке объявлений (поверх
    # превью фото), до захода в само объявление — определяем его тут, ДО того
    # как NOISE_RE вырежет этот же текст как шум из заголовка.
    imei_verified_badge = bool(IMEI_VERIFIED_BADGE_RE.search(raw_text))
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
    return title, price, condition, memory, is_vip, is_top, imei_verified_badge


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


# --- Память по IMEI ---------------------------------------------------------
# Somon.tj показывает IMEI замаскированным ("8682080XXXXXX4"), но первые 8 цифр
# (TAC) видны. Запоминаем связку «TAC -> официальная модель из блока проверки
# IMEI»: если у следующего лота плашки с моделью не окажется, а IMEI есть —
# модель возьмём из этой памяти, а не из заголовка с опечатками.
IMEI_TAC_FILE = "imei_tac.json"
# Сколько страниц выдачи подтягивать за прогон (1 — как было).
SOMON_MAX_PAGES = int(os.environ.get("SOMON_MAX_PAGES", "1"))


def load_tac_map():
    return load_json(IMEI_TAC_FILE, {})


def save_tac_map(data):
    """Атомарная запись: обрыв прогона не оставит недописанный JSON."""
    tmp_path = f"{IMEI_TAC_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, IMEI_TAC_FILE)


def tac_from_imei(imei_number):
    """Первые 8 цифр IMEI (TAC) — по ним идентифицируется модель устройства."""
    if not imei_number or len(imei_number) < 8:
        return None
    tac = imei_number[:8]
    return tac if tac.isdigit() else None


def fetch_listings():
    """Выдача со страницы поиска.

    * SOMON_MAX_PAGES>1 — подтягивает следующие страницы (по умолчанию 1, поведение
      не меняется; включается переменной окружения, если понадобится больше лотов).
    * Город и время публикации берутся прямо с карточки (split_card_meta) — они есть
      у каждого объявления, ещё до запроса его страницы.
    * Белый список городов парсится из заголовка страницы поиска, чтобы не угадывать
      названия вручную.
    """
    global CITY_WHITELIST
    listings, seen_links, first_soup = [], set(), None
    for page in range(1, SOMON_MAX_PAGES + 1):
        url = SEARCH_URL if page == 1 else f"{SEARCH_URL}&page={page}"
        if SOMON_MAX_PAGES > 1:
            print(f"Загружаю страницу выдачи {page}: {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"Страница выдачи {page} не загрузилась: {e}")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        if first_soup is None:
            first_soup = soup
            CITY_WHITELIST = parse_city_whitelist(soup.get_text(" ", strip=True))
            print(f"Города в поиске ({len(CITY_WHITELIST)}): {', '.join(CITY_WHITELIST[:12])}")
        added, vip_count, top_count = 0, 0, 0
        for a in soup.find_all("a", href=re.compile(r"/adv/|/item/|\d{5,}")):
            href = absolute_url(a.get("href", ""))
            if not href or href in seen_links:
                continue
            raw_text = a.get_text(" ", strip=True)
            if len(raw_text) < 5:
                continue
            card_title, card_published, card_city = split_card_meta(raw_text, CITY_WHITELIST)
            title, price, condition, memory, is_vip, is_top, imei_verified_badge = clean_title(card_title)
            if not title:
                continue
            seen_links.add(href)
            added += 1
            vip_count += 1 if is_vip else 0
            top_count += 1 if is_top else 0
            listings.append({
                "id": re.sub(r"\D", "", href)[-8:] or href,
                "title": title, "price": price, "condition": condition,
                "memory": memory, "url": href, "vip": is_vip, "top": is_top,
                "imei_verified_badge": imei_verified_badge,
                "city_list": card_city, "published_list": card_published,
            })
        print(f"Страница {page}: карточек {added} (платных: VIP {vip_count}, ТОП {top_count})")
        if added == 0:
            break
    if first_soup is None:
        first_soup = BeautifulSoup("<html></html>", "html.parser")
    return listings, first_soup


_UNICODE_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def decode_escapes(text):
    """Разэкранировать текст страницы: \u0414\u0443... -> Душанбе.

    Сохранённая страница объявления отдаёт подписи и значения в escape-виде
    (в HTML буквально лежит \u041c\u043e\u0434\u0435\u043b\u044c вместо «Модель»).
    Без этого разбора метка «Модель» не совпадала ни с одной строкой, и блок IMEI
    выглядел пустым. Если экранирования нет, функция ничего не меняет.
    """
    if not isinstance(text, str) or ("\\u" not in text and "\\n" not in text):
        return text
    out = _UNICODE_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    for a, b in (("\\n", " "), ("\\r", " "), ("\\t", " "), ("\\/", "/"), ('\\"', '"'), ("\\'", "'")):
        out = out.replace(a, b)
    return out.strip()


def _txt(node):
    """Весь текст узла одной строкой, с разэкранированием."""
    return decode_escapes(node.get_text(" ", strip=True))


def _by_class(container, prefix, limit=1):
    """Элементы, у которых есть класс, начинающийся на prefix.

    На Somon.tj имена классов содержат хеш сборки (IMEI_item__NQM7S, IMEI_name__kI06h,
    Location_name___MpLW, Features_item__22IsR). Хеш меняется при каждой пересборке
    сайта, а префикс — нет. Селекторы сняты с реальной сохранённой страницы
    adv/14742183_xiaomi-redmi-15-8-8-256gb-global-versi.
    """
    out = []
    for node in container.find_all(True):
        for cls in (node.get("class") or []):
            if cls.startswith(prefix):
                out.append(node)
                break
        if len(out) >= limit:
            break
    return out


def normalize_imei_value(text):
    """IMEI из блока проверки -> (номер, замаскирован ли).

    Сайт отдаёт номер кусками: 8694390<span class="cross">XXXXXX</span>3, после
    get_text получается "8694390XXXXXX3". Полный 15-значный номер бывает у лотов
    из чёрного/серого списка, у остальных середина закрыта.
    """
    if not text:
        return None, False
    raw = re.sub(r"[^0-9Xx]", "", str(text)).upper()
    if len(raw) < 8:
        return None, False
    if "X" in raw:
        return raw, True
    return (raw, False) if (raw.isdigit() and len(raw) == 15) else (None, False)


def parse_imei_block(soup):
    """Блок проверки IMEI: component="IMEIMobile".

      <div component="IMEIMobile" class="IMEI_container___D7Yq">
        <div class="IMEI_header__yk9C6"><div>IMEI телефона в белом списке</div></div>
        <div class="IMEI_list__2AEqJ">
          <div class="IMEI_item__NQM7S"><div class="IMEI_name__kI06h">IMEI</div>
               <div>8694390<span class="cross">XXXXXX</span>3</div></div>
          <div class="IMEI_item__NQM7S"><div class="IMEI_name__kI06h">Модель</div>
               <div>Xiaomi Redmi 12 5G</div></div>
    """
    container = soup.find(attrs={"component": "IMEIMobile"})
    if not container:
        found = _by_class(soup, "IMEI_container")
        container = found[0] if found else None
    if not container:
        return {"header": None, "items": {}}
    headers = _by_class(container, "IMEI_header")
    header = _txt(headers[0]) if headers else None
    items = {}
    for item in _by_class(container, "IMEI_item", limit=10):
        children = item.find_all("div", recursive=False)
        if len(children) < 2:
            children = item.find_all("div")
        if len(children) < 2:
            continue
        label = _txt(children[0]).strip(" :")
        value = _txt(children[1])
        if label and value:
            items[label.lower()] = value
    return {"header": header, "items": items}


def parse_city_block(soup):
    """Город: component="LocationMobile" -> «Город: <span class="Location_name">Душанбе</span>».

    В этом и была причина прочерка в Telegram: labeled_value() искал отдельный
    текстовый узел, равный «Город», а подпись и значение лежат в одном узле
    «Город: Душанбе», поэтому условие не выполнялось никогда.
    """
    node = soup.find(attrs={"component": "LocationMobile"})
    if not node:
        found = _by_class(soup, "Location_container")
        node = found[0] if found else None
    if not node:
        return None
    name = _by_class(node, "Location_name")
    if name:
        return _txt(name[0]) or None
    text = _txt(node)
    for pref in ("Город:", "Город :", "Город"):
        if text.startswith(pref):
            text = text[len(pref):]
            break
    return text.strip() or None


def parse_features(soup):
    """Характеристики: component="FeaturesMobile" -> Состояние / Встроенная память."""
    out = {}
    node = soup.find(attrs={"component": "FeaturesMobile"})
    if not node:
        found = _by_class(soup, "Features_item")
        node = found[0].parent if found else None
    if not node:
        return out
    for item in _by_class(node, "Features_item", limit=15):
        value = item.find(class_=re.compile(r"^Features_value"))
        divs = item.find_all("div", recursive=False)
        name = divs[0] if divs else None
        if value is None and len(divs) > 1:
            value = divs[1]
        if name is not None and value is not None:
            key = _txt(name).lower()
            if key:
                out[key] = _txt(value)
    return out


def parse_info_icons(soup):
    """Просмотры и время публикации (блоки Info_icon).

    Формат времени отдаём сайта как есть («19 минут назад», «Сегодня 12:30»):
    пересчитывать в минуты не нужно, возраст лота считает card_age_minutes().
    """
    views, published = None, None
    for node in _by_class(soup, "Info_icon", limit=20):
        txt = _txt(node)
        if not txt:
            continue
        if re.search(r"назад|Сегодня|Вчера", txt, re.I):
            if published is None:
                published = txt[:40]
        elif txt.isdigit() and views is None:
            views = int(txt)
    return views, published


def parse_price(soup):
    """Цена со страницы объявления: Price_current («2 250 c.» -> 2250)."""
    nodes = _by_class(soup, "Price_current")
    if not nodes:
        return None
    text = _txt(nodes[0])
    m = PRICE_RE.search(text)
    return int(m.group(1).replace(" ", "")) if m else None


def parse_ad_page(soup):
    """Разбор страницы объявления по структуре DOM."""
    block = parse_imei_block(soup)
    items = block["items"]
    features = parse_features(soup)
    views, published = parse_info_icons(soup)
    imei_number, imei_masked = normalize_imei_value(items.get("imei"))
    return {
        "imei_header": block["header"],
        "imei_number": imei_number,
        "imei_masked": imei_masked,
        "model": items.get("модель"),
        "city": parse_city_block(soup),
        "condition_raw": features.get("состояние"),
        "memory_raw": features.get("встроенная память") or features.get("память"),
        "color": features.get("цвет"),
        "published": published,
        "views": views,
        "price": parse_price(soup),
    }


def load_worker_inbox():
    """Находки воркеров из worker_inbox.jsonl (их собрал merge_workers.py).

    Воркеры опрашивают выдачу раз в 15 секунд, поэтому у них первыми оказываются
    самые свежие объявления — их и разбираем в первую очередь. Записи без id или
    url отбрасываем: без них лот нечем ни запомнить, ни открыть.
    """
    if not os.path.exists(WORKER_INBOX_FILE):
        return []
    out = []
    try:
        with open(WORKER_INBOX_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") and rec.get("url"):
                    out.append(rec)
    except Exception as e:
        print(f"⚠️ {WORKER_INBOX_FILE} не прочитался: {e}")
    # Свежие находки первыми.
    out.sort(key=lambda r: r.get("found_at") or "", reverse=True)
    return out


def prune_worker_inbox(processed_ids):
    """Убрать из входящих то, что этот прогон уже разобрал.

    Файл перезаписывается атомарно: обрыв на середине не оставит пустой список,
    а неразобранные находки останутся для следующего запуска.
    """
    if not os.path.exists(WORKER_INBOX_FILE):
        return 0
    rest = [r for r in load_worker_inbox() if r.get("id") not in processed_ids]
    tmp = WORKER_INBOX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rest:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, WORKER_INBOX_FILE)
    return len(rest)


def fetch_detail(ad_url):
    resp = requests.get(ad_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text(" ", strip=True)
    # Сначала структура DOM, и лишь если узла нет — разбор текста.
    dom = parse_ad_page(soup)

    condition = normalize_condition(dom["condition_raw"] or labeled_value(soup, ["Состояние"]))
    memory_raw = dom["memory_raw"] or decode_escapes(labeled_value(soup, ["Встроенная память", "Память"]))
    memory_match = re.search(r"(\d+)\s*(?:gb|гб)", memory_raw or "", re.I)
    memory = int(memory_match.group(1)) if memory_match else None
    imei_status = detect_imei_status(dom["imei_header"] or full_text)
    # Город: подпись «Город: X», «№17361978 in Худжанд» (страница объявления),
    # «6 дней назад Худжанд» (блок похожих).
    city = dom["city"] or decode_escapes(labeled_value(soup, ["Город"]))
    if not city:
        in_match = re.search(r"№\s*\d+\s+in\s+([^\-–|\n]{2,40})", full_text)
        if in_match:
            found = find_city(in_match.group(1), CITY_WHITELIST)
            city = found or in_match.group(1).strip() or None
    if not city:
        city = find_city(full_text[:2000], CITY_WHITELIST)
    if not city:
        city_match = CITY_RE.search(full_text)
        city = city_match.group(1) if city_match else None
    published_match = PUBLISHED_RE.search(full_text)
    published_at = dom["published"] or (published_match.group(1) if published_match else None)

    # "Модель" в блоке проверки IMEI — официальное название устройства из
    # государственной базы IMEI, а не то, что напечатал продавец в заголовке.
    # Появляется обычно только когда IMEI найден в базе (белый/серый список).
    # is_meaningful_model_text отсекает случаи, когда labeled_value() случайно
    # зацепил слово "Модель" не в этом блоке и подставил мусор вроде ":".
    raw_verified_model = labeled_value(soup, ["Модель"])
    verified_model = raw_verified_model if is_meaningful_model_text(raw_verified_model) else None
    # Название модели из базы IMEI важнее заголовка продавца: на сохранённой странице
    # заголовок «Xiaomi Redmi 15 8+8/256GB Global Version 2026», а база IMEI отдаёт
    # «Xiaomi Redmi 12 5G» — это разные телефоны.
    if dom["model"] and is_meaningful_model_text(dom["model"]):
        verified_model = dom["model"]
    imei_number, imei_masked = parse_imei_number(full_text)
    if dom["imei_number"]:
        imei_number, imei_masked = dom["imei_number"], dom["imei_masked"]

    desc_match = DESC_RE.search(full_text)
    # Полное описание продавца (раньше обрезалось до 500 символов при разборе и до
    # 800 при отправке в Gemini — терялись важные детали).
    description = desc_match.group(1).strip() if desc_match else full_text[:DESCRIPTION_MAX_CHARS]

    photo_urls, seen = [], set()
    for img in soup.find_all("img"):
        src = absolute_url(img.get("src") or img.get("data-src") or "")
        if "somon" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]) and src not in seen:
            seen.add(src)
            photo_urls.append(src)

    seller_name, seller_url, seller_active_ads, seller_verified = extract_seller_info(soup, full_text, resp.text)

    return (condition, memory, description, photo_urls[:4], imei_status, city, published_at, verified_model,
            seller_name, seller_url, seller_active_ads, seller_verified,
            {"number": imei_number, "masked": imei_masked, "luhn": luhn_ok(imei_number),
             "views": dom["views"], "page_price": dom["price"], "color": dom["color"]})


def extract_seller_info(soup, full_text, raw_html=""):
    """Данные о продавце со страницы объявления: имя, ссылка на его профиль/все
    объявления и число активных объявлений — ключевые сигналы для отсева
    перекупщиков (магазин с кучей объявлений vs частник с одним телефоном).

    Раньше всё держалось на одном атрибуте data-marker. Если сервер отдавал
    страницу без этого блока, продавец не опознавался вообще: name оставался None
    (поэтому в уведомлениях вместо имени печатался внутренний номер), а без
    seller_url не работала и проверка чёрного списка — продавец анализировался
    заново и снова попадал в бан. Фолбэки: блок Contacts_title (имя аккаунта) и
    ссылка на профиль прямо из HTML.
    """
    seller_name, seller_url = None, None
    author_link = soup.find("a", attrs={"data-marker": re.compile(r"^advert-all_adverts_author-advert:")})
    if author_link:
        seller_name = (author_link.get("aria-label") or author_link.get_text(strip=True) or "").strip() or None
        seller_url = absolute_url(author_link.get("href", "")) or None

    if not seller_name:
        title_el = soup.find(class_=re.compile(r"^Contacts_title"))
        if title_el:
            seller_name = title_el.get_text(" ", strip=True) or None

    if not seller_url and raw_html:
        profile_match = re.search(
            r"(/items/author/\d+/?|/c/[A-Za-z0-9_.\-]+/?|/author/[A-Za-z0-9_.\-]+/?)", raw_html)
        if profile_match:
            seller_url = absolute_url(profile_match.group(1)) or None

    active_ads_match = re.search(r"(\d+)\s*актив\w*\s*объявлен\w*", full_text, re.I)
    seller_active_ads = int(active_ads_match.group(1)) if active_ads_match else None

    seller_verified = bool(soup.find(class_=re.compile(r"^Contacts_verified")))

    return seller_name, seller_url, seller_active_ads, seller_verified


# ---------- Gemini: анализ ----------

def memory_text(rec, item_memory):
    mem = record_memory(rec)
    if not mem:
        return ""
    text = f", память {mem}GB"
    if item_memory and mem != item_memory:
        text += f" ⚠️ ДРУГОЙ ОБЪЁМ (у оцениваемого лота {item_memory}GB) — цену напрямую не сравнивай"
    return text


# Поля прошлого разбора, которые НЕЛЬЗЯ отдавать в следующий анализ.
# Это догадки Gemini о цене. Если кормить ими новую оценку, она опирается на
# собственный прошлый вывод как на факт: оценка подтверждает себя, а реальные
# цены рынка перестают её поправлять. В базе они остаются (нужны владельцу и
# /sold), но в промпт не попадают никогда.
PROMPT_FORBIDDEN_FIELDS = ("estimated_total_cost", "estimated_resale_price",
                           "estimated_repair_cost", "market_verdict", "confidence")


def sanitize_prior_analysis(rec):
    """Запись прошлого разбора без оценочных выводов Gemini.

    Оставляем только наблюдаемое: цену объявления, состояние, память, город, дату,
    статус IMEI, описание продавца. Дефекты по фото остаются как наблюдение.
    """
    return {k: v for k, v in (rec or {}).items() if k not in PROMPT_FORBIDDEN_FIELDS}


def format_market_points(points, item_memory=None):
    """Компактная таблица всех рыночных точек модели из базы (полный список)."""
    if not points:
        return ""
    lines = ["Цены реальных объявлений из нашей базы (наших оценок стоимости тут нет, "
             f"считай сам): {len(points)} лотов, "
             "от новых к старым) — цена | состояние | память | город | дата | название:"
             ]
    for rec in points:
        parts = [f"{rec.get('price', '—')} TJS"]
        if rec.get("condition"):
            parts.append(str(rec["condition"]))
        mem = record_memory(rec)
        if mem:
            parts.append(f"{mem}GB" + (" ⚠️ другой объём" if item_memory and mem != item_memory else ""))
        if rec.get("city"):
            parts.append(str(rec["city"]))
        if rec.get("date"):
            parts.append(str(rec["date"]))
        parts.append(f"«{rec.get('title', '')}»")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def format_similar_examples(examples, item_memory=None):
    if not examples:
        return "Похожих проверенных лотов этой модели из нашей базы пока нет."
    lines = ["Похожие проверенные лоты этой модели из нашей базы (от новых к старым, "
             "у каждого указан возраст данных) — судьи сам по каждому, чем этот лот "
             "отличается по дефектам и цене. НАШИХ прошлых оценок стоимости здесь "
             "намеренно НЕТ — считай цену сам по ценам реальных объявлений и фото:"]
    for i, rec in enumerate(examples, 1):
        defects = ", ".join(rec.get("visible_defects") or []) or "не обнаружены"
        positives = ", ".join(rec.get("positive_features") or []) or "—"
        # Оценка ремонта от Gemini не передаётся (PROMPT_FORBIDDEN_FIELDS).
        repair_text = ""
        age = days_since(rec.get("collected_at"))
        if age is None:
            age_text = ", дата сбора неизвестна"
        elif age > STALE_EXAMPLE_DAYS:
            age_text = f", собрано {age} дн. назад ⚠️ СТАРЫЕ ДАННЫЕ — цена могла устареть"
        else:
            age_text = f", собрано {age} дн. назад"
        title_text = f"«{rec['title']}»" if rec.get("title") else "без названия"
        extra = []
        if rec.get("condition"):
            extra.append(f"состояние от продавца: {rec['condition']}")
        if rec.get("city"):
            extra.append(f"город: {rec['city']}")
        if rec.get("published_at"):
            extra.append(f"публикация: {rec['published_at']}")
        if rec.get("imei_status"):
            extra.append(f"IMEI: {rec['imei_status']}")
        if rec.get("description"):
            extra.append("описание продавца: «" + re.sub(r"\s+", " ", str(rec["description"]))[:400] + "»")
        extra_text = ("; " + "; ".join(extra)) if extra else ""
        link_text = f" | объявление: {rec['url']}" if rec.get("url") else ""
        lines.append(
            f"{i}. {title_text}{memory_text(rec, item_memory)}. Цена {rec.get('price', '—')} TJS, состояние по фото: "
            f"{rec.get('overall_visual_condition', 'неизвестно')}, "
            f"дефекты: {defects}, плюсы: {positives}{repair_text}{age_text}{extra_text}{link_text}"
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


def format_confirmed_sales(records, item_memory=None):
    if not records:
        return ""
    lines = ["✅ ПОДТВЕРЖДЁННЫЕ РЕАЛЬНЫЕ ПРОДАЖИ этой модели (объявление реально было продано за эту цену, это не догадка):"]
    for r in records:
        age = days_since(r.get("confirmed_at"))
        age_text = (f", {age} дн. назад ⚠️ старая продажа, цена могла устареть" if age and age > STALE_EXAMPLE_DAYS
                    else (f", {age} дн. назад" if age is not None else ""))
        title_text = f"«{r['title']}»" if r.get("title") else "без названия"
        lines.append(f"- {title_text}{memory_text(r, item_memory)}: продано за {r.get('price')} TJS, "
                     f"состояние: {r.get('condition') or 'не указано'}{age_text}")
    return "\n".join(lines)


def format_manual_notes(notes):
    if not notes:
        return ""
    lines = ["Заметки, добавленные вручную владельцем бота (доверяй им как надёжному источнику):"]
    for n in notes:
        lines.append(f"- {n['text']}")
    return "\n".join(lines)


def format_imei_block(item, usd_rate):
    status = item.get("imei_status")
    if status == "white" or status == "registered":
        return ("\nIMEI в БЕЛОМ списке (растаможка оплачена, устройство легально) — дополнительных таможенных "
                "расходов и рисков не будет. НЕ спрашивай продавца про растаможку/IMEI, это уже известно.")
    if status in CUSTOMS_DUE_STATUSES:
        if status == "gray":
            status_text = (
                "IMEI в СЕРОМ СПИСКЕ: устройство ввезено/активировано, но растаможка ещё НЕ оплачена. 30 дней с "
                "первой активации SIM связь и интернет работают как обычно; за это время владелец должен "
                "оформить и оплатить растаможку через Единый портал IMEI, иначе IMEI уйдёт в ЧЁРНЫЙ список "
                "(телефон перестанет работать во всех сетях Таджикистана). Сколько дней осталось — неизвестно."
            )
            ask_text = (
                "Про сам факт серого списка не спрашивай, но можно спросить, когда была первая активация SIM "
                "(сколько дней осталось до блокировки). Если срок почти вышел — это серьёзный риск, отрази его в "
                "confidence."
            )
        else:
            status_text = "IMEI НЕ зарегистрирован в Таджикистане (обычная, не критичная растаможка ещё не оплачена)."
            ask_text = "НЕ спрашивай продавца про растаможку/IMEI, это уже известно и посчитано."
        return (
            f"\nВАЖНО: {status_text} Точный расчёт по формуле (пошлина 20% + НДС 14% от таможенной стоимости + "
            f"сбор + услуги оформления, курс {usd_rate} TJS за $1) уже посчитан программой: примерно "
            f"{item.get('estimated_customs_cost')} TJS. Обязательно прибавь эту сумму к итоговой стоимости. {ask_text}"
        )
    if status == "black":
        return (
            "\nВАЖНО И СЕРЬЁЗНО: IMEI этого телефона в ЧЁРНОМ СПИСКЕ — устройству полностью запрещён доступ к "
            "мобильным сетям ВСЕХ операторов Таджикистана (звонки, SMS, интернет не работают). В чёрный список "
            "попадают по двум причинам: (1) серый список продержался больше 30 дней, а растаможка не оплачена — "
            "это потенциально исправимо оплатой растаможки; (2) телефон "
            "заявлен как утерянный/украденный — тогда исправить, скорее всего, нельзя, а покупка сопряжена с "
            "юридическим риском. Причина в объявлении неизвестна. Не придумывай точных штрафов и сроков "
            "разблокировки. Цену перепродажи считай так, будто телефон без связи, если легализация не очевидна; "
            "вердикт \"недооценено\" ставь только при явной возможности исправить статус, и снижай confidence. "
            "Про сам факт чёрного списка продавца не спрашивай — спроси, почему он там оказался (просрочена "
            "растаможка или заявлен как утерянный/украденный) и можно ли его разблокировать."
        )
    return "\nСтатус IMEI на странице определить не удалось — если это важно, можешь спросить у продавца напрямую про растаможку."


def format_seller_block(item, has_photos):
    """Контекст о продавце для Gemini: сколько у него активных объявлений, платит ли
    он за продвижение (VIP/ТОП), и просьба отдельно оценить фон на фото — это вместе
    помогает отличить частника от перекупщика/магазина, продающего 'убитые' телефоны
    под видом выгодных лотов."""
    lines = []
    if item.get("seller_name"):
        lines.append(f"Продавец: {item['seller_name']}")
    if item.get("seller_active_ads") is not None:
        lines.append(f"Активных объявлений у продавца сейчас: {item['seller_active_ads']}")
    promo = []
    if item.get("vip"):
        promo.append("VIP")
    if item.get("top"):
        promo.append("ТОП")
    if promo:
        lines.append(f"Объявление продвигается платно: {', '.join(promo)}")
    if not lines:
        return ""

    if has_photos:
        instruction = (
            "\nОТДЕЛЬНО оцени ФОН на фотографиях: если видна витрина/стенд магазина телефонов, много "
            "других телефонов или разной техники в кадре, фирменная выкладка товара — это признак "
            "перекупщика/магазина, торгующего 'убитыми' телефонами под видом выгодных лотов, а не "
            "частного владельца. Если фон обычный бытовой/случайный (рука, стол, диван, одежда, "
            "комната) — это обычный частник, даже если у него несколько объявлений или платное "
            "продвижение. Решай по совокупности: чем больше активных объявлений и чем больше фон "
            "похож на магазин, тем увереннее ставь 'похоже на перекупщика'."
        )
    else:
        instruction = (
            "\nФото не анализируются, поэтому суди о продавце ТОЛЬКО по числу активных объявлений и "
            "платному продвижению выше — если явных признаков магазина нет, ставь 'неясно', не выдумывай."
        )
    return "\n" + "\n".join(lines) + instruction


def build_prompt(item, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate, has_photos=True, condition_note=None, no_photo_reason=None, market_points=None):
    try:
        item_memory = int(item.get("memory")) if item.get("memory") else None
    except (TypeError, ValueError):
        item_memory = None
    points_text = format_market_points(market_points or [], item_memory)
    official_name, official_codes = parse_verified_model(item.get("verified_model"))
    title_key = extract_model_key(item.get("title") or "")
    official_key = extract_model_key(official_name or "")
    model_lines = [f"Заголовок от продавца (может содержать опечатки): {item['title']}"]
    if official_name:
        model_lines.append("Официальная модель из базы IMEI сайта (полная, БЕЗ опечаток — "
                           f"самый надёжный источник названия): {official_name}")
    if official_codes:
        model_lines.append(f"Код(ы) варианта устройства: {', '.join(official_codes)}")
    if item.get("imei_number"):
        masked_note = (" (номер замаскирован сайтом: видны не все цифры, контрольная сумма "
                       "недоступна)") if item.get("imei_masked") else ""
        model_lines.append(f"IMEI со страницы: {item['imei_number']}{masked_note}")
    if official_key and title_key and official_key != title_key:
        model_lines.append("ВНИМАНИЕ: заголовок продавца расходится с официальной моделью "
                           f"(из заголовка выходит «{title_key}», официально «{official_key}»). "
                           "Доверяй официальной модели.")
    model_text = "\n".join(model_lines)
    examples_text = format_similar_examples(similar_examples, item_memory)
    while len(examples_text) > PROMPT_MAX_CHARS // 2 and len(similar_examples or []) > 1:
        similar_examples = similar_examples[:-1]
        examples_text = format_similar_examples(similar_examples, item_memory)
    if points_text and len(points_text) > PROMPT_MAX_CHARS // 3:
        points_text = ("\n".join(points_text.split("\n")[:80]) +
                       "\n…(таблица обрезана по лимиту размера промпта)")
    good_calls_text = format_confirmed_good_calls(confirmed_good_calls)
    sales_text = format_confirmed_sales(confirmed_sales, item_memory)
    web_text = (
        "Результаты веб-поиска по этой модели (реальные страницы из интернета). ВАЖНО: даты у них "
        "обычно не видно — данные могут быть устаревшими, а страницы — про другой рынок (не "
        "Таджикистан) и с ценами в другой валюте (USD, RUB, EUR, KZT, UZS и т.п.). Не переноси эти "
        "цифры на TJS напрямую и не считай их ценой на Somon.tj: используй только как грубый "
        "ориентир по модели и характеристикам, а при расхождении доверяй данным нашей базы и "
        f"подтверждённым продажам выше:\n{web_results}"
        if web_results else "Веб-поиск не дал результатов в этот раз."
    )
    manual_text = format_manual_notes(manual_notes)
    imei_text = format_imei_block(item, usd_rate)
    seller_text = format_seller_block(item, has_photos)

    if has_photos:
        intro = "Изучи текст объявления и фото."
        no_photo_note = ""
    elif no_photo_reason is None:
        intro = "Фото для этого объявления НЕ анализируются."
        no_photo_note = (
            "\nВАЖНО: фото не предоставлены (устройство уверенно определено как новое/запечатанное — "
            "смотреть визуальные дефекты не имеет смысла). Ставь overall_visual_condition \"новое\", "
            "visible_defects оставляй пустым списком — не придумывай дефекты, которых не видел. "
            "Оценивай сделку по тексту, цене, сравнению с рынком и историей продаж ниже.\n"
        )
    else:
        intro = "Фото для этого объявления НЕ анализируются."
        no_photo_note = (
            f"\nВАЖНО: {no_photo_reason}. Определяй overall_visual_condition и visible_defects ТОЛЬКО по тому, "
            "что прямо написано в тексте/описании — не придумывай ничего, чего там нет, и не считай устройство "
            "новым, если это не сказано явно (по умолчанию, если состояние неясно, ставь overall_visual_condition "
            "\"неизвестно\"). Оценивай сделку по тексту, цене, сравнению с рынком и историей продаж ниже.\n"
        )
    if condition_note:
        no_photo_note += f"\n{condition_note}\n"

    prompt = f"""
Ты — эксперт по оценке смартфонов для перепродажи в Таджикистане. {intro}
Используй не только присланные данные ниже, но и свои собственные знания о рынке смартфонов.

{model_text}
Цена: {item['price']} TJS
Город продавца: {item.get('city') or item.get('city_list') or 'не указан'}
Опубликовано: {item.get('published_at') or item.get('published_list') or 'неизвестно'}
Состояние по словам продавца: {item.get('condition') or 'не указано'}
Описание продавца (полный текст): {item.get('description', '')[:DESCRIPTION_MAX_CHARS]}
{imei_text}{seller_text}{no_photo_note}

{good_calls_text}

{sales_text}

{points_text}

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
её при серьёзных рисках (например, чёрный или серый список IMEI).

ВАЖНО про вопросы продавцу: не спрашивай о том, что уже прямо указано в данных выше (состояние,
память, город, дата публикации, статус IMEI — если он определён). Задавай вопросы только о том, что
реально неизвестно и важно для решения (например, состояние аккумулятора, комплектация).

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
  "questions_for_seller": [],
  "seller_type": "частник|похоже на перекупщика|неясно",
  "seller_evidence": ""
}}
Если данных совсем мало — verdict "недостаточно данных", не выдумывай цифры.
seller_type и seller_evidence заполняй по правилам про фон на фото и контекст о продавце выше; если
контекста о продавце не было вообще (блок про продавца отсутствовал) — ставь "неясно" и оставляй
seller_evidence пустой строкой.
""".strip()
    if len(prompt) > PROMPT_MAX_CHARS:
        # Страховка от переполнения контекста: режем хвост, а не роняем вызов.
        prompt = prompt[:PROMPT_MAX_CHARS] + "\n…(промпт обрезан по лимиту размера)"
    return prompt


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


def gemini_json_call(prompt_text, usage, temperature=0.2):
    """Один вызов Gemini с ответом строго в JSON (без фотографий).

    Нужен разбору аномалий: страницы объявлений скачивает он сам (Gemini не умеет
    ходить по ссылкам), а в модель уходят только текст страниц и служебные данные.
    Учёт квоты, ротация сочетаний ключ+модель и повторы — те же, что в analyze_listing.

    Возвращает (результат-dict, None) или (None, текст ошибки).
    """
    if len(prompt_text) > PROMPT_MAX_CHARS:
        prompt_text = prompt_text[:PROMPT_MAX_CHARS] + "\n…(промпт обрезан по лимиту размера)"
    if not pick_available_combo(usage):
        return None, "дневный лимит Gemini исчерпан на всех сочетаниях ключ+модель"
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }
    tried, last_error = [], None
    while True:
        combo = pick_available_combo(usage, exclude=tried)
        if not combo:
            break
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{combo['model']}:generateContent?key={combo['key']}")
        combo_failed = False
        for attempt in range(1, GEMINI_RETRIES_PER_COMBO + 1):
            try:
                resp = requests.post(url, json=payload, timeout=90)
            except Exception as e:
                last_error = f"сбой сети: {type(e).__name__}"
                if attempt < GEMINI_RETRIES_PER_COMBO:
                    time.sleep(GEMINI_RETRY_DELAY * attempt)
                    continue
                combo_failed = True
                break
            if resp.ok:
                usage[combo["id"]] = usage.get(combo["id"], 0) + 1
                save_daily_usage(GEMINI_DAILY_FILE, usage)
                try:
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                except Exception as e:
                    return None, f"ответ без разбираемого текста: {type(e).__name__}"
                result = parse_gemini_json(text)
                if isinstance(result, dict) and result.get("reasoning") == "не удалось разобрать ответ":
                    return None, "Gemini вернул не JSON"
                return result, None
            code = resp.status_code
            last_error = f"HTTP {code}"
            if code == 429:
                usage[combo["id"]] = GEMINI_DAILY_LIMIT_PER_COMBO
                save_daily_usage(GEMINI_DAILY_FILE, usage)
                combo_failed = True
                break
            if code >= 500 and attempt < GEMINI_RETRIES_PER_COMBO:
                time.sleep(GEMINI_RETRY_DELAY * attempt)
                continue
            combo_failed = True
            break
        if combo_failed:
            tried.append(combo["id"])
    return None, f"все сочетания не сработали ({', '.join(tried) or 'нет доступных'}): {last_error}"


def analyze_listing(item, photo_urls, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate, usage, condition_note=None, no_photo_reason=None, market_points=None):
    if not pick_available_combo(usage):
        return {"market_verdict": "недостаточно данных",
                "reasoning": "дневной лимит Gemini исчерпан на всех сочетаниях ключ+модель, анализ отложен",
                "visible_defects": [], "_failed": True, "_fail_reason": "лимит исчерпан"}

    prompt_text = build_prompt(item, similar_examples, confirmed_good_calls, confirmed_sales, web_results,
                               manual_notes, usd_rate, has_photos=bool(photo_urls),
                               condition_note=condition_note, no_photo_reason=no_photo_reason,
                               market_points=market_points)
    print(f"Gemini: промпт по лоту {item['id']} — {len(prompt_text)} символов, похожих лотов "
          f"{len(similar_examples or [])}, рыночных точек {len(market_points or [])}")
    parts = [{"text": prompt_text}]
    for url in photo_urls:
        try:
            img = requests.get(url, headers=HEADERS, timeout=15)
            img.raise_for_status()
            mime = "image/png" if ".png" in url.lower() else "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img.content).decode()}})
        except Exception as e:
            print("Не удалось скачать фото:", url, e)

    payload = {"contents": [{"parts": parts}]}
    tried = []          # сочетания, которые в этом вызове уже не сработали
    last_error = None

    while True:
        combo = pick_available_combo(usage, exclude=tried)
        if not combo:
            break
        if tried:
            print(f"Gemini: переключение {tried[-1]} → {combo['id']} ({last_error})")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{combo['model']}:generateContent?key={combo['key']}"
        )
        combo_failed = False
        for attempt in range(1, GEMINI_RETRIES_PER_COMBO + 1):
            try:
                resp = requests.post(url, json=payload, timeout=60)
            except Exception as e:
                last_error = f"сбой сети: {type(e).__name__}"
                print(f"Gemini: {combo['id']} попытка {attempt}/{GEMINI_RETRIES_PER_COMBO} — {last_error}")
                if attempt < GEMINI_RETRIES_PER_COMBO:
                    time.sleep(GEMINI_RETRY_DELAY * attempt)
                    continue
                combo_failed = True
                break

            if resp.ok:
                usage[combo["id"]] = usage.get(combo["id"], 0) + 1
                save_daily_usage(GEMINI_DAILY_FILE, usage)
                try:
                    result = parse_gemini_json(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
                except Exception as e:
                    # Ответ пришёл, но без текста (например, блокировка по безопасности) —
                    # это не сбой ключа, повторять на другом ключе бессмысленно.
                    print(f"Gemini: {combo['id']} вернул ответ без разбираемого текста: {type(e).__name__}")
                    return {"market_verdict": "недостаточно данных",
                            "reasoning": "не удалось разобрать ответ", "visible_defects": []}
                if tried:
                    print(f"Gemini: успешно через {combo['id']} после сбоя {', '.join(tried)}")
                result["used_web_search"] = bool(web_results)
                return result

            code = resp.status_code
            last_error = f"HTTP {code}"
            if code == 429:
                print(f"Gemini: {combo['id']} исчерпал квоту (429), до конца дня не используется")
                usage[combo["id"]] = GEMINI_DAILY_LIMIT_PER_COMBO
                save_daily_usage(GEMINI_DAILY_FILE, usage)
                combo_failed = True
                break
            if code >= 500:
                print(f"Gemini: {combo['id']} попытка {attempt}/{GEMINI_RETRIES_PER_COMBO} — "
                      f"{code} {resp.text[:200].strip()!r}")
                if attempt < GEMINI_RETRIES_PER_COMBO:
                    time.sleep(GEMINI_RETRY_DELAY * attempt)
                    continue
                combo_failed = True
                break
            # 400/401/403/404 — проблема с ключом или моделью, повторять бессмысленно
            print(f"Gemini: {combo['id']} — {code} {resp.text[:300].strip()!r}, пропускаю в этом прогоне")
            combo_failed = True
            break

        if combo_failed:
            tried.append(combo["id"])

    if tried:
        print(f"Gemini: все доступные сочетания не сработали ({', '.join(tried)}), последняя ошибка: {last_error}")
        return {"market_verdict": "недостаточно данных", "reasoning": "анализ не удался",
                "visible_defects": [], "_failed": True, "_fail_reason": last_error}
    return {"market_verdict": "недостаточно данных",
            "reasoning": "дневной лимит Gemini исчерпан на всех сочетаниях ключ+модель, анализ отложен",
            "visible_defects": [], "_failed": True, "_fail_reason": "лимит исчерпан"}


# ---------- Фильтрация ----------

def keyword_variants(keyword):
    keyword = keyword.lower()
    return [keyword] + TRANSLIT_MAP.get(keyword, []) + REVERSE_TRANSLIT_MAP.get(keyword, [])


def matches_search(item, search):
    title = item["title"].lower()
    query = search.get("query")
    if query:
        keywords = query if isinstance(query, list) else [query]
        title_words = re.findall(r"[a-zа-я0-9]+", title)
        if not any(
            any(word_in_text(v, title, title_words) for v in keyword_variants(k))
            for k in keywords
        ):
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
    if item.get("imei_status") in ("not_registered", "black", "gray"):
        sources.append("статус IMEI со страницы")
    if not sources:
        sources.append("только фото и текст объявления, без доп. данных")
    return sources


# ---------- Ручная гипотетическая проверка (без реального объявления) ----------

MANUAL_NO_PHOTO_REASON = (
    "это гипотетическая проверка, введённая вручную в чат (не настоящее объявление с Somon.tj) — "
    "фото физически нет и не будет"
)


def run_manual_check(title, price, condition, description, usd_rate, usage):
    combined_text = f"{title} {description}"
    memory_match = re.search(r"(\d+)\s*(?:gb|гб)", combined_text, re.I)
    memory = int(memory_match.group(1)) if memory_match else None
    imei_status = detect_imei_status(combined_text)

    item = {
        "title": title, "price": price, "condition": condition, "memory": memory,
        "description": description, "imei_status": imei_status,
        "city": None, "published_at": "ручной ввод",
    }
    if imei_status in CUSTOMS_DUE_STATUSES:
        item["estimated_customs_cost"] = estimate_customs_cost(price, usd_rate)

    model_key = extract_model_key(title)
    similar_examples = similar_full_analyses(model_key, condition, exclude_id=None)
    confirmed_good_calls = get_confirmed_good_calls(model_key)
    confirmed_sales = get_confirmed_sales(model_key)
    manual_notes = get_manual_notes(model_key)

    web_results = None
    if model_key:
        memory_part = f"{memory}gb " if memory else ""
        condition_part = condition or "б/у"
        web_results = web_search_lookup(f"{title} {memory_part}{condition_part} цена Таджикистан Somon")

    data_sources = build_data_sources(similar_examples, confirmed_good_calls, confirmed_sales, manual_notes, web_results, item)
    analysis = analyze_listing(item, [], similar_examples, confirmed_good_calls, confirmed_sales, web_results,
                                manual_notes, usd_rate, usage, no_photo_reason=MANUAL_NO_PHOTO_REASON)

    verdict = analysis.get("market_verdict", "недостаточно данных")
    defects = ", ".join(analysis.get("visible_defects", [])) or "не указаны/не обнаружены"
    positives = ", ".join(analysis.get("positive_features", [])) or "—"
    repair = analysis.get("estimated_repair_cost")
    total = analysis.get("estimated_total_cost")
    resale = analysis.get("estimated_resale_price")
    confidence = analysis.get("confidence")
    questions = analysis.get("questions_for_seller", [])

    profit = None
    if isinstance(resale, (int, float)) and isinstance(total, (int, float)):
        profit = round(resale - total)

    cost_lines = ""
    if repair:
        cost_lines += f"🔧 Примерный ремонт: {repair} TJS\n"
    if item.get("imei_status") == "black":
        cost_lines += "🚫 IMEI в чёрном списке — серьёзный риск\n"
    elif item.get("imei_status") == "gray":
        cost_lines += "⚠️ IMEI в сером списке — растаможка не оплачена; через 30 дней с первой активации SIM станет чёрным\n"
    if item.get("estimated_customs_cost"):
        cost_lines += f"🛃 Примерная растаможка (ещё не оплачена): {item['estimated_customs_cost']} TJS\n"
    if total:
        cost_lines += f"🧮 Итоговая цена (лот + расходы): {total} TJS\n"
    if resale:
        cost_lines += f"📈 Продать можно примерно за: {resale} TJS\n"
    if profit is not None:
        cost_lines += f"💵 Примерная выгода: {profit} TJS\n"
    if isinstance(confidence, (int, float)):
        cost_lines += f"🎯 Уверенность Gemini: {round(confidence * 100)}%\n"

    questions_lines = ""
    if questions:
        questions_lines = "❓ Вопросы продавцу:\n" + "\n".join(f"  • {q}" for q in questions) + "\n"

    return (
        f"🧪 Гипотетическая проверка (без фото, введено вручную)\n\n{title}\n"
        f"💰 Цена: {price} TJS\n"
        f"🩺 Состояние по вводу: {condition or 'не указано'}\n"
        f"📊 Состояние по описанию (мнение Gemini): {analysis.get('overall_visual_condition', 'неизвестно')}\n"
        f"🛠 Дефекты: {defects}\n"
        f"✅ Плюсы: {positives}\n"
        f"{cost_lines}"
        f"{questions_lines}"
        f"📚 На основе: {', '.join(data_sources)}\n"
        f"🧭 Вердикт: {verdict}\n"
        f"💡 {analysis.get('reasoning', '')}"
    )


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
    min_confidence = get_min_confidence()
    max_price = get_max_price()

    try:
        listings, soup = fetch_listings()
        report_fetch_result(health, success=True)
    except Exception as e:
        print("Не удалось загрузить Somon.tj:", e)
        report_fetch_result(health, success=False)
        # Не выходим: находки воркеров лежат в worker_inbox.jsonl и их надо разобрать
        # даже если сама выдача сейчас недоступна.
        listings, soup = [], BeautifulSoup("<html></html>", "html.parser")

    worker_items = load_worker_inbox()
    if worker_items:
        have = {it["id"] for it in listings}
        fresh = [w for w in worker_items if w["id"] not in have]
        listings = fresh + listings
        print(f"📥 от воркеров: {len(fresh)} новых лотов (во входящих {len(worker_items)})")

    if not listings:
        print("Объявления не найдены:", soup.get_text()[:2000])
        return

    current_ids = {item["id"] for item in listings}

    usage_str = ", ".join(f"{c['id']}: {usage.get(c['id'], 0)}/{GEMINI_DAILY_LIMIT_PER_COMBO}" for c in GEMINI_COMBOS)
    search_usage = get_search_usage()
    search_usage_str = ", ".join(f"{p['id']}: {search_usage.get(p['id'], 0)}/{p['limit']}" for p in SEARCH_PROVIDERS)
    print(f"Режим: {MODES[mode]}; поисков: {len(searches)}; объявлений: {len(listings)}; "
          f"подписчиков: {len(subscribers)}; мин. выгода: {min_profit} TJS; мин. уверенность: {round(min_confidence * 100)}%; "
          f"потолок цены: {max_price if max_price else 'не задан'} TJS; курс USD/TJS: {usd_rate}; "
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
        and not seen.get(item["id"], {}).get("photo_ok")
    ]
    # Свежие — первыми. Верх страницы выдачи занят платными VIP/ТОП-объявлениями
    # (на живой странице первые карточки были 4-дневной давности), поэтому без
    # такой сортировки новое объявление ждало разбора в очереди.
    candidates.sort(key=lambda it: card_age_minutes(it.get("published_list")))
    allowed = allowed_analyses_per_run(usage)
    candidates = candidates[:allowed]

    print(f"На полный анализ в этом прогоне: {len(candidates)} (лимит прогона {allowed}, "
          f"свежие разбираются первыми)")

    ai_failures = 0
    for item in candidates:
        if run_budget_exhausted():
            print("⏱ Бюджет времени прогона исчерпан — новые лоты разберутся в следующем запуске.")
            break
        entry = seen.get(item["id"], {})
        try:
            # Бейдж "IMEI проверен" виден уже на карточке в списке объявлений — если
            # его там нет, продавец вообще не проверял IMEI на сайте. Отсеиваем такие
            # лоты ПЕРВЫМ делом, даже раньше потолка цены — незачем тратить запрос к
            # странице объявления и бюджет Gemini/поиска на лот без этой проверки.
            if not item.get("imei_verified_badge"):
                log_rejected(
                    item, {"market_verdict": "недостаточно данных"},
                    note="На карточке нет плашки «IMEI проверен» — не анализировался",
                )
                entry["photo_ok"] = True
                entry["url"] = item["url"]
                entry["title"] = item["title"]
                entry["price"] = item["price"]
                entry["condition"] = item.get("condition")
                entry["model_key"] = extract_model_key(item["title"])
                seen[item["id"]] = entry
                continue

            # Потолок цены проверяем СЛЕДОМ, до похода на страницу объявления и
            # вызова Gemini — если лот дороже, чем владелец готов рассматривать,
            # незачем тратить на него запрос к странице, фото и бюджет Gemini/поиска.
            # В базу (market_point выше) он уже попал — для /price это не мешает.
            if max_price and item.get("price") and item["price"] > max_price:
                log_rejected(
                    item, {"market_verdict": "недостаточно данных"},
                    note=f"Цена ({item['price']} TJS) выше потолка /maxprice ({max_price} TJS) — не анализировался",
                )
                entry["photo_ok"] = True
                entry["url"] = item["url"]
                entry["title"] = item["title"]
                entry["price"] = item["price"]
                entry["condition"] = item.get("condition")
                entry["model_key"] = extract_model_key(item["title"])
                seen[item["id"]] = entry
                continue

            junk_reason = junk_title_reason(item["title"])
            skip_reason = None if junk_reason else skip_analysis_reason(item["title"])
            if junk_reason or skip_reason:
                log_rejected(item, {"market_verdict": "недостаточно данных"}, note=junk_reason or skip_reason)
                entry["photo_ok"] = True
                entry["url"] = item["url"]
                entry["title"] = item["title"]
                entry["price"] = item["price"]
                entry["condition"] = item.get("condition")
                entry["model_key"] = None if junk_reason else extract_model_key(item["title"])
                seen[item["id"]] = entry
                continue

            (condition, memory, description, photo_urls, imei_status, city, published_at, verified_model,
             seller_name, seller_url, seller_active_ads, seller_verified, imei_info) = fetch_detail(item["url"])
            item["condition"] = condition or item.get("condition")
            item["memory"] = memory or item.get("memory")
            item["description"] = description
            item["imei_status"] = imei_status
            # Город со страницы объявления, а если там пусто — из карточки списка
            # (раньше в Telegram всегда печаталось «—»).
            # Город со страницы объявления, а если там пусто — из карточки списка
            # (раньше в Telegram всегда печаталось «—»).
            item["city"] = city or item.get("city_list")
            # Печать тайминга: в логе прогона видно, когда лот обнаружен и сколько ему
            # минут от публикации — по этому логу проверяется задержка уведомления.
            _age = card_age_minutes(item.get("published_list"))
            _age_txt = ("объявление опубликовано ~%d мин назад" % _age if _age < 10 ** 6
                        else "возраст в выдаче неизвестен")
            print("[тайминг] лот %s обнаружен %s — %s"
                  % (item["id"], time.strftime("%H:%M:%S"), _age_txt))
            # --- IMEI: номер со страницы и память по TAC --------------------------
            # Somon показывает IMEI замаскированным ("8682080XXXXXX4"), первые 8 цифр
            # видны. Запоминаем связку «TAC -> официальная модель из блока проверки
            # IMEI»: если у следующего лота плашки с моделью не окажется, а IMEI есть —
            # модель возьмётся из этой памяти, а не из заголовка с опечатками.
            item["imei_number"] = (imei_info or {}).get("number")
            item["imei_masked"] = (imei_info or {}).get("masked")
            tac = tac_from_imei(item["imei_number"])
            if tac:
                tac_map = load_tac_map()
                known = tac_map.get(tac) or {}
                if verified_model:
                    tac_name, tac_codes = parse_verified_model(verified_model)
                    if tac_name or tac_codes:
                        history = [u for u in (known.get("history") or []) if u != item["url"]]
                        tac_map[tac] = {
                            "model": tac_name or known.get("model"),
                            "codes": tac_codes or known.get("codes") or [],
                            "updated_at": time.strftime("%Y-%m-%d"),
                            "history": (history + [item["url"]])[-5:],
                        }
                        save_tac_map(tac_map)
                elif known.get("model") or known.get("codes"):
                    verified_model = known.get("model") or verified_model
                    item["verified_model"] = verified_model
                    item["model_codes"] = known.get("codes") or []
                    print(f"IMEI TAC {tac}: модель взята из памяти по IMEI — {known.get('model')}")
            if verified_model and not item.get("model_codes"):
                item["model_codes"] = parse_verified_model(verified_model)[1] or item.get("city_list")
            item["published_at"] = published_at
            item["verified_model"] = verified_model
            item["seller_name"] = seller_name
            item["seller_url"] = seller_url
            item["seller_active_ads"] = seller_active_ads
            item["seller_verified"] = seller_verified
            if imei_status in CUSTOMS_DUE_STATUSES:
                item["estimated_customs_cost"] = estimate_customs_cost(item["price"], usd_rate)

            # ---- Чёрный список продавцов: отсеиваем ДО похода к Gemini/веб-поиску ----
            seller_key = seller_key_from_url(seller_url)
            if seller_key:
                bl_entry = is_seller_blacklisted(seller_key)
                if bl_entry:
                    log_rejected(
                        item, {"market_verdict": "недостаточно данных"},
                        note=f"Продавец «{bl_entry.get('name') or seller_key}» в чёрном списке: "
                             f"{bl_entry.get('reason')}",
                    )
                    entry["photo_ok"] = True
                    entry["url"] = item["url"]
                    entry["title"] = item["title"]
                    entry["price"] = item["price"]
                    entry["condition"] = item.get("condition")
                    entry["model_key"] = extract_model_key(item["title"])
                    seen[item["id"]] = entry
                    continue

                # Считаем долю телефонных объявлений у продавца только если по количеству
                # объявлений он вообще похож на кандидата в бан — незачем лишний раз ходить
                # на его страницу ради частника с парой объявлений.
                phone_ratio, phone_sample = None, 0
                active_ads_now = item.get("seller_active_ads")
                if active_ads_now is not None and active_ads_now >= RESELLER_SOFT_ACTIVE_ADS:
                    phone_ratio, phone_sample = seller_phone_ratio(seller_url)

                heuristic_reason = reseller_heuristic_reason(item, phone_ratio, phone_sample)
                if heuristic_reason:
                    bl_entry_new, is_new_ban = blacklist_seller(
                        seller_key, seller_name, seller_url, heuristic_reason, item["url"],
                        extra={"active_ads_at_block": seller_active_ads})
                    log_rejected(item, {"market_verdict": "недостаточно данных"},
                                 note=f"Продавец добавлен в чёрный список: {heuristic_reason}")
                    if is_new_ban:
                        alert_owner(
                            f"🚫 Продавец «{seller_name or seller_key}» добавлен в чёрный список.\n"
                            f"Причина: {heuristic_reason}\nОбъявление: {item['url']}"
                        )
                    else:
                        print(f"Продавец {seller_key} уже в чёрном списке — повторно не уведомляю")
                    entry["photo_ok"] = True
                    entry["url"] = item["url"]
                    entry["title"] = item["title"]
                    entry["price"] = item["price"]
                    entry["condition"] = item.get("condition")
                    entry["model_key"] = extract_model_key(item["title"])
                    seen[item["id"]] = entry
                    continue

            # Верифицированная модель из базы IMEI надёжнее заголовка продавца —
            # используем её, когда она есть, и падаем на разбор заголовка только
            # если проверка IMEI не дала результата (не найден в базе и т.п.).
            model_key = official_model_key(verified_model, item["title"])
            similar_examples = similar_full_analyses(model_key, item.get("condition"), item["id"])
            confirmed_good_calls = get_confirmed_good_calls(model_key)
            confirmed_sales = get_confirmed_sales(model_key)
            manual_notes = get_manual_notes(model_key)

            web_results = None
            if model_key:
                memory_part = f"{item.get('memory')}gb " if item.get("memory") else ""
                condition_part = item.get("condition") or "б/у"
                # Заголовок объявления, а не model_key — он теперь может выглядеть
                # как "imei:sm-s936b" (внутренний формат для точного сравнения),
                # что бесполезно в качестве поискового запроса для веб-поиска.
                query = f"{item['title']} {memory_part}{condition_part} цена Таджикистан Somon"
                web_results = web_search_lookup(query)

            data_sources = build_data_sources(similar_examples, confirmed_good_calls, confirmed_sales, manual_notes, web_results, item)

            photos_for_analysis = [] if condition_signal(item) == "new" else photo_urls

            analysis = analyze_listing(item, photos_for_analysis, similar_examples, confirmed_good_calls, confirmed_sales, web_results, manual_notes, usd_rate, usage)
            if analysis.get("_failed"):
                # Анализ не состоялся (все ключи недоступны / лимит). Не пишем «недостаточно
                # данных» в историю и не помечаем лот просмотренным — он попадёт в следующий прогон.
                ai_failures += 1
                print(f"Анализ отложен для {item['id']}: {analysis.get('_fail_reason')}")
                if ai_failures >= 2:
                    print("Gemini недоступен два раза подряд — остальные лоты откладываю на следующий прогон")
                    break
                continue
            ai_failures = 0
            verdict = analysis.get("market_verdict", "недостаточно данных")

            log_full_analysis(item, analysis, verdict, data_sources, model_key)

            # ---- Продавец похож на перекупщика по фону на фото (Gemini) ----
            if seller_key and analysis.get("seller_type") == "похоже на перекупщика":
                evidence = analysis.get("seller_evidence") or "по фону на фотографиях"
                reason = f"похоже на перекупщика по фото: {evidence}"
                bl_entry_new, is_new_ban = blacklist_seller(
                    seller_key, seller_name, seller_url, reason, item["url"],
                    extra={"active_ads_at_block": seller_active_ads})
                log_rejected(item, analysis, note=f"Продавец добавлен в чёрный список: {reason}")
                if is_new_ban:
                    alert_owner(
                        f"🚫 Продавец «{seller_name or seller_key}» добавлен в чёрный список (по фото).\n"
                        f"Причина: {evidence}\nОбъявление: {item['url']}"
                    )
                else:
                    print(f"Продавец {seller_key} уже в чёрном списке — повторно не уведомляю")
                entry["photo_ok"] = True
                entry["url"] = item["url"]
                entry["title"] = item["title"]
                entry["price"] = item["price"]
                entry["condition"] = item.get("condition")
                entry["model_key"] = model_key
                entry["verified_model"] = verified_model
                seen[item["id"]] = entry
                continue

            resale = analysis.get("estimated_resale_price")
            total = analysis.get("estimated_total_cost")
            profit = None
            if isinstance(resale, (int, float)) and isinstance(total, (int, float)):
                profit = round(resale - total)

            passes_profit = profit is not None and profit >= min_profit

            gem_confidence = analysis.get("confidence")
            passes_confidence = min_confidence <= 0 or (
                isinstance(gem_confidence, (int, float)) and gem_confidence >= min_confidence
            )

            if verdict == "недооценено" and passes_profit and passes_confidence:
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
                elif item.get("imei_status") == "gray":
                    cost_lines += "⚠️ IMEI в сером списке — растаможка не оплачена; через 30 дней с первой активации SIM станет чёрным\n"
                if customs:
                    cost_lines += f"🛃 Примерная растаможка (ещё не оплачена): {customs} TJS\n"
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
                    f"📍 Город: {item.get('city') or '—'}\n"
                    f"🕒 Опубликовано: {item.get('published_at') or '—'}\n"
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
                if verdict == "недооценено":
                    reasons = []
                    if not passes_profit:
                        reasons.append(f"выгода ({profit if profit is not None else 'не посчитана'} TJS) ниже порога {min_profit} TJS")
                    if not passes_confidence:
                        conf_str = f"{round(gem_confidence * 100)}%" if isinstance(gem_confidence, (int, float)) else "не указана"
                        reasons.append(f"уверенность ({conf_str}) ниже порога {round(min_confidence * 100)}%")
                    note = "Gemini счёл недооценённым, но " + " и ".join(reasons)
                log_rejected(item, analysis, note=note)

            entry["photo_ok"] = True
            entry["url"] = item["url"]
            entry["title"] = item["title"]
            entry["price"] = item["price"]
            entry["condition"] = item.get("condition")
            entry["model_key"] = model_key
            entry["verified_model"] = verified_model
            seen[item["id"]] = entry
            time.sleep(INTER_ITEM_DELAY)
        except Exception as e:
            print("Ошибка при обработке объявления", item["id"], ":", e)
        finally:
            save_seen(seen)

    # Домашние дела (отметка исчезнувших объявлений и досбор ключей) идут ПОСЛЕ
    # разбора свежих лотов: раньше они шли до него, каждое делает сетевые запросы и
    # съедало время прогона, из-за чего уведомление о новом лоте уходило позже.
    seen = process_disappeared_ads(seen, current_ids)
    process_remigrate_live_batch(subscribers)

    known_anomalies = load_json(ANOMALY_STATE_FILE, {})
    anomalies = find_price_anomalies()
    merged = [a for a in anomalies
              if a["kind"] == "merged"
              and known_anomalies.get(a["group_key"], {}).get("kind") not in ("merged", "reviewed")]
    outliers = [a for a in anomalies if a["kind"] == "outlier"]
    if merged:
        lines = ["🔍 Похоже, в один model_key слиплись разные модели — цены внутри ключа "
                 "распались на несколько коридоров:"]
        for a in merged[:5]:
            lines.append(
                f"- {a['label']} ({a['count']} лотов): коридоры "
                + ", ".join(f"{c[0]}–{c[-1]}" for c in a["clusters"])
                + f" TJS; примеры: «{a['min_title']}» … «{a['max_title']}»"
            )
        lines.append(f"\nОдиночных выбросов в других группах (ключ, скорее всего, чистый): {len(outliers)}")
        lines.append("\nПричины и правки — /fixanomalies, сводка — /anomalies")
        alert_owner("\n".join(lines))
    if anomalies:
        for a in anomalies:
            state = known_anomalies.setdefault(a["group_key"], {"first_seen": time.strftime("%Y-%m-%d")})
            state["ratio"] = a["ratio"]
            state["kind"] = a["kind"]
        with open(ANOMALY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(known_anomalies, f, ensure_ascii=False, indent=2)

    # Лоты, попавшие в seen_ids.json, разобраны — убираем их из входящих воркеров,
    # чтобы следующий запуск не тянул их заново. Остальное остаётся: если прогон
    # прервался по бюджету времени, лот разберётся в следующий раз.
    # Разбор аномалий через Gemini: открывает страницы лотов и убирает ложные
    # аномалии. Включено по env ANOMALY_REVIEW_ENABLED=1, чтобы прогон не тратил
    # квоту Gemini, когда разбор не нужен.
    if os.environ.get("ANOMALY_REVIEW_ENABLED") == "1":
        try:
            import anomaly_review
            review = anomaly_review.run_once(
                limit=int(os.environ.get("ANOMALY_REVIEW_PER_RUN", "0")
                          or anomaly_review.ANOMALY_REVIEW_PER_RUN))
            for r in review["reviewed"]:
                print(f"🩺 разобран {r['label']}: {r['verdict']}")
        except Exception as e:
            print(f"⚠️ разбор аномалий не удался: {type(e).__name__}: {str(e)[:200]}")

    left = prune_worker_inbox(set(seen.keys()))
    print(f"📤 во входящих воркеров осталось {left} лотов на следующий запуск")


if __name__ == "__main__":
    main()
