from pydantic import BaseModel
from dotenv import load_dotenv
import os


load_dotenv()


class Settings(BaseModel):
    # Основные настройки приложения
    APP_ENV: str = os.getenv("APP_ENV", "dev")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")

    # API ключи для сервисов
    VEO_API_KEY: str = os.getenv("VEO_API_KEY", "")
    LUMA_API_KEY: str = os.getenv("LUMA_API_KEY", "")

    # Настройки лимитов пользователя
    FREE_TOKENS_ON_JOIN: int = int(os.getenv("FREE_TOKENS_ON_JOIN", 2))
    MAX_ACTIVE_JOBS_PER_USER: int = int(os.getenv("MAX_ACTIVE_JOBS_PER_USER", 1))
    DAILY_JOB_LIMIT: int = int(os.getenv("DAILY_JOB_LIMIT", 20))

    # Настройки таймаутов и интервалов
    POLL_TIMEOUT: int = int(os.getenv("POLL_TIMEOUT", 25))
    JOB_POLL_INTERVAL_SEC: int = int(os.getenv("JOB_POLL_INTERVAL_SEC", 8))
    JOB_MAX_WAIT_MIN: int = int(os.getenv("JOB_MAX_WAIT_MIN", 20))

    # Настройки модерации
    TEXT_BLOCK_SCORE: float = float(os.getenv("TEXT_BLOCK_SCORE", 0.8))
    TEXT_SOFT_SCORE: float = float(os.getenv("TEXT_SOFT_SCORE", 0.6))


settings = Settings()