# -*- coding: utf-8 -*-
"""Разбор аномалий цен через Gemini.

Зачем
-----
find_price_anomalies() умеет только показать группу, где цены распались на
коридоры: он не открывает страницы объявлений и не знает, почему так вышло.
Gemini сам страницы открывать не умеет — поэтому текст страниц ему подаёт этот
модуль (обычный requests + BeautifulSoup, как в самом скрапере; если сайт отдал
403 — запасной путь Tavily /extract).

Что делает по каждой группе
---------------------------
1. собирает ссылки лотов из группы (они приходят из find_price_anomalies);
2. скачивает ПОЛНЫЙ текст каждой страницы объявления и разбирает её тем же
   парсером (IMEI-блок, «Модель», состояние, память, город);
3. просит Gemini ответить строгим JSON: причина + по каждому лоту «оставить /
   исправить поля / исключить»;
4. записывает правки в anomaly_corrections.json — этот слой читает
   find_price_anomalies() первым, поэтому разобранная группа больше не выглядит
   аномальной (журнал price_history.jsonl остаётся неприкосновенным);
5. складывает причину и подсказку для кода в anomaly_reasons.jsonl.

Запуск:  python anomaly_review.py --limit 2 [--kinds merged,outlier] [--dry-run]
Из бота: кнопка «🩺 Разобрать аномалии» (команда /fixanomalies).
"""
import argparse
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup


def _load_scraper():
    """Модуль scraper: при запуске из бота это уже загруженный __main__.

    Иначе (запуск скриптом из workflow) — импортируем обычным способом.
    """
    main_mod = sys.modules.get("__main__")
    if (main_mod is not None
            and getattr(main_mod, "__file__", "").endswith("scraper.py")
            and hasattr(main_mod, "find_price_anomalies")):
        return main_mod
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    return importlib.import_module("scraper")


sc = _load_scraper()

PAGE_TEXT_LIMIT = int(os.environ.get("PAGE_TEXT_LIMIT", "12000"))   # знаков текста страницы в промпт
MAX_PAGES_PER_GROUP = int(os.environ.get("MAX_PAGES_PER_GROUP", "6"))
PAGE_PAUSE = float(os.environ.get("PAGE_PAUSE", "0.4"))             # пауза между запросами страниц
ANOMALY_REVIEW_PER_RUN = int(os.environ.get("ANOMALY_REVIEW_PER_RUN", "2"))
DEFAULT_KINDS = tuple(os.environ.get("ANOMALY_REVIEW_KINDS", "merged,outlier").split(","))

REVIEW_INSTRUCTIONS = """Ты — инженер парсера объявлений somon.tj (Таджикистан, цены в сомони/TJS).

Тебе дают группу объявлений, которую алгоритм сгруппировал под один model_key и
признал аномальной: цены внутри ключа распались на коридоры. Ниже — служебные
данные группы и ПОЛНЫЙ текст страниц этих объявлений.

Твоя задача — по тексту страниц понять, что происходит на самом деле, и дать
правки, которые уберут ложную аномалию.

Жёсткие правила:
* Опирайся ТОЛЬКО на текст страниц ниже. Веб-поиском не пользуйся.
* Любые оценки стоимости из прошлых разборов — чужое мнение, игнорируй их.
* Блок проверки IMEI важнее заголовка: если в нём указана другая модель, чем в
  заголовке продавца, — верна модель из блока IMEI.
* Если лот — это копия, коробка, запчасть, аксессуар, услуга или приманка
  («цена за корпус», «позвоните», «обмен»), помечай его exclude.
* Если в группу попали разные модели: исправь model_key тем лотам, что не
  относятся к модели группы.
* Если цена на странице отличается от цены в базе (лот переоценили) — исправь price.
* Если память объёма не совпадает с ключом группы — исправь memory_gb.
* Если расхождение объясняется реальным рынком (комплект, состояние, регион) —
  так и напиши в verdict, лоты не трогай.
* Если данных не хватает — verdict "непонятно", per_lot пустой.

Ответ — строго JSON без пояснений вокруг:
{
  "verdict": "разные_модели | ошибка_парсинга | приманка | реальная_разница_цен | непонятно",
  "summary": "1-3 предложения: в чём причина аномалии",
  "code_hint": "что поправить в коде парсера, чтобы это не повторялось (или пусто)",
  "per_lot": [
    {"id": "id лота из служебных данных",
     "action": "keep | fix | exclude",
     "model_key": "правильный ключ модели (только для action=fix)",
     "memory_gb": 0,
     "price": 0,
     "reason": "чем это подтверждается в тексте страницы"}
  ]
}"""


# ---------- Загрузка страниц ----------

def http_fetch(url):
    resp = requests.get(url, headers=sc.HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def tavily_extract(url, key=None):
    """Запасной путь, если сайт отдал 403 (Cloudflare): Tavily /extract.

    Бесплатный тариф Tavily — 1000 кредитов в месяц; extract тоже считается
    кредитами, поэтому используется только как резерв.
    """
    key = key or os.environ.get("TAVILY_API_KEY") or os.environ.get("TAVILY_API_KEY_2") or ""
    if not key:
        return ""
    resp = requests.post("https://api.tavily.com/extract",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"urls": [url]}, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    return results[0].get("raw_content", "") if results else ""


def load_page(url, opener=None):
    """Текст страницы объявления + разобранные поля тем же парсером.

    Возвращает {"url","text","source","dom","error"}: ошибка не роняет разбор
    группы — Gemini просто получит меньше данных, а причина останется в журнале.
    """
    out = {"url": url, "text": "", "source": "", "dom": None, "error": ""}
    if not url:
        out["error"] = "в базе нет ссылки на лот"
        return out
    opener = opener or http_fetch
    raw = ""
    try:
        raw = opener(url) or ""
        out["source"] = "сайт"
    except Exception as e:
        out["error"] = f"загрузка страницы не удалась: {type(e).__name__}"
        try:
            raw = tavily_extract(url) or ""
            out["source"] = "tavily" if raw else ""
            if raw:
                out["error"] = ""
        except Exception as e2:
            out["error"] += f"; tavily: {type(e2).__name__}"
    if not raw:
        return out
    soup = BeautifulSoup(raw, "html.parser")
    try:
        out["dom"] = sc.parse_ad_page(soup)
    except Exception:
        out["dom"] = None
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    out["text"] = text[:PAGE_TEXT_LIMIT]
    if not out["text"] and not out["error"]:
        out["error"] = "со страницы не извлёкся текст"
    return out


# ---------- Промпт ----------

def lots_of(anomaly):
    """Лоты группы для разбора: сначала крайние цены, потом остальные."""
    lots = list(anomaly.get("lots") or [])
    ordered, seen = [], set()
    for key in ("min_id", "max_id"):
        lid = anomaly.get(key)
        for lot in lots:
            if lot.get("id") == lid and lot["id"] not in seen:
                ordered.append(lot)
                seen.add(lot["id"])
    for lot in sorted(lots, key=lambda l: l["price"]):
        if lot.get("id") not in seen:
            ordered.append(lot)
            seen.add(lot["id"])
    return ordered[:MAX_PAGES_PER_GROUP]


def build_review_prompt(anomaly, pages):
    lines = [
        REVIEW_INSTRUCTIONS,
        "\n=== СЛУЖЕБНЫЕ ДАННЫЕ ГРУППЫ ===",
        f"model_key группы: {anomaly['model_key']}",
        f"память: {anomaly.get('memory') or 'не указана'}",
        f"лотов в группе: {anomaly['count']}, тип аномалии: {anomaly['kind']}, "
        f"отношение цен: ×{anomaly['ratio']}",
        "коридоры цен: " + ", ".join(f"{c[0]}–{c[-1]} TJS ({len(c)} лотов)" for c in anomaly["clusters"]),
        f"самый дешёвый: {anomaly['min_price']} TJS «{anomaly['min_title']}»",
        f"самый дорогой: {anomaly['max_price']} TJS «{anomaly['max_title']}»",
        "\nВсе лоты группы (id | цена | память | состояние | город | заголовок | ссылка):",
    ]
    for lot in anomaly.get("lots") or []:
        lines.append(f"- {lot.get('id')} | {lot.get('price')} TJS | {lot.get('memory') or '?'} GB | "
                     f"{lot.get('condition') or '?'} | {lot.get('city') or '?'} | "
                     f"«{lot.get('title')}» | {lot.get('url')}")
    lines.append("\n=== ТЕКСТЫ СТРАНИЦ ===")
    for page in pages:
        lines.append(f"\n--- {page['url']} (источник: {page['source'] or 'нет'}"
                     f"{', ' + page['error'] if page['error'] else ''}) ---")
        dom = page.get("dom") or {}
        if dom:
            lines.append("[структура страницы] " + ", ".join(
                f"{k}={dom.get(k)}" for k in ("imei_header", "verified_model", "model_codes",
                                              "condition_raw", "memory_raw", "city", "price", "views")
                if dom.get(k) not in (None, "", [])))
        lines.append(page["text"] or "(текст страницы получить не удалось)")
    lines.append('\nОтвет: строго JSON по схеме выше.')
    return "\n".join(lines)


# ---------- Применение ответа ----------

def apply_review(anomaly, review, corrections, meta=None):
    """Записать правки в слой исправлений и причину — в журнал.

    corrections — словарь {id лота: {"action": "exclude"} | {"model_key": ...,
    "memory_gb": ..., "price": ...}}. Возвращает (applied, excluded).
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    applied, excluded, skipped = [], [], []
    for lot in (review.get("per_lot") or []):
        if not isinstance(lot, dict):
            continue
        lid = str(lot.get("id") or "").strip()
        action = str(lot.get("action") or "keep").lower()
        if not lid or action == "keep":
            continue
        reason = str(lot.get("reason") or "")[:400]
        if action == "exclude":
            corrections[lid] = {"action": "exclude", "group_key": anomaly["group_key"],
                                "reason": reason, "source": "gemini-review", "reviewed_at": ts}
            excluded.append(lid)
            continue
        fix = {}
        key = (lot.get("model_key") or "").strip()
        if key:
            if sc.is_valid_model_key(key):
                fix["model_key"] = key
            else:
                skipped.append(f"{lid}: ключ «{key}» негоден, не записан")
        for field in ("memory_gb", "price"):
            value = lot.get(field)
            if value in (None, "", 0, "0"):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                skipped.append(f"{lid}: {field}={value!r} не число, не записано")
                continue
            if field == "price" and value < sc.ANOMALY_MIN_PRICE:
                skipped.append(f"{lid}: цена {value} ниже порога статистики, не записана")
                continue
            if field == "memory_gb" and not (1 <= value <= 4096):
                skipped.append(f"{lid}: память {value} выглядит неверной, не записана")
                continue
            fix[field] = value
        if not fix:
            continue
        fix.update({"group_key": anomaly["group_key"], "reason": reason,
                    "source": "gemini-review", "reviewed_at": ts})
        corrections[lid] = fix
        applied.append(lid)

    entry = {
        "reviewed_at": ts,
        "group_key": anomaly["group_key"], "label": anomaly["label"], "kind": anomaly["kind"],
        "count": anomaly["count"], "ratio": anomaly["ratio"],
        "corridors": [[c[0], c[-1]] for c in anomaly["clusters"]],
        "verdict": review.get("verdict"), "summary": review.get("summary"),
        "code_hint": review.get("code_hint"),
        "applied": applied, "excluded": excluded, "skipped": skipped,
        "per_lot": review.get("per_lot") or [],
        "pages": [{"url": p["url"], "source": p["source"], "chars": len(p["text"]),
                   "error": p["error"]} for p in (meta.get("pages") if meta else []) or []],
        "model_used": (meta or {}).get("model_used", ""),
        "prompt_chars": (meta or {}).get("prompt_chars", 0),
    }
    sc.log_anomaly_reason(entry)
    return applied, excluded


def mark_reviewed(anomaly):
    """Отметить группу разобранной, чтобы уведомление не приходило снова."""
    state = sc.load_json(sc.ANOMALY_STATE_FILE, {})
    item = state.setdefault(anomaly["group_key"], {})
    item["kind"] = "reviewed"
    item["reviewed_at"] = time.strftime("%Y-%m-%d")
    with open(sc.ANOMALY_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- Прогон ----------

def run_once(limit=None, kinds=None, dry_run=False, opener=None, notify=None):
    """Разобрать до limit групп. Возвращает отчёт для бота/лога."""
    limit = ANOMALY_REVIEW_PER_RUN if limit is None else int(limit)
    kinds = tuple(kinds or DEFAULT_KINDS)
    corrections = sc.load_corrections()
    anomalies = sc.find_price_anomalies(corrections=corrections)
    targets = [a for a in anomalies if a["kind"] in kinds][:max(0, limit)]
    report = {"found": len(anomalies), "targets": len(targets), "reviewed": [],
              "errors": [], "corrections_total": len(corrections)}
    if not targets:
        print("Разбирать нечего: аномальных групп выбранных типов нет.")
        return report
    usage = sc.get_daily_usage(sc.GEMINI_DAILY_FILE, [c["id"] for c in sc.GEMINI_COMBOS])
    for anomaly in targets:
        print(f"\n=== {anomaly['label']} ({anomaly['kind']}, {anomaly['count']} лотов, ×{anomaly['ratio']}) ===")
        if notify:
            notify(f"🩺 Разбираю {anomaly['label']} ({anomaly['kind']})…")
        pages = []
        for lot in lots_of(anomaly):
            page = load_page(lot.get("url"), opener=opener)
            print(f"   страница {lot.get('url') or '—'}: {len(page['text'])} знаков, "
                  f"источник {page['source'] or 'нет'}{', ' + page['error'] if page['error'] else ''}")
            pages.append(page)
            time.sleep(PAGE_PAUSE)
        prompt = build_review_prompt(anomaly, pages)
        review, error = sc.gemini_json_call(prompt, usage)
        if error or not isinstance(review, dict):
            msg = f"{anomaly['label']}: Gemini не ответил — {error or 'пустой ответ'}"
            print("⚠️ " + msg)
            report["errors"].append(msg)
            continue
        meta = {"pages": pages, "model_used": "gemini", "prompt_chars": len(prompt)}
        applied, excluded = apply_review(anomaly, review, corrections, meta=meta)
        if not dry_run:
            sc.save_corrections(corrections)
            mark_reviewed(anomaly)
        verdict = review.get("verdict") or "?"
        print(f"   вердикт: {verdict}; правок: {len(applied)}, исключено: {len(excluded)}")
        print(f"   причина: {str(review.get('summary'))[:300]}")
        if review.get("code_hint"):
            print(f"   что поправить в коде: {str(review['code_hint'])[:300]}")
        report["reviewed"].append({"label": anomaly["label"], "verdict": verdict,
                                  "applied": applied, "excluded": excluded,
                                  "summary": review.get("summary"),
                                  "code_hint": review.get("code_hint")})
    if not dry_run:
        sc.save_corrections(corrections)
    report["corrections_total"] = len(sc.load_corrections())
    print(f"\nВсего правок в слое исправлений: {report['corrections_total']}, "
          f"журнал причин: {sc.ANOMALY_REASONS_FILE}")
    return report


def main():
    parser = argparse.ArgumentParser(description="Разбор аномалий цен через Gemini")
    parser.add_argument("--limit", type=int, default=ANOMALY_REVIEW_PER_RUN,
                        help="сколько групп разобрать за этот прогон")
    parser.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                        help="типы аномалий: merged,outlier,wide")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, ничего не записывать")
    args = parser.parse_args()
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    report = run_once(limit=args.limit, kinds=kinds, dry_run=args.dry_run)
    if report["errors"] and not report["reviewed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
