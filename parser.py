"""
parser.py — асинхронный парсер товарных страниц.

Стратегия (каскад):
  1. JSON-LD (schema.org Product) — наиболее точный источник
  2. Open Graph + CSS-селекторы цены + itemprop
  3. Фолбэк → Gemini API (анализирует усечённый HTML и возвращает JSON)
"""

import json
import logging
import os
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from google import genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Браузерные заголовки — снижают вероятность блокировки
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Ограничение HTML для Gemini (символов) — чтобы уложиться в контекст
_GEMINI_HTML_LIMIT = 35_000


@dataclass
class ParseResult:
    title: str
    price: str        # строка: "1 299.00"
    currency: str     # символ или код: "₴", "USD"
    is_in_stock: bool
    source: str       # "jsonld" | "css" | "gemini" | "error"


# ── Публичный API ─────────────────────────────────────────────────────────────
async def parse_product(url: str) -> ParseResult:
    """
    Скачивает страницу и последовательно применяет парсеры.
    Всегда возвращает ParseResult — даже при ошибке (source='error').
    """
    try:
        html = await _fetch(url)
    except Exception as exc:
        logger.warning("Fetch error for %s: %s", url, exc)
        return ParseResult("", "", "", False, "error")

    soup = BeautifulSoup(html, "html.parser")

    result = _parse_jsonld(soup)
    if result:
        logger.info("Parsed via JSON-LD: %s", url)
        return result

    result = _parse_css(soup)
    if result:
        logger.info("Parsed via CSS selectors: %s", url)
        return result

    logger.info("Falling back to Gemini for: %s", url)
    return await _parse_gemini(url, html)


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
            availability = offers.get("availability", "")
            is_in_stock = "InStock" in str(availability)

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
    # itemprop="availability"
    avail = soup.find(attrs={"itemprop": "availability"})
    if avail:
        content = (avail.get("content", "") + avail.get_text()).lower()
        return "instock" in content

    text = soup.get_text(" ", strip=True).lower()
    in_stock_kw = ["в наявності", "є в наявності", "в наличии", "in stock", "available"]
    out_stock_kw = ["немає в наявності", "нет в наличии", "out of stock", "not available", "розпродано"]
    if any(w in text for w in in_stock_kw):
        return True
    if any(w in text for w in out_stock_kw):
        return False
    return False


# ── Шаг 3: Gemini API fallback ────────────────────────────────────────────────
async def _parse_gemini(url: str, html: str) -> ParseResult:
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("GEMINI_API_KEY не задан, фолбэк невозможен")
        return ParseResult("", "", "", False, "error")


    truncated = html[:_GEMINI_HTML_LIMIT]
    prompt = (
        f"You are a web scraper. Analyze this HTML from a product page ({url}).\n"
        "Return ONLY a valid JSON object (no markdown, no code blocks) with:\n"
        '  "title": product name (string),\n'
        '  "price": price as string (e.g. "1299.99"), null if not found,\n'
        '  "currency": currency symbol/code (e.g. "₴", "USD"), null if not found,\n'
        '  "is_in_stock": boolean (true = available)\n\n'
        f"HTML:\n{truncated}"
    )

    try:
        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(

            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text.strip()
        # Убираем возможные markdown-обёртки
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
        timeout=20,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text
