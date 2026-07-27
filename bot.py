"""
PriceSniper Bot — Telegram-бот для отслеживания цен и наличия товаров.
Stack: aiogram 3.x | python-dotenv | httpx | beautifulsoup4 | google-genai | aiosqlite

Команды бота:
  /start    → приветствие + главное меню
  /list     → список отслеживаемых товаров
  /language → сменить язык интерфейса

Обработчики ReplyKeyboard:
  📋 Мои товары   → _send_list()
  🔄 Обновить цены → ручной рефреш всех товаров
  🌐 Сменить язык → показать выбор языка (с кнопкой «Назад»)

Callback-обработчики (InlineKeyboard):
  lang:{code}  → установить язык
  del:{id}     → удалить товар из БД
  back:main    → вернуться в главное меню

URL в тексте → парсинг + сохранение + карточка с кнопками
"""

import asyncio
import logging
import os
import re

from aiohttp import web
from dotenv import load_dotenv

# .env ОБЯЗАТЕЛЬНО загрузить до импорта локальных модулей
load_dotenv()

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    get_user_items,
    get_user_language,
    init_db,
    save_item,
    update_item_data,
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
            await ensure_user(event_user.id, default_lang=lang)
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

# ── Множества текстов кнопок (для F.text.in_() фильтров) ─────────────────────
_LIST_BTNS        = {"📋 Мої товари",    "📋 My Items",      "📋 Мои товары"}
_REFRESH_BTNS     = {"🔄 Оновити ціни",  "🔄 Refresh Prices","🔄 Обновить цены"}
_CHANGE_LANG_BTNS = {"🌐 Змінити мову",  "🌐 Change Language","🌐 Сменить язык"}


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
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _lang_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура выбора языка.
    Если lang задан (пользователь уже выбирал язык) — добавляет кнопку «Назад».
    Для новых пользователей кнопки «Назад» нет.
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


def _item_kb(item_id: int, url: str, lang: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура под карточкой товара: [🗑 Удалить] [🔗 Открыть]."""
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
            ]
        ]
    )


# ── Настройка команд бота (синяя кнопка «Меню» в Telegram) ───────────────────
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
    # Дефолтный список (для пользователей без совпадающего языка)
    await bot.set_my_commands(commands["en"])

    # Для администратора — отдельное меню с /admin (видно только ему)
    if ADMIN_ID:
        admin_cmds = [
            BotCommand(command="admin",    description="\u2699\ufe0f Панель администратора"),
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
    if user_id not in _lang_cache:
        lang = await get_user_language(user_id)
        if lang:
            _lang_cache[user_id] = lang
    return _lang_cache.get(user_id)


async def _set_lang(user_id: int, lang: str) -> None:
    """Сохраняет язык в кэш и в БД."""
    _lang_cache[user_id] = lang
    await upsert_user(user_id, lang)


def _format_card(
    lang: str,
    title: str,
    price: str,
    currency: str,
    is_in_stock: bool,
) -> str:
    """Универсальная функция форматирования карточки товара."""
    display_title = title or t(lang, "title_unknown")
    price_str = (
        f"{price} {currency}".strip() if price else t(lang, "price_unknown")
    )
    stock_emoji = "✅" if is_in_stock else "❌"
    stock_label = t(lang, "stock_yes" if is_in_stock else "stock_no")
    return t(
        lang, "item_card",
        title=display_title,
        price_str=price_str,
        stock_emoji=stock_emoji,
        stock_label=stock_label,
    )


def _card_from_db(lang: str, item: dict) -> str:
    return _format_card(
        lang,
        title=item.get("title") or "",
        price=item.get("price") or "",
        currency=item.get("currency") or "",
        is_in_stock=bool(item.get("is_in_stock", False)),
    )


def _card_from_result(lang: str, result: ParseResult) -> str:
    return _format_card(
        lang,
        title=result.title,
        price=result.price,
        currency=result.currency,
        is_in_stock=result.is_in_stock,
    )


async def _send_list(target: Message, user_id: int, lang: str) -> None:
    """
    Отправляет список всех товаров пользователя.
    Каждый товар — отдельное сообщение с инлайн-кнопками.
    """
    items = await get_user_items(user_id)
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
            reply_markup=_item_kb(item["id"], item["url"], lang),
        )


def _no_lang_prompt() -> str:
    return "👋 | 🇺🇦 Оберіть мову  •  🇬🇧 Choose language  •  🇷🇺 Выберите язык:"


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    lang = await _get_lang(user_id)

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
    user_id = message.from_user.id  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return
    await _send_list(message, user_id, lang)


# ── /language ─────────────────────────────────────────────────────────────────
@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    """Показывает выбор языка с кнопкой «Назад» (если язык уже выбран)."""
    user_id = message.from_user.id  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    await message.answer(
        _no_lang_prompt(),
        reply_markup=_lang_kb(lang),  # lang=None → без кнопки «Назад»
    )


# ── Callback: выбор языка ─────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("lang:"))
async def cb_select_language(callback: CallbackQuery) -> None:
    lang = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = callback.from_user.id

    if lang not in ("uk", "en", "ru"):
        await callback.answer("Unknown language")
        return

    await _set_lang(user_id, lang)
    logger.info("User %s selected language: %s", user_id, lang)

    await callback.message.delete()  # type: ignore[union-attr]
    await callback.message.answer(  # type: ignore[union-attr]
        t(lang, "welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(lang),
    )
    await callback.answer()


# ── Callback: «Назад» → главное меню ─────────────────────────────────────────
@router.callback_query(F.data == "back:main")
async def cb_back_main(callback: CallbackQuery) -> None:
    """Возвращает пользователя в главное меню из экрана выбора языка."""
    user_id = callback.from_user.id
    lang = await _get_lang(user_id) or "en"

    await callback.message.delete()  # type: ignore[union-attr]
    await callback.message.answer(  # type: ignore[union-attr]
        t(lang, "welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(lang),
    )
    await callback.answer()


# ── Callback: удаление товара ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("del:"))
async def cb_delete_item(callback: CallbackQuery) -> None:
    item_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    user_id = callback.from_user.id
    lang = await _get_lang(user_id) or "en"

    await delete_item(item_id, user_id)
    logger.info("User %s deleted item #%s", user_id, item_id)

    await callback.message.edit_text(  # type: ignore[union-attr]
        t(lang, "item_deleted"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer(t(lang, "item_deleted_popup"), show_alert=False)


# ── 📋 Мои товары ─────────────────────────────────────────────────────────────
@router.message(F.text.in_(_LIST_BTNS))
async def btn_my_items(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    if not lang:
        await message.answer(_no_lang_prompt(), reply_markup=_lang_kb())
        return
    await _send_list(message, user_id, lang)


# ── 🔄 Обновить цены ──────────────────────────────────────────────────────────
@router.message(F.text.in_(_REFRESH_BTNS))
async def btn_refresh(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
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
    user_id = message.from_user.id  # type: ignore[union-attr]
    lang = await _get_lang(user_id)
    await message.answer(
        _no_lang_prompt(),
        reply_markup=_lang_kb(lang),  # с кнопкой «Назад» — язык уже выбран
    )


# ── Обработчик URL (текстовые сообщения) ─────────────────────────────────────
@router.message(F.text)
async def handle_text(message: Message) -> None:
    """
    Ловит URL в любом текстовом сообщении.
    Все остальные тексты (кнопки меню) перехвачены обработчиками выше.
    """
    text = message.text or ""
    user_id = message.from_user.id  # type: ignore[union-attr]
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
        "Parsed [%s] user=%s title='%s' price=%s%s stock=%s url=%s",
        result.source, user_id,
        result.title, result.price, result.currency, result.is_in_stock, url,
    )

    # 3. Ошибка парсинга
    if result.source == "error" and not result.title and not result.price:
        await status_msg.edit_text(
            t(lang, "parse_error"), parse_mode=ParseMode.HTML
        )
        return

    # 4. Сохраняем → получаем item_id
    item_id = await save_item(
        user_id=user_id,
        url=url,
        title=result.title,
        price=result.price,
        currency=result.currency,
        is_in_stock=result.is_in_stock,
    )

    # 5. Редактируем "изучаю..." → карточка + кнопки
    await status_msg.edit_text(
        _card_from_result(lang, result),
        parse_mode=ParseMode.HTML,
        reply_markup=_item_kb(item_id, url, lang),
    )

    # 6. Подтверждение добавления
    await message.answer(t(lang, "item_added"), parse_mode=ParseMode.HTML)


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

    # admin_router ОБЯЗАТЕЛЬНО регистрируем первым:
    # его FSM-фильтр (BroadcastState) должен перехватывать сообщения
    # раньше, чем универсальный F.text-обработчик основного роутера.
    dp.include_router(admin_router)
    dp.include_router(router)

    # Запускаем фоновый HTTP-сервер для Render Health Check
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

