# -*- coding: utf-8 -*-
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from db import _prepare, connect, ensure_user, get_user_balance, GENERATION_COST_TOKENS
from handlers.video import start_veo_wizard, start_luma_wizard
from keyboards.main_menu_kb import (
    back_to_main_menu_kb,
    main_menu_kb,
    video_menu_kb,
    balance_kb_placeholder,
)
from texts import HELP, WELCOME

router = Router()


def _is_not_modified_error(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def _send_main_menu(msg: Message) -> None:
    await msg.answer(WELCOME, reply_markup=main_menu_kb())


async def _edit_main_menu(message: Message) -> None:
    try:
        await message.edit_text(text=WELCOME, reply_markup=main_menu_kb())
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
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
    async with connect() as db:
        await _prepare(db)
        await ensure_user(db, msg.from_user.id, msg.from_user.username, settings.FREE_TOKENS_ON_JOIN)
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


@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    async with connect() as db:
        await _prepare(db)
        balance = await get_user_balance(db, cb.from_user.id)
    text = (
        f"Баланс токенов: {balance}\n\n"
        f"Стоимость генерации видео: {GENERATION_COST_TOKENS} токена.\n"
        f"Нажмите «Пополнить», чтобы увеличить баланс (заглушка)."
    )
    try:
        await cb.message.edit_text(text, reply_markup=balance_kb_placeholder())
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
            raise


@router.callback_query(F.data == "balance:topup")
async def balance_topup_placeholder(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    # Заглушка — здесь позже будет реальная платёжка
    text = (
        "Пополнение баланса (заглушка).\n\n"
        "В одной из следующих версий здесь появится окно оплаты.\n"
        "Пока что вернитесь назад."
    )
    try:
        await cb.message.edit_text(text, reply_markup=back_to_main_menu_kb())
    except TelegramBadRequest as exc:
        if not _is_not_modified_error(exc):
            raise


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
    await _edit_main_menu(cb.message)
