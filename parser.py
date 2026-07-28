"""
parser.py — асинхронный парсер товарных страниц.

Стратегия (каскад):
  0. Shopify JSON API (/products/*.json) — прямой доступ к метаданным вариантов
  1. JSON-LD (schema.org Product) — наиболее точный микроразметочный источник
  2. Open Graph + CSS-селекторы цены + itemprop / наличие вариаций
  3. Фолбэк → Gemini API (анализирует HTML с расширенной инструкцией наличия)
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
    source: str       # "shopify_json" | "jsonld" | "css" | "gemini" | "error"
    variants: list[dict] = field(default_factory=list)


def clean_url(url: str) -> str:
    """
    Очищает ссылку от тяжелых отслеживающих меток (utm_*, fbclid, gclid и т.д.),
    оставляя чистый URL товара с параметрами вариантов.
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

    # 0. Прямой запрос к Shopify JSON API (/products/*.json)
    shopify_res = await _parse_shopify_json(target_url)
    if shopify_res:
        # Если вернулись варианты — перекрёстно проверяем с HTML страницы.
        # Это отфильтровывает "фантомные" варианты (XS, XXL), которые есть в
        # Shopify-бэкенде, но скрыты темой магазина, и корректирует статус
        # наличия по визуальным маркерам "Розпродано" / "Sold out" в HTML.
        if shopify_res.variants:
            try:
                html = await _fetch(target_url)
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


# ── Фильтрация вариантов Shopify по видимым элементам HTML ───────────────────
def _filter_shopify_variants_by_html(result: ParseResult, soup: BeautifulSoup) -> ParseResult:
    """
    Перекрёстная проверка вариантов Shopify JSON с HTML-разметкой страницы.

    Решает две проблемы:
    1. Phantom variants — Shopify хранит XS/XXL в бэкенде, но тема их скрывает.
       Функция собирает только ВИДИМЫЕ кнопки размеров из HTML и отфильтровывает
       варианты, которых в HTML нет.
    2. Sold-out override — Shopify API может отдавать available=true, когда на сайте
       кнопка уже имеет класс "sold-out" / "disabled" / зачёркнута. HTML-состояние
       считается приоритетным.

    Возвращает исходный result без изменений, если в HTML не найдено ни одного
    видимого элемента выбора размера.
    """
    # Словарь: title_lower -> is_sold_out (bool)
    visible: dict[str, bool] = {}

    # ── Стратегия 1: radio-inputs с name="Size" / "option[0]" и т.д. ─────────
    for inp in soup.find_all("input", type="radio"):
        name_attr = (inp.get("name") or "").lower()
        if not any(k in name_attr for k in ["size", "option", "variant"]):
            continue
        val = (inp.get("value") or "").strip()
        if not val or len(val) > 30:
            continue
        # Скрытые radio — пропускаем
        style = (inp.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or inp.get("hidden") is not None:
            continue

        is_disabled = inp.get("disabled") is not None
        # Проверяем родительский label/div на классы sold-out
        parent = inp.parent
        if parent and hasattr(parent, "get"):
            p_classes = " ".join(parent.get("class", [])).lower()
            if any(c in p_classes for c in [
                "sold-out", "soldout", "is-unavailable", "unavailable",
                "disabled", "is-disabled", "no-stock", "out-of-stock"
            ]):
                is_disabled = True
            p_style = (parent.get("style") or "").replace(" ", "").lower()
            if "display:none" in p_style or "line-through" in p_style:
                continue  # скрытый шаблон — пропускаем целиком

        visible[val.lower()] = is_disabled

    # ── Стратегия 2: label/span с data-val или data-value ────────────────────
    if not visible:
        for el in soup.select("[data-val], [data-value]"):
            val = (el.get("data-val") or el.get("data-value") or "").strip()
            if not val or len(val) > 30:
                continue
            # Фильтруем скрытые элементы
            style = (el.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or el.get("hidden") is not None:
                continue
            parent = el.parent
            if parent and hasattr(parent, "get"):
                p_style = (parent.get("style") or "").replace(" ", "").lower()
                if "display:none" in p_style:
                    continue

            classes = " ".join(el.get("class", [])).lower()
            is_disabled = (
                el.get("disabled") is not None
                or any(c in classes for c in [
                    "sold-out", "soldout", "unavailable", "disabled",
                    "is-disabled", "no-stock", "out-of-stock"
                ])
            )
            visible[val.lower()] = is_disabled

    # ── Стратегия 3: видимые <option> в <select name*=size/option> ───────────
    if not visible:
        for sel in soup.find_all("select"):
            name_attr = (sel.get("name") or "").lower()
            if not any(k in name_attr for k in ["size", "option", "variant"]):
                continue
            sel_style = (sel.get("style") or "").replace(" ", "").lower()
            if "display:none" in sel_style or sel.get("hidden") is not None:
                continue
            for opt in sel.find_all("option"):
                val = (opt.get("value") or opt.get_text(strip=True)).strip()
                if not val or len(val) > 30:
                    continue
                if any(p in val.lower() for p in ["вибер", "choose", "select", "оберіть"]):
                    continue
                is_disabled = opt.get("disabled") is not None
                visible[val.lower()] = is_disabled

    if not visible:
        # HTML не дал нам список размеров — возвращаем данные API без изменений
        logger.debug("[Shopify filter] No visible size elements found in HTML, using API data as-is")
        return result

    # ── Фильтрация и коррекция статуса ────────────────────────────────────────
    filtered: list[dict] = []
    for v in result.variants:
        key = v["title"].lower()
        if key not in visible:
            logger.debug("[Shopify filter] Dropping phantom variant '%s' (not in HTML)", v["title"])
            continue
        v_copy = dict(v)
        if visible[key]:  # HTML говорит: sold-out
            v_copy["in_stock"] = False
        filtered.append(v_copy)

    if not filtered:
        # После фильтрации ничего не осталось — скорее всего HTML не совпал.
        # Безопасный откат: возвращаем оригинал
        logger.warning("[Shopify filter] All variants filtered out, falling back to API data")
        return result

    logger.info(
        "[Shopify filter] %d → %d variants after HTML cross-ref (visible: %s)",
        len(result.variants), len(filtered), list(visible.keys())
    )
    return ParseResult(
        title=result.title,
        price=filtered[0]["price"] or result.price,
        currency=result.currency,
        is_in_stock=any(v["in_stock"] for v in filtered),
        source=result.source,
        variants=filtered,
    )


# ── Shopify JSON API (/products/<handle>.json) ────────────────────────────────
async def _parse_shopify_json(url: str) -> ParseResult | None:
    """
    Прямой запрос к Shopify JSON API.
    Подходит для сайтов peresvitbrand.com и любых других магазинов на Shopify.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path
        if "/products/" not in path:
            return None

        clean_path = path.rstrip("/")
        if clean_path.endswith(".json"):
            json_path = clean_path
        else:
            json_path = f"{clean_path}.json"

        json_url = f"{parsed.scheme}://{parsed.netloc}{json_path}"
        logger.info("[Shopify API] GET %s", json_url)

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
        size_pattern = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|3XL|4XL|5XL|[2-5]?XL|\d{2,3})$", re.IGNORECASE)

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
                    "price": v_price
                })

        currency = "₴" if any(domain in parsed.netloc for domain in [".ua", "peresvit", "ukraine"]) else "$"

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
                if v["title"].lower() == target_clean or f"/{target_clean}" in v["title"].lower() or f"{target_clean} " in v["title"].lower():
                    matched_v = v
                    break

        if matched_v:
            v_title = matched_v["title"]
            variant_label = f"Размер: {v_title}" if size_pattern.match(v_title) else f"Вариант: {v_title}"
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
def _parse_css(soup: BeautifulSoup, url: str = "", embedded_variants: list[dict] | None = None) -> ParseResult | None:
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


def _extract_availability(soup: BeautifulSoup, url: str = "", embedded_variants: list[dict] | None = None) -> bool:
    """
    Продвинутое определение наличия товара с учетом конкретно выбранной вариации и активных кнопок.
    """
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
    out_stock_words = ["розпродано", "out of stock", "немає в наявності", "нет в наличии", "sold out", "немає", "не доступно", "not available", "уведомить о поступлении", "повідомити про надходження"]

    for btn in buy_buttons:
        btn_text = btn.get_text(strip=True).lower()
        btn_classes = [c.lower() for c in btn.get("class", [])]
        is_btn_disabled = (
            btn.get("disabled") is not None
            or btn.get("aria-disabled") == "true"
            or any(d in btn_classes for d in ["disabled", "is-disabled", "out-of-stock", "sold-out"])
            or any(w in btn_text for w in out_stock_words)
        )
        if not is_btn_disabled and any(kw in btn_text for kw in ["купити", "у кошик", "в корзину", "buy", "додати", "заказать", "купить"]):
            return True

    selects = soup.find_all("select")
    for select in selects:
        selected_opt = select.find("option", selected=True)
        if selected_opt:
            val = selected_opt.get_text(strip=True).lower()
            if val and not any(dis in val for dis in ["немає", "нет", "out of stock", "disabled", "розпродано"]):
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


# ── Извлечение встроенных JSON-данных (Shopify / WooCommerce) ───────────────
def _extract_embedded_variants(soup: BeautifulSoup | None) -> tuple[list[dict], str]:
    """
    Извлекает данные вариантов товара из внутренних скриптов и атрибутов
    (Shopify ProductJson, WooCommerce data-product_variations, JS-переменные, LD+JSON).

    Возвращает:
      (variants_list, json_snippet_str)
    """
    variants = []
    snippets = []

    if not soup:
        return variants, ""

    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        sid = (script.get("id") or "").lower()
        stext = script.string or script.get_text() or ""
        if not stext:
            continue

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
                        elif isinstance(offers, dict) and "offers" in offers and isinstance(offers["offers"], list):
                            for off in offers["offers"]:
                                norm = _normalize_variant(off, source="schema")
                                if norm:
                                    variants.append(norm)
                            snippets.append(json.dumps(offers["offers"], ensure_ascii=False)[:3000])
            except Exception:
                pass

        if not stype or stype in {"text/javascript", "application/javascript"}:
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

    for el in soup.select("[data-product_variations], [data-product-json]"):
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

    unique_variants = []
    seen = set()
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


# ── Вариативность товаров ───────────────────────────────────────────────────
def _detect_variant_and_stock(
    soup: BeautifulSoup | None,
    url: str,
    embedded_variants: list[dict] | None = None
) -> tuple[str | None, bool | None, list[dict]]:
    """
    Анализирует URL, HTML и извлечённый JSON вариантов для определения
    выбранного размера/цвета/опции, его доступности и списка всех найденных вариантов.

    Возвращает:
      (variant_label, variant_is_in_stock, all_variants_list)
    """
    variant_name = None
    variant_label = None
    variant_disabled = False
    all_variants = []
    seen_variants = set()

    size_pattern = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|3XL|4XL|5XL|[2-5]?XL|\d{2,3})$", re.IGNORECASE)
    sku_pattern = re.compile(r"\b[A-Za-z0-9]+-[A-Za-z0-9]+-([A-Za-z0-9]+)\b")

    if embedded_variants is None and soup:
        embedded_variants, _ = _extract_embedded_variants(soup)

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
                    "price": str(v.get("price") or "").strip()
                })

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
                        if size_pattern.match(extracted):
                            variant_name = extracted
                            variant_label = f"Размер: {extracted}"
                        else:
                            variant_name = v_clean
                            variant_label = f"Вариант: {v_clean}"
                    elif size_pattern.match(v_clean):
                        variant_name = v_clean
                        variant_label = f"Размер: {v_clean}"
                    else:
                        variant_name = v_clean
                        variant_label = f"Вариант: {v_clean}"
                    break
        except Exception as exc:
            logger.warning("Could not parse URL query for variants: %s", exc)

    if embedded_variants:
        target_term = (variant_name or "").strip().lower()
        matched_v = None

        if target_term:
            for v in embedded_variants:
                v_id = v["id"].lower()
                v_sku = v["sku"].lower()
                v_title = v["title"].lower()
                v_opt1 = v["option1"].lower()

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
            if size_pattern.match(clean_title):
                variant_label = f"Размер: {clean_title}"
            else:
                variant_label = f"Вариант: {clean_title}"
            return variant_label, matched_v["available"], []

        if not target_term and embedded_variants:
            avail_sizes = [v["title"] for v in embedded_variants if v["available"]]
            oos_sizes = [v["title"] for v in embedded_variants if not v["available"]]

            if not avail_sizes and oos_sizes:
                return None, False, all_variants
            elif avail_sizes and oos_sizes:
                avail_str = ", ".join(avail_sizes)
                oos_str = ", ".join(oos_sizes)
                summary_label = f"В наличии ({avail_str}), {oos_str} — распродан"
                return summary_label, True, all_variants
            elif avail_sizes:
                return None, True, all_variants

    if not all_variants and soup:
        # Хелпер: проверка видимости элемента (скрытые JS-шаблоны исключаем)
        def _is_hidden(el) -> bool:
            style = (el.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or "visibility:hidden" in style:
                return True
            if el.get("hidden") is not None:
                return True
            if el.get("type", "").lower() == "hidden":
                return True
            # Проверяем родителей (до 3 уровней вверх)
            parent = el.parent
            for _ in range(3):
                if parent is None:
                    break
                p_style = (parent.get("style") or "").replace(" ", "").lower() if hasattr(parent, 'get') else ""
                if "display:none" in p_style or "visibility:hidden" in p_style:
                    return True
                if hasattr(parent, 'get') and parent.get("hidden") is not None:
                    return True
                parent = parent.parent
            return False

        # 1. Поиск видимых элементов выбора вариантов в контейнере товара
        product_box = soup.select_one(
            "form[action*='cart'], [class*='product-detail'], [class*='product-info'], "
            "[class*='product-single'], main, #main"
        ) or soup
        variant_elements = product_box.select(
            ".variant-item, [class*='variant-item'], [class*='size-item'], "
            "[class*='swatch-element'], .size-swatch, .variant-option"
        )
        for el in variant_elements:
            # Пропускаем скрытые JS-шаблоны
            if _is_hidden(el):
                continue
            t_clean = el.get_text(strip=True)
            if not t_clean or len(t_clean) > 25:
                continue
            classes = [c.lower() for c in el.get("class", [])]
            is_disabled = (
                el.get("disabled") is not None
                or el.get("aria-disabled") == "true"
                or any(d in classes for d in [
                    "disabled", "is-disabled", "out-of-stock",
                    "sold-out", "unavailable", "no-stock", "cross"
                ])
            )
            clean_name = re.sub(
                r"\s*\((?:немає|нет|out of stock|розпродано)[^)]*\)", "",
                t_clean, flags=re.IGNORECASE
            ).strip()
            key = clean_name.lower()
            if clean_name and key not in seen_variants:
                seen_variants.add(key)
                all_variants.append({
                    "id": el.get("data-value") or el.get("data-id") or clean_name,
                    "title": clean_name,
                    "in_stock": not is_disabled,
                    "price": ""
                })

        # 2. Поиск <option> в видимых выпадающих списках <select>
        for select in soup.find_all("select"):
            if _is_hidden(select):
                continue
            for opt in select.find_all("option"):
                if opt.get("type", "").lower() == "hidden":
                    continue
                opt_text = opt.get_text(strip=True)
                if not opt_text:
                    continue
                if any(p in opt_text.lower() for p in ["выберите", "оберіть", "choose", "select"]):
                    continue
                classes = [c.lower() for c in opt.get("class", [])]
                opt_disabled = (
                    opt.get("disabled") is not None
                    or any(d in classes for d in ["disabled", "out-of-stock", "sold-out", "unavailable"])
                    or any(w in opt_text.lower() for w in ["немає", "нет в наличии", "out of stock", "розпродано"])
                )
                clean_name = re.sub(
                    r"\s*\((?:немає|нет|out of stock|розпродано)[^)]*\)", "",
                    opt_text, flags=re.IGNORECASE
                ).strip()
                key = clean_name.lower()
                if clean_name and key not in seen_variants:
                    seen_variants.add(key)
                    all_variants.append({
                        "id": opt.get("value") or clean_name,
                        "title": clean_name,
                        "in_stock": not opt_disabled,
                        "price": ""
                    })

    if not variant_label and soup:
        for select in soup.find_all("select"):
            # Пропускаем скрытые select
            style = (select.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or select.get("hidden") is not None:
                continue
            selected_opt = select.find("option", selected=True)
            if not selected_opt:
                for opt in select.find_all("option"):
                    if opt.get("selected") is not None:
                        selected_opt = opt
                        break
            if selected_opt:
                opt_text = selected_opt.get_text(strip=True)
                if opt_text and not any(p in opt_text.lower() for p in ["выберите", "оберіть", "choose", "select"]):
                    classes = [c.lower() for c in selected_opt.get("class", [])]
                    opt_disabled = (
                        selected_opt.get("disabled") is not None
                        or any(d in classes for d in ["disabled", "out-of-stock", "sold-out", "unavailable"])
                        or any(w in opt_text.lower() for w in ["немає", "нет в наличии", "out of stock", "розпродано"])
                    )
                    clean_name = re.sub(
                        r"\s*\((?:немає|нет|out of stock|розпродано)[^)]*\)", "",
                        opt_text, flags=re.IGNORECASE
                    ).strip()
                    if size_pattern.match(clean_name):
                        variant_label = f"Размер: {clean_name}"
                    else:
                        variant_label = f"Вариант: {clean_name}"
                    variant_name = clean_name
                    if opt_disabled:
                        variant_disabled = True
                    break

    if not variant_label and soup:
        active_selectors = [
            ".selected", ".active", ".checked",
            "[aria-checked='true']", "[data-selected='true']",
            "input[type='radio']:checked + label",
            "input[type='radio']:checked"
        ]
        for sel in active_selectors:
            elements = soup.select(sel)
            for el in elements:
                parent = el.find_parent(attrs={"class": re.compile(r"size|variant|option|color|sku|attr|swatch", re.I)})
                el_classes = " ".join(el.get("class", [])).lower()
                is_attr_elem = parent is not None or any(k in el_classes for k in ["size", "variant", "option", "color", "sku", "swatch"])
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
                    or any(d in classes for d in ["disabled", "is-disabled", "out-of-stock", "sold-out", "unavailable", "line-through", "cross", "no-stock"])
                )

                clean_name = text.strip()
                if size_pattern.match(clean_name):
                    variant_label = f"Размер: {clean_name}"
                else:
                    variant_label = f"Вариант: {clean_name}"
                variant_name = clean_name
                if el_disabled:
                    variant_disabled = True
                break
            if variant_label:
                break

    if not variant_label and soup:
        sku_el = soup.find(text=sku_pattern) or soup.select_one("[class*='sku'], [id*='sku']")
        if sku_el:
            sku_text = sku_el.get_text(strip=True) if hasattr(sku_el, "get_text") else str(sku_el)
            sku_match = sku_pattern.search(sku_text)
            if sku_match:
                extracted = sku_match.group(1)
                if size_pattern.match(extracted):
                    variant_name = extracted
                    variant_label = f"Размер: {extracted}"

    if variant_disabled:
        return variant_label, False, all_variants

    variant_stock_status = None
    if soup:
        buy_buttons = soup.select(
            "button[type='submit'], input[type='submit'], .btn-buy, .buy-button, "
            "[class*='add-to-cart'], [class*='add_to_cart'], [id*='add-to-cart'], "
            "[class*='buy'], [id*='buy'], [name*='add-to-cart']"
        )
        out_stock_words = ["розпродано", "out of stock", "немає в наявності", "нет в наличии", "sold out", "немає", "не доступно", "not available", "уведомить о поступлении", "повідомити про надходження"]

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
            elif any(kw in btn_text for kw in ["купити", "у кошик", "в корзину", "buy", "додати", "заказать", "купить"]):
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
    embedded_variants: list[dict] | None = None
) -> ParseResult | None:
    """
    Подставляет выбранную опцию в название товара и строжайше проверяет наличие выбранного варианта.
    """
    if not result:
        return result

    variant_label, variant_stock, all_variants = _detect_variant_and_stock(soup, url, embedded_variants)

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
        "   Если в URL или параметрах не указан конкретный variant_id, проверь статус каждого размера (S, M, L, XL).",
        "   Если выбран конкретный размер (например, M) и в JSON у него available: false, возвращай in_stock = false, даже если другие размеры есть в наличии.",
        "   Если подтянуть конкретный размер не удалось, но часть размеров распродана — формируй статус с указанием доступных и недоступных размеров (например: 'В наличии (XL), M — распродан').",
        "3. Добавь найденный вариант в скобках к названию товара, например: 'Core Leather MMA Gloves (Размер: M)'.\n",
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
