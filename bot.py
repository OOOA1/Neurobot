# -*- coding: utf-8 -*-
import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import settings
from db import migrate
from handlers import start as start_handlers
from handlers import video as video_handlers


async def main() -> None:
    """Entrypoint configuring logging, migrations and bot polling."""

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger(__name__)
    logger.info("GEMINI API Key set: %s", "Yes" if settings.GEMINI_API_KEY else "No")
    logger.info("LUMA API Key set: %s", "Yes" if settings.LUMA_API_KEY else "No")

    await migrate()

    bot_token = settings.BOT_TOKEN or settings.TG_BOT_TOKEN
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    bot = Bot(token=bot_token)
    dp = Dispatcher()

    dp.include_router(start_handlers.router)
    dp.include_router(video_handlers.router)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
