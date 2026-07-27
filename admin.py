"""
admin.py — Закрытая панель администратора PriceSniper Bot.

Доступ: только пользователь с ADMIN_ID из .env (проверка на уровне роутера).
Функции:
  /admin          → главное меню админки
  📊 Статистика   → отчёт: пользователи, товары, топ-3 сайта
  📢 Объявление   → FSM-рассылка: ввод текста → предпросмотр → рассылка всем
  🔙 Отмена       → выход из FSM, возврат в меню

Архитектура: отдельный Router с filter IsAdmin.
FSM States: BroadcastState.waiting_for_text
"""

import asyncio
import logging
import os

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import get_admin_stats, get_all_user_ids

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))


# ── FSM ───────────────────────────────────────────────────────────────────────
class BroadcastState(StatesGroup):
    waiting_for_text = State()


# ── Фильтр «только для администратора» ───────────────────────────────────────
class IsAdmin(BaseFilter):
    """
    Пропускает событие только если отправитель == ADMIN_ID.
    ADMIN_ID=0 → режим без администратора (команда /admin недоступна).
    """
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if ADMIN_ID == 0:
            return False
        uid = getattr(getattr(event, "from_user", None), "id", None)
        return uid == ADMIN_ID


# ── Клавиатуры ────────────────────────────────────────────────────────────────
ADMIN_MENU_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика",   callback_data="adm:stats"),
            InlineKeyboardButton(text="📢 Объявление",   callback_data="adm:broadcast"),
        ]
    ]
)

CONFIRM_BROADCAST_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить всем", callback_data="adm:confirm"),
            InlineKeyboardButton(text="❌ Отмена",         callback_data="adm:cancel"),
        ]
    ]
)

CANCEL_ONLY_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:cancel")]
    ]
)


# ── Роутер (фильтр IsAdmin применяется ко всем обработчикам) ──────────────────
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ── /admin — главное меню ─────────────────────────────────────────────────────
@admin_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(
        "⚙️ <b>Панель администратора</b>\n\n"
        "Добро пожаловать! Выберите действие:",
        reply_markup=ADMIN_MENU_KB,
    )


# ── Callback: 📊 Статистика ───────────────────────────────────────────────────
@admin_router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    stats = await get_admin_stats()

    # Формируем строки топ-доменов
    medals = ["🥇", "🥈", "🥉"]
    if stats["top_domains"]:
        top_lines = "\n".join(
            f"  {medals[i]}  <code>{domain}</code> — {count} тов."
            for i, (domain, count) in enumerate(stats["top_domains"])
        )
    else:
        top_lines = "  — нет данных —"

    text = (
        "📊 <b>Статистика бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤  Пользователей: <b>{stats['user_count']}</b>\n"
        f"🛒  Товаров в базе: <b>{stats['item_count']}</b>\n\n"
        f"🏆  <b>Топ-3 сайта:</b>\n{top_lines}"
    )

    await callback.message.edit_text(text, reply_markup=ADMIN_MENU_KB)  # type: ignore[union-attr]
    await callback.answer()


# ── Callback: 📢 Объявление — запуск FSM ──────────────────────────────────────
@admin_router.callback_query(F.data == "adm:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastState.waiting_for_text)

    user_count = len(await get_all_user_ids())

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📢 <b>Рассылка объявления</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Получателей: <b>{user_count}</b> пользователей\n\n"
        f"Введите текст сообщения.\n"
        f"<i>Поддерживается HTML-форматирование.</i>",
        reply_markup=CANCEL_ONLY_KB,
    )
    await callback.answer()


# ── Message: получение текста рассылки (FSM state) ────────────────────────────
@admin_router.message(BroadcastState.waiting_for_text)
async def handle_broadcast_text(message: Message, state: FSMContext) -> None:
    text = message.html_text or ""
    if not text.strip():
        await message.answer("⚠️ Текст не может быть пустым. Введите сообщение:")
        return

    # Сохраняем текст в FSM data
    await state.update_data(broadcast_text=text)

    # Показываем предпросмотр с кнопками подтверждения
    divider = "─" * 28
    preview = (
        f"📋 <b>Предпросмотр сообщения:</b>\n"
        f"<code>{divider}</code>\n"
        f"{text}\n"
        f"<code>{divider}</code>\n\n"
        f"Отправить это сообщение <b>всем</b> пользователям?"
    )
    await message.answer(preview, reply_markup=CONFIRM_BROADCAST_KB)


# ── Callback: ✅ Подтвердить рассылку ─────────────────────────────────────────
@admin_router.callback_query(F.data == "adm:confirm")
async def cb_confirm_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    broadcast_text: str = data.get("broadcast_text", "")
    await state.clear()

    if not broadcast_text:
        await callback.answer("⚠️ Текст рассылки утерян. Начните заново.", show_alert=True)
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚙️ <b>Панель администратора</b>\n\nВыберите действие:",
            reply_markup=ADMIN_MENU_KB,
        )
        return

    user_ids = await get_all_user_ids()
    total = len(user_ids)

    # Сразу обновляем статус "рассылаю..."
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📤 <b>Рассылаю сообщение...</b>\n"
        f"Получателей: <b>{total}</b>"
    )
    await callback.answer()

    bot: Bot = callback.bot  # type: ignore[assignment]
    ok = 0
    fail = 0

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, broadcast_text)
            ok += 1
            await asyncio.sleep(0.05)   # ≈ 20 сообщений/сек, в рамках лимитов Telegram
        except Exception as exc:
            logger.warning("Broadcast: cannot send to user %s — %s", user_id, exc)
            fail += 1

    # Итоговый отчёт
    result_text = (
        f"✅ <b>Рассылка завершена!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📨  Доставлено: <b>{ok}</b> из <b>{total}</b>\n"
        f"❌  Ошибок:     <b>{fail}</b>"
    )
    await callback.message.edit_text(result_text, reply_markup=ADMIN_MENU_KB)  # type: ignore[union-attr]
    logger.info("Broadcast complete: ok=%s fail=%s total=%s", ok, fail, total)


# ── Callback: ❌ Отмена ───────────────────────────────────────────────────────
@admin_router.callback_query(F.data == "adm:cancel")
async def cb_cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        "⚙️ <b>Панель администратора</b>\n\n"
        "Действие отменено. Выберите следующий шаг:",
        reply_markup=ADMIN_MENU_KB,
    )
    await callback.answer("Отменено ✓")
