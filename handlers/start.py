from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from keyboards import main_kb
from texts import WELCOME, HELP
from db import ensure_user, connect, _prepare
from config import settings


router = Router()


@router.message(CommandStart())
async def cmd_start(msg: Message):
    async with connect() as db:
        await _prepare(db)
        user = await ensure_user(db, msg.from_user.id, msg.from_user.username, settings.FREE_TOKENS_ON_JOIN)
        await msg.answer(WELCOME, reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP, reply_markup=main_kb())