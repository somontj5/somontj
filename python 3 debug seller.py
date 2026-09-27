"""
Диагностика: смотрим, что РЕАЛЬНО присылает сервер боту (requests, без JS,
без cookies) для блока с продавцом — и сравниваем с тем, что видно в браузере.

Запускать на сервере, где крутится сам scraper.py (там есть интернет):
    python3 debug_seller.py

Ничего не пишет в базу и не трогает Telegram — только печатает в консоль.
"""
import re
import requests
from bs4 import BeautifulSoup

# Тот же самый URL, что в вашем скриншоте/файле с Samsung Galaxy S24 Ultra
AD_URL = "https://m.somon.tj/adv/17211581_samsung-galaxsy-s24-ultra-512-g/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

print(f"Запрашиваю (как это делает scraper.py, без cookies/сессии): {AD_URL}\n")
resp = requests.get(AD_URL, headers=HEADERS, timeout=20)
print("HTTP статус:", resp.status_code)
print("Длина ответа (символов):", len(resp.text))

soup = BeautifulSoup(resp.text, "html.parser")

idx = resp.text.find("all_adverts_author")
print("\n--- Сырой кусок вокруг блока продавца (что реально прислал сервер) ---")
if idx == -1:
    print("Блок 'all_adverts_author' в ответе НЕ НАЙДЕН вообще — сервер, похоже,"
          " прислал урезанную/иную страницу без этого блока.")
else:
    print(resp.text[max(0, idx - 200): idx + 500])

title_el = soup.find(class_=re.compile(r"^Contacts_title"))
print("\nContacts_title текст:", title_el.get_text(strip=True) if title_el else "(не найден)")

author_link = soup.find("a", attrs={"data-marker": re.compile(r"^advert-all_adverts_author-advert:")})
if author_link:
    print("author_link aria-label:", author_link.get("aria-label"))
    print("author_link href:", author_link.get("href"))
else:
    print("author_link (по data-marker) не найден.")

# заодно сохраним сырой ответ целиком — если что, можно прислать файл
with open("raw_response.html", "w", encoding="utf-8") as f:
    f.write(resp.text)
print("\nПолный сырой ответ сохранён в raw_response.html — можно прислать этот файл,"
      " если в консоли не видно ничего полезного.")
