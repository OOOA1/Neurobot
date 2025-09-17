import aiosqlite
import os

_DB_PATH = os.path.join(os.getcwd(), "mvp.sqlite3")

def connect():
    # возвращаем контекст-менеджер, БЕЗ await здесь
    return aiosqlite.connect(_DB_PATH)

async def _prepare(db: aiosqlite.Connection):
    db.row_factory = aiosqlite.Row
    return db

async def migrate():
    async with connect() as db:
        await _prepare(db)
        for sql in _MIGRATIONS:
            await db.executescript(sql)
            await db.commit()


async def get_user_by_tg(db, tg_user_id: int):
    """Получает пользователя по его Telegram ID."""
    cur = await db.execute(
        "SELECT * FROM users WHERE tg_user_id = ?",
        (tg_user_id,)
    )
    return await cur.fetchone()


async def ensure_user(db, tg_user_id: int, username: str | None, free_tokens: int):
    """Создает нового пользователя, если он не существует."""
    user = await get_user_by_tg(db, tg_user_id)
    if user:
        return user
    
    await db.execute(
        "INSERT INTO users (tg_user_id, username, balance_tokens) VALUES (?,?,?)",
        (tg_user_id, username, free_tokens),
    )
    await db.commit()
    return await get_user_by_tg(db, tg_user_id)


async def create_job(db, user_id: int, model: str, aspect: str, prompt: str):
    """Создает новую задачу для пользователя."""
    await db.execute(
        "INSERT INTO jobs (user_id, model, aspect, prompt_text, status) VALUES (?,?,?,?,?)",
        (user_id, str(model), aspect, prompt, "queued"),
    )
    await db.commit()
    
    cur = await db.execute("SELECT last_insert_rowid() AS id")
    row = await cur.fetchone()
    return row["id"]


async def set_job_status(db, job_id: int, status: str, result_tg_file_id: str | None = None):
    """Обновляет статус задачи и, опционально, ID файла результата."""
    await db.execute(
        "UPDATE jobs SET status = ?, result_tg_file_id = COALESCE(?, result_tg_file_id) WHERE id = ?",
        (status, result_tg_file_id, job_id),
    )
    await db.commit()


async def get_job(db, job_id: int):
    """Получает задачу по её ID."""
    cur = await db.execute(
        "SELECT * FROM jobs WHERE id = ?",
        (job_id,)
    )
    return await cur.fetchone()


# SQL-запросы для миграции базы данных
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
        model TEXT NOT NULL,
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