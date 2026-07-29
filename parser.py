"""
parser.py — асинхронный парсер товарных страниц.

Транспортный каскад (_fetch_smart):
  1. curl_cffi (AsyncSession, Chrome TLS fingerprint) — обходит Cloudflare/DDoS-Guard
  2. httpx.AsyncClient (fallback с полноценными browser-заголовками)

Парсерный каскад:
  0. Shopify JSON API (/products/*.json) — прямой доступ к метаданным вариантов
  1. JSON-LD (schema.org Product) — наиболее точный микроразметочный источник
  2. Open Graph + CSS-селекторы цены + itemprop
  3. Fallback → Gemini API (анализирует HTML с расширенной инструкцией наличия)

Ключевые правила:
  - НЕТ хардкод-массивов размеров («XS», «M», «L», «XL» и т.д.)
  - Размеры парсятся ТОЛЬКО из видимых DOM-элементов форм покупки
  - Скрытые элементы (type=hidden, display:none, visibility:hidden) строго игнорируются
  - URL очищается от UTM-параметров перед запросом
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from google import genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# ── Браузерные заголовки (Chrome 125) ────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

# Ограничение HTML для Gemini (символов)
_GEMINI_HTML_LIMIT = 35_000

# Паттерн стандартных размеров одежды/обуви (для определения типа варианта)
_SIZE_PATTERN = re.compile(
    r"^(XXS|XS|S|M|L|XL|XXL|XXXL|3XL|4XL|5XL|[2-5]?XL|\d{2,3}(?:[./]\d{1,2})?)$",
    re.IGNORECASE,
)

# UTM и tracking параметры для очистки URL
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "yclid", "_ga", "mc_eid", "vgo_ee",
    "ref", "source", "medium", "campaign", "msclkid", "twclid",
})


@dataclass
class ParseResult:
    title: str
    price: str        # строка: «1 299.00»
    currency: str     # символ или код: «₴», «USD»
    is_in_stock: bool
    source: str       # «shopify_json» | «jsonld» | «css» | «gemini» | «error»
    variants: list[dict] = field(default_factory=list)
    # Каждый вариант: {"id": str, "title": str, "in_stock": bool, "price": str}


# ── Утилита: очистка URL ──────────────────────────────────────────────────────
def clean_url(url: str) -> str:
    """
    Удаляет отслеживающие параметры (utm_*, fbclid, gclid и т.д.),
    сохраняет параметры товара (variant, size, color и т.п.).
    """
    try:
        parsed = urlparse(url.strip())
        params = parse_qsl(parsed.query, keep_blank_values=True)
        clean_params = [
            (k, v) for k, v in params
            if not k.lower().startswith("utm_")
            and k.lower() not in _TRACKING_PARAMS
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


# ── Транспортный слой: умный fetch с антибот-обходом ─────────────────────────
async def _fetch_smart(url: str, timeout: float = 15.0) -> str:
    """
    Скачивает HTML страницы. Каскад:
      1. curl_cffi с Chrome TLS fingerprint (обходит Cloudflare/DDoS-Guard)
      2. httpx.AsyncClient с браузерными заголовками (fallback)

    Выбрасывает исключение если оба метода провалились.
    """
    # ── Шаг 1: curl_cffi (Chrome TLS fingerprint) ─────────────────────────────
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore[import]
        async with AsyncSession(impersonate="chrome124") as session:
            resp = await session.get(
                url,
                headers=_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                logger.debug("[curl_cffi] OK: %s", url)
                return resp.text
            elif resp.status_code in (403, 429, 503):
                logger.warning(
                    "[curl_cffi] Status %s for %s — falling back to httpx",
                    resp.status_code, url
                )
            else:
                resp.raise_for_status()
    except ImportError:
        logger.warning("[curl_cffi] Not installed — using httpx fallback")
    except Exception as exc:
        logger.warning("[curl_cffi] Failed for %s: %s — trying httpx", url, exc)

    # ── Шаг 2: httpx (fallback) ────────────────────────────────────────────────
    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=timeout,
        verify=True,
    ) as client:
        response = await client.get(url)
        logger.info("[httpx] GET %s | Status: %s", url, response.status_code)
        response.raise_for_status()
        return response.text


async def _fetch(url: str) -> str:
    """Публичный псевдоним _fetch_smart для обратной совместимости."""
    return await _fetch_smart(url)


# ── Публичный API ─────────────────────────────────────────────────────────────
async def parse_product(url: str) -> ParseResult:
    """
    Очищает URL, скачивает страницу и последовательно применяет парсеры.
    Всегда возвращает ParseResult — даже при ошибке (source='error').
    """
    target_url = clean_url(url)

    # 0. Прямой запрос к Shopify JSON API (/products/*.json)
    shopify_res = await _parse_shopify_json(target_url)
    if shopify_res:
        # Перекрёстная проверка с HTML: фильтруем «фантомные» варианты
        # (скрытые темой магазина) и корректируем статус наличия
        if shopify_res.variants:
            try:
                html = await _fetch_smart(target_url)
                soup = BeautifulSoup(html, "html.parser")
                shopify_res = _filter_shopify_variants_by_html(shopify_res, soup)
            except Exception as exc:
                logger.warning("[Shopify] HTML cross-ref failed, using API data as-is: %s", exc)
        logger.info(
            "Parsed via Shopify JSON API: %s | visible variants: %d",
            target_url, len(shopify_res.variants)
        )
        return shopify_res

    # 1. Загрузка HTML
    try:
        html = await _fetch_smart(target_url)
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
    embedded_variants, json_snippet = _extract_embedded_variants(soup)

    result = _parse_jsonld(soup)
    if result:
        logger.info("Parsed via JSON-LD: %s", target_url)
        return _apply_variant_info(result, soup, target_url, embedded_variants)

    result = _parse_css(soup, target_url, embedded_variants)
    if result:
        logger.info("Parsed via CSS selectors: %s", target_url)
        return _apply_variant_info(result, soup, target_url, embedded_variants)

    logger.info("Falling back to Gemini for: %s", target_url)
    gemini_result = await _parse_gemini(target_url, html, json_snippet)
    return _apply_variant_info(gemini_result, soup, target_url, embedded_variants)


# ── Проверка видимости DOM-элемента ──────────────────────────────────────────
def _is_hidden(el, max_depth: int = 4) -> bool:
    """
    Возвращает True если элемент или один из его предков скрыт:
      - style="display:none" или style="visibility:hidden"
      - атрибут hidden
      - type="hidden"
    Проверяет до max_depth уровней вверх по дереву.
    """
    def _check_one(node) -> bool:
        if node is None or not hasattr(node, "get"):
            return False
        style = (node.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if node.get("hidden") is not None:
            return True
        if str(node.get("type", "")).lower() == "hidden":
            return True
        return False

    if _check_one(el):
        return True
    parent = getattr(el, "parent", None)
    for _ in range(max_depth):
        if parent is None:
            break
        if _check_one(parent):
            return True
        parent = getattr(parent, "parent", None)
    return False


def _is_disabled_variant(el) -> bool:
    """
    Определяет, является ли элемент варианта недоступным/распроданным.
    Проверяет атрибуты disabled, aria-disabled, CSS-классы и зачёркнутый стиль.
    """
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled") == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    if any(c in classes for c in [
        "disabled", "is-disabled", "sold-out", "soldout",
        "out-of-stock", "outofstock", "unavailable", "no-stock",
        "inactive", "inactive-block", "cross",
    ]):
        return True
    style = (el.get("style") or "").lower()
    if "line-through" in style or "text-decoration:line-through" in style.replace(" ", ""):
        return True
    return False


# ── Фильтрация вариантов Shopify по видимым элементам HTML ───────────────────
def _filter_shopify_variants_by_html(result: ParseResult, soup: BeautifulSoup) -> ParseResult:
    """
    Перекрёстная проверка вариантов Shopify JSON с HTML-разметкой страницы.
    Стратегии (в порядке приоритета):
      0. div.variant-block[title] (Peresvit/custom темы)
      1. radio[type=radio] с name, содержащим size/option/variant
      2. [data-val] / [data-value] элементы
      3. <select name*=size/option>
    Фантомные варианты (присутствующие в JSON, но скрытые в HTML) — удаляются.
    """
    visible: dict[str, bool] = {}   # title_lower -> is_sold_out
    prices:  dict[str, str]  = {}   # title_lower -> price string

    # ── Стратегия 0: div.variant-block[title] (Peresvit и аналогичные темы) ──
    for el in soup.find_all("div", class_="variant-block"):
        if _is_hidden(el):
            continue
        title_attr = (el.get("title") or el.get_text(strip=True)).strip()
        if not title_attr or len(title_attr) > 30:
            continue

        is_sold_out = _is_disabled_variant(el)
        if not is_sold_out:
            classes = " ".join(el.get("class", [])).lower()
            if "active-block" in classes and "inactive-block" not in classes:
                is_sold_out = False
            elif "inactive-block" in classes:
                is_sold_out = True
            else:
                try:
                    is_sold_out = float(el.get("data-count", "0") or "0") <= 0
                except (ValueError, TypeError):
                    is_sold_out = False

        data_price = el.get("data-price") or el.get("data-main-price") or ""
        if data_price:
            try:
                p_float = float(data_price)
                p_str = str(int(p_float // 100)) if p_float > 10000 else str(int(p_float))
                prices[title_attr.lower()] = p_str
            except (ValueError, TypeError):
                pass

        visible[title_attr.lower()] = is_sold_out

    # ── Стратегия 1: radio[type=radio] с name=size/option/variant ────────────
    if not visible:
        for inp in soup.find_all("input", type="radio"):
            if _is_hidden(inp):
                continue
            name_attr = (inp.get("name") or "").lower()
            if not any(k in name_attr for k in ["size", "option", "variant", "attr"]):
                continue
            val = (inp.get("value") or "").strip()
            if not val or len(val) > 30:
                continue

            is_disabled = _is_disabled_variant(inp)
            # Проверяем родителский label/wrapper
            parent = inp.parent
            if parent and hasattr(parent, "get"):
                if _is_disabled_variant(parent):
                    is_disabled = True
                p_style = (parent.get("style") or "").replace(" ", "").lower()
                if "display:none" in p_style:
                    continue  # скрытый контейнер — пропускаем
            visible[val.lower()] = is_disabled

    # ── Стратегия 2: [data-val] / [data-value] ────────────────────────────────
    if not visible:
        for el in soup.select("[data-val], [data-value]"):
            if _is_hidden(el):
                continue
            val = (el.get("data-val") or el.get("data-value") or "").strip()
            if not val or len(val) > 30:
                continue
            visible[val.lower()] = _is_disabled_variant(el)

    # ── Стратегия 3: <select name*=size/option> ───────────────────────────────
    if not visible:
        for sel in soup.find_all("select"):
            if _is_hidden(sel):
                continue
            name = (sel.get("name") or sel.get("id") or "").lower()
            if not any(k in name for k in ["size", "option", "variant", "attr"]):
                continue
            for opt in sel.find_all("option"):
                val = (opt.get("value") or opt.get_text(strip=True)).strip()
                if not val or len(val) > 30:
                    continue
                if any(p in val.lower() for p in ["вибер", "choose", "select", "оберіть", "выберите"]):
                    continue
                visible[val.lower()] = (
                    opt.get("disabled") is not None
                    or any(w in opt.get_text(strip=True).lower() for w in [
                        "немає", "нет", "out of stock", "розпродано", "sold out"
                    ])
                )

    if not visible:
        logger.debug("[Shopify filter] No HTML size elements found, using API data as-is")
        return result

    # ── Фильтрация и коррекция ────────────────────────────────────────────────
    filtered: list[dict] = []
    for v in result.variants:
        key = v["title"].lower()
        if key not in visible:
            logger.debug("[Shopify filter] Dropping phantom variant '%s'", v["title"])
            continue
        v_copy = dict(v)
        if visible[key]:  # is_sold_out == True
            v_copy["in_stock"] = False
        if key in prices and prices[key]:
            v_copy["price"] = prices[key]
        filtered.append(v_copy)

    if not filtered:
        logger.warning("[Shopify filter] Filtering removed all variants, falling back to API data")
        return result

    logger.info(
        "[Shopify filter] %d -> %d variants | %s",
        len(result.variants), len(filtered),
        {k: ("sold-out" if sv else "in-stock") for k, sv in visible.items()},
    )

    in_stock_vs = [v for v in filtered if v["in_stock"]]
    base_price = (in_stock_vs[0]["price"] if in_stock_vs else filtered[0]["price"]) or result.price

    return ParseResult(
        title=result.title,
        price=base_price,
        currency=result.currency,
        is_in_stock=bool(in_stock_vs),
        source=result.source,
        variants=filtered,
    )


# ── Shopify JSON API (/products/<handle>.json) ────────────────────────────────
async def _parse_shopify_json(url: str) -> ParseResult | None:
    """
    Прямой запрос к Shopify JSON API.
    Возвращает None если URL не является Shopify-продуктом или запрос провалился.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path
        if "/products/" not in path:
            return None

        clean_path = path.rstrip("/")
        json_path = clean_path if clean_path.endswith(".json") else f"{clean_path}.json"
        json_url = f"{parsed.scheme}://{parsed.netloc}{json_path}"
        logger.info("[Shopify API] GET %s", json_url)

        try:
            from curl_cffi.requests import AsyncSession  # type: ignore[import]
            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.get(json_url, headers=_HEADERS, timeout=10.0)
                if resp.status_code != 200:
                    raise ValueError(f"Status {resp.status_code}")
                data = resp.json()
        except Exception:
            async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=10.0) as client:
                resp = await client.get(json_url)
                if resp.status_code != 200:
                    return None
                data = resp.json()

        product = data.get("product")
        if not isinstance(product, dict):
            return None

        base_title = str(product.get("title") or "").strip()
        if not base_title:
            return None

        raw_variants = product.get("variants", [])
        variants_list = []

        for v in raw_variants:
            v_id = str(v.get("id") or "").strip()
            v_title = str(v.get("title") or v.get("option1") or "").strip()
            v_available = bool(v.get("available", True))
            v_price = str(v.get("price") or "").strip()
            if "." in v_price:
                v_price = v_price.rstrip("0").rstrip(".")

            if v_title:
                variants_list.append({
                    "id": v_id,
                    "title": v_title,
                    "in_stock": v_available,
                    "price": v_price,
                })

        currency = "₴" if any(d in parsed.netloc for d in [".ua", "peresvit", "ukraine"]) else "$"

        # Проверяем, указан ли конкретный вариант в URL
        query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        target_variant_id = query_params.get("variant") or query_params.get("variant_id") or query_params.get("v")
        target_size = query_params.get("size") or query_params.get("sz") or query_params.get("option")

        matched_v = None
        if target_variant_id:
            for v in variants_list:
                if v["id"] == str(target_variant_id).strip():
                    matched_v = v
                    break

        if not matched_v and target_size:
            target_clean = str(target_size).strip().lower()
            for v in variants_list:
                if (
                    v["title"].lower() == target_clean
                    or f"/{target_clean}" in v["title"].lower()
                    or f" {target_clean}" in v["title"].lower()
                ):
                    matched_v = v
                    break

        if matched_v:
            v_title = matched_v["title"]
            variant_label = f"Размер: {v_title}" if _SIZE_PATTERN.match(v_title) else f"Вариант: {v_title}"
            full_title = f"{base_title} ({variant_label})"
            return ParseResult(
                title=full_title,
                price=matched_v["price"],
                currency=currency,
                is_in_stock=matched_v["in_stock"],
                source="shopify_json",
                variants=[],
            )

        first_price = variants_list[0]["price"] if variants_list else ""
        overall_in_stock = any(v["in_stock"] for v in variants_list)

        return ParseResult(
            title=base_title,
            price=first_price,
            currency=currency,
            is_in_stock=overall_in_stock,
            source="shopify_json",
            variants=variants_list,
        )
    except Exception as exc:
        logger.warning("[Shopify API] Error parsing %s: %s", url, exc)
        return None


# ── Шаг 1: JSON-LD ────────────────────────────────────────────────────────────
def _parse_jsonld(soup: BeautifulSoup) -> ParseResult | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

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
def _parse_css(
    soup: BeautifulSoup,
    url: str = "",
    embedded_variants: list[dict] | None = None,
) -> ParseResult | None:
    title = ""
    if (og := soup.find("meta", property="og:title")):
        title = og.get("content", "").strip()
    if not title and (h1 := soup.find("h1")):
        title = h1.get_text(strip=True)
    if not title and (tag := soup.title):
        title = tag.get_text(strip=True)

    price, currency = _extract_price(soup)
    is_in_stock = _extract_availability(soup, url, embedded_variants)

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


def _extract_availability(
    soup: BeautifulSoup,
    url: str = "",
    embedded_variants: list[dict] | None = None,
) -> bool:
    """Определяет наличие товара с учётом выбранной вариации."""
    if url or embedded_variants:
        _, variant_stock, _ = _detect_variant_and_stock(soup, url, embedded_variants)
        if variant_stock is not None:
            return variant_stock

    avail = soup.find(attrs={"itemprop": "availability"})
    if avail:
        content = (avail.get("content", "") + avail.get_text()).lower()
        if "instock" in content:
            return True
        if "outofstock" in content:
            return False

    buy_buttons = soup.select(
        "button[type='submit'], input[type='submit'], .btn-buy, .buy-button, "
        "[class*='add-to-cart'], [class*='add_to_cart'], [id*='add-to-cart'], "
        "[class*='buy'], [id*='buy'], [name*='add-to-cart']"
    )
    out_stock_words = [
        "розпродано", "out of stock", "немає в наявності", "нет в наличии",
        "sold out", "немає", "не доступно", "not available",
        "уведомить о поступлении", "повідомити про надходження",
    ]

    for btn in buy_buttons:
        btn_text = btn.get_text(strip=True).lower()
        btn_classes = [c.lower() for c in btn.get("class", [])]
        is_btn_disabled = (
            btn.get("disabled") is not None
            or btn.get("aria-disabled") == "true"
            or any(d in btn_classes for d in ["disabled", "is-disabled", "out-of-stock", "sold-out"])
            or any(w in btn_text for w in out_stock_words)
        )
        if not is_btn_disabled and any(kw in btn_text for kw in [
            "купити", "у кошик", "в корзину", "buy", "додати", "заказать", "купить", "add to cart"
        ]):
            return True

    text = soup.get_text(" ", strip=True).lower()
    in_stock_kw = ["в наявності", "є в наявності", "в наличии", "in stock", "available"]
    out_stock_kw = ["немає в наявності", "нет в наличии", "out of stock", "not available", "закінчився", "розпродано"]

    if any(w in text for w in out_stock_kw):
        if any(w in text for w in in_stock_kw):
            return True
        return False

    if any(w in text for w in in_stock_kw):
        return True

    for btn in buy_buttons:
        btn_text = btn.get_text(strip=True).lower()
        if btn.get("disabled") is None and not any(w in btn_text for w in out_stock_words):
            return True

    return False


# ── Извлечение встроенных JSON-данных (Shopify / WooCommerce) ─────────────────
def _extract_embedded_variants(soup: BeautifulSoup | None) -> tuple[list[dict], str]:
    """
    Извлекает данные вариантов товара из внутренних скриптов и атрибутов.

    КРИТИЧНО: Скрипты, находящиеся внутри скрытых элементов (display:none,
    visibility:hidden, type=hidden), полностью ИГНОРИРУЮТСЯ — именно они
    содержат JS-шаблоны с «фантомными» размерами.

    Возвращает: (variants_list, json_snippet_str)
    """
    variants: list[dict] = []
    snippets: list[str] = []

    if not soup:
        return variants, ""

    for script in soup.find_all("script"):
        # ── Строгая проверка: пропускаем скрипты внутри скрытых контейнеров ──
        if _is_hidden(script):
            continue

        stype = (script.get("type") or "").lower()
        sid = (script.get("id") or "").lower()
        stext = script.string or script.get_text() or ""
        if not stext.strip():
            continue

        # Явные JSON-скрипты с данными продукта
        if stype == "application/json" or "product" in sid or "variant" in sid:
            try:
                data = json.loads(stext)
                candidates = data if isinstance(data, list) else [data]
                for item in candidates:
                    if isinstance(item, dict):
                        if "variants" in item and isinstance(item["variants"], list):
                            for v in item["variants"]:
                                norm = _normalize_variant(v, source="shopify")
                                if norm:
                                    variants.append(norm)
                            snippets.append(json.dumps(item["variants"], ensure_ascii=False)[:3000])
                        elif "offers" in item and isinstance(item["offers"], list):
                            for v in item["offers"]:
                                norm = _normalize_variant(v, source="schema")
                                if norm:
                                    variants.append(norm)
                            snippets.append(json.dumps(item["offers"], ensure_ascii=False)[:3000])
            except Exception:
                pass

        # JSON-LD (schema.org)
        if stype == "application/ld+json":
            try:
                data = json.loads(stext)
                candidates = data if isinstance(data, list) else [data]
                for item in candidates:
                    if isinstance(item, dict) and item.get("@graph"):
                        candidates.extend(item["@graph"])
                for item in candidates:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        offers = item.get("offers")
                        if isinstance(offers, list):
                            for off in offers:
                                norm = _normalize_variant(off, source="schema")
                                if norm:
                                    variants.append(norm)
                            snippets.append(json.dumps(offers, ensure_ascii=False)[:3000])
            except Exception:
                pass

        # Встроенные JS-переменные типа: variants: [{...}]
        # ВАЖНО: только если скрипт не является шаблоном (не скрыт)
        if not stype or stype in {"text/javascript", "application/javascript"}:
            # Дополнительная проверка: пропускаем скрипты типа Mustache/Handlebars
            if "{{" in stext or "{%" in stext:
                continue
            var_matches = re.findall(r'"variants"\s*:\s*(\[\s*\{.*?\}\s*\])', stext, re.DOTALL)
            for m in var_matches:
                try:
                    parsed_v = json.loads(m)
                    if isinstance(parsed_v, list):
                        for v in parsed_v:
                            norm = _normalize_variant(v, source="shopify_js")
                            if norm:
                                variants.append(norm)
                        snippets.append(m[:3000])
                except Exception:
                    pass

    # data-product_variations (WooCommerce) — только видимые элементы
    for el in soup.select("[data-product_variations], [data-product-json]"):
        if _is_hidden(el):
            continue
        attr_val = el.get("data-product_variations") or el.get("data-product-json")
        if attr_val:
            try:
                data = json.loads(attr_val)
                if isinstance(data, list):
                    for v in data:
                        norm = _normalize_variant(v, source="woocommerce")
                        if norm:
                            variants.append(norm)
                    snippets.append(json.dumps(data, ensure_ascii=False)[:3000])
                elif isinstance(data, dict) and "variants" in data:
                    for v in data["variants"]:
                        norm = _normalize_variant(v, source="shopify")
                        if norm:
                            variants.append(norm)
                    snippets.append(json.dumps(data["variants"], ensure_ascii=False)[:3000])
            except Exception:
                pass

    # Дедупликация
    unique_variants: list[dict] = []
    seen: set[tuple] = set()
    for v in variants:
        key = (v.get("id"), v.get("title", "").lower(), v.get("sku", "").lower())
        if key not in seen:
            seen.add(key)
            unique_variants.append(v)

    combined_snippet = "\n---\n".join(snippets[:3])
    return unique_variants, combined_snippet


def _normalize_variant(raw: dict, source: str) -> dict | None:
    """Приводит сырые данные варианта к единой структуре."""
    if not isinstance(raw, dict):
        return None

    v_id = str(raw.get("id") or raw.get("variation_id") or raw.get("sku") or "").strip()
    title = str(
        raw.get("title")
        or raw.get("name")
        or raw.get("public_title")
        or raw.get("option1")
        or ""
    ).strip()

    if not title and isinstance(raw.get("attributes"), dict):
        title = " / ".join(str(val) for val in raw["attributes"].values() if val).strip()

    option1 = str(raw.get("option1") or title or "").strip()
    sku = str(raw.get("sku") or "").strip()

    available = True
    if "available" in raw:
        available = bool(raw["available"])
    elif "is_in_stock" in raw:
        available = bool(raw["is_in_stock"]) and bool(raw.get("is_purchasable", True))
    elif "inventory_quantity" in raw:
        available = int(raw["inventory_quantity"] or 0) > 0
    elif "availability" in raw:
        avail_str = str(raw["availability"])
        available = "InStock" in avail_str or "http://schema.org/InStock" in avail_str

    if not title and not v_id and not sku:
        return None

    return {
        "id": v_id,
        "title": title,
        "option1": option1,
        "sku": sku,
        "available": available,
        "price": str(raw.get("price") or raw.get("display_price") or "").strip(),
    }


# ── Парсинг вариантов из видимых DOM-элементов форм ──────────────────────────
def _parse_variants_from_form(soup: BeautifulSoup) -> list[dict]:
    """
    Парсит варианты товара СТРОГО из видимых элементов форм покупки.
    Ищет только в контейнерах: form[action*=cart], .product-form, .variant-picker, .swatches.
    Полностью игнорирует скрытые элементы и JS-шаблоны.

    Возвращает список вариантов: [{"title": str, "in_stock": bool, "price": ""}]
    """
    # Контейнеры формы покупки
    form_containers = soup.select(
        "form[action*='cart'], form[action*='checkout'], "
        "[class*='product-form'], [class*='variant-picker'], "
        "[class*='swatches'], [class*='size-selector'], "
        "[class*='option-selector'], [class*='product-options']"
    )
    if not form_containers:
        form_containers = [soup]

    variants: list[dict] = []
    seen: set[str] = set()

    for container in form_containers:
        # ── 1. Radio inputs (наиболее распространённые) ───────────────────────
        for inp in container.find_all("input", type="radio"):
            if _is_hidden(inp):
                continue
            val = (inp.get("value") or "").strip()
            if not val or len(val) > 30:
                continue
            key = val.lower()
            if key in seen:
                continue
            seen.add(key)

            is_disabled = _is_disabled_variant(inp)
            # Проверяем label-обёртку
            parent = inp.parent
            if parent and hasattr(parent, "get"):
                if _is_disabled_variant(parent):
                    is_disabled = True
                if _is_hidden(parent):
                    seen.discard(key)
                    continue

            variants.append({"title": val, "in_stock": not is_disabled, "price": ""})

        # ── 2. [data-value] / [data-option-value] кнопки ─────────────────────
        for el in container.select(
            "[data-value], [data-option-value], "
            "[class*='swatch-element'], [class*='variant-item'], "
            "[class*='size-item'], [class*='size-swatch']"
        ):
            if _is_hidden(el):
                continue
            val = (
                el.get("data-value")
                or el.get("data-option-value")
                or el.get_text(strip=True)
            ).strip()
            if not val or len(val) > 30:
                continue
            key = val.lower()
            if key in seen:
                continue
            seen.add(key)
            variants.append({"title": val, "in_stock": not _is_disabled_variant(el), "price": ""})

        # ── 3. <select> выпадающие списки ────────────────────────────────────
        for sel in container.find_all("select"):
            if _is_hidden(sel):
                continue
            name = (sel.get("name") or sel.get("id") or "").lower()
            # Игнорируем не-вариантные select (кол-во, цвет фона и т.д.)
            if name and not any(k in name for k in [
                "size", "option", "variant", "attr", "colour", "color"
            ]):
                continue
            for opt in sel.find_all("option"):
                if _is_hidden(opt):
                    continue
                val = (opt.get("value") or opt.get_text(strip=True)).strip()
                if not val or len(val) > 30:
                    continue
                if any(p in val.lower() for p in [
                    "вибер", "choose", "select", "оберіть", "выберите", "---"
                ]):
                    continue
                key = val.lower()
                if key in seen:
                    continue
                seen.add(key)

                is_disabled = (
                    opt.get("disabled") is not None
                    or any(w in val.lower() for w in [
                        "немає", "нет", "out of stock", "розпродано", "sold out"
                    ])
                )
                clean_val = re.sub(
                    r"\s*\((?:немає|нет|out of stock|розпродано)[^)]*\)", "",
                    val, flags=re.IGNORECASE
                ).strip()
                if clean_val:
                    variants.append({"title": clean_val, "in_stock": not is_disabled, "price": ""})

        # Если нашли варианты в первом подходящем контейнере — стоп
        if variants:
            break

    return variants


# ── Вариативность товаров ─────────────────────────────────────────────────────
def _detect_variant_and_stock(
    soup: BeautifulSoup | None,
    url: str,
    embedded_variants: list[dict] | None = None,
) -> tuple[str | None, bool | None, list[dict]]:
    """
    Анализирует URL, HTML и извлечённый JSON вариантов для определения
    выбранного размера/цвета/опции, его доступности и списка всех вариантов.

    Возвращает: (variant_label, variant_is_in_stock, all_variants_list)
    """
    variant_name = None
    variant_label = None
    variant_disabled = False
    all_variants: list[dict] = []
    seen_variants: set[str] = set()

    sku_pattern = re.compile(r"\b[A-Za-z0-9]+-[A-Za-z0-9]+-([A-Za-z0-9]+)\b")

    if embedded_variants is None and soup:
        embedded_variants, _ = _extract_embedded_variants(soup)

    # Собираем all_variants из embedded_variants (JSON из скриптов)
    if embedded_variants:
        for v in embedded_variants:
            v_title = v.get("title") or v.get("option1") or ""
            if not v_title:
                continue
            key = v_title.lower()
            if key not in seen_variants:
                seen_variants.add(key)
                all_variants.append({
                    "id": str(v.get("id") or v_title),
                    "title": v_title,
                    "in_stock": bool(v.get("available", True)),
                    "price": str(v.get("price") or "").strip(),
                })

    # Если в JSON ничего нет — парсим форму напрямую из DOM
    if not all_variants and soup:
        form_variants = _parse_variants_from_form(soup)
        for fv in form_variants:
            key = fv["title"].lower()
            if key not in seen_variants:
                seen_variants.add(key)
                all_variants.append({
                    "id": fv["title"],
                    "title": fv["title"],
                    "in_stock": fv["in_stock"],
                    "price": fv.get("price", ""),
                })

    # Определяем выбранный вариант по URL-параметрам
    if url:
        try:
            parsed_url = urlparse(url)
            query_params = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
            for k, v in query_params.items():
                key_lower = k.lower()
                v_clean = v.strip()
                if not v_clean:
                    continue
                if key_lower in {"size", "sz", "sizes", "waist", "length"}:
                    variant_name = v_clean
                    variant_label = f"Размер: {v_clean}"
                    break
                elif key_lower in {"color", "colour", "col", "shade"}:
                    variant_name = v_clean
                    variant_label = f"Цвет: {v_clean}"
                    break
                elif key_lower in {"variant", "variant_id", "option", "sku", "v"}:
                    sku_match = sku_pattern.search(v_clean)
                    if sku_match:
                        extracted = sku_match.group(1)
                        if _SIZE_PATTERN.match(extracted):
                            variant_name = extracted
                            variant_label = f"Размер: {extracted}"
                        else:
                            variant_name = v_clean
                            variant_label = f"Вариант: {v_clean}"
                    elif _SIZE_PATTERN.match(v_clean):
                        variant_name = v_clean
                        variant_label = f"Размер: {v_clean}"
                    else:
                        variant_name = v_clean
                        variant_label = f"Вариант: {v_clean}"
                    break
        except Exception as exc:
            logger.warning("Could not parse URL query for variants: %s", exc)

    # Сопоставляем вариант из URL с данными из embedded_variants
    if embedded_variants and (target_term := (variant_name or "").strip().lower()):
        matched_v = None
        for v in embedded_variants:
            v_id = v["id"].lower()
            v_sku = v.get("sku", "").lower()
            v_title = v["title"].lower()
            v_opt1 = v.get("option1", "").lower()
            if (
                target_term == v_id
                or target_term == v_sku
                or target_term == v_title
                or target_term == v_opt1
                or f"-{target_term}" in v_sku
                or f"/{target_term}" in v_title
                or f" {target_term}" in v_title
            ):
                matched_v = v
                break

        if matched_v:
            clean_title = matched_v["title"]
            variant_label = f"Размер: {clean_title}" if _SIZE_PATTERN.match(clean_title) else f"Вариант: {clean_title}"
            return variant_label, matched_v["available"], []

    # Если URL не указывает вариант — анализируем общую доступность
    if not variant_name and all_variants:
        avail = [v["title"] for v in all_variants if v["in_stock"]]
        oos = [v["title"] for v in all_variants if not v["in_stock"]]
        if not avail and oos:
            return None, False, all_variants
        elif avail and oos:
            return None, True, all_variants
        elif avail:
            return None, True, all_variants

    # Определяем активный (выбранный) вариант из HTML
    if not variant_label and soup:
        active_selectors = [
            ".selected", ".active", ".checked",
            "[aria-checked='true']", "[data-selected='true']",
            "input[type='radio']:checked",
        ]
        for sel_str in active_selectors:
            elements = soup.select(sel_str)
            for el in elements:
                if _is_hidden(el):
                    continue
                parent = el.find_parent(attrs={"class": re.compile(
                    r"size|variant|option|color|sku|attr|swatch", re.I
                )})
                el_classes = " ".join(el.get("class", [])).lower()
                is_attr_elem = parent is not None or any(
                    k in el_classes for k in ["size", "variant", "option", "color", "sku", "swatch"]
                )
                if not is_attr_elem:
                    continue

                text = el.get_text(strip=True) or el.get("data-value", "").strip() or el.get("title", "").strip()
                if not text or len(text) > 30:
                    continue

                classes = [c.lower() for c in el.get("class", [])]
                if parent:
                    classes.extend([c.lower() for c in parent.get("class", [])])

                el_disabled = (
                    el.get("disabled") is not None
                    or el.get("aria-disabled") == "true"
                    or any(d in classes for d in [
                        "disabled", "is-disabled", "out-of-stock", "sold-out",
                        "unavailable", "line-through", "cross", "no-stock",
                    ])
                )

                variant_label = f"Размер: {text}" if _SIZE_PATTERN.match(text) else f"Вариант: {text}"
                variant_name = text
                if el_disabled:
                    variant_disabled = True
                break
            if variant_label:
                break

    if variant_disabled:
        return variant_label, False, all_variants

    # Финальный анализ кнопок «Купить»
    variant_stock_status = None
    if soup:
        buy_buttons = soup.select(
            "button[type='submit'], input[type='submit'], .btn-buy, .buy-button, "
            "[class*='add-to-cart'], [class*='add_to_cart'], [id*='add-to-cart'], "
            "[class*='buy'], [id*='buy'], [name*='add-to-cart']"
        )
        out_stock_words = [
            "розпродано", "out of stock", "немає в наявності", "нет в наличии",
            "sold out", "немає", "не доступно", "not available",
            "уведомить о поступлении", "повідомити про надходження",
        ]

        has_active_buy_button = False
        has_disabled_buy_button = False

        for btn in buy_buttons:
            btn_text = btn.get_text(strip=True).lower()
            btn_classes = [c.lower() for c in btn.get("class", [])]
            is_btn_disabled = (
                btn.get("disabled") is not None
                or btn.get("aria-disabled") == "true"
                or any(d in btn_classes for d in ["disabled", "is-disabled", "out-of-stock", "sold-out"])
                or any(w in btn_text for w in out_stock_words)
            )
            if is_btn_disabled:
                has_disabled_buy_button = True
            elif any(kw in btn_text for kw in [
                "купити", "у кошик", "в корзину", "buy", "додати", "заказать", "купить", "add to cart"
            ]):
                has_active_buy_button = True

        if has_disabled_buy_button and not has_active_buy_button:
            variant_stock_status = False
        elif has_active_buy_button:
            variant_stock_status = True

    return variant_label, variant_stock_status, all_variants


def _apply_variant_info(
    result: ParseResult | None,
    soup: BeautifulSoup,
    url: str,
    embedded_variants: list[dict] | None = None,
) -> ParseResult | None:
    """
    Подставляет выбранную опцию в название товара и строжайше проверяет
    наличие выбранного варианта. Список вариантов берётся из HTML/JSON.
    """
    if not result:
        return result

    variant_label, variant_stock, all_variants = _detect_variant_and_stock(
        soup, url, embedded_variants
    )

    title = result.title
    if variant_label and variant_label not in title:
        title = f"{title} ({variant_label})"

    is_in_stock = result.is_in_stock
    if variant_stock is not None:
        is_in_stock = variant_stock

    return ParseResult(
        title=title,
        price=result.price,
        currency=result.currency,
        is_in_stock=is_in_stock,
        source=result.source,
        variants=all_variants,
    )


# ── Шаг 3: Gemini API fallback ────────────────────────────────────────────────
async def _parse_gemini(url: str, html: str, json_snippet: str = "") -> ParseResult:
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("GEMINI_API_KEY не задан, фолбэк невозможен")
        return ParseResult("", "", "", False, "error")

    truncated = html[:_GEMINI_HTML_LIMIT]
    prompt_parts = [
        "Ты — эксперт по анализу карточек товаров.",
        "1. Найди выбранную комбинацию (размер, цвет, объем) по URL или активным элементам страницы.",
        "2. Проанализируй список всех вариантов размера (variants).",
        "   Если в URL или параметрах не указан конкретный variant_id, проверь статус каждого размера.",
        "   Если выбран конкретный размер (например, M) и в JSON у него available: false, возвращай in_stock = false.",
        "3. Добавь найденный вариант в скобках к названию товара.\n",
        f"URL товара: {url}",
    ]

    if json_snippet:
        prompt_parts.append(f"Найденные структурированные данные вариантов (JSON):\n{json_snippet}\n")

    prompt_parts.append(
        "Верни ИСКЛЮЧИТЕЛЬНО валидный JSON-объект в формате:\n"
        "{\n"
        '  "title": "название товара (Размер: M)",\n'
        '  "price": "цена строкой, например 1299",\n'
        '  "currency": "валюта, например ₴ или UAH",\n'
        '  "is_in_stock": true/false\n'
        "}\n\n"
        f"HTML фрагмент:\n{truncated}"
    )

    prompt = "\n".join(prompt_parts)

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
