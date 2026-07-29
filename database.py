"""
database.py — асинхронная работа с SQLite через aiosqlite.

Таблицы:
  • users         — user_id + выбранный язык
  • items         — отслеживаемые товары (ссылка, название, цена, старая цена, наличие, вариант, JSON вариантов)
  • price_history — история изменения цен товаров для формирования графиков динамики

Правила:
  - user_id всегда приводится к int
  - После каждого INSERT / UPDATE / DELETE вызывается conn.commit()
  - Путь к БД фиксирован через Path(__file__).resolve().parent
  - Поддерживается авто-миграция существующей БД (новые колонки добавляются безопасно)
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Единый явный абсолютный путь к файлу БД относительно корня проекта
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str((BASE_DIR / "price_sniper.db").resolve()))


# ── Инициализация ─────────────────────────────────────────────────────────────
async def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет.
    При повторном запуске безопасно добавляет новые колонки (авто-миграция).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # ── Таблица пользователей ─────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER UNIQUE NOT NULL,
                language TEXT    NOT NULL DEFAULT 'en'
            )
        """)

        # ── Таблица товаров (полная схема) ────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                url              TEXT    NOT NULL,
                title            TEXT,
                price            TEXT,
                old_price        TEXT,
                discount_percent INTEGER,
                currency         TEXT,
                in_stock         INTEGER NOT NULL DEFAULT 0,
                selected_variant TEXT,
                variants_json    TEXT,
                created_at       TEXT    NOT NULL,
                updated_at       TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # ── Таблица истории цен ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER NOT NULL,
                price      REAL    NOT NULL,
                old_price  REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            )
        """)
        await db.commit()

        # ── Авто-миграция: добавляем новые колонки в существующую БД ─────────
        new_columns = [
            ("selected_variant", "TEXT"),
            ("variants_json",    "TEXT"),
            ("updated_at",       "TEXT"),
            ("old_price",        "TEXT"),
            ("discount_percent", "INTEGER"),
        ]
        async with db.execute("PRAGMA table_info(items)") as cur:
            existing = {row[1] for row in await cur.fetchall()}

        for col_name, col_type in new_columns:
            if col_name not in existing:
                try:
                    await db.execute(
                        f"ALTER TABLE items ADD COLUMN {col_name} {col_type}"
                    )
                    await db.commit()
                    logger.info("[DB Migration] Added column: items.%s", col_name)
                except Exception as exc:
                    logger.warning("[DB Migration] Could not add column %s: %s", col_name, exc)

        # Совместимость: если в старой БД колонка называется is_in_stock, а не in_stock
        if "is_in_stock" in existing and "in_stock" not in existing:
            try:
                await db.execute(
                    "ALTER TABLE items ADD COLUMN in_stock INTEGER NOT NULL DEFAULT 0"
                )
                await db.execute("UPDATE items SET in_stock = is_in_stock")
                await db.commit()
                logger.info("[DB Migration] Migrated is_in_stock -> in_stock")
            except Exception as exc:
                logger.warning("[DB Migration] in_stock migration failed: %s", exc)


# ── Утилиты ───────────────────────────────────────────────────────────────────
def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_price_num(val: float | str | None) -> float | None:
    """Очищает форматированную цену и возвращает чистый float или None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    
    s = str(val).strip()
    if not s:
        return None
    
    cleaned = re.sub(r"[^\d.,]", "", s)
    if not cleaned:
        return None
    
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
            
    try:
        res = float(cleaned)
        return res if res > 0 else None
    except ValueError:
        return None


# ── История цен ───────────────────────────────────────────────────────────────
async def add_price_record(
    item_id: int,
    price: float | str,
    old_price: float | str | None = None,
) -> None:
    """
    Сохраняет новую точку истории цен в таблицу price_history.
    """
    item_id = int(item_id)
    p_float = _clean_price_num(price)
    if p_float is None:
        return

    op_float = _clean_price_num(old_price)
    now = _now_utc()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO price_history (item_id, price, old_price, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, p_float, op_float, now),
        )
        await db.commit()


async def get_price_history(item_id: int, limit: int = 30) -> list[tuple[str, float]]:
    """
    Возвращает хронологический список кортежей [(created_at, price), ...]
    для товара, отсортированный по возрастанию даты.
    """
    item_id = int(item_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT created_at, price
            FROM (
                SELECT created_at, price, id
                FROM   price_history
                WHERE  item_id = ?
                ORDER  BY id DESC
                LIMIT  ?
            )
            ORDER BY id ASC
            """,
            (item_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [(str(row[0]), float(row[1])) for row in rows]


# ── Users ─────────────────────────────────────────────────────────────────────
async def ensure_user(user_id: int, default_lang: str = "en") -> None:
    """Гарантирует, что пользователь существует в БД."""
    user_id = int(user_id)
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
    user_id = int(user_id)
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
    user_id = int(user_id)
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
    selected_variant: str | None = None,
    variants: list[dict] | None = None,
    old_price: str | None = None,
    discount_percent: int | None = None,
) -> int:
    """
    Сохраняет товар и возвращает его id.
    Опционально сохраняет JSON-список вариантов, выбранный вариант, старую цену и процент скидки.
    Также создаёт первую запись в источнике истории цен.
    """
    user_id = int(user_id)
    now = _now_utc()
    variants_json_str = json.dumps(variants, ensure_ascii=False) if variants else None

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO items
                (user_id, url, title, price, old_price, discount_percent, currency,
                 in_stock, selected_variant, variants_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, url, title, price, old_price, discount_percent, currency,
                int(is_in_stock), selected_variant, variants_json_str, now, now,
            ),
        )
        await db.commit()
        item_id = cursor.lastrowid  # type: ignore[assignment]

    if item_id:
        await add_price_record(item_id, price, old_price)

    return item_id  # type: ignore[return-value]


# Псевдоним функции сохранения
add_item = save_item


async def get_item_by_id(item_id: int) -> dict | None:
    """Возвращает товар по его id или None, если не найден."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_items(user_id: int) -> list[dict]:
    """Возвращает все товары пользователя, отсортированные по дате добавления."""
    user_id = int(user_id)
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
    old_price: str | None = None,
    discount_percent: int | None = None,
) -> None:
    """
    Обновляет цену, старую цену, скидку, валюту и наличие товара.
    Сохраняет новую точку в истории цен.
    """
    item_id = int(item_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE items
            SET    price = ?, old_price = ?, discount_percent = ?,
                   currency = ?, in_stock = ?, updated_at = ?
            WHERE  id = ?
            """,
            (price, old_price, discount_percent, currency, int(is_in_stock), _now_utc(), item_id),
        )
        await db.commit()

    await add_price_record(item_id, price, old_price)


# Совместимый алиас для планировщика
update_item_price = update_item_data


async def update_item_variant(
    item_id: int,
    url: str,
    title: str,
    price: str,
    currency: str,
    is_in_stock: bool,
    selected_variant: str | None = None,
    old_price: str | None = None,
    discount_percent: int | None = None,
) -> None:
    """
    Обновляет URL, название, цену, старую цену, скидку, валюту, наличие и выбранный вариант.
    Сохраняет новую точку в истории цен.
    """
    item_id = int(item_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE items
            SET    url = ?, title = ?, price = ?, old_price = ?, discount_percent = ?,
                   currency = ?, in_stock = ?, selected_variant = ?, updated_at = ?
            WHERE  id = ?
            """,
            (
                url, title, price, old_price, discount_percent,
                currency, int(is_in_stock), selected_variant, _now_utc(), item_id,
            ),
        )
        await db.commit()

    await add_price_record(item_id, price, old_price)


async def update_item_variants_json(item_id: int, variants: list[dict]) -> None:
    """
    Сохраняет JSON-список всех вариантов товара.
    """
    item_id = int(item_id)
    variants_json_str = json.dumps(variants, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE items SET variants_json = ?, updated_at = ? WHERE id = ?",
            (variants_json_str, _now_utc(), item_id),
        )
        await db.commit()


async def delete_item(item_id: int, user_id: int) -> None:
    """
    Удаляет товар из БД.
    """
    user_id = int(user_id)
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
    Возвращает агрегированную статистику для панели администратора.
    """
    from urllib.parse import urlparse

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            user_count: int = (await cur.fetchone())[0]  # type: ignore[index]

        async with db.execute("SELECT COUNT(*) FROM items") as cur:
            item_count: int = (await cur.fetchone())[0]  # type: ignore[index]

        async with db.execute("SELECT url FROM items") as cur:
            urls = [row[0] for row in await cur.fetchall()]

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
