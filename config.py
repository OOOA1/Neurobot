# -*- coding: utf-8 -*-
from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("VEO_API_KEY", "")


class Settings(BaseModel):
    """Application-level configuration derived from environment variables."""

    APP_ENV: str = os.getenv("APP_ENV", "dev")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    BOT_TOKEN: str = _BOT_TOKEN or os.getenv("TG_BOT_TOKEN", "")
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "") or _BOT_TOKEN

    GEMINI_API_KEY: str = _GEMINI_API_KEY
    LUMA_API_KEY: str = os.getenv("LUMA_API_KEY", "")

    FREE_TOKENS_ON_JOIN: int = int(os.getenv("FREE_TOKENS_ON_JOIN", 2))
    MAX_ACTIVE_JOBS_PER_USER: int = int(os.getenv("MAX_ACTIVE_JOBS_PER_USER", 1))
    DAILY_JOB_LIMIT: int = int(os.getenv("DAILY_JOB_LIMIT", 20))

    POLL_TIMEOUT: int = int(os.getenv("POLL_TIMEOUT", 25))
    JOB_POLL_INTERVAL_SEC: int = int(os.getenv("JOB_POLL_INTERVAL_SEC", 8))
    JOB_MAX_WAIT_MIN: int = int(os.getenv("JOB_MAX_WAIT_MIN", 20))

    TEXT_BLOCK_SCORE: float = float(os.getenv("TEXT_BLOCK_SCORE", 0.8))
    TEXT_SOFT_SCORE: float = float(os.getenv("TEXT_SOFT_SCORE", 0.6))


settings = Settings()
