"""
Локализация / Internationalization (i18n)
Поддерживаемые языки: uk (Українська), en (English), ru (Русский)

Все тексты бота, кнопки, статусы и уведомления сосредоточены здесь.
Функция t() принимает язык, ключ и именованные параметры для format().
"""

TEXTS: dict[str, dict[str, str]] = {

    # ── Українська 🇺🇦 ─────────────────────────────────────────────────────────
    "uk": {
        # Старт / выбор языка
        "choose_language": "👋 Вітаю! Будь ласка, оберіть мову інтерфейсу:",
        "welcome": (
            "✅ <b>Мова: Українська 🇺🇦</b>\n\n"
            "🎯 <b>PriceSniper</b> — ваш особистий снайпер цін!\n\n"
            "Що я вмію:\n"
            "• Надішліть посилання — я додам товар до списку відстеження\n"
            "• 📋 Переглядайте та керуйте своїми товарами\n"
            "• 🔔 Отримуйте сповіщення про зниження ціни або появу в наявності\n\n"
            "<i>Використовуйте кнопки меню нижче 👇</i>"
        ),
        "language_set": "✅ Мову змінено: Українська 🇺🇦",

        # Кнопки головного меню (ReplyKeyboard)
        "btn_my_items":   "📋 Мої товари",
        "btn_refresh":    "🔄 Оновити ціни",
        "btn_change_lang":"🌐 Змінити мову",

        # Кнопки інлайн-клавіатури на картці товару
        "btn_delete":     "🗑 Видалити",
        "btn_open_link":  "🔗 Перейти",
        "btn_back":       "🔙 Назад",

        # Парсинг
        "parsing_start": "🔍 <i>Снайпер вивчає сторінку...</i>",
        "parse_error": (
            "⚠️ Не вдалося отримати дані про товар.\n"
            "Перевірте посилання або спробуйте пізніше."
        ),

        # Картка товару (використовується у списку та після додавання)
        "item_card": (
            "📌 <b>{title}</b>\n\n"
            "💰 Ціна: <b>{price_str}</b>\n"
            "📦 Статус: {stock_emoji} <b>{stock_label}</b>"
        ),
        "item_added": "✅ <b>Ціль додано до списку відстеження!</b>",

        # Список товарів
        "list_header": "📋 <b>Ваші товари ({count} шт.):</b>",
        "list_empty": (
            "📭 <b>Ваш список порожній.</b>\n\n"
            "Надішліть посилання на товар — і я одразу почну за ним стежити!"
        ),

        # Видалення
        "item_deleted":       "🗑 <i>Товар вилучено з відстеження.</i>",
        "item_deleted_popup": "Товар вилучено ✓",

        # Ручне оновлення
        "refresh_start":    "🔄 <i>Оновлюю дані по вашим товарам...</i>",
        "refresh_done":     "✅ <b>Перевірку завершено!</b> Оновлено: {count} товар(ів).",
        "refresh_no_items": "📭 Немає товарів для оновлення. Надішліть посилання на товар!",

        # Статуси наявності та значення за замовчуванням
        "stock_yes":     "В наявності",
        "stock_no":      "Немає в наявності",
        "price_unknown": "Невідомо",
        "title_unknown": "Назва не знайдена",

        # Сповіщення планировщика
        "price_drop": (
            "🔥 <b>Снайпер зафіксував знижку!</b>\n\n"
            "📌 {title}\n"
            "💰 Стара ціна: <s>{old_price}</s>\n"
            "💵 Нова ціна: <b>{new_price}</b>\n"
            "🔗 <a href='{url}'>Відкрити сторінку</a>"
        ),
        "back_in_stock": (
            "🎉 <b>Товар знову з'явився в наявності!</b>\n\n"
            "📌 {title}\n"
            "💰 Ціна: <b>{price}</b>\n"
            "⏰ Поспішай купити!\n"
            "🔗 <a href='{url}'>Відкрити сторінку</a>"
        ),
    },

    # ── English 🇬🇧 ────────────────────────────────────────────────────────────
    "en": {
        # Start / language selection
        "choose_language": "👋 Hello! Please choose your interface language:",
        "welcome": (
            "✅ <b>Language: English 🇬🇧</b>\n\n"
            "🎯 <b>PriceSniper</b> — your personal price sniper!\n\n"
            "What I can do:\n"
            "• Send a link — I'll add the item to your tracking list\n"
            "• 📋 View and manage your tracked items\n"
            "• 🔔 Get notified when prices drop or items come back in stock\n\n"
            "<i>Use the menu buttons below 👇</i>"
        ),
        "language_set": "✅ Language changed: English 🇬🇧",

        # Main menu buttons (ReplyKeyboard)
        "btn_my_items":   "📋 My Items",
        "btn_refresh":    "🔄 Refresh Prices",
        "btn_change_lang":"🌐 Change Language",

        # Inline keyboard buttons on item card
        "btn_delete":    "🗑 Delete",
        "btn_open_link": "🔗 Open Link",
        "btn_back":      "🔙 Back",

        # Parsing
        "parsing_start": "🔍 <i>Sniper is studying the page...</i>",
        "parse_error": (
            "⚠️ Could not retrieve product data.\n"
            "Check the link or try again later."
        ),

        # Item card
        "item_card": (
            "📌 <b>{title}</b>\n\n"
            "💰 Price: <b>{price_str}</b>\n"
            "📦 Status: {stock_emoji} <b>{stock_label}</b>"
        ),
        "item_added": "✅ <b>Target added to tracking list!</b>",

        # Item list
        "list_header": "📋 <b>Your Items ({count}):</b>",
        "list_empty": (
            "📭 <b>Your list is empty.</b>\n\n"
            "Send a product link — and I'll start tracking it right away!"
        ),

        # Deletion
        "item_deleted":       "🗑 <i>Item removed from tracking.</i>",
        "item_deleted_popup": "Item removed ✓",

        # Manual refresh
        "refresh_start":    "🔄 <i>Refreshing data for your items...</i>",
        "refresh_done":     "✅ <b>Refresh complete!</b> Updated: {count} item(s).",
        "refresh_no_items": "📭 No items to refresh. Send a product link!",

        # Stock status & defaults
        "stock_yes":     "In Stock",
        "stock_no":      "Out of Stock",
        "price_unknown": "Unknown",
        "title_unknown": "Title not found",

        # Scheduler notifications
        "price_drop": (
            "🔥 <b>Sniper spotted a price drop!</b>\n\n"
            "📌 {title}\n"
            "💰 Old price: <s>{old_price}</s>\n"
            "💵 New price: <b>{new_price}</b>\n"
            "🔗 <a href='{url}'>Open page</a>"
        ),
        "back_in_stock": (
            "🎉 <b>Item is back in stock!</b>\n\n"
            "📌 {title}\n"
            "💰 Price: <b>{price}</b>\n"
            "⏰ Grab it while you can!\n"
            "🔗 <a href='{url}'>Open page</a>"
        ),
    },

    # ── Русский 🇷🇺 ────────────────────────────────────────────────────────────
    "ru": {
        # Старт / выбор языка
        "choose_language": "👋 Привет! Пожалуйста, выберите язык интерфейса:",
        "welcome": (
            "✅ <b>Язык: Русский 🇷🇺</b>\n\n"
            "🎯 <b>PriceSniper</b> — ваш персональный снайпер цен!\n\n"
            "Что я умею:\n"
            "• Пришлите ссылку — добавлю товар в список отслеживания\n"
            "• 📋 Просматривайте и управляйте своими товарами\n"
            "• 🔔 Получайте уведомления о снижении цены или появлении товара\n\n"
            "<i>Используйте кнопки меню ниже 👇</i>"
        ),
        "language_set": "✅ Язык изменён: Русский 🇷🇺",

        # Кнопки главного меню (ReplyKeyboard)
        "btn_my_items":   "📋 Мои товары",
        "btn_refresh":    "🔄 Обновить цены",
        "btn_change_lang":"🌐 Сменить язык",

        # Инлайн-кнопки на карточке товара
        "btn_delete":    "🗑 Удалить",
        "btn_open_link": "🔗 Перейти",
        "btn_back":      "🔙 Назад",

        # Парсинг
        "parsing_start": "🔍 <i>Снайпер изучает страницу...</i>",
        "parse_error": (
            "⚠️ Не удалось получить данные о товаре.\n"
            "Проверьте ссылку или попробуйте позже."
        ),

        # Карточка товара
        "item_card": (
            "📌 <b>{title}</b>\n\n"
            "💰 Цена: <b>{price_str}</b>\n"
            "📦 Статус: {stock_emoji} <b>{stock_label}</b>"
        ),
        "item_added": "✅ <b>Цель добавлена в список отслеживания!</b>",

        # Список товаров
        "list_header": "📋 <b>Ваши товары ({count} шт.):</b>",
        "list_empty": (
            "📭 <b>Ваш список пуст.</b>\n\n"
            "Пришлите ссылку на товар — и я сразу начну за ним следить!"
        ),

        # Удаление
        "item_deleted":       "🗑 <i>Товар удалён из отслеживания.</i>",
        "item_deleted_popup": "Товар удалён ✓",

        # Ручное обновление
        "refresh_start":    "🔄 <i>Обновляю данные по вашим товарам...</i>",
        "refresh_done":     "✅ <b>Проверка завершена!</b> Обновлено: {count} товар(ов).",
        "refresh_no_items": "📭 Нет товаров для обновления. Пришлите ссылку на товар!",

        # Статусы наличия и значения по умолчанию
        "stock_yes":     "В наличии",
        "stock_no":      "Нет в наличии",
        "price_unknown": "Неизвестно",
        "title_unknown": "Название не найдено",

        # Уведомления планировщика
        "price_drop": (
            "🔥 <b>Снайпер зафиксировал скидку!</b>\n\n"
            "📌 {title}\n"
            "💰 Старая цена: <s>{old_price}</s>\n"
            "💵 Новая цена: <b>{new_price}</b>\n"
            "🔗 <a href='{url}'>Открыть страницу</a>"
        ),
        "back_in_stock": (
            "🎉 <b>Товар снова появился в наличии!</b>\n\n"
            "📌 {title}\n"
            "💰 Цена: <b>{price}</b>\n"
            "⏰ Успей купить!\n"
            "🔗 <a href='{url}'>Открыть страницу</a>"
        ),
    },
}

# Язык по умолчанию (если в БД нет записи)
DEFAULT_LANG = "en"


def t(lang: str, key: str, **kwargs: str | int) -> str:
    """
    Возвращает локализованный текст.
    Если язык или ключ не найден — использует DEFAULT_LANG.
    Поддерживает format()-подстановки через **kwargs.
    """
    text = (
        TEXTS.get(lang, TEXTS[DEFAULT_LANG])
        .get(key, TEXTS[DEFAULT_LANG].get(key, key))
    )
    return text.format(**kwargs) if kwargs else text
