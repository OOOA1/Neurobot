# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
from typing import Any, List, Tuple

import aiosqlite

from providers.base import Provider

_DB_PATH = os.path.join(os.getcwd(), "mvp.sqlite3")

# Стоимость одной генерации (в токенах)
GENERATION_COST_TOKENS = 2


def connect() -> aiosqlite.Connection:
    """Return raw sqlite connection (aio)."""
    return aiosqlite.connect(_DB_PATH)


async def _prepare(db: aiosqlite.Connection) -> aiosqlite.Connection:
    db.row_factory = aiosqlite.Row
    return db


# -------------------------
# Баланс / токены (helpers)
# -------------------------

async def get_user_balance(db: aiosqlite.Connection, tg_user_id: int) -> int:
    cur = await db.execute(
        "SELECT balance_tokens FROM users WHERE tg_user_id = ?",
        (tg_user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row["balance_tokens"]) if row and row["balance_tokens"] is not None else 0


async def set_user_balance(db: aiosqlite.Connection, tg_user_id: int, new_balance: int) -> None:
    await db.execute(
        "UPDATE users SET balance_tokens = ? WHERE tg_user_id = ?",
        (new_balance, tg_user_id),
    )
    await db.commit()


async def add_user_tokens(db: aiosqlite.Connection, tg_user_id: int, amount: int) -> None:
    await db.execute(
        "UPDATE users SET balance_tokens = balance_tokens + ? WHERE tg_user_id = ?",
        (amount, tg_user_id),
    )
    await db.commit()


async def charge_user_tokens(db: aiosqlite.Connection, tg_user_id: int, amount: int) -> bool:
    """
    Атомарно списывает amount токенов.
    Возвращает True, если списание успешно, иначе False (недостаточно токенов).
    """
    cur = await db.execute(
        """
        UPDATE users
           SET balance_tokens = balance_tokens - ?
         WHERE tg_user_id = ?
           AND balance_tokens >= ?
        """,
        (amount, tg_user_id, amount),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def refund_user_tokens(db: aiosqlite.Connection, tg_user_id: int, amount: int) -> None:
    """Возврат токенов пользователю (на случай неудачной генерации)."""
    await add_user_tokens(db, tg_user_id, amount)


# -------------------------
# Перевод токенов между пользователями
# -------------------------

async def transfer_tokens(db: aiosqlite.Connection, from_tg: int, to_tg: int, amount: int) -> bool:
    """
    Перевод токенов от одного пользователя к другому (атомарно).
    Возвращает True при успехе.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            """
            UPDATE users
               SET balance_tokens = balance_tokens - ?
             WHERE tg_user_id = ?
               AND balance_tokens >= ?
            """,
            (amount, from_tg, amount),
        )
        if (cur.rowcount or 0) == 0:
            await db.execute("ROLLBACK")
            return False
        await db.execute(
            "UPDATE users SET balance_tokens = balance_tokens + ? WHERE tg_user_id = ?",
            (amount, to_tg),
        )
        await db.execute("COMMIT")
        return True
    except Exception:
        await db.execute("ROLLBACK")
        raise


# -------------------------
# Миграции / схема
# -------------------------

async def migrate() -> None:
    """Apply schema migrations and column backfills."""
    async with connect() as db:
        await _prepare(db)
        for sql in _MIGRATIONS:
            await db.executescript(sql)
            await db.commit()
        await _ensure_job_schema(db)
        await _ensure_user_columns(db)
        await _ensure_promocodes_schema(db)          # скидочные промокоды (если используешь)
        await _ensure_token_promocodes_schema(db)    # одноразовые промокоды на токены
        await _ensure_token_promo_campaigns_schema(db)  # многоразовые промокоды с TTL


async def _ensure_user_columns(db: aiosqlite.Connection) -> None:
    """Добиваем недостающие колонки в users (например, discount_percent)."""
    cur = await db.execute("PRAGMA table_info(users)")
    columns = {row["name"] for row in await cur.fetchall()}
    await cur.close()

    if "discount_percent" not in columns:
        await db.execute("ALTER TABLE users ADD COLUMN discount_percent INTEGER DEFAULT 0")
        await db.commit()


async def _ensure_promocodes_schema(db: aiosqlite.Connection) -> None:
    """Создаём таблицу промокодов-СКИДОК и уникальный индекс, если их нет."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            discount_percent INTEGER NOT NULL,
            is_used INTEGER DEFAULT 0,
            used_by INTEGER,
            created_by INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_promocodes_code ON promocodes(code);
        """
    )
    await db.commit()


async def _ensure_token_promocodes_schema(db: aiosqlite.Connection) -> None:
    """Создаём таблицу промокодов с начислением токенов (одноразовые)."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            tokens INTEGER NOT NULL,
            is_used INTEGER DEFAULT 0,
            used_by INTEGER,
            created_by INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_token_promocodes_code ON token_promocodes(code);
        """
    )
    await db.commit()


async def _ensure_token_promo_campaigns_schema(db: aiosqlite.Connection) -> None:
    """Таблицы многоразовых промокодов с TTL и учётом уникальных активаций."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_promo_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            tokens INTEGER NOT NULL,
            starts_at INTEGER DEFAULT (strftime('%s','now')),
            expires_at INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS token_promo_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            tg_user_id INTEGER NOT NULL,
            used_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(campaign_id, tg_user_id),
            FOREIGN KEY(campaign_id) REFERENCES token_promo_campaigns(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_token_promo_campaigns_code ON token_promo_campaigns(code);
        CREATE INDEX IF NOT EXISTS idx_token_promo_redemptions_campaign ON token_promo_redemptions(campaign_id);
        """
    )
    await db.commit()


# -------------------------
# Пользователи
# -------------------------

async def get_user_by_tg(db: aiosqlite.Connection, tg_user_id: int):
    """Fetch single user row by Telegram user id."""
    cur = await db.execute(
        "SELECT * FROM users WHERE tg_user_id = ?",
        (tg_user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row


async def get_user_by_username(db: aiosqlite.Connection, username: str):
    """Ищем пользователя по username (без @, регистронезависимо)."""
    username = (username or "").lstrip("@")
    cur = await db.execute(
        "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
        (username,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row


async def ensure_user(
    db: aiosqlite.Connection,
    tg_user_id: int,
    username: str | None,
    free_tokens: int,
):
    """Create user if missing and return current row."""
    user = await get_user_by_tg(db, tg_user_id)
    if user:
        return user

    await db.execute(
        "INSERT INTO users (tg_user_id, username, balance_tokens) VALUES (?,?,?)",
        (tg_user_id, username, free_tokens),
    )
    await db.commit()
    return await get_user_by_tg(db, tg_user_id)


# -------------------------
# Джобы (генерации)
# -------------------------

async def create_job(
    db: aiosqlite.Connection,
    user_id: int,
    provider: Provider,
    *,
    prompt: str,
    aspect: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    status: str = "queued",
    provider_job_id: str | None = None,
) -> int:
    """Insert new generation job row and return primary key."""
    await db.execute(
        """
        INSERT INTO jobs (user_id, provider, model, mode, aspect, prompt_text, status, provider_job_id)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            user_id,
            provider.value,
            model,
            mode,
            aspect,
            prompt,
            status,
            provider_job_id,
        ),
    )
    await db.commit()

    cur = await db.execute("SELECT last_insert_rowid() AS id")
    row = await cur.fetchone()
    await cur.close()
    return int(row["id"])  # type: ignore[index]


async def set_job_status(
    db: aiosqlite.Connection,
    job_id: int,
    status: str,
    *,
    result_tg_file_id: str | None = None,
) -> None:
    """Update status (and optionally telegram file id) for job."""
    await db.execute(
        """
        UPDATE jobs
           SET status = ?,
               result_tg_file_id = COALESCE(?, result_tg_file_id)
         WHERE id = ?
        """,
        (status, result_tg_file_id, job_id),
    )
    await db.commit()


async def set_provider_job_id(
    db: aiosqlite.Connection,
    job_id: int,
    provider_job_id: str,
) -> None:
    """Persist provider-specific job identifier."""
    await db.execute(
        "UPDATE jobs SET provider_job_id = ? WHERE id = ?",
        (provider_job_id, job_id),
    )
    await db.commit()


async def get_job(db: aiosqlite.Connection, job_id: int):
    """Fetch job row by primary key."""
    cur = await db.execute(
        "SELECT * FROM jobs WHERE id = ?",
        (job_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row


async def _ensure_job_schema(db: aiosqlite.Connection) -> None:
    """Add new job columns when migrating from older versions."""
    cur = await db.execute("PRAGMA table_info(jobs)")
    columns = {row["name"] for row in await cur.fetchall()}
    await cur.close()
    if "provider" not in columns:
        await db.execute("ALTER TABLE jobs ADD COLUMN provider TEXT")
        await db.commit()


# -------------------------
# Промокоды-СКИДКИ (старые)
# -------------------------

async def create_promocode(
    db: aiosqlite.Connection,
    code: str,
    discount_percent: int,
    created_by_tg: int,
) -> None:
    """Создать одноразовый промокод-скидку (если используешь скидки)."""
    code = code.strip()
    discount_percent = max(1, min(100, int(discount_percent)))
    await db.execute(
        """
        INSERT INTO promocodes (code, discount_percent, is_used, created_by)
        VALUES (?, ?, 0, ?)
        """,
        (code, discount_percent, created_by_tg),
    )
    await db.commit()


async def list_promocodes(db: aiosqlite.Connection, limit: int = 20) -> list[dict[str, Any]]:
    cur = await db.execute(
        """
        SELECT code, discount_percent, is_used, used_by, created_by, created_at
          FROM promocodes
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def redeem_promocode(
    db: aiosqlite.Connection,
    tg_user_id: int,
    code: str,
    min_percent: int = 0,
) -> bool:
    """
    Отметить промокод-скидку использованным (одноразово) и применить скидку пользователю.
    Возвращает True, если код успешно применён.
    """
    code = (code or "").strip()
    if not code:
        return False

    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            "SELECT id, discount_percent, is_used FROM promocodes WHERE code = ?",
            (code,),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row or int(row["is_used"] or 0) != 0:
            await db.execute("ROLLBACK")
            return False

        cur2 = await db.execute(
            "UPDATE promocodes SET is_used = 1, used_by = ? WHERE code = ? AND is_used = 0",
            (tg_user_id, code),
        )
        if (cur2.rowcount or 0) == 0:
            await db.execute("ROLLBACK")
            return False

        await db.execute("COMMIT")
        return True
    except Exception:
        await db.execute("ROLLBACK")
        raise


# -------------------------
# Промокоды на ТОКЕНЫ (одноразовые)
# -------------------------

async def create_token_promocode(
    db: aiosqlite.Connection,
    code: str,
    tokens: int,
    created_by_tg: int,
) -> None:
    """Создать одноразовый промокод, начисляющий токены."""
    code = (code or "").strip().upper()
    tokens = max(1, int(tokens))
    await db.execute(
        "INSERT INTO token_promocodes (code, tokens, is_used, created_by) VALUES (?,?,0,?)",
        (code, tokens, created_by_tg),
    )
    await db.commit()


async def list_token_promocodes(db: aiosqlite.Connection, limit: int = 30) -> list[dict[str, Any]]:
    cur = await db.execute(
        """
        SELECT code, tokens, is_used, used_by, created_by, created_at
          FROM token_promocodes
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def redeem_token_promocode(
    db: aiosqlite.Connection,
    tg_user_id: int,
    code: str,
) -> int:
    """
    Пометить код использованным и начислить токены пользователю в одной транзакции.
    Возвращает количество начисленных токенов (>0 при успехе, 0 — код не найден/уже использован).
    """
    code = (code or "").strip().upper()
    if not code:
        return 0

    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            "SELECT id, tokens, is_used FROM token_promocodes WHERE code = ?",
            (code,),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row or int(row["is_used"] or 0) != 0:
            await db.execute("ROLLBACK")
            return 0

        tokens = int(row["tokens"])

        # помечаем использованным
        cur2 = await db.execute(
            "UPDATE token_promocodes SET is_used = 1, used_by = ? WHERE code = ? AND is_used = 0",
            (tg_user_id, code),
        )
        if (cur2.rowcount or 0) == 0:
            await db.execute("ROLLBACK")
            return 0

        # начисляем токены пользователю
        await db.execute(
            "UPDATE users SET balance_tokens = COALESCE(balance_tokens,0) + ? WHERE tg_user_id = ?",
            (tokens, tg_user_id),
        )

        await db.execute("COMMIT")
        return tokens
    except Exception:
        await db.execute("ROLLBACK")
        raise


# -------------------------
# Промокоды на ТОКЕНЫ (многоразовые с TTL)
# -------------------------

async def create_token_promo_campaign(
    db: aiosqlite.Connection,
    code: str,
    tokens: int,
    ttl_hours: int,
    created_by_tg: int,
) -> None:
    """Создать многоразовый промокод с ограничением по времени (TTL)."""
    code = (code or "").strip().upper()
    tokens = max(1, int(tokens))
    ttl_hours = max(1, int(ttl_hours))
    now = int(time.time())
    expires_at = now + ttl_hours * 3600
    await db.execute(
        """
        INSERT INTO token_promo_campaigns (code, tokens, starts_at, expires_at, is_active, created_by)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (code, tokens, now, expires_at, created_by_tg),
    )
    await db.commit()


async def generate_token_promo_codes(
    db: aiosqlite.Connection,
    count: int,
    tokens: int,
    ttl_hours: int,
    created_by_tg: int,
    prefix: str | None = None,
) -> List[str]:
    """
    Сгенерировать пачку кодов-кампаний и вернуть список кодов.
    prefix (опц.) будет добавлен в начало (например, AUTUMN-XXXXXX).
    """
    import secrets, string

    count = max(1, min(100, int(count)))
    tokens = max(1, int(tokens))
    ttl_hours = max(1, int(ttl_hours))
    alphabet = string.ascii_uppercase + string.digits

    codes: List[str] = []
    for _ in range(count):
        rand = "".join(secrets.choice(alphabet) for _ in range(8))
        code = f"{prefix.strip().upper()}-{rand}" if prefix else rand
        await create_token_promo_campaign(db, code, tokens, ttl_hours, created_by_tg)
        codes.append(code)
    return codes


async def redeem_token_promo_code_ttl(
    db: aiosqlite.Connection,
    tg_user_id: int,
    code: str,
) -> Tuple[int, str]:
    """
    Активировать многоразовый код с TTL, если:
      - код существует, активен и не истёк;
      - пользователь ещё не активировал его ранее.
    Возвращает (начислено токенов, статус), где статус: ok|expired|inactive|already_used|not_found.
    """
    code = (code or "").strip().upper()
    if not code:
        return 0, "not_found"

    now = int(time.time())
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            """
            SELECT id, tokens, expires_at, is_active
              FROM token_promo_campaigns
             WHERE code = ?
            """,
            (code,),
        )
        camp = await cur.fetchone()
        await cur.close()
        if not camp:
            await db.execute("ROLLBACK")
            return 0, "not_found"
        if int(camp["is_active"] or 0) == 0:
            await db.execute("ROLLBACK")
            return 0, "inactive"
        if now > int(camp["expires_at"]):
            await db.execute("ROLLBACK")
            return 0, "expired"

        campaign_id = int(camp["id"])
        tokens = int(camp["tokens"])

        # проверяем, активировал ли уже этот пользователь
        cur2 = await db.execute(
            "SELECT 1 FROM token_promo_redemptions WHERE campaign_id = ? AND tg_user_id = ?",
            (campaign_id, tg_user_id),
        )
        used = await cur2.fetchone()
        await cur2.close()
        if used:
            await db.execute("ROLLBACK")
            return 0, "already_used"

        # записываем активацию
        await db.execute(
            "INSERT INTO token_promo_redemptions (campaign_id, tg_user_id) VALUES (?, ?)",
            (campaign_id, tg_user_id),
        )

        # начисляем токены
        await db.execute(
            "UPDATE users SET balance_tokens = COALESCE(balance_tokens,0) + ? WHERE tg_user_id = ?",
            (tokens, tg_user_id),
        )

        await db.execute("COMMIT")
        return tokens, "ok"
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def list_token_promo_campaigns(db: aiosqlite.Connection, limit: int = 30) -> list[dict[str, Any]]:
    cur = await db.execute(
        """
        SELECT c.id, c.code, c.tokens, c.starts_at, c.expires_at, c.is_active, c.created_by, c.created_at,
               COALESCE(r.cnt, 0) AS redemptions
          FROM token_promo_campaigns c
          LEFT JOIN (
              SELECT campaign_id, COUNT(*) AS cnt
                FROM token_promo_redemptions
               GROUP BY campaign_id
          ) r ON r.campaign_id = c.id
         ORDER BY c.created_at DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# Base migrations used for fresh deployments
_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_user_id INTEGER UNIQUE,
        username TEXT,
        balance_tokens INTEGER DEFAULT 0,
        daily_jobs_count INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        cooldown_until INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (strftime('%s','now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        provider TEXT,
        model TEXT,
        mode TEXT,
        aspect TEXT,
        prompt_text TEXT,
        status TEXT,
        provider_job_id TEXT,
        result_tg_file_id TEXT,
        created_at INTEGER DEFAULT (strftime('%s','now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """
]
