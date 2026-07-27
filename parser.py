"""
parser.py — асинхронный парсер товарных страниц.

Стратегия (каскад):
  1. JSON-LD (schema.org Product) — наиболее точный источник
  2. Open Graph + CSS-селекторы цены + itemprop / наличие вариаций
  3. Фолбэк → Gemini API (анализирует HTML с расширенной инструкцией наличия)
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from google import genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Полноценные маскировочные браузерные заголовки
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Ограничение HTML для Gemini (символов)
_GEMINI_HTML_LIMIT = 35_000


@dataclass
class ParseResult:
    title: str
    price: str        # строка: "1 299.00"
    currency: str     # символ или код: "₴", "USD"
    is_in_stock: bool
    source: str       # "jsonld" | "css" | "gemini" | "error"


def clean_url(url: str) -> str:
    """
    Очищает ссылку от тяжелых отслеживающих меток (utm_*, fbclid, gclid и т.д.),
    оставляя чистый URL товара.
    """
    try:
        parsed = urlparse(url)
        params = parse_qsl(parsed.query, keep_blank_values=True)
        clean_params = [
            (k, v) for k, v in params
            if not k.lower().startswith("utm_")
            and k.lower() not in {"fbclid", "gclid", "yclid", "_ga", "mc_eid", "vgo_ee"}
        ]
        cleaned_query = urlencode(clean_params)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            cleaned_query,
            parsed.fragment,
        ))
    except Exception as exc:
        logger.warning("Could not clean URL %s: %s", url, exc)
        return url


# ── Публичный API ─────────────────────────────────────────────────────────────
async def parse_product(url: str) -> ParseResult:
    """
    Очищает URL, скачивает страницу и последовательно применяет парсеры.
    Всегда возвращает ParseResult — даже при ошибке (source='error').
    """
    target_url = clean_url(url)
    try:
        html = await _fetch(target_url)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "[Parse Error] HTTP status %s for URL %s: %s",
            exc.response.status_code, target_url, exc
        )
        return ParseResult("", "", "", False, "error")
    except Exception as exc:
        logger.warning("[Parse Error] Fetch failed for URL %s: %s", target_url, exc)
        return ParseResult("", "", "", False, "error")

    soup = BeautifulSoup(html, "html.parser")

    result = _parse_jsonld(soup)
    if result:
        logger.info("Parsed via JSON-LD: %s", target_url)
        return result

    result = _parse_css(soup)
    if result:
        logger.info("Parsed via CSS selectors: %s", target_url)
        return result

    logger.info("Falling back to Gemini for: %s", target_url)
    return await _parse_gemini(target_url, html)


# ── Шаг 1: JSON-LD ────────────────────────────────────────────────────────────
def _parse_jsonld(soup: BeautifulSoup) -> ParseResult | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Поддержка @graph-обёрток
        candidates = raw if isinstance(raw, list) else [raw]
        for item in candidates:
            if isinstance(item, dict) and item.get("@graph"):
                candidates = item["@graph"]
                break

        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            title = item.get("name", "").strip()
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            price_raw = str(offers.get("price", "")).strip()
            currency = str(offers.get("priceCurrency", "")).strip()
            availability = str(offers.get("availability", ""))
            is_in_stock = "InStock" in availability or "http://schema.org/InStock" in availability

            if title and price_raw:
                return ParseResult(title, price_raw, currency, is_in_stock, "jsonld")

    return None


# ── Шаг 2: CSS / itemprop / Open Graph ───────────────────────────────────────
def _parse_css(soup: BeautifulSoup) -> ParseResult | None:
    # --- Название ---
    title = ""
    if (og := soup.find("meta", property="og:title")):
        title = og.get("content", "").strip()
    if not title and (h1 := soup.find("h1")):
        title = h1.get_text(strip=True)
    if not title and (tag := soup.title):
        title = tag.get_text(strip=True)

    # --- Цена ---
    price, currency = _extract_price(soup)

    # --- Наличие ---
    is_in_stock = _extract_availability(soup)

    if title and price:
        return ParseResult(title, price, currency, is_in_stock, "css")
    return None


def _extract_price(soup: BeautifulSoup) -> tuple[str, str]:
    """Ищет цену через itemprop, data-атрибуты и CSS-классы."""
    price_selectors = [
        "[itemprop='price']",
        "[itemprop='offerPrice']",
        "[data-price]",
        "[class*='product-price']",
        "[class*='offer-price']",
        "[class*='current-price']",
        "[class*='sale-price']",
        ".price__current",
        ".price-value",
        ".price",
        "#price",
    ]
    currency_re = re.compile(r"[₴$€£¥₽]|UAH|USD|EUR|GBP|RUB|грн", re.IGNORECASE)
    digit_re = re.compile(r"[\d\s,.]+")

    for sel in price_selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        # Приоритет: content / data-price → текст элемента
        raw = (
            el.get("content")
            or el.get("data-price")
            or el.get_text(strip=True)
        )
        if not raw:
            continue
        nums = digit_re.findall(raw)
        if not nums:
            continue
        price = nums[0].strip().replace(" ", "")
        cur_match = currency_re.search(raw)
        currency = cur_match.group() if cur_match else ""
        if price:
            return price, currency

    return "", ""


def _extract_availability(soup: BeautifulSoup) -> bool:
    """
    Продвинутое определение наличия товара с учетом вариаций размеров и активных кнопок.
    """
    # 1. Проверка микроразметки schema.org
    avail = soup.find(attrs={"itemprop": "availability"})
    if avail:
        content = (avail.get("content", "") + avail.get_text()).lower()
        if "instock" in content:
            return True
        if "outofstock" in content:
            return False

    # 2. Поиск активных кнопок покупки ('Купити', 'У кошик', 'В корзину', 'Buy')
    buy_buttons = soup.select(
        "button[type='submit'], input[type='submit'], .btn-buy, .buy-button, "
        "[class*='add-to-cart'], [class*='add_to_cart'], [id*='add-to-cart'], "
        "[class*='buy'], [id*='buy'], [name*='add-to-cart']"
    )
    for btn in buy_buttons:
        if btn.get("disabled") is not None or "disabled" in btn.get("class", []):
            continue
        btn_text = btn.get_text(strip=True).lower()
        if any(kw in btn_text for kw in ["купити", "у кошик", "в корзину", "buy", "додати"]):
            return True

    # 3. Наличие выпадающего списка с доступными размерами/опциями
    selects = soup.find_all("select")
    for select in selects:
        options = select.find_all("option")
        for opt in options:
            val = opt.get_text(strip=True).lower()
            if val and not any(dis in val for dis in ["немає", "нет", "out of stock", "disabled"]):
                return True

    # 4. Текстовые ключевые слова
    text = soup.get_text(" ", strip=True).lower()
    in_stock_kw = ["в наявності", "є в наявності", "в наличии", "in stock", "available"]
    out_stock_kw = ["немає в наявності", "нет в наличии", "out of stock", "not available", "закінчився", "розпродано"]

    if any(w in text for w in out_stock_kw):
        if any(w in text for w in in_stock_kw):
            return True
        return False

    if any(w in text for w in in_stock_kw):
        return True

    # Если есть активная форма покупки — по умолчанию в наличии
    if buy_buttons:
        return True

    return False


# ── Шаг 3: Gemini API fallback ────────────────────────────────────────────────
async def _parse_gemini(url: str, html: str) -> ParseResult:
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("GEMINI_API_KEY не задан, фолбэк невозможен")
        return ParseResult("", "", "", False, "error")

    truncated = html[:_GEMINI_HTML_LIMIT]
    prompt = (
        "Ты — эксперт по парсингу товаров. Тщательно проанализируй HTML страницы.\n"
        "Правила определения наличия:\n"
        "1. Если на странице есть активная кнопка 'Купити', 'У кошик', 'В корзину', 'Buy' или выпадающий список с хотя бы ОДНИМ доступным размером — товар СЧИТАЕТСЯ В НАЛИЧИИ (is_in_stock = true).\n"
        "2. Проверяй микроразметку schema.org (http://schema.org/InStock).\n"
        "3. Статус 'out_of_stock' (is_in_stock = false) ставить ТОЛЬКО если везде явно написано 'Немає в наявності', 'Закінчився' или кнопка покупки заблокирована для всех вариаций.\n\n"
        f"URL товара: {url}\n"
        "Верни ИСКЛЮЧИТЕЛЬНО валидный JSON-объект в формате:\n"
        "{\n"
        '  "title": "название товара",\n'
        '  "price": "цена строкой, например 1299",\n'
        '  "currency": "валюта, например ₴ или UAH",\n'
        '  "is_in_stock": true/false\n'
        "}\n\n"
        f"HTML фрагмент:\n{truncated}"
    )

    try:
        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        return ParseResult(
            title=str(data.get("title") or "").strip(),
            price=str(data.get("price") or "").strip(),
            currency=str(data.get("currency") or "").strip(),
            is_in_stock=bool(data.get("is_in_stock", False)),
            source="gemini",
        )
    except Exception as exc:
        logger.error("Gemini parse error for %s: %s", url, exc)
        return ParseResult("", "", "", False, "error")


# ── Утилита: скачивание страницы ──────────────────────────────────────────────
async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        response = await client.get(url)
        logger.info("[HTTP Fetch] GET %s | Status: %s", url, response.status_code)
        response.raise_for_status()
        return response.text
