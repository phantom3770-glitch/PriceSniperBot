"""
PriceSniper Bot — Telegram-бот для отслеживания цен и наличия товаров.
Stack: aiogram 3.x | python-dotenv | curl_cffi | httpx | beautifulsoup4 | google-genai | aiosqlite

Команды бота:
  /start    → приветствие + главное меню
  /list     → список отслеживаемых товаров
  /language → сменить язык интерфейса

Обработчики ReplyKeyboard:
  📋 Мои товары   → _send_list()
  🔄 Обновить цены → ручной рефреш всех товаров
  🌐 Сменить язык → показать выбор языка

Callback-обработчики (InlineKeyboard):
  lang:{code}           → установить язык
  del:{id}              → удалить товар из БД
  back:main             → вернуться в главное меню
  sz:{item_id}:{variant} → выбрать вариант товара (stateless — данные в callback_data)

URL в тексте → парсинг + сохранение + карточка с кнопками

Принципы:
  - callback.answer() ВСЕГДА первым в любом callback-handler
  - Статус наличия варианта берётся из variants_json в БД (без HTTP-запроса)
  - is_in_stock в карточке читается СТРОГО из поля in_stock в SQLite
  - user_id всегда приводится к int
"""

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote, unquote

from aiohttp import web
from dotenv import load_dotenv

# .env ОБЯЗАТЕЛЬНО загрузить до импорта локальных модулей
load_dotenv()

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
    User,
)

from admin import ADMIN_ID, admin_router
from database import (
    delete_item,
    ensure_user,
    get_item_by_id,
    get_user_items,
    get_user_language,
    init_db,
    save_item,
    update_item_data,
    update_item_variant,
    update_item_variants_json,
    upsert_user,
)
from locales import t
from parser import ParseResult, parse_product
from scheduler import start_scheduler

# ── Middleware автосохранения пользователей ───────────────────────────────────
class UserAutoSaveMiddleware(BaseMiddleware):
    """
    Middleware для автоматического сохранения любого пользователя в таблицу users
    при вызове /start, отправке сообщений или нажатии любых кнопок.
    """

    async def __call__(
        self,
        handler,
        event: TelegramObject,
        data: dict,
    ):
        event_user: User | None = data.get("event_from_user")
        if event_user:
            lang = "en"
            if event_user.language_code:
                code = event_user.language_code.lower()
                if code in ("uk", "ru", "en"):
                    lang = code
                elif code.startswith("uk") or code.startswith("ua"):
                    lang = "uk"
                elif code.startswith("ru"):
                    lang = "ru"
            await ensure_user(int(event_user.id), default_lang=lang)
        return await handler(event, data)


# ── Конфигурация ──────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Кэш языков пользователей в памяти ────────────────────────────────────────
_lang_cache: dict[int, str] = {}

# ── Регулярка для поиска URL в тексте ────────────────────────────────────────
URL_RE = re.compile(
    r"https?://[^\s]+"
    r"|www\.[^\s]+"
    r"|[a-zA-Z0-9-]+\.(com|ua|ru|net|org|shop|store|biz)/[^\s]*",
    re.IGNORECASE,
)

# Паттерн стандартных размеров одежды/обуви
_SIZE_PATTERN = re.compile(
    r"^(XXS|XS|S|M|L|XL|XXL|XXXL|3XL|4XL|5XL|[2-5]?XL|\d{2,3}(?:[./]\d{1,2})?)$",
    re.IGNORECASE,
)

# ── Множества текстов кнопок (для F.text.in_() фильтров) ─────────────────────
_LIST_BTNS        = {"📋 Мої товари",    "📋 My Items",      "📋 Мои товары"}
_REFRESH_BTNS     = {"🔄 Оновити ціни",  "🔄 Refresh Prices","🔄 Обновить цены"}
_CHANGE_LANG_BTNS = {"🌐 Змінити мову",  "🌐 Change Language","🌐 Сменить язык"}
_INVITE_BTNS      = {"🎁 Запросити друга", "🎁 Invite Friend", "🎁 Пригласить друга"}


# ── Функции построения клавиатур ──────────────────────────────────────────────
def _main_kb(lang: str) -> ReplyKeyboardMarkup:
    """Постоянная нижняя ReplyKeyboard-клавиатура с кнопками главного меню."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_my_items"))],
            [
                KeyboardButton(text=t(lang, "btn_refresh")),
                KeyboardButton(text=t(lang, "btn_change_lang")),
            ],
            [KeyboardButton(text=t(lang, "btn_invite_friend"))],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _lang_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура выбора языка.
    Если lang задан — добавляет кнопку «Назад».
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang:uk"),
            InlineKeyboardButton(text="🇬🇧 English",    callback_data="lang:en"),
            InlineKeyboardButton(text="🇷🇺 Русский",    callback_data="lang:ru"),
        ]
    ]
    if lang:
        rows.append([
            InlineKeyboardButton(
                text=t(lang, "btn_back"),
                callback_data="back:main",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _item_kb(item_id: int, url: str, lang: str, title: str = "") -> InlineKeyboardMarkup:
    """Инлайн-клавиатура под карточкой товара: [🗑 Удалить] [🔗 Открыть] [📤 Поделиться]."""
    bot_username = os.getenv("BOT_USERNAME", "PriceSniper_tracker_bot").removeprefix("@")
    deep_link = f"https://t.me/{bot_username}?start=item_{item_id}"
    share_text = t(lang, "share_item_text", title=title, item_id=str(item_id))
    share_url = f"https://t.me/share/url?url={quote(deep_link)}&text={quote(share_text)}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "btn_delete"),
                    callback_data=f"del:{item_id}",
                ),
                InlineKeyboardButton(
                    text=t(lang, "btn_open_link"),
                    url=url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t(lang, "btn_share_item"),
                    url=share_url,
                ),
            ],
        ]
    )


def _variant_selector_kb(item_id: int, variants: list[dict], lang: str = "en") -> InlineKeyboardMarkup:
    """
    Создаёт инлайн-кнопки выбора вариантов товара.
    Данные варианта кодируются прямо в callback_data (stateless).
    Формат: sz:{item_id}:{url_encoded_variant_name}
    """
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    no_stock_label = {
        "uk": "Немає",
        "en": "Out of stock",
        "ru": "Нет",
    }.get(lang, "Out of stock")

    for v in variants:
        in_stock = v.get("in_stock", True)
        emoji = "🟢" if in_stock else "🔴"
        title = str(v.get("title", "")).strip()
        if not title:
            continue

        btn_text = f"{emoji} {title}" if in_stock else f"{emoji} {title} ({no_stock_label})"

        # Название варианта кодируется в URL-safe формат для callback_data
        safe_title = quote(title, safe="")

        current_row.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"sz:{item_id}:{safe_title}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Настройка команд бота ─────────────────────────────────────────────────────
async def _set_bot_commands(bot: Bot) -> None:
    """Регистрирует команды бота для каждого языка интерфейса Telegram."""
    commands: dict[str, list[BotCommand]] = {
        "uk": [
            BotCommand(command="start",    description="\U0001f680 Запустити / Головне меню"),
            BotCommand(command="list",     description="\U0001f4cb Мої товари"),
            BotCommand(command="language", description="\U0001f310 Змінити мову"),
        ],
        "en": [
            BotCommand(command="start",    description="\U0001f680 Start / Main Menu"),
            BotCommand(command="list",     description="\U0001f4cb My Items"),
            BotCommand(command="language", description="\U0001f310 Change Language"),
        ],
        "ru": [
            BotCommand(command="start",    description="\U0001f680 Запустить / Главное меню"),
            BotCommand(command="list",     description="\U0001f4cb Мои товары"),
            BotCommand(command="language", description="\U0001f310 Сменить язык"),
        ],
    }
    for lang_code, cmds in commands.items():
        await bot.set_my_commands(cmds, language_code=lang_code)
    await bot.set_my_commands(commands["en"])

    if ADMIN_ID:
        admin_cmds = [
            BotCommand(command="admin", description="\u2699\ufe0f Панель администратора"),
            *commands["ru"],
        ]
        try:
            await bot.set_my_commands(
                admin_cmds,
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
            logger.info("Admin commands set for user id=%s.", ADMIN_ID)
        except Exception as exc:
            logger.warning("Could not set admin command scope: %s", exc)

    logger.info("Bot commands registered for uk / en / ru.")


# ── Роутер ────────────────────────────────────────────────────────────────────
router = Router()


# ── Вспомогательные функции ───────────────────────────────────────────────────
async def _get_lang(user_id: int) -> str | None:
    """Язык из кэша; при промахе — из БД."""
    uid = int(user_id)
    if uid not in _lang_cache:
        lang = await get_user_language(uid)
        if lang:
            _lang_cache[uid] = lang
    return _lang_cache.get(uid)


async def _set_lang(user_id: int, lang: str) -> None:
    """Сохраняет язык в кэш и в БД."""
    uid = int(user_id)
    _lang_cache[uid] = lang
    await upsert_user(uid, lang)


def _format_item_text(
    lang: str,
    title: str,
    price: str,
    currency: str,
    in_stock: int | bool,
) -> str:
    """
    Единственный источник правды для отображения карточки товара.
    Статус читается ИСКЛЮЧИТЕЛЬНО из переданного поля in_stock (0/1 из SQLite или bool).
      in_stock == 1 (True)  → ✅ В наличии
      in_stock == 0 (False) → ❌ Нет в наличии
    """
    display_title = title or t(lang, "title_unknown")
    price_str = (
        f"{price} {currency}".strip() if price else t(lang, "price_unknown")
    )
    is_available = bool(int(in_stock) if isinstance(in_stock, (int, str)) else in_stock)
    stock_emoji = "✅" if is_available else "❌"
    stock_label = t(lang, "stock_yes" if is_available else "stock_no")
    return t(
        lang, "item_card",
        title=display_title,
        price_str=price_str,
        stock_emoji=stock_emoji,
        stock_label=stock_label,
    )


def _card_from_db(lang: str, item: dict) -> str:
    """Форматирует карточку из записи БД. Статус берётся из поля in_stock."""
    return _format_item_text(
        lang,
        title=item.get("title") or "",
        price=item.get("price") or "",
        currency=item.get("currency") or "",
        in_stock=item.get("in_stock", 0),
    )


def _card_from_result(lang: str, result: ParseResult) -> str:
    """Форматирует карточку из ParseResult."""
    return _format_item_text(
        lang,
        title=result.title,
        price=result.price,
        currency=result.currency,
        in_stock=result.is_in_stock,
    )


async def _send_list(target: Message, user_id: int, lang: str) -> None:
    """
    Отправляет список всех товаров пользователя.
    Каждый товар — отдельное сообщение с инлайн-кнопками.
    Статус читается из in_stock поля в БД.
    """
    items = await get_user_items(int(user_id))
    if not items:
        await target.answer(t(lang, "list_empty"), parse_mode=ParseMode.HTML)
        return

    await target.answer(
        t(lang, "list_header", count=str(len(items))),
        parse_mode=ParseMode.HTML,
    )
    for item in items:
        await target.answer(
            _card_from_db(lang, item),
            parse_mode=ParseMode.HTML,
            reply_markup=_item_kb(item["id"], item["url"], lang, item.get("title") or ""),
        )


def _no_lang_prompt() -> str:
    return "👋 | 🇺🇦 Оберіть мову  •  🇬🇧 Choose language  •  🇷🇺 Выберите язык:"


def _strip_variant_suffix(title: str) -> str:
    """Удаляет суффикс «(Размер: M)» / «(Вариант: ...)» / «(Розмір: ...)» из названия."""
    return re.sub(
        r"\s*\((?:Размер|Вариант|Розмір|Розмір|Size|Variant|Колір|Цвет|Colour):[^)]*\)",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    args = command.args

    # Обработка глубокой ссылки (/start item_123)
    if args and args.startswith("item_"):
        item_id_str = args.removeprefix("item_")
        if item_id_str.isdigit():
            item_id = int(item_id_str)
            item = await get_item_by_id(item_id)
            if item:
                display_lang = lang or "en"
                card_text = (
                    f"🎁 <b>{t(display_lang, 'deep_link_item_prompt')}</b>\n\n"
                    f"{_card_from_db(display_lang, item)}"
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t(display_lang, "btn_add_to_tracking"),
                                callback_data=f"add:{item_id}",
                            ),
                            InlineKeyboardButton(
                                text=t(display_lang, "btn_open_link"),
                                url=item["url"],
                            ),
                        ]
                    ]
                )
                await message.answer(
                    card_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
                return

    if lang:
        await message.answer(
            t(lang, "welcome"),
            parse_mode=ParseMode.HTML,
            reply_markup=_main_kb(lang),
        )
    else:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())


# ── /list ─────────────────────────────────────────────────────────────────────
@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return
    await _send_list(message, user_id, lang)


# ── /language ─────────────────────────────────────────────────────────────────
@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    await message.answer(
        _no_lang_prompt(),
        reply_markup=_lang_kb(lang),
    )


# ── Callback: выбор языка ─────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("lang:"))
async def cb_select_language(callback: CallbackQuery) -> None:
    # ПЕРВЫМ ДЕЛОМ — гасим анимацию нажатия кнопки
    await callback.answer()

    lang = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = int(callback.from_user.id)

    if lang not in ("uk", "en", "ru"):
        return

    await _set_lang(user_id, lang)
    logger.info("User %s selected language: %s", user_id, lang)

    await callback.message.delete()  # type: ignore[union-attr]
    await callback.message.answer(  # type: ignore[union-attr]
        t(lang, "welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(lang),
    )


# ── Callback: «Назад» → главное меню ─────────────────────────────────────────
@router.callback_query(F.data == "back:main")
async def cb_back_main(callback: CallbackQuery) -> None:
    # ПЕРВЫМ ДЕЛОМ — гасим анимацию нажатия кнопки
    await callback.answer()

    user_id = int(callback.from_user.id)
    lang = await _get_lang(user_id) or "en"

    await callback.message.delete()  # type: ignore[union-attr]
    await callback.message.answer(  # type: ignore[union-attr]
        t(lang, "welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(lang),
    )


# ── Callback: удаление товара ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("del:"))
async def cb_delete_item(callback: CallbackQuery) -> None:
    # ПЕРВЫМ ДЕЛОМ — гасим анимацию нажатия кнопки
    await callback.answer(show_alert=False)

    item_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    user_id = int(callback.from_user.id)
    lang = await _get_lang(user_id) or "en"

    await delete_item(item_id, user_id)
    logger.info("User %s deleted item #%s", user_id, item_id)

    await callback.message.edit_text(  # type: ignore[union-attr]
        t(lang, "item_deleted"),
        parse_mode=ParseMode.HTML,
    )


# ── Callback: добавление товара по шеринг-ссылке ─────────────────────────────
@router.callback_query(F.data.startswith("add:"))
async def cb_add_shared_item(callback: CallbackQuery) -> None:
    # ПЕРВЫМ ДЕЛОМ — гасим анимацию нажатия кнопки
    await callback.answer()

    item_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    user_id = int(callback.from_user.id)
    lang = await _get_lang(user_id) or "en"

    item = await get_item_by_id(item_id)
    if not item:
        await callback.answer(t(lang, "parse_error"), show_alert=True)
        return

    # Восстанавливаем варианты из JSON если есть
    variants: list[dict] | None = None
    if item.get("variants_json"):
        try:
            variants = json.loads(item["variants_json"])
        except Exception:
            variants = None

    new_item_id = await save_item(
        user_id=user_id,
        url=item["url"],
        title=item.get("title") or "",
        price=item.get("price") or "",
        currency=item.get("currency") or "",
        is_in_stock=bool(item.get("in_stock", 0)),
        selected_variant=item.get("selected_variant"),
        variants=variants,
    )

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{_card_from_db(lang, item)}\n\n{t(lang, 'item_added')}",
        parse_mode=ParseMode.HTML,
        reply_markup=_item_kb(new_item_id, item["url"], lang, item.get("title") or ""),
    )
    await callback.answer(t(lang, "item_added_popup"), show_alert=False)


# ── 📋 Мои товары ─────────────────────────────────────────────────────────────
@router.message(F.text.in_(_LIST_BTNS))
async def btn_my_items(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return
    await _send_list(message, user_id, lang)


# ── 🔄 Обновить цены ──────────────────────────────────────────────────────────
@router.message(F.text.in_(_REFRESH_BTNS))
async def btn_refresh(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return

    items = await get_user_items(user_id)
    if not items:
        await message.answer(t(lang, "refresh_no_items"), parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(
        t(lang, "refresh_start"), parse_mode=ParseMode.HTML
    )

    updated = 0
    for item in items:
        try:
            result = await parse_product(item["url"])
            if result.source != "error":
                await update_item_data(
                    item["id"], result.price, result.currency, result.is_in_stock
                )
                updated += 1
        except Exception as exc:
            logger.warning("Refresh failed for item #%s: %s", item["id"], exc)

    await status_msg.edit_text(
        t(lang, "refresh_done", count=str(updated)),
        parse_mode=ParseMode.HTML,
    )
    await _send_list(message, user_id, lang)


# ── 🌐 Сменить язык (ReplyKeyboard кнопка) ───────────────────────────────────
@router.message(F.text.in_(_CHANGE_LANG_BTNS))
async def btn_change_lang(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    await message.answer(
        _no_lang_prompt(),
        reply_markup=_lang_kb(lang),
    )


# ── 🎁 Пригласить друга (ReplyKeyboard кнопка) ────────────────────────────────
@router.message(F.text.in_(_INVITE_BTNS))
async def btn_invite_friend(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id) or "en"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "btn_send_to_friend"),
                    switch_inline_query="",
                )
            ]
        ]
    )
    await message.answer(
        t(lang, "invite_msg"),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# ── Inline Query (для кнопок switch_inline_query) ─────────────────────────────
@router.inline_query()
async def inline_query_handler(query: InlineQuery) -> None:
    user_id = int(query.from_user.id)
    lang = await _get_lang(user_id) or "en"
    raw_q = query.query.strip()

    results = []
    if raw_q.startswith("item_"):
        item_id_str = raw_q.removeprefix("item_")
        if item_id_str.isdigit():
            item_id = int(item_id_str)
            item = await get_item_by_id(item_id)
            if item:
                title = item.get("title") or t(lang, "title_unknown")
                share_text = t(
                    lang,
                    "share_item_text",
                    title=title,
                    item_id=str(item_id),
                )
                results.append(
                    InlineQueryResultArticle(
                        id=f"share_item_{item_id}",
                        title=title,
                        description=t(lang, "btn_share_item"),
                        input_message_content=InputTextMessageContent(
                            message_text=share_text,
                            parse_mode=ParseMode.HTML,
                        ),
                    )
                )

    if not results:
        share_bot_text = t(lang, "share_bot_text")
        results.append(
            InlineQueryResultArticle(
                id="share_bot",
                title="PriceSniper Bot",
                description=t(lang, "share_bot_desc"),
                input_message_content=InputTextMessageContent(
                    message_text=share_bot_text,
                    parse_mode=ParseMode.HTML,
                ),
            )
        )

    await query.answer(results, cache_time=1, is_personal=True)


# ── /debug — диагностика БД (только для ADMIN_ID) ────────────────────────────
@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    if ADMIN_ID and user_id != ADMIN_ID:
        return

    from database import DB_PATH
    import os

    items = await get_user_items(user_id)
    all_items_raw: list[dict] = []
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, user_id, title, in_stock FROM items LIMIT 20") as cur:
                rows = await cur.fetchall()
                all_items_raw = [dict(r) for r in rows]
    except Exception as exc:
        all_items_raw = [{"error": str(exc)}]

    db_exists = os.path.isfile(DB_PATH)
    db_size = os.path.getsize(DB_PATH) if db_exists else 0

    lines = [
        f"<b>🔧 Debug Info</b>",
        f"user_id: <code>{user_id}</code>",
        f"DB path: <code>{DB_PATH}</code>",
        f"DB exists: {db_exists} ({db_size} bytes)",
        f"Your items: <b>{len(items)}</b>",
        f"All items in DB: <b>{len(all_items_raw)}</b>",
    ]
    for i in all_items_raw[:10]:
        lines.append(
            f"  • id={i.get('id')} user={i.get('user_id')} "
            f"in_stock={i.get('in_stock')} {str(i.get('title',''))[:40]}"
        )

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Обработчик URL (текстовые сообщения) ─────────────────────────────────────
@router.message(F.text)
async def handle_text(message: Message) -> None:
    """
    Ловит URL в любом текстовом сообщении.
    Все остальные тексты (кнопки меню) перехвачены обработчиками выше.
    """
    text = message.text or ""
    user_id = int(message.from_user.id)  # type: ignore[union-attr]
    lang = await _get_lang(user_id)

    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return

    match = URL_RE.search(text)
    if not match:
        return  # не URL — игнорируем

    url = match.group()
    if not url.startswith("http"):
        url = "https://" + url

    # 1. Мгновенный ответ
    status_msg = await message.answer(
        t(lang, "parsing_start"), parse_mode=ParseMode.HTML
    )

    # 2. Парсинг
    result = await parse_product(url)
    logger.info(
        "Parsed [%s] user=%s title='%s' price=%s%s stock=%s variants=%d url=%s",
        result.source, user_id,
        result.title, result.price, result.currency,
        result.is_in_stock, len(result.variants), url,
    )

    # 3. Ошибка парсинга
    if result.source == "error" and not result.title and not result.price:
        await status_msg.edit_text(
            t(lang, "parse_error"), parse_mode=ParseMode.HTML
        )
        return

    # 4. Сохраняем базовый товар в БД → получаем item_id
    #    Варианты сохраняем сразу в variants_json
    try:
        item_id = await save_item(
            user_id=user_id,
            url=url,
            title=result.title,
            price=result.price,
            currency=result.currency,
            is_in_stock=result.is_in_stock,
            variants=result.variants if result.variants else None,
        )
    except Exception as exc:
        logger.error("Failed to save item for user %s: %s", user_id, exc, exc_info=True)
        await status_msg.edit_text(
            t(lang, "save_error"), parse_mode=ParseMode.HTML
        )
        return

    # 5. Если у товара несколько вариантов — выводим инлайн-клавиатуру выбора
    if len(result.variants) > 1:
        await status_msg.edit_text(
            t(lang, "select_variant_prompt"),
            parse_mode=ParseMode.HTML,
            reply_markup=_variant_selector_kb(item_id, result.variants, lang),
        )
        return

    # 6. Если вариант один / нет вариантов — выводим готовую карточку товара
    await status_msg.edit_text(
        _card_from_result(lang, result),
        parse_mode=ParseMode.HTML,
        reply_markup=_item_kb(item_id, url, lang, result.title or ""),
    )

    # 7. Подтверждение добавления
    await message.answer(t(lang, "item_added"), parse_mode=ParseMode.HTML)


# ── Callback: обработка выбора размера/варианта ───────────────────────────────
@router.callback_query(F.data.startswith("sz:"))
async def cb_select_size(callback: CallbackQuery) -> None:
    """
    Обработчик выбора варианта товара (размер, цвет и т.д.).

    Stateless: все данные берутся из callback_data и БД — без RAM-состояний.
    Статус наличия определяется из variants_json в БД (без HTTP-запроса).
    Если variants_json пуст — статус берётся из последнего парсинга.
    """
    # ПЕРВЫМ ДЕЛОМ — гасим анимацию нажатия кнопки (Telegram не зависает)
    await callback.answer()

    try:
        parts = callback.data.split(":")  # type: ignore[union-attr]
        if len(parts) < 3:
            return

        item_id = int(parts[1])
        # Декодируем название варианта (может содержать ":" — берём всё после второго ":")
        size_name = unquote(":".join(parts[2:]))
        user_id = int(callback.from_user.id)
        lang = await _get_lang(user_id) or "en"

        # 1. Загружаем товар из SQLite (единственный источник правды)
        item = await get_item_by_id(item_id)
        if not item:
            await callback.message.edit_text(  # type: ignore[union-attr]
                t(lang, "parse_error"), parse_mode=ParseMode.HTML
            )
            return

        # 2. ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ: определяем статус выбранного варианта
        #    из сохранённого variants_json — БЕЗ повторного HTTP-запроса
        is_in_stock: bool | None = None
        variant_price: str = item.get("price") or ""
        variant_currency: str = item.get("currency") or "₴"

        if item.get("variants_json"):
            try:
                saved_variants: list[dict] = json.loads(item["variants_json"])
                size_name_lower = size_name.lower()
                for sv in saved_variants:
                    if sv.get("title", "").lower() == size_name_lower:
                        is_in_stock = bool(sv.get("in_stock", True))
                        if sv.get("price"):
                            variant_price = sv["price"]
                        break
            except Exception as exc:
                logger.warning("Failed to parse variants_json for item #%s: %s", item_id, exc)

        # 3. Если variants_json не дал ответа — статус False по умолчанию,
        #    так как неизвестный вариант лучше отметить как недоступный
        #    (чтобы не показывать ложный «В наличии»)
        if is_in_stock is None:
            logger.warning(
                "Variant '%s' not found in variants_json for item #%s, assuming in_stock=False",
                size_name, item_id
            )
            is_in_stock = False

        # 4. Формируем название с понятной меткой варианта
        variant_label = (
            f"Размер: {size_name}" if _SIZE_PATTERN.match(size_name)
            else f"Вариант: {size_name}"
        )

        raw_title = item.get("title") or ""
        clean_base_title = _strip_variant_suffix(raw_title)
        full_title = f"{clean_base_title} ({variant_label})"

        # 5. Сохраняем ОБНОВЛЁННЫЕ данные в SQLite (commit гарантирован в database.py)
        await update_item_variant(
            item_id=item_id,
            url=item["url"],
            title=full_title,
            price=variant_price,
            currency=variant_currency,
            is_in_stock=is_in_stock,
            selected_variant=size_name,
        )

        logger.info(
            "Variant selected: item_id=%s variant='%s' in_stock=%s user=%s",
            item_id, size_name, is_in_stock, user_id
        )

        # 6. Строим карточку через _format_item_text — статус из is_in_stock
        card_text = _format_item_text(
            lang,
            title=full_title,
            price=variant_price,
            currency=variant_currency,
            in_stock=is_in_stock,
        )

        # 7. Заменяем меню выбора вариантов итоговой карточкой товара
        await callback.message.edit_text(  # type: ignore[union-attr]
            card_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_item_kb(item_id, item["url"], lang, full_title),
        )

        # 8. Подтверждающее сообщение — зависит от наличия
        confirm_key = "variant_added_in_stock" if is_in_stock else "variant_target_added"
        await callback.message.answer(  # type: ignore[union-attr]
            t(lang, confirm_key, variant=size_name),
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:
        logger.error(
            "cb_select_size error [callback.data=%s]: %s",
            callback.data, exc, exc_info=True
        )
        try:
            user_id = int(callback.from_user.id)
            lang = await _get_lang(user_id) or "en"
            await callback.message.edit_text(  # type: ignore[union-attr]
                t(lang, "parse_error"), parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


# ── Точка входа ───────────────────────────────────────────────────────────────
async def main() -> None:
    await init_db()
    logger.info("Database initialised.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Регистрируем команды бота (синяя кнопка «Меню» в Telegram)
    await _set_bot_commands(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserAutoSaveMiddleware())
    dp.callback_query.outer_middleware(UserAutoSaveMiddleware())

    # admin_router ОБЯЗАТЕЛЬНО первым: FSM-фильтр перехватывает до основного роутера
    dp.include_router(admin_router)
    dp.include_router(router)

    # Фоновый HTTP-сервер для Render Health Check
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("🌐 Health check web server running on port %s", port)

    # Запускаем планировщик фоновой проверки цен
    scheduler_task = start_scheduler(bot)

    logger.info("🚀 PriceSniper Bot is starting...")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await app_runner.cleanup()
        scheduler_task.cancel()
        try:
            await scheduler_task
        except Exception:
            pass
        logger.info("Scheduler stopped. Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
