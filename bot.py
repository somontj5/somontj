# -*- coding: utf-8 -*-
"""Постоянный слушатель команд Telegram.

Почему он нужен
---------------
Раньше команды обрабатывались только внутри прогона scraper.py: один вызов
getUpdates раз в несколько минут. Пока прогон не случился, бот не реагировал —
и выглядел как «не слушает». Этот процесс работает непрерывно и забирает команды
через длинный опрос (getUpdates с timeout=50): ответ приходит сразу после нажатия.

Как устроен
-----------
Обработчики не дублируются: вызывается та же функция scraper.check_telegram_commands,
что и в прогоне, поэтому все команды, кнопки и пороги работают одинаково.
В прогоне главного процесса опрос отключён (SKIP_TELEGRAM_POLL=1), иначе два
процесса делили бы один offset и часть команд терялась.

Смещение (offset) сохраняется на диск, а в репозиторий уходит раз в
BOT_COMMIT_EVERY секунд, чтобы не засорять историю коммитами.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as sc

MAX_RUNTIME = float(os.environ.get("BOT_MAX_RUNTIME", "20400"))     # 5 ч 40 мин
COMMIT_EVERY = float(os.environ.get("BOT_COMMIT_EVERY", "300"))     # 5 минут
PAUSE = float(os.environ.get("BOT_POLL_PAUSE", "1"))
SKIP_FILES = os.environ.get("BOT_SKIP_FILES", "1") == "1"


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip()[:300])
    if r.returncode != 0 and r.stderr.strip():
        print(f"⚠️ {cmd} -> {r.stderr.strip()[:250]}")
    return r.returncode


def save_state():
    """Сохранить смещение и список команд в репозиторий.

    Прогон главного процесса тоже коммитит состояние, поэтому перед пушем делаем
    rebase с автостешем, как в основном скрипте.
    """
    files = [sc.OFFSET_FILE, sc.SUBSCRIBERS_FILE, sc.SEARCHES_FILE, sc.BOT_SETUP_FILE]
    if SKIP_FILES:
        files = [sc.OFFSET_FILE, sc.BOT_SETUP_FILE]
    existing = [f for f in files if os.path.exists(f)]
    if not existing:
        return
    run("git add " + " ".join(existing))
    if run("git diff --cached --quiet") == 0:
        return
    run('git commit -q -m "bot: состояние слушателя"')
    for i in (1, 2, 3):
        if run("git pull --rebase --autostash") == 0 and run("git push") == 0:
            print(f"💾 состояние слушателя сохранено (попытка {i})")
            return
        if os.path.exists(".git/rebase-merge") or os.path.exists(".git/rebase-apply"):
            run("git rebase --abort")
        time.sleep(5 * i)
    print("⚠️ не удалось сохранить состояние слушателя — продолжу без коммита")


def main():
    started, loops, last_commit = time.time(), 0, time.time()
    print(f"=== Слушатель Telegram запущен (длинный опрос, потолок {MAX_RUNTIME / 60:.0f} мин) ===")
    sc.register_bot_commands(force=True)
    while time.time() - started < MAX_RUNTIME:
        loops += 1
        try:
            searches, subs = sc.check_telegram_commands(
                sc.load_json(sc.SEARCHES_FILE, []), sc.load_subscribers())
        except Exception as e:
            print(f"⚠️ цикл {loops}: {type(e).__name__}: {str(e)[:200]}")
            time.sleep(5)
            continue
        if time.time() - last_commit >= COMMIT_EVERY:
            save_state()
            last_commit = time.time()
        time.sleep(PAUSE)
    print(f"=== Слушатель завершает работу: {loops} циклов ===")
    save_state()


if __name__ == "__main__":
    main()
