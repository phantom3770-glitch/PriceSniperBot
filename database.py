"""
database.py — асинхронная работа с SQLite через aiosqlite.

Таблицы:
  • users  — user_id + выбранный язык
  • items  — отслеживаемые товары (ссылка, название, цена, наличие)
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# Единый явный абсолютный путь к файлу БД относительно корня проекта
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str((BASE_DIR / "price_sniper.db").resolve()))


# ── Инициализация ─────────────────────────────────────────────────────────────
async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER UNIQUE NOT NULL,
                language TEXT    NOT NULL DEFAULT 'en'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                url         TEXT    NOT NULL,
                title       TEXT,
                price       TEXT,
                currency    TEXT,
                is_in_stock INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()


# ── Users ─────────────────────────────────────────────────────────────────────
async def ensure_user(user_id: int, default_lang: str = "en") -> None:
    """
    Гарантирует, что пользователь существует в БД.
    Если запись уже есть, её данные (включая выбор языка) не перезаписываются.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, language) VALUES (?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, default_lang),
        )
        await db.commit()


async def upsert_user(user_id: int, language: str) -> None:
    """Создаёт или обновляет запись пользователя с его языком."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, language) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET language = excluded.language
            """,
            (user_id, language),
        )
        await db.commit()


async def get_user_language(user_id: int) -> str | None:
    """Возвращает язык пользователя из БД или None, если пользователь не найден."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT language FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# ── Items ─────────────────────────────────────────────────────────────────────
async def save_item(
    user_id: int,
    url: str,
    title: str,
    price: str,
    currency: str,
    is_in_stock: bool,
) -> int:
    """Сохраняет товар и возвращает его id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO items (user_id, url, title, price, currency, is_in_stock, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, url, title, price, currency, int(is_in_stock), now),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def get_user_items(user_id: int) -> list[dict]:
    """Возвращает все товары пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM items WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_all_items_with_lang() -> list[dict]:
    """
    Возвращает все товары из всех пользователей, дополненные языком пользователя.
    Используется планировщиком для периодической проверки цен.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT i.*, u.language
            FROM   items i
            JOIN   users u ON i.user_id = u.user_id
            ORDER  BY i.id
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_item_data(
    item_id: int,
    price: str,
    currency: str,
    is_in_stock: bool,
) -> None:
    """
    Обновляет цену, валюту и наличие товара после очередной проверки.
    Вызывается планировщиком только при обнаружении изменений.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE items
            SET    price = ?, currency = ?, is_in_stock = ?
            WHERE  id = ?
            """,
            (price, currency, int(is_in_stock), item_id),
        )
        await db.commit()


async def delete_item(item_id: int, user_id: int) -> None:
    """
    Удаляет товар из БД.
    Проверяет user_id для защиты от удаления чужих товаров.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        )
        await db.commit()


# ── Admin ─────────────────────────────────────────────────────────────────────
async def get_all_user_ids() -> list[int]:
    """Возвращает список всех user_id из таблицы users (для рассылки)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def get_admin_stats() -> dict:
    """
    Возвращает агрегированную статистику для панели администратора:
      - user_count  : общее число пользователей
      - item_count  : общее число товаров в базе
      - top_domains : топ-3 доменов по числу товаров [(domain, count), ...]
    """
    from urllib.parse import urlparse

    async with aiosqlite.connect(DB_PATH) as db:
        # Пользователи
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            user_count: int = (await cur.fetchone())[0]  # type: ignore[index]

        # Товары
        async with db.execute("SELECT COUNT(*) FROM items") as cur:
            item_count: int = (await cur.fetchone())[0]  # type: ignore[index]

        # Все URL для подсчёта доменов
        async with db.execute("SELECT url FROM items") as cur:
            urls = [row[0] for row in await cur.fetchall()]

    # Считаем частоту доменов
    domain_counts: dict[str, int] = {}
    for url in urls:
        try:
            netloc = urlparse(url).netloc
            domain = netloc.removeprefix("www.")
            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
        except Exception:
            pass

    top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    return {
        "user_count": user_count,
        "item_count": item_count,
        "top_domains": top_domains,
    }
