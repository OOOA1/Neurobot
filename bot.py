# -*- coding: utf-8 -*-
import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import settings
from db import migrate
from handlers import start as start_handlers
from handlers import video as video_handlers
from handlers import balance as balance_handlers
from handlers import promo as promo_handlers
from handlers import gift as gift_handlers
from handlers import referral as referral_handlers


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger(__name__)
    logger.info("APP_ENV: %s", settings.APP_ENV)
    logger.info("GEMINI API Key set: %s", "Yes" if settings.GEMINI_API_KEY else "No")
    logger.info("LUMA API Key set: %s", "Yes" if settings.LUMA_API_KEY else "No")
    try:
        admin_ids = getattr(settings, "admin_ids", None)
        admin_ids_str = ",".join(str(x) for x in (admin_ids() if callable(admin_ids) else []))
    except Exception:
        admin_ids_str = (getattr(settings, "ADMIN_USER_IDS", "") or "").replace(" ", "")
    logger.info("ADMIN_USER_IDS: %s", admin_ids_str or "(not set)")
    logger.info("BOT_USERNAME: %s", getattr(settings, "BOT_USERNAME", "") or "(not set)")

    await migrate()

    bot_token = settings.BOT_TOKEN or settings.TG_BOT_TOKEN
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    bot = Bot(token=bot_token)
    dp = Dispatcher()

    # Core routes
    dp.include_router(start_handlers.router)
    dp.include_router(video_handlers.router)
    dp.include_router(balance_handlers.router)

    # New features
    dp.include_router(promo_handlers.router)
    dp.include_router(gift_handlers.router)
    dp.include_router(referral_handlers.router)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
