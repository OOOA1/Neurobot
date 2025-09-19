# -*- coding: utf-8 -*-
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from keyboards.main_menu_kb import back_to_main_menu_kb

router = Router()


async def _get_bot_username(obj) -> str | None:
    """
    Возвращает @username бота. Сначала пробуем settings.BOT_USERNAME (если добавите),
    иначе спрашиваем у Telegram через get_me().
    """
    try:
        from config import settings  # локальный импорт, чтобы не ломать загрузку
        bot_username = getattr(settings, "BOT_USERNAME", "") or None
        if bot_username:
            return bot_username.lstrip("@")
    except Exception:
        pass

    me = await obj.bot.get_me()
    return (me.username or "").lstrip("@") or None


def _ref_text(link: str) -> str:
    return (
        "Ваша реферальная ссылка:\n"
        f"{link}\n\n"
        "За каждого приглашённого пользователя вам начисляется 2 токена — это одна бесплатная генерация.\n"
        "Благодарим вас за рекомендацию — это помогает нашему сервису работать для вас и развиваться! ❤️"
    )


@router.callback_query(F.data == "menu:ref")
async def on_referral(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return

    username = await _get_bot_username(cb)
    if not username:
        await cb.message.edit_text(
            "Не удалось получить username бота. Попробуйте позже.",
            reply_markup=back_to_main_menu_kb(),
        )
        return

    # персональная ссылка с параметром start=<tg_user_id>
    link = f"https://t.me/{username}?start={cb.from_user.id}"
    await cb.message.edit_text(_ref_text(link), reply_markup=back_to_main_menu_kb())


@router.message(Command("ref"))
@router.message(Command("referral"))
async def cmd_referral(msg: Message) -> None:
    username = await _get_bot_username(msg)
    if not username:
        await msg.reply("Не удалось получить username бота. Попробуйте позже.")
        return
    link = f"https://t.me/{username}?start={msg.from_user.id}"
    await msg.reply(_ref_text(link), reply_markup=back_to_main_menu_kb())


@router.callback_query(F.data == "menu:examples")
async def on_examples(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    await cb.message.edit_text(
        "В будущем будет переход на тг канал с примерами генераций.",
        reply_markup=back_to_main_menu_kb(),
    )
