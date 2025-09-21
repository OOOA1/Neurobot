# -*- coding: utf-8 -*-
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from db import _prepare, connect, ensure_user, get_user_balance
from db import award_referral_if_eligible  # <-- важно
from handlers.video import start_veo_wizard, start_luma_wizard
from keyboards.main_menu_kb import (
    back_to_main_menu_kb,
    main_menu_kb,
    video_menu_kb,
)
from texts import HELP, WELCOME

router = Router()


def _is_not_modified_error(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def _send_main_menu(msg: Message) -> None:
    async with connect() as db:
        await _prepare(db)
        # гарантируем наличие пользователя (чтобы не упереться в отсутствие записи)
        await ensure_user(db, msg.from_user.id, msg.from_user.username, settings.FREE_TOKENS_ON_JOIN)
        balance = await get_user_balance(db, msg.from_user.id)
    await msg.answer(WELCOME, reply_markup=main_menu_kb(balance))


async def _edit_main_menu(message: Message) -> None:
    """
    Пытаемся отредактировать текст сообщения на главное меню.
    Если исходное сообщение без текста (например, видео) — отправляем новое.
    """
    async with connect() as db:
        await _prepare(db)
        # в приватных чатах chat.id == user_id; дополнительно гарантируем запись
        await ensure_user(db, message.chat.id, None, settings.FREE_TOKENS_ON_JOIN)
        balance = await get_user_balance(db, message.chat.id)

    try:
        if message.text:
            await message.edit_text(text=WELCOME, reply_markup=main_menu_kb(balance))
        else:
            # У медиа-сообщений (видео/фото) текста нет — редактировать нечего.
            await message.answer(text=WELCOME, reply_markup=main_menu_kb(balance))
    except TelegramBadRequest as exc:
        # Если not modified — игнорируем, иначе пробрасываем.
        if not _is_not_modified_error(exc):
            # Возможен кейс: телеграм не даёт редактировать (старое сообщение).
            # Подстрахуемся отправкой нового сообщения.
            try:
                await message.answer(text=WELCOME, reply_markup=main_menu_kb(balance))
            except TelegramBadRequest as inner_exc:
                if not _is_not_modified_error(inner_exc):
                    raise


async def _clear_markup(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
            raise


async def _edit_with_back(message: Message, *, text: str) -> None:
    try:
        await message.edit_text(text=text, reply_markup=back_to_main_menu_kb())
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
            raise


async def _edit_video_menu(message: Message) -> None:
    try:
        await message.edit_text(
            text="Выберите провайдера для работы с видео",
            reply_markup=video_menu_kb(),
        )
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
            raise


@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    """
    Регистрируем пользователя (даём стартовые токены) и показываем меню.
    Если /start пришёл с payload вида ?start=<referrer_tg>, начисляем +2 токена владельцу ссылки.
    Приглашённому ничего не начисляем сверх его стартовых.
    """
    async with connect() as db:
        await _prepare(db)
        await ensure_user(
            db, msg.from_user.id, msg.from_user.username, settings.FREE_TOKENS_ON_JOIN
        )

        # Обработка реферального payload
        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) == 2:
            payload = (parts[1] or "").strip()
            if payload.isdigit():
                referrer_tg = int(payload)
                if referrer_tg != msg.from_user.id:
                    try:
                        awarded = await award_referral_if_eligible(
                            db,
                            invited_tg=msg.from_user.id,
                            referrer_tg=referrer_tg,
                            tokens=2,
                        )
                    except Exception:
                        awarded = False

                    if awarded:
                        # Уведомляем реферера о +2 токенах (ошибку глушим)
                        try:
                            await msg.bot.send_message(
                                referrer_tg,
                                "🎉 У вас +2 токена за приглашение друга!",
                            )
                        except Exception:
                            pass

    # Главное меню отправляем всегда (вне блока try/except и независимо от payload)
    await _send_main_menu(msg)


@router.message(Command("menu"))
async def cmd_menu(msg: Message) -> None:
    await _send_main_menu(msg)


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP, reply_markup=back_to_main_menu_kb())


@router.callback_query(F.data == "menu:video")
async def menu_video(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await _clear_markup(cb.message)
    await _edit_video_menu(cb.message)


@router.callback_query(F.data == "menu:video:veo")
async def menu_video_veo(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await start_veo_wizard(cb.message, state)


@router.callback_query(F.data == "menu:video:luma")
async def menu_video_luma(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await start_luma_wizard(cb.message, state)


@router.callback_query(F.data == "menu:help")
async def menu_help(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    await _edit_with_back(cb.message, text=HELP)


@router.callback_query(F.data == "menu:back")
async def menu_back(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    # Пытаемся показать главное меню: если это было медиа-сообщение, отправим новое.
    await _edit_main_menu(cb.message)
