# -*- coding: utf-8 -*-
"""Собрать находки всех воркеров в один список для главного процесса.

Читает worker-findings-N.jsonl из веток origin/worker-N (или из рабочей копии,
если веток нет) и пишет worker_inbox.jsonl — из него scraper.py берёт лоты.

Почему через ветки, а не через общий файл: если бы воркеры писали в один файл в
main, каждый push требовал бы rebase и постоянно конфликтовал. Своя ветка у каждого
воркера убирает конфликты полностью, а main пишет только главный процесс.
"""
import json
import os
import subprocess
from collections import OrderedDict

INBOX = os.environ.get("WORKER_INBOX", "worker_inbox.jsonl")
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "3"))
# Сколько записей держать во входящем файле (главный обрабатывает и удаляет).
INBOX_LIMIT = int(os.environ.get("WORKER_INBOX_LIMIT", "5000"))


def git_show(ref, path):
    r = subprocess.run(f"git show {ref}:{path}", shell=True, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def load_jsonl(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main():
    merged = OrderedDict()
    sources = []
    for i in range(1, WORKER_COUNT + 1):
        name = f"worker-findings-{i}.jsonl"
        text = git_show(f"origin/worker-{i}", name)
        src = f"origin/worker-{i}"
        if not text:
            text = git_show("HEAD", name)
            src = "рабочая копия"
        items = load_jsonl(text)
        if items:
            sources.append(f"{src}: {len(items)}")
        for it in items:
            key = str(it.get("id") or it.get("url") or "")
            if not key:
                continue
            # Если один лот нашли два воркера — храним самую свежую запись.
            prev = merged.get(key)
            if prev is None or (it.get("found_at") or "") > (prev.get("found_at") or ""):
                merged[key] = it
    # Свежие находки первыми: главный обрабатывает их в этом порядке.
    rows = sorted(merged.values(), key=lambda r: r.get("found_at") or "", reverse=True)[:INBOX_LIMIT]
    with open(INBOX, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Входящие от воркеров: {len(rows)} уникальных лотов из {len(sources)} источников")
    for s in sources:
        print(f"   {s}")
    if not rows:
        print("ℹ️ находок нет — главный процесс отработает по странице выдачи как обычно")


if __name__ == "__main__":
    main()
