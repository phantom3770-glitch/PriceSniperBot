"""
scheduler.py — Автономный планировщик проверки цен.

Запускается как фоновая asyncio-задача вместе с ботом.
Каждые CHECK_INTERVAL секунд обходит все товары из БД,
сравнивает новую цену/наличие со старыми и отправляет
пользователям уведомления при изменениях.

Настройки через .env:
  CHECK_INTERVAL_SECONDS — интервал проверки (по умолчанию 43 200 = 12 часов)
  REQUEST_DELAY_SECONDS  — пауза между запросами к сайтам (по умолчанию 3 сек)
"""

import asyncio
import logging
import os
import re

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from database import get_all_items_with_lang, update_item_data
from locales import t
from parser import parse_product

logger = logging.getLogger(__name__)

# ── Параметры (читаются из .env, иначе дефолт) ────────────────────────────────
def _get_check_interval() -> int:
    """Возвращает интервал проверки в секундах из .env (по умолчанию 12 часов)."""
    return int(os.getenv("CHECK_INTERVAL_SECONDS", str(12 * 60 * 60)))

def _get_request_delay() -> int:
    """Возвращает паузу между запросами к сайтам в секундах (по умолчанию 3 сек)."""
    return int(os.getenv("REQUEST_DELAY_SECONDS", "3"))



# ── Утилита: извлечение числовой цены из строки ───────────────────────────────
def _to_float(price_str: str) -> float | None:
    """
    Парсит цену из строки вида "1 299,99" → 1299.99.
    Возвращает None, если не удалось распознать число.
    """
    if not price_str:
        return None
    # Оставляем только цифры, точки и запятые
    cleaned = re.sub(r"[^\d.,]", "", price_str)
    if not cleaned:
        return None
    # Если запятая — десятичный разделитель (European): "1.299,99" → 1299.99
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    # Убираем дублирующиеся точки (тысячные разделители): "1.299.99" → "1299.99"
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fmt_price(price: str, currency: str) -> str:
    """Форматирует цену для вывода в уведомлении."""
    return f"{price} {currency}".strip() if price else "?"


# ── Логика уведомлений ────────────────────────────────────────────────────────
async def _notify(bot: Bot, user_id: int, text: str) -> None:
    """
    Безопасная отправка уведомления.
    Обрабатывает блокировку бота пользователем и Telegram rate-limits.
    """
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — тихо пропускаем
        logger.warning("User %s blocked the bot — skipping notification.", user_id)
    except TelegramRetryAfter as e:
        # Telegram просит подождать — соблюдаем rate limit
        logger.warning("Telegram rate limit: sleeping %ss", e.retry_after)
        await asyncio.sleep(e.retry_after)
        await _notify(bot, user_id, text)  # повторная попытка
    except Exception as exc:
        logger.error("Failed to notify user %s: %s", user_id, exc)


# ── Проверка одного товара ────────────────────────────────────────────────────
async def _check_item(bot: Bot, item: dict) -> None:
    """
    Парсит одну ссылку и сравнивает результат с данными из БД.
    При изменениях — отправляет уведомление и обновляет БД.
    """
    item_id: int   = item["id"]
    user_id: int   = item["user_id"]
    url: str       = item["url"]
    lang: str      = item["language"]
    title: str     = item["title"] or ""
    old_price: str = item["price"] or ""
    old_cur: str   = item["currency"] or ""
    old_stock: bool = bool(item["is_in_stock"])

    logger.info("[Scheduler] Checking item #%s: %s", item_id, url)

    try:
        result = await parse_product(url)
    except Exception as exc:
        logger.warning("[Scheduler] Parse error for item #%s: %s", item_id, exc)
        return

    # Парсинг полностью провалился — пропускаем
    if result.source == "error" and not result.price:
        logger.warning("[Scheduler] Skipping item #%s — parse returned error.", item_id)
        return

    new_price = result.price
    new_cur   = result.currency
    new_stock = result.is_in_stock
    display_title = result.title or title  # берём из БД, если парсер не нашёл

    # ── Сравнение цен ─────────────────────────────────────────────────────────
    old_val = _to_float(old_price)
    new_val = _to_float(new_price)
    price_dropped = (
        old_val is not None
        and new_val is not None
        and new_val < old_val
    )

    # ── Смена статуса наличия ─────────────────────────────────────────────────
    appeared_in_stock = (not old_stock) and new_stock

    # ── Отправка уведомлений ──────────────────────────────────────────────────
    if price_dropped:
        old_fmt = _fmt_price(old_price, old_cur)
        new_fmt = _fmt_price(new_price, new_cur)
        msg = t(lang, "price_drop",
                title=display_title,
                old_price=old_fmt,
                new_price=new_fmt,
                url=url)
        await _notify(bot, user_id, msg)
        logger.info(
            "[Scheduler] Price drop on item #%s: %s → %s",
            item_id, old_fmt, new_fmt,
        )

    if appeared_in_stock:
        price_fmt = _fmt_price(new_price, new_cur)
        msg = t(lang, "back_in_stock",
                title=display_title,
                price=price_fmt,
                url=url)
        await _notify(bot, user_id, msg)
        logger.info("[Scheduler] Item #%s is back in stock.", item_id)

    # ── Обновление БД (только при реальных изменениях) ───────────────────────
    data_changed = (
        new_price != old_price
        or new_cur != old_cur
        or new_stock != old_stock
    )
    if data_changed:
        await update_item_data(item_id, new_price, new_cur, new_stock)
        logger.info("[Scheduler] Updated item #%s in DB.", item_id)


# ── Главный цикл планировщика ─────────────────────────────────────────────────
async def _monitor_loop(bot: Bot) -> None:
    """
    Бесконечный цикл: спим interval секунд → проверяем все товары → повтор.
    Первый прогон происходит сразу после запуска (после паузы interval).
    Все исключения перехватываются, чтобы цикл не умирал при случайной ошибке.
    """
    interval = _get_check_interval()
    delay = _get_request_delay()
    logger.info(
        "[Scheduler] Started. Check interval: %ss (%s min).",
        interval, interval // 60,
    )

    while True:
        interval = _get_check_interval()
        delay = _get_request_delay()

        # Ждём между циклами в самом начале итерации,
        # чтобы не перегружать сервисы сразу при старте бота.
        await asyncio.sleep(interval)

        logger.info("[Scheduler] Starting price check cycle...")
        try:
            items = await get_all_items_with_lang()
            logger.info("[Scheduler] %d item(s) to check.", len(items))

            for item in items:
                await _check_item(bot, item)
                # Вежливая пауза между запросами к разным сайтам
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            # Бот завершает работу — выходим из цикла штатно
            logger.info("[Scheduler] Cancelled — shutting down.")
            raise
        except Exception as exc:
            # Любая другая ошибка — логируем и продолжаем следующий цикл
            logger.error("[Scheduler] Unexpected error in cycle: %s", exc, exc_info=True)

        logger.info("[Scheduler] Cycle complete. Next check in %ss.", interval)



# ── Публичный API ─────────────────────────────────────────────────────────────
def start_scheduler(bot: Bot) -> asyncio.Task:
    """
    Создаёт и возвращает фоновую asyncio.Task с циклом мониторинга.
    Вызывать из main() после инициализации бота.
    """
    task = asyncio.create_task(_monitor_loop(bot), name="price_monitor")
    logger.info("[Scheduler] Background task created: price_monitor")
    return task
