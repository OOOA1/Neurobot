# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Any

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
    # rowcount > 0 означает, что UPDATE сработал (т.е. токенов хватило)
    return (cur.rowcount or 0) > 0


async def refund_user_tokens(db: aiosqlite.Connection, tg_user_id: int, amount: int) -> None:
    """Возврат токенов пользователю (на случай неудачной генерации)."""
    await add_user_tokens(db, tg_user_id, amount)


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
