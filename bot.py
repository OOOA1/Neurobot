import asyncio
import logging
from aiogram import Bot, Dispatcher
from config import settings
from db import migrate
from handlers import start as start_handlers
from handlers import video as video_handlers


async def main():
    # Настраиваем более подробное логирование
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Отладочный вывод токена и API ключей (замаскированных)
    logger = logging.getLogger(__name__)
    logger.info(f"VEO API Key set: {'Yes' if settings.VEO_API_KEY else 'No'}")
    logger.info(f"LUMA API Key set: {'Yes' if settings.LUMA_API_KEY else 'No'}")
    
    await migrate()

    bot = Bot(token=settings.TG_BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(start_handlers.router)
    dp.include_router(video_handlers.router)

    await dp.start_polling(bot, allowed_updates=["message"]) # long-polling


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass