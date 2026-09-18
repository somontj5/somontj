"""Run scraper.py with a second Gemini key as a transparent fallback.

The existing scraper remains unchanged: failed Gemini requests are retried
with GEMINI_API_KEY_2 before the scraper's normal retry logic continues.
"""
import os

import requests


_original_post = requests.post
_secondary_key = os.environ.get("GEMINI_API_KEY_2", "").strip()
_secondary_url = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-3.6-flash:generateContent?key={_secondary_key}"
    if _secondary_key
    else ""
)


def _post_with_fallback(url, *args, **kwargs):
    """Try the primary Gemini request, then the secondary key on failure."""
    is_gemini = "generativelanguage.googleapis.com" in url and "key=" in url
    if not is_gemini or not _secondary_url:
        return _original_post(url, *args, **kwargs)

    try:
        primary_response = _original_post(url, *args, **kwargs)
    except requests.RequestException as primary_error:
        try:
            secondary_response = _original_post(_secondary_url, *args, **kwargs)
            print("Gemini: использован резервный API-ключ после ошибки первого ключа")
            return secondary_response
        except requests.RequestException as secondary_error:
            print("Gemini: оба API-ключа недоступны:", secondary_error)
            raise primary_error

    if primary_response.ok:
        return primary_response

    try:
        secondary_response = _original_post(_secondary_url, *args, **kwargs)
        if secondary_response.ok:
            print("Gemini: использован резервный API-ключ после ошибки первого ключа")
            return secondary_response
        print("Gemini: резервный ключ также вернул ошибку", secondary_response.status_code)
    except requests.RequestException as secondary_error:
        print("Gemini: ошибка резервного API-ключа:", secondary_error)

    # Return the primary response so scraper.py keeps its existing retry behavior.
    return primary_response


requests.post = _post_with_fallback

import scraper  # noqa: E402

if __name__ == "__main__":
    scraper.main()
