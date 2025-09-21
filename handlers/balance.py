# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from aiogram import F, Router
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from config import settings
from db import connect, _prepare, ensure_user, get_user_balance, add_user_tokens
from keyboards.balance_kb import balance_kb
from keyboards.main_menu_kb import main_menu_kb
from texts import BALANCE_VIEW, WELCOME

router = Router()
log = logging.getLogger(__name__)

# Тарифы и количества токенов — для DEV можем сразу начислять при клике (песочница)
PLAN_TOKENS = {
    "trial": 2,
    "base": 12,
    "neuro": 30,
    "vip": 120,
    "top": 600,
}


def _is_admin(user_id: int) -> bool:
    try:
        return user_id in settings.admin_ids()
    except Exception:
        raw = (getattr(settings, "ADMIN_USER_IDS", "") or "").replace(" ", "")
        return str(user_id) in {x for x in raw.split(",") if x}


async def _send_balance_view(message_or_cb, tg_user_id: int, username: str | None):
    async with connect() as db:
        await _prepare(db)
        await ensure_user(db, tg_user_id, username, settings.FREE_TOKENS_ON_JOIN)
        balance_int = await get_user_balance(db, tg_user_id)

    balance_text = "∞" if _is_admin(tg_user_id) else str(balance_int)
    text = BALANCE_VIEW.format(balance=balance_text)
    kb = balance_kb()

    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=kb)
    else:
        await message_or_cb.message.edit_text(text, reply_markup=kb)


@router.message(F.text.casefold() == "баланс")
async def balance_entry(msg: Message, state: FSMContext):
    await _send_balance_view(msg, msg.from_user.id, msg.from_user.username)


@router.callback_query(F.data == "menu:balance")
async def balance_from_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not cb.message:
        return
    await _send_balance_view(cb, cb.from_user.id, cb.from_user.username)


@router.callback_query(F.data == "balance:back")
async def balance_back(cb: CallbackQuery, state: FSMContext):
    # Возврат в главное меню с актуальным балансом в кнопке
    async with connect() as db:
        await _prepare(db)
        balance = await get_user_balance(db, cb.from_user.id)
    await cb.message.edit_text(WELCOME, reply_markup=main_menu_kb(balance))
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def balance_buy(cb: CallbackQuery):
    plan = cb.data.split(":", 1)[1]
    await cb.answer()  # закрываем «часики» без алёртов

    if not cb.message:
        return

    # Админу токены не нужны — у него безлимит
    if _is_admin(cb.from_user.id):
        await cb.message.answer("У вас безлимитные токены (администратор). Покупка не требуется.")
        return

    # DEV-режим: начисляем сразу, без всплывающих алёртов
    if settings.APP_ENV.lower() == "dev" and plan in PLAN_TOKENS:
        async with connect() as db:
            await _prepare(db)
            await ensure_user(db, cb.from_user.id, cb.from_user.username, settings.FREE_TOKENS_ON_JOIN)
            await add_user_tokens(db, cb.from_user.id, PLAN_TOKENS[plan])

        await cb.message.answer("Тестовый режим: токены начислены ✅")
        await _send_balance_view(cb, cb.from_user.id, cb.from_user.username)
        return

    # Прод: пока заглушка без алёртов
    await cb.message.answer("Оплата этого тарифа скоро будет доступна 💳")
