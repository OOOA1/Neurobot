# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import secrets
import string
from typing import Iterable
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from db import (
    _prepare,
    connect,
    ensure_user,
    # TTL-многоразовые промокоды:
    create_token_promo_campaign,
    generate_token_promo_codes,
    list_token_promo_campaigns,
    redeem_token_promo_code_ttl,
)

router = Router()


class PromoStates(StatesGroup):
    waiting_code = State()


DEFAULT_TTL_HOURS = int(getattr(settings, "PROMO_TTL_HOURS", 3) or 3)


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


def _gen_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------- Пользовательский флоу ----------------

@router.callback_query(F.data == "menu:promo")
async def promo_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.set_state(PromoStates.waiting_code)
    await cb.message.edit_text("Пришлите промокод (одно слово/набор символов).")


@router.message(PromoStates.waiting_code)
async def promo_redeem(msg: Message, state: FSMContext) -> None:
    code = (msg.text or "").strip()
    if not code or len(code) > 64 or " " in code:
        await msg.reply("Некорректный промокод. Пришлите одно слово/набор символов без пробелов.")
        return

    async with connect() as db:
        await _prepare(db)
        await ensure_user(db, msg.from_user.id, msg.from_user.username, 0)
        tokens, status = await redeem_token_promo_code_ttl(db, msg.from_user.id, code)

    if status == "ok":
        await state.clear()
        await msg.reply(f"Промокод активирован! Начислено {tokens} токенов.")
    elif status == "already_used":
        await msg.reply("Этот промокод вы уже использовали.")
    elif status == "expired":
        await msg.reply("Промокод просрочен.")
    elif status == "inactive":
        await msg.reply("Промокод отключён.")
    else:
        await msg.reply("Неверный промокод, попробуйте ещё раз.")


# ---------------- Админ-команды (текстовые) ----------------

@router.message(Command("promo_new"))
async def promo_new(msg: Message) -> None:
    """
    Создать один многоразовый TTL-код.
    Использование: /promo_new CODE TOKENS [HOURS]
    Пример: /promo_new SEPTSALE 3 3  -> +3 токена, живёт 3 часа.
    """
    if not _is_admin(msg.from_user.id):
        return

    args = (msg.text or "").split()
    if len(args) < 3 or not args[2].isdigit():
        await msg.reply(
            "Использование: /promo_new CODE TOKENS [HOURS]\n"
            "Пример: /promo_new SEPTSALE 3 3"
        )
        return

    code = args[1].strip().upper()
    tokens = max(1, int(args[2]))
    hours = int(args[3]) if len(args) >= 4 and args[3].isdigit() else DEFAULT_TTL_HOURS

    async with connect() as db:
        await _prepare(db)
        await create_token_promo_campaign(db, code, tokens, hours, msg.from_user.id)

    await msg.reply(f"TTL-промокод создан: {code} (+{tokens} ток.) на {hours} ч.")


@router.message(Command("promo_gen"))
async def promo_gen(msg: Message) -> None:
    """
    Сгенерировать пачку TTL-кодов.
    Использование: /promo_gen COUNT TOKENS [HOURS] [PREFIX]
    Пример: /promo_gen 5 2 3 AUTUMN  -> 5 кодов AUTUMN-XXXXXX, +2 токена, живут 3 часа.
    """
    if not _is_admin(msg.from_user.id):
        return

    args = (msg.text or "").split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await msg.reply(
            "Использование: /promo_gen COUNT TOKENS [HOURS] [PREFIX]\n"
            "Пример: /promo_gen 5 2 3 AUTUMN"
        )
        return

    count = max(1, min(100, int(args[1])))
    tokens = max(1, int(args[2]))
    hours = int(args[3]) if len(args) >= 4 and args[3].isdigit() else DEFAULT_TTL_HOURS
    prefix = args[4].strip() if len(args) >= 5 else None

    async with connect() as db:
        await _prepare(db)
        codes = await generate_token_promo_codes(
            db, count=count, tokens=tokens, ttl_hours=hours, created_by_tg=msg.from_user.id, prefix=prefix
        )

    await msg.reply("Сгенерированы промокоды:\n" + "\n".join(f"{c} (+{tokens})" for c in codes))


@router.message(Command("promo_list"))
async def promo_list(msg: Message) -> None:
    """Показать последние TTL-кампании промокодов и число уникальных активаций."""
    if not _is_admin(msg.from_user.id):
        return

    async with connect() as db:
        await _prepare(db)
        rows = await list_token_promo_campaigns(db, limit=30)

    if not rows:
        await msg.reply("Промокампаний нет.")
        return

    def fmt_ts(ts: int) -> str:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
        except Exception:
            return str(ts)

    lines = []
    now_ts = int(datetime.now().timestamp())
    for r in rows:
        code = r.get("code")
        t = r.get("tokens")
        exp = int(r.get("expires_at") or 0)
        active = bool(r.get("is_active"))
        used = int(r.get("redemptions") or 0)
        status = "активен" if active and exp > now_ts else ("просрочен" if exp <= now_ts else "off")
        lines.append(f"{code} — +{t} ток. — до {fmt_ts(exp)} — {status} — использований: {used}")

    await msg.reply("\n".join(lines))


# ---------------- Админ-панель (/admin + кнопки) ----------------

@router.message(Command("admin"))
async def admin_panel(msg: Message) -> None:
    if not _is_admin(msg.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Один код", callback_data="admin:promo_new_help"),
        InlineKeyboardButton(text="📦 Пачка кодов", callback_data="admin:promo_gen_help"),
    )
    kb.row(InlineKeyboardButton(text="📋 Список кампаний", callback_data="admin:promo_list"))
    # Кнопка для рассылки (ведёт в handlers/broadcast.py)
    kb.row(InlineKeyboardButton(text="📣 Сделать рассылку", callback_data="admin:broadcast"))
    await msg.answer(
        "Админ-панель:\n"
        "• Промокоды (TTL, многоразовые) — управление ниже.\n"
        "• «📣 Сделать рассылку» — отправка текста/фото всем пользователям.\n",
        reply_markup=kb.as_markup()
    )


@router.callback_query(F.data == "admin:promo_new_help")
async def admin_promo_new_help(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    await cb.message.answer(
        "Создать один код:\n"
        "`/promo_new CODE TOKENS [HOURS]`\n\n"
        "Пример: `/promo_new FALL2025 3 3` — +3 токена, срок 3 часа.",
        parse_mode="Markdown"
    )
    await cb.answer()


@router.callback_query(F.data == "admin:promo_gen_help")
async def admin_promo_gen_help(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    await cb.message.answer(
        "Сгенерировать пачку кодов:\n"
        "`/promo_gen COUNT TOKENS [HOURS] [PREFIX]`\n\n"
        "Пример: `/promo_gen 5 2 3 AUTUMN` — 5 кодов, каждый +2 токена, срок 3 часа.",
        parse_mode="Markdown"
    )
    await cb.answer()


@router.callback_query(F.data == "admin:promo_list")
async def admin_promo_list(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    async with connect() as db:
        await _prepare(db)
        rows = await list_token_promo_campaigns(db, limit=30)

    if not rows:
        await cb.message.answer("Промокампаний нет.")
        await cb.answer()
        return

    now_ts = int(datetime.now().timestamp())

    def fmt_ts(ts: int) -> str:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
        except Exception:
            return str(ts)

    lines = []
    for r in rows:
        code = r.get("code"); t = r.get("tokens")
        exp = int(r.get("expires_at") or 0)
        active = bool(r.get("is_active")); used = int(r.get("redemptions") or 0)
        status = "активен" if active and exp > now_ts else ("просрочен" if exp <= now_ts else "off")
        lines.append(f"{code} — +{t} ток. — до {fmt_ts(exp)} — {status} — использований: {used}")
    await cb.message.answer("\n".join(lines))
    await cb.answer()
