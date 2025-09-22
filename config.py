# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Set, Tuple

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Загружаем .env до чтения переменных
load_dotenv()

# ---------- Вспомогательные функции ----------

def _coalesce_env(*names: str, default: str = "") -> str:
    """Берём первое непустое значение из списка имён переменных окружения."""
    for n in names:
        v = os.getenv(n, "")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


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


def _resolve_executable(p: str, fallback: str) -> str:
    """
    Если задан абсолютный/относительный путь к .exe — норм.
    Если пусто — вернём fallback (ожидается, что он есть в PATH).
    """
    p = (p or "").strip()
    if not p:
        return fallback
    # Раскрываем ~ и переменные окружения
    p = os.path.expandvars(os.path.expanduser(p))
    # Нормализуем слэши
    p = str(Path(p))
    return p


# ---------- Совместимость по токенам ----------

# Токен бота: поддерживаем BOT_TOKEN и TG_BOT_TOKEN
_BOT_TOKEN = _coalesce_env("BOT_TOKEN", "TG_BOT_TOKEN")

# Gemini / Veo / Google API ключ:
# Поддерживаем GEMINI_API_KEY, VEO_API_KEY и GOOGLE_API_KEY как синонимы
_GEMINI_API_KEY = _coalesce_env("GEMINI_API_KEY", "VEO_API_KEY", "GOOGLE_API_KEY")

# ---------- Модель конфигурации ----------

class Settings(BaseModel):
    """Application-level configuration derived from environment variables."""

    # Общие
    APP_ENV: str = os.getenv("APP_ENV", "dev")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Токены бота (оставляем оба поля для совместимости)
    BOT_TOKEN: str = _BOT_TOKEN
    TG_BOT_TOKEN: str = _BOT_TOKEN  # дублируем, чтобы старый код не ломался

    # Ключи провайдеров
    GEMINI_API_KEY: str = _GEMINI_API_KEY
    LUMA_API_KEY: str = os.getenv("LUMA_API_KEY", "")

    # Настройки моделей/провайдеров
    VEO_MODEL_NAME: str = os.getenv("VEO_MODEL_NAME", "veo-3.0-fast-generate-001")

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

    # FFmpeg/FFprobe
    # Если пути не заданы — используем имена бинарей и рассчитываем на PATH
    FFMPEG_PATH: str = Field(default_factory=lambda: _resolve_executable(os.getenv("FFMPEG_PATH", ""), "ffmpeg"))
    FFPROBE_PATH: str = Field(default_factory=lambda: _resolve_executable(os.getenv("FFPROBE_PATH", ""), "ffprobe"))

    # Доп. настройки кодека/логирования для media_tools (опционально)
    VIDEO_CRF: int = int(os.getenv("VIDEO_CRF", 18))
    FFMPEG_PRESET: str = os.getenv("FFMPEG_PRESET", "slow")
    FFMPEG_LOG_CMD: bool = os.getenv("FFMPEG_LOG_CMD", "0").lower() in ("1", "true", "yes")

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

    def ffmpeg_bins(self) -> Tuple[str, str]:
        """Удобный доступ к путям до ffmpeg/ffprobe (с учётом .env/PATH)."""
        return self.FFMPEG_PATH, self.FFPROBE_PATH


settings = Settings()

# --------- Экспорт переменных окружения для совместимости ---------
# Некоторые библиотеки и наши утилиты (services/media_tools.py) читают переменные
# окружения напрямую. Подставим значения из Settings, если они ещё не заданы.

if not os.getenv("FFMPEG_PATH") and settings.FFMPEG_PATH:
    os.environ["FFMPEG_PATH"] = settings.FFMPEG_PATH

if not os.getenv("FFPROBE_PATH") and settings.FFPROBE_PATH:
    os.environ["FFPROBE_PATH"] = settings.FFPROBE_PATH

# SDK Google может ожидать GOOGLE_API_KEY в окружении
if not os.getenv("GEMINI_API_KEY") and settings.GEMINI_API_KEY:
    os.environ["GEMINI_API_KEY"] = settings.GEMINI_API_KEY
if not os.getenv("GOOGLE_API_KEY") and settings.GEMINI_API_KEY:
    os.environ["GOOGLE_API_KEY"] = settings.GEMINI_API_KEY
