# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from typing import Iterable, Set

from dotenv import load_dotenv
from pydantic import BaseModel

# Загружаем .env до чтения переменных
load_dotenv()

# Поддержка двух переменных для токена — берём любую доступную (но не «затираем» вторую)
_BOT_TOKEN = os.getenv("BOT_TOKEN", "") or ""
_TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "") or ""
if not _BOT_TOKEN and _TG_BOT_TOKEN:
    _BOT_TOKEN = _TG_BOT_TOKEN
if not _TG_BOT_TOKEN and _BOT_TOKEN:
    _TG_BOT_TOKEN = _BOT_TOKEN

# Backward-compat: допускаем VEO_API_KEY как синоним GEMINI_API_KEY
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("VEO_API_KEY", "")


def _parse_admin_ids(raw: object) -> Set[int]:
    """
    Универсальный парсер ADMIN_USER_IDS:
    - "1,2,3" / "1 2 3" / "1;2;3"
    - JSON-массивы: "[1, 2, 3]" или '["1","2"]'
    - Python-подобные коллекции: "{1,2}" / "(1,2)"
    - Уже-построенные коллекции (list/tuple/set)
    """
    ids: Set[int] = set()

    # Коллекции в рантайме
    if isinstance(raw, (list, tuple, set)):
        for x in raw:
            try:
                ids.add(int(x))
            except Exception:
                continue
        return ids

    s = str(raw or "").strip()
    if not s:
        return ids

    # Попытка распарсить как JSON/псевдо-JSON
    if s[0] in "[{(" and s[-1] in "]})":
        try:
            data = json.loads(
                s.replace("(", "[").replace(")", "]").replace("{", "[").replace("}", "]")
            )
            if isinstance(data, (list, tuple, set)):
                for x in data:
                    try:
                        ids.add(int(x))
                    except Exception:
                        continue
                return ids
        except Exception:
            pass  # пойдём простым путём

    # Простые разделители
    s = s.replace(";", ",").replace(" ", ",")
    for token in (t for t in s.split(",") if t):
        try:
            ids.add(int(token))
        except Exception:
            continue

    return ids


class Settings(BaseModel):
    """Application-level configuration derived from environment variables."""

    # Общие
    APP_ENV: str = os.getenv("APP_ENV", "dev")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Токены бота
    BOT_TOKEN: str = _BOT_TOKEN
    TG_BOT_TOKEN: str = _TG_BOT_TOKEN  # оставляем для совместимости

    # Ключи провайдеров
    GEMINI_API_KEY: str = _GEMINI_API_KEY
    LUMA_API_KEY: str = os.getenv("LUMA_API_KEY", "")

    # Биллинг/квоты
    FREE_TOKENS_ON_JOIN: int = int(os.getenv("FREE_TOKENS_ON_JOIN", 2))
    MAX_ACTIVE_JOBS_PER_USER: int = int(os.getenv("MAX_ACTIVE_JOBS_PER_USER", 1))
    DAILY_JOB_LIMIT: int = int(os.getenv("DAILY_JOB_LIMIT", 20))

    # Пулы/ожидания
    POLL_TIMEOUT: int = int(os.getenv("POLL_TIMEOUT", 25))
    JOB_POLL_INTERVAL_SEC: int = int(os.getenv("JOB_POLL_INTERVAL_SEC", 8))
    JOB_MAX_WAIT_MIN: int = int(os.getenv("JOB_MAX_WAIT_MIN", 20))

    # Модерация текста
    TEXT_BLOCK_SCORE: float = float(os.getenv("TEXT_BLOCK_SCORE", 0.8))
    TEXT_SOFT_SCORE: float = float(os.getenv("TEXT_SOFT_SCORE", 0.6))

    # Прочее
    ADMIN_USER_IDS: str = os.getenv("ADMIN_USER_IDS", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    PROMO_TTL_HOURS: int = int(os.getenv("PROMO_TTL_HOURS", 3))

    # ---------- Утилиты ----------
    def admin_ids(self) -> Set[int]:
        """Возвращает множество admin user ids (надёжный парсинг из .env/окружения)."""
        return _parse_admin_ids(self.ADMIN_USER_IDS)

    def is_admin(self, user_id: int | str) -> bool:
        """Проверка, что пользователь — админ (удобно вызывать из хендлеров)."""
        try:
            uid = int(user_id)
        except Exception:
            return False
        return uid in self.admin_ids()

    def bot_username_clean(self) -> str:
        """Возвращает username бота без префикса @."""
        return self.BOT_USERNAME.lstrip("@") if self.BOT_USERNAME else ""


settings = Settings()
