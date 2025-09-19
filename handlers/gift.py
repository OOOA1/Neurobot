# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Optional, Tuple, Iterable, Any, Dict

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import settings
from db import (
    _prepare,
    connect,
    ensure_user,
    get_user_by_tg,
    get_user_by_username,
    transfer_tokens,
    add_user_tokens,
)
from texts import (
    GIFT_ASK,
    GIFT_FORMAT_ERROR,
    GIFT_AMOUNT_ERROR,
    GIFT_SELF_ERROR,
    GIFT_NOT_REGISTERED,
    GIFT_NOT_ENOUGH,
    GIFT_SUCCESS,
)

router = Router()


class GiftStates(StatesGroup):
    waiting_input = State()


# --------- admin helper ---------

def _admin_ids() -> set[int]:
    raw = getattr(settings, "ADMIN_USER_IDS", "") or ""
    if isinstance(raw, (list, tuple, set)):
        return {int(x) for x in raw}
    parts: Iterable[str] = re.split(r"[,\s]+", str(raw).strip()) if raw else []
    return {int(p) for p in parts if p.isdigit()}

def _is_admin(user_id: int) -> bool:
    try:
        return user_id in settings.admin_ids()
    except Exception:
        return user_id in _admin_ids()


# --------- parsing & lookup ---------

_USERNAME_OR_ID_RE = re.compile(
    r"^\s*(?:@?(?P<username>[A-Za-z0-9_]{5,32})|(?P<tgid>\d{5,12}))\s+(?P<amount>\d+)\s*$"
)

def _parse_input(text: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Возвращает (username, tgid_str, amount) из строки вида:
    '@user 5' или '123456789 10'
    """
    m = _USERNAME_OR_ID_RE.match(text or "")
    if not m:
        return None, None, None
    username = m.group("username")
    tgid_str = m.group("tgid")
    try:
        amount = int(m.group("amount"))
    except Exception:
        return username, tgid_str, None
    return username, tgid_str, amount


async def _find_recipient(username: Optional[str], tgid_str: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict-представление пользователя либо None.
    Ищем только среди зарегистрированных пользователей (уже есть в БД).
    """
    async with connect() as db:
        await _prepare(db)
        row = None
        if username:
            row = await get_user_by_username(db, username)
        elif tgid_str:
            try:
                row = await get_user_by_tg(db, int(tgid_str))
            except ValueError:
                return None
        if row is None:
            return None
        # aiosqlite.Row -> dict
        return dict(row)


# --------- entry points ---------

@router.callback_query(F.data == "menu:gift")
async def gift_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.set_state(GiftStates.waiting_input)
    await cb.message.edit_text(GIFT_ASK)


@router.message(Command("gift"))
async def gift_cmd(msg: Message, state: FSMContext) -> None:
    await state.set_state(GiftStates.waiting_input)
    await msg.answer(GIFT_ASK)


@router.message(GiftStates.waiting_input)
async def gift_handle_input(msg: Message, state: FSMContext) -> None:
    username, tgid_str, amount = _parse_input(msg.text or "")

    if amount is None:
        await msg.reply(GIFT_FORMAT_ERROR)
        return
    if amount <= 0:
        await msg.reply(GIFT_AMOUNT_ERROR)
        return

    # донор должен существовать в БД
    async with connect() as db:
        await _prepare(db)
        await ensure_user(db, msg.from_user.id, msg.from_user.username, 0)

    # получатель — по username/ID среди зарегистрированных
    recipient = await _find_recipient(username, tgid_str)
    if not recipient:
        who = f"@{username}" if username else (tgid_str or "пользователь")
        await msg.reply(GIFT_NOT_REGISTERED.format(who=who))
        return

    recipient_tg = int(recipient["tg_user_id"])
    if recipient_tg == msg.from_user.id:
        await msg.reply(GIFT_SELF_ERROR)
        return

    # Если дарит АДМИН — не списываем, просто начисляем получателю
    if _is_admin(msg.from_user.id):
        async with connect() as db:
            await _prepare(db)
            await add_user_tokens(db, recipient_tg, amount)
        pretty_recipient = f"@{recipient['username']}" if recipient.get("username") else str(recipient_tg)
        await state.clear()
        await msg.reply(GIFT_SUCCESS.format(amount=amount, recipient=pretty_recipient))
        return

    # Обычный пользователь: атомарный перевод с проверкой баланса
    async with connect() as db:
        await _prepare(db)
        ok = await transfer_tokens(db, from_tg=msg.from_user.id, to_tg=recipient_tg, amount=amount)

    if not ok:
        await msg.reply(GIFT_NOT_ENOUGH)
        return

    pretty_recipient = f"@{recipient['username']}" if recipient.get("username") else str(recipient_tg)
    await state.clear()
    await msg.reply(GIFT_SUCCESS.format(amount=amount, recipient=pretty_recipient))
