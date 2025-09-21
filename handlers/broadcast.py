# handlers/broadcast.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import settings
from db import connect, _prepare

router = Router()
log = logging.getLogger(__name__)


# ---------- Админ-проверка ----------
def _is_admin(user_id: int) -> bool:
    try:
        return user_id in settings.admin_ids()
    except Exception:
        raw = (getattr(settings, "ADMIN_USER_IDS", "") or "").replace(" ", "")
        return str(user_id) in {x for x in raw.split(",") if x}


# ---------- FSM ----------
class BroadcastStates(StatesGroup):
    waiting_content = State()


# ---------- Запуск из команды ----------
@router.message(Command("broadcast"))
async def start_broadcast_cmd(msg: Message, state: FSMContext) -> None:
    if not _is_admin(msg.from_user.id):
        # Тихо игнорируем, чтобы команда «не существовала» для не-админа
        return
    await state.set_state(BroadcastStates.waiting_content)
    await msg.answer(
        "📣 Сделать рассылку\n\n"
        "Пришлите текстовое сообщение или картинку с подписью. "
        "Это сообщение будет отправлено всем пользователям бота."
    )


# ---------- Запуск из админ-кнопки ----------
@router.callback_query(F.data == "admin:broadcast")
async def start_broadcast_cb(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer()
    await state.set_state(BroadcastStates.waiting_content)
    if cb.message:
        await cb.message.answer(
            "📣 Сделать рассылку\n\n"
            "Пришлите текст или картинку с подписью для отправки всем пользователям."
        )


# ---------- Приём контента и рассылка ----------
@router.message(BroadcastStates.waiting_content)
async def broadcast_collect_and_send(msg: Message, state: FSMContext) -> None:
    if not _is_admin(msg.from_user.id):
        await state.clear()
        return

    # 1) Подготовим «контент рассылки» из входящего сообщения
    content: dict[str, Any] | None = None

    if msg.text:
        content = {"type": "text", "text": msg.text}
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        caption = (msg.caption or "").strip() or None
        content = {"type": "photo", "file_id": file_id, "caption": caption}
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
        caption = (msg.caption or "").strip() or None
        content = {"type": "photo", "file_id": file_id, "caption": caption}

    if not content:
        await msg.answer("Нужно прислать либо текст, либо изображение (фото) с подписью.")
        return

    await state.clear()

    # 2) Получим список получателей из БД
    admin_id = msg.from_user.id
    user_ids = await _list_recipients(exclude_ids={admin_id})

    if not user_ids:
        await msg.answer("В базе нет пользователей для рассылки.")
        return

    # 3) Сообщим админу и запустим рассылку
    status = await msg.answer(f"Рассылка запущена…\nПолучателей: {len(user_ids)}")
    sent = 0
    failed = 0

    # Небольшой троттлинг, чтобы не попасть под лимиты
    per_message_delay = 0.05

    for idx, uid in enumerate(user_ids, start=1):
        try:
            if content["type"] == "text":
                await msg.bot.send_message(uid, content["text"])
            else:
                await msg.bot.send_photo(uid, content["file_id"], caption=content.get("caption"))
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            # Юзер удалил / заблокировал бота или некорректный file_id — пропускаем
            failed += 1
            log.warning("Broadcast send failed to %s: %s", uid, exc)
        except TelegramAPIError as exc:
            failed += 1
            log.exception("Broadcast API error to %s: %s", uid, exc)
        except Exception as exc:
            failed += 1
            log.exception("Broadcast unexpected error to %s: %s", uid, exc)

        # Обновляем прогресс аккуратно раз в N сообщений
        if idx % 50 == 0:
            try:
                await status.edit_text(
                    f"Рассылка идёт… {idx}/{len(user_ids)}\n"
                    f"Успешно: {sent} | Ошибок: {failed}"
                )
            except TelegramBadRequest:
                pass

        await asyncio.sleep(per_message_delay)

    # 4) Итог
    try:
        await status.edit_text(
            "✅ Рассылка завершена\n"
            f"Всего: {len(user_ids)}\nУспешно: {sent}\nОшибок: {failed}"
        )
    except TelegramBadRequest:
        await msg.answer(
            "✅ Рассылка завершена\n"
            f"Всего: {len(user_ids)}\nУспешно: {sent}\nОшибок: {failed}"
        )


# ---------- Вспомогательные ----------
async def _list_recipients(*, exclude_ids: Iterable[int] = ()) -> list[int]:
    """
    Возвращает список tg_user_id для рассылки.
    Исключает пользователей из exclude_ids и забаненных.
    """
    exclude = set(int(x) for x in exclude_ids or [])
    async with connect() as db:
        await _prepare(db)
        cur = await db.execute(
            """
            SELECT tg_user_id
              FROM users
             WHERE tg_user_id IS NOT NULL
               AND COALESCE(is_banned, 0) = 0
            """
        )
        rows = await cur.fetchall()
        await cur.close()

    ids = []
    for r in rows:
        uid = int(r["tg_user_id"])
        if uid not in exclude:
            ids.append(uid)
    return ids
