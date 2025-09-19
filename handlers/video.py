# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from db import (
    _prepare,
    connect,
    create_job,
    ensure_user,
    set_job_status,
    set_provider_job_id,
    get_user_balance,
    charge_user_tokens,
    refund_user_tokens,
    GENERATION_COST_TOKENS,
)
from keyboards.main_menu_kb import main_menu_kb, balance_kb_placeholder
from keyboards.veo_kb import veo_options_kb
from keyboards.luma_kb import luma_options_kb
from providers.base import Provider
from providers.models import GenerationParams
from services import generation_service
from services.moderation import check_text
from texts import WELCOME, INSUFFICIENT_TOKENS, GENERATION_FAILED

router = Router()
log = logging.getLogger(__name__)


class VeoWizardStates(StatesGroup):
    summary = State()
    prompt_input = State()
    negative_input = State()
    reference_input = State()


VEO_DEFAULT_STATE: dict[str, Any] = {
    "prompt": None,
    "negative_enabled": False,
    "negative_text": None,
    "ar": "16:9",
    "resolution": "720p",
    "mode": "quality",
    "reference_file_id": None,
}

SUMMARY_META_KEY = "veo_summary_message"
DATA_KEY = "veo_state"


def _not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def _get_data(state: FSMContext) -> dict[str, Any]:
    data = await state.get_data()
    stored = data.get(DATA_KEY)
    if stored is None:
        stored = VEO_DEFAULT_STATE.copy()
        await state.update_data({DATA_KEY: stored})
    return dict(stored)


async def _set_data(state: FSMContext, new_state: dict[str, Any]) -> None:
    await state.update_data({DATA_KEY: new_state})


async def _store_summary(message: Message, state: FSMContext) -> None:
    meta = {"chat_id": message.chat.id, "message_id": message.message_id}
    if message.message_thread_id is not None:
        meta["thread_id"] = message.message_thread_id
    await state.update_data({SUMMARY_META_KEY: meta})


async def _get_summary_meta(state: FSMContext) -> dict[str, Any] | None:
    data = await state.get_data()
    meta = data.get(SUMMARY_META_KEY)
    if isinstance(meta, dict):
        return meta
    return None


def _render_summary(state: dict[str, Any]) -> str:
    prompt = state.get("prompt") or "—"
    aspect = state.get("ar") or "—"
    resolution = state.get("resolution") or "720p"
    mode = (state.get("mode") or "quality").lower()
    mode_label = "Быстро" if mode == "fast" else "Качество"
    negative_enabled = bool(state.get("negative_enabled"))
    if negative_enabled and state.get("negative_text"):
        neg_display = str(state.get("negative_text"))
    else:
        neg_display = "Выкл"
    lines = [
        "🎬 Veo3 генерация",
        f"Промт: {prompt}",
        f"Соотношение сторон: {aspect}",
        f"Разрешение: {resolution}",
        f"Режим: {mode_label}",
        f"Negative prompt: {neg_display}",
        "Настройте параметры кнопками ниже.",
    ]
    return "\n".join(lines)


async def _edit_summary(*, message: Message | None, bot, state: FSMContext, data: dict[str, Any]) -> None:
    text = _render_summary(data)
    markup = veo_options_kb(data)
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                try:
                    await message.edit_reply_markup(reply_markup=markup)
                except TelegramBadRequest as inner_exc:
                    if not _not_modified(inner_exc):
                        raise
            else:
                raise
        return
    if bot is None:
        return
    meta = await _get_summary_meta(state)
    if not meta:
        return
    chat_id = meta.get("chat_id")
    message_id = meta.get("message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if _not_modified(exc):
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except TelegramBadRequest as inner_exc:
                if not _not_modified(inner_exc):
                    raise
        else:
            raise


async def _ensure_summary_message(msg: Message, state: FSMContext) -> Message:
    data = await _get_data(state)
    summary_text = _render_summary(data)
    markup = veo_options_kb(data)
    sent = await msg.answer(summary_text, reply_markup=markup)
    await _store_summary(sent, state)
    await state.set_state(VeoWizardStates.summary)
    return sent


def _parse_callback(data: str) -> tuple[str, str | None]:
    parts = (data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else None
    return action, value


async def _update_data(state: FSMContext, **changes: Any) -> dict[str, Any]:
    current = await _get_data(state)
    current.update(changes)
    await _set_data(state, current)
    return current


async def start_veo_wizard(msg: Message, state: FSMContext) -> None:
    meta = await _get_summary_meta(state)
    if meta:
        with suppress(TelegramBadRequest):
            await msg.bot.edit_message_reply_markup(chat_id=meta["chat_id"], message_id=meta["message_id"], reply_markup=None)
    await _set_data(state, VEO_DEFAULT_STATE.copy())
    await _ensure_summary_message(msg, state)


@router.callback_query(F.data == "menu:video")
async def menu_entry(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await start_veo_wizard(cb.message, state)


@router.message(Command("veo"))
async def cmd_veo(msg: Message, state: FSMContext) -> None:
    await start_veo_wizard(msg, state)


@router.callback_query(F.data.startswith("veo:"))
async def veo_callback(cb: CallbackQuery, state: FSMContext) -> None:
    message = cb.message
    if message is None:
        await cb.answer()
        return
    if not cb.data:
        await cb.answer()
        return
    action, value = _parse_callback(cb.data)
    data = await _get_data(state)
    if action == "ar":
        mapping = {"16_9": "16:9", "9_16": "9:16", "1_1": "1:1"}
        chosen = mapping.get(value or "")
        if chosen:
            data = await _update_data(state, ar=chosen)
            await cb.answer("Выбрано соотношение сторон")
        else:
            await cb.answer()
            return
        await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        return
    if action == "res":
        current = data.get("resolution") or "720p"
        new_value = "1080p" if current == "720p" else "720p"
        data = await _update_data(state, resolution=new_value)
        await cb.answer(f"Разрешение: {new_value}")
        await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        return
    if action == "mode":
        if value in {"fast", "quality"}:
            data = await _update_data(state, mode=value)
            await cb.answer("Режим обновлён")
            await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        else:
            await cb.answer()
        return
    if action == "neg" and value == "toggle":
        enabled = not bool(data.get("negative_enabled"))
        data = await _update_data(state, negative_enabled=enabled)
        await cb.answer("Negative prompt: Вкл" if enabled else "Negative prompt: Выкл")
        await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        return
    if action == "neg" and value == "input":
        await cb.answer("Введите negative prompt")
        await message.answer("Отправьте текст negative prompt")
        await state.set_state(VeoWizardStates.negative_input)
        return
    if action == "prompt" and value == "input":
        await cb.answer("Введите промпт")
        await message.answer("Отправьте текст промпта")
        await state.set_state(VeoWizardStates.prompt_input)
        return
    if action == "ref" and value == "attach":
        await cb.answer("Пришлите изображение-референс")
        await state.set_state(VeoWizardStates.reference_input)
        return
    if action == "generate":
        prompt = (data.get("prompt") or "").strip()
        aspect = data.get("ar")
        if not prompt or not aspect:
            await cb.answer("Укажите промт (✍️ Промт) и соотношение сторон (16:9 / 9:16)", show_alert=True)
            return

        # авто-даунгрейд для 9:16 -> 720п
        resolution = (data.get("resolution") or "720p").lower()
        if aspect == "9:16" and resolution != "720p":
            resolution = "720p"
        mode = (data.get("mode") or "quality").lower()
        negative_prompt = (data.get("negative_text") or None) if data.get("negative_enabled") else None
        resolution_value = int(str(resolution).rstrip("p"))

        # 1) Баланс
        async with connect() as db:
            await _prepare(db)
            bal = await get_user_balance(db, cb.from_user.id)
        if bal < GENERATION_COST_TOKENS:
            await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
            await cb.answer()
            return

        # 2) Списываем токены (атомарно)
        async with connect() as db:
            await _prepare(db)
            charged = await charge_user_tokens(db, cb.from_user.id, GENERATION_COST_TOKENS)
        if not charged:
            await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
            await cb.answer()
            return

        await cb.answer("Генерация запущена")
        status_message = await message.answer("Генерация началась")

        try:
            job_id = await generation_service.create_video(
                provider="veo3",
                prompt=prompt,
                aspect_ratio=aspect,
                resolution=resolution_value,
                negative_prompt=negative_prompt,
                fast=(mode == "fast"),
                reference_file_id=data.get("reference_file_id"),
            )
        except Exception as exc:
            # провайдер не принял задачу — вернём токены
            async with connect() as db:
                await _prepare(db)
                await refund_user_tokens(db, cb.from_user.id, GENERATION_COST_TOKENS)

            log.exception("Veo3 submit failed: %s", exc)
            text = str(exc).lower()
            if "resource_exhausted" in text or "quota" in text or "rate limit" in text:
                await status_message.edit_text(
                    "❗ Не удалось начать генерацию: превышен лимит/квота Gemini API.\n"
                    "Попробуйте позже, включите оплату в Google AI Studio, либо переключите режим на Fast и 720p."
                )
            else:
                await status_message.edit_text("Не удалось начать генерацию")
            return

        poll_interval = max(3.0, settings.JOB_POLL_INTERVAL_SEC)
        failure_text = None
        while True:
            try:
                status = await generation_service.poll_video("veo3", job_id)
            except Exception as exc:
                log.exception("Veo3 poll failed: %s", exc)
                failure_text = "Ошибка при получении статуса генерации"
                break

            if status.status == "failed":
                # финальный провал — возвращаем токены
                async with connect() as db:
                    await _prepare(db)
                    await refund_user_tokens(db, cb.from_user.id, GENERATION_COST_TOKENS)
                failure_text = status.error or "Генерация завершилась с ошибкой"
                break

            if status.status == "succeeded":
                try:
                    await status_message.edit_text("Генерация завершена, готовлю видео…")
                except TelegramBadRequest as exc:
                    if not _not_modified(exc):
                        raise
                try:
                    video_path = await generation_service.download_video("veo3", job_id)
                except Exception as exc:
                    log.exception("Veo3 download failed: %s", exc)
                    # возврат токенов при невозможности получить результат
                    async with connect() as db:
                        await _prepare(db)
                        await refund_user_tokens(db, cb.from_user.id, GENERATION_COST_TOKENS)
                    failure_text = "Не удалось скачать видео"
                    break
                try:
                    await message.answer_video(video=FSInputFile(video_path), caption="Готово!")
                finally:
                    with suppress(OSError):
                        os.remove(video_path)
                await status_message.edit_text("Видео отправлено")
                return

            progress = status.progress or 0
            try:
                await status_message.edit_text(f"Генерация началась\nГотовность: {progress}%")
            except TelegramBadRequest as exc:
                if not _not_modified(exc):
                    raise
            await asyncio.sleep(poll_interval)

        if failure_text:
            await status_message.edit_text(failure_text)
        return

    if action == "reset":
        await _set_data(state, VEO_DEFAULT_STATE.copy())
        await cb.answer("Настройки сброшены")
        data = await _get_data(state)
        await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        await state.set_state(VeoWizardStates.summary)
        return
    if action == "back":
        await cb.answer()
        await state.clear()
        try:
            await message.edit_text(text=WELCOME, reply_markup=main_menu_kb())
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise
        return
    await cb.answer()


@router.message(VeoWizardStates.prompt_input)
async def prompt_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Промт не может быть пустым, попробуйте снова")
        return
    data = await _update_data(state, prompt=text)
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Промт обновлён")


@router.message(VeoWizardStates.negative_input)
async def negative_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Negative prompt не может быть пустым")
        return
    data = await _update_data(state, negative_text=text)
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Negative prompt сохранён")


@router.message(VeoWizardStates.reference_input, F.photo)
async def reference_input(msg: Message, state: FSMContext) -> None:
    file = msg.photo[-1]
    data = await _update_data(state, reference_file_id=file.file_id)
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Референс сохранён")


@router.message(VeoWizardStates.reference_input)
async def reference_input_invalid(msg: Message) -> None:
    await msg.answer("Пришлите изображение в качестве референса")


class LumaWizardStates(StatesGroup):
    summary = State()
    prompt_input = State()
    video_input = State()


LUMA_DATA_KEY = "luma_state"
LUMA_META_KEY = "luma_summary_meta"
LUMA_DEFAULT_STATE: dict[str, Any] = {"prompt": None, "video_file_id": None, "intensity": 1}


async def _luma_get_data(state: FSMContext) -> dict[str, Any]:
    data = await state.get_data()
    stored = data.get(LUMA_DATA_KEY)
    if stored is None:
        stored = LUMA_DEFAULT_STATE.copy()
        await state.update_data({LUMA_DATA_KEY: stored})
    return dict(stored)


async def _luma_set_data(state: FSMContext, new_state: dict[str, Any]) -> None:
    await state.update_data({LUMA_DATA_KEY: new_state})


async def _luma_update_data(state: FSMContext, **changes: Any) -> dict[str, Any]:
    current = await _luma_get_data(state)
    current.update(changes)
    await _luma_set_data(state, current)
    return current


async def _luma_store_summary(message: Message, state: FSMContext) -> None:
    meta = {"chat_id": message.chat.id, "message_id": message.message_id}
    if message.message_thread_id is not None:
        meta["thread_id"] = message.message_thread_id
    await state.update_data({LUMA_META_KEY: meta})


async def _luma_get_summary_meta(state: FSMContext) -> dict[str, Any] | None:
    data = await state.get_data()
    meta = data.get(LUMA_META_KEY)
    if isinstance(meta, dict):
        return meta
    return None


def _render_luma_summary(state: dict[str, Any]) -> str:
    # версия без строки «Режим»
    prompt = state.get("prompt") or "—"
    video = "да" if state.get("video_file_id") else "нет"
    intensity = int(state.get("intensity") or 1)
    lines = [
        "✂️ Luma",
        f"📝 Промпт: {prompt}",
        f"🎬 Видео: {video}",
        f"🎚️ Интенсивность: x{intensity}",
        "📎 Можно сгенерировать видео по промпту или отредактировать своё видео (загрузите файл и добавьте промпт).",
    ]
    return "\n".join(lines)


async def _luma_update_view(*, message: Message | None, bot, state: FSMContext, data: dict[str, Any]) -> None:
    text = _render_luma_summary(data)
    markup = luma_options_kb(data)
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                try:
                    await message.edit_reply_markup(reply_markup=markup)
                except TelegramBadRequest as inner_exc:
                    if not _not_modified(inner_exc):
                        raise
            else:
                raise
        return
    if bot is None:
        return
    meta = await _luma_get_summary_meta(state)
    if not meta:
        return
    chat_id = meta.get("chat_id")
    message_id = meta.get("message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if _not_modified(exc):
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except TelegramBadRequest as inner_exc:
                if not _not_modified(inner_exc):
                    raise
        else:
            raise


async def _luma_ensure_summary_message(msg: Message, state: FSMContext) -> Message:
    data = await _luma_get_data(state)
    summary_text = _render_luma_summary(data)
    markup = luma_options_kb(data)
    sent = await msg.answer(summary_text, reply_markup=markup)
    await _luma_store_summary(sent, state)
    await state.set_state(LumaWizardStates.summary)
    return sent


async def start_luma_wizard(msg: Message, state: FSMContext) -> None:
    meta = await _luma_get_summary_meta(state)
    if meta:
        with suppress(TelegramBadRequest):
            await msg.bot.edit_message_reply_markup(chat_id=meta["chat_id"], message_id=meta["message_id"], reply_markup=None)
    await _luma_set_data(state, LUMA_DEFAULT_STATE.copy())
    await _luma_ensure_summary_message(msg, state)


@router.message(Command("luma"))
async def cmd_luma(msg: Message, state: FSMContext) -> None:
    await start_luma_wizard(msg, state)


@router.callback_query(F.data.startswith("luma:"))
async def luma_callback(cb: CallbackQuery, state: FSMContext) -> None:
    message = cb.message
    if message is None or cb.data is None:
        await cb.answer()
        return
    action, value = _parse_callback(cb.data)
    data = await _luma_get_data(state)
    if action == "video" and value == "attach":
        await cb.answer("Загрузите видео для редактирования")
        await message.answer("Пришлите видео или mp4-файл для редактирования")
        await state.set_state(LumaWizardStates.video_input)
        return
    if action == "prompt" and value == "input":
        await cb.answer("Введите промпт")
        await message.answer("Отправьте текст промпта для редактирования")
        await state.set_state(LumaWizardStates.prompt_input)
        return
    if action == "intensity" and value == "cycle":
        current = int(data.get("intensity") or 1)
        new_value = 1 if current >= 3 else current + 1
        data = await _luma_update_data(state, intensity=new_value)
        await cb.answer(f"Интенсивность: x{new_value}")
        await _luma_update_view(message=message, bot=None, state=state, data=data)
        return
    if action == "generate":
        prompt = (data.get("prompt") or "").strip()
        video_file_id = data.get("video_file_id")
        if video_file_id:
            if not prompt:
                await cb.answer("Добавьте промт (для редактирования видео)", show_alert=True)
                return
        else:
            if not prompt:
                await cb.answer("Добавьте промт (генерация по тексту)", show_alert=True)
                return
        await cb.answer("Запуск…")
        await _run_luma_generation(message, data)
        return
    if action == "reset":
        data = LUMA_DEFAULT_STATE.copy()
        await _luma_set_data(state, data)
        await cb.answer("Настройки сброшены")
        await _luma_update_view(message=message, bot=None, state=state, data=data)
        await state.set_state(LumaWizardStates.summary)
        return
    if action == "back":
        await cb.answer()
        await state.clear()
        try:
            await message.edit_text(text=WELCOME, reply_markup=main_menu_kb())
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise
        return
    await cb.answer()


@router.message(LumaWizardStates.prompt_input)
async def luma_prompt_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Промпт не может быть пустым, попробуйте снова.")
        return
    moderation = check_text(text)
    if not moderation.allow:
        await msg.answer(f"Промпт отклонён модерацией: {moderation.reason}")
        return
    data = await _luma_update_data(state, prompt=text)
    await state.set_state(LumaWizardStates.summary)
    await _luma_update_view(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Промпт сохранён")


@router.message(LumaWizardStates.video_input)
async def luma_video_input(msg: Message, state: FSMContext) -> None:
    file_id: str | None = None
    if msg.video:
        file_id = msg.video.file_id
    elif msg.document and (msg.document.mime_type == "video/mp4" or (msg.document.file_name or "").lower().endswith(".mp4")):
        file_id = msg.document.file_id
    if not file_id:
        await msg.answer("Пришлите видео или mp4-файл.")
        return
    data = await _luma_update_data(state, video_file_id=file_id)
    await state.set_state(LumaWizardStates.summary)
    await _luma_update_view(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Видео сохранено")


async def _run_luma_generation(message: Message, data: dict[str, Any]) -> None:
    prompt = (data.get("prompt") or "").strip()
    video_file_id = data.get("video_file_id")
    intensity = int(data.get("intensity") or 1)
    mode = "edit" if video_file_id else "generate"

    # 1) Баланс
    async with connect() as db:
        await _prepare(db)
        bal = await get_user_balance(db, message.from_user.id)
    if bal < GENERATION_COST_TOKENS:
        await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
        return

    # 2) Списываем токены (атомарно)
    async with connect() as db:
        await _prepare(db)
        charged = await charge_user_tokens(db, message.from_user.id, GENERATION_COST_TOKENS)
    if not charged:
        await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
        return

    status_message = await message.answer("Генерация началась…")
    extras: dict[str, Any] = {"intensity": intensity}
    if mode == "edit" and video_file_id:
        extras["video_file_id"] = video_file_id

    params = GenerationParams(
        prompt=prompt,
        provider=Provider.LUMA,
        model=None,
        extras=extras,
    )

    async with connect() as db:
        await _prepare(db)
        user = await ensure_user(db, message.from_user.id, message.from_user.username, 0)
        job_id = await create_job(
            db,
            user_id=user["id"],
            provider=Provider.LUMA,
            prompt=prompt,
            model=mode,
            mode=(f"x{intensity}" if mode == "edit" else "text2video"),
        )

    try:
        provider_job_id = await generation_service.create_job(params)
    except Exception as exc:
        log.exception("Luma submission failed: %s", exc)
        await status_message.edit_text(f"Не удалось отправить задачу Luma\n{exc}")
        async with connect() as db:
            await _prepare(db)
            await set_job_status(db, job_id, "failed")
            # возврат токенов
            await refund_user_tokens(db, message.from_user.id, GENERATION_COST_TOKENS)
        return

    async with connect() as db:
        await _prepare(db)
        await set_provider_job_id(db, job_id, provider_job_id)
        await set_job_status(db, job_id, "running")

    poll_interval = max(3.0, settings.JOB_POLL_INTERVAL_SEC)
    failure_text: str | None = None
    while True:
        try:
            status = await generation_service.poll_job(Provider.LUMA, provider_job_id)
        except Exception as exc:
            log.exception("Luma poll failed: %s", exc)
            failure_text = "Ошибка при получении статуса Luma"
            break

        if status.status == "failed":
            async with connect() as db:
                await _prepare(db)
                await refund_user_tokens(db, message.from_user.id, GENERATION_COST_TOKENS)
            failure_text = status.error or "Luma не смогла завершить задачу"
            break

        if status.status == "succeeded":
            try:
                await status_message.edit_text("Генерация завершена, готовлю видео…")
            except TelegramBadRequest as exc:
                if not _not_modified(exc):
                    raise
            try:
                video_path = await generation_service.download_job(Provider.LUMA, provider_job_id)
            except Exception as exc:
                log.exception("Luma download failed: %s", exc)
                async with connect() as db:
                    await _prepare(db)
                    await refund_user_tokens(db, message.from_user.id, GENERATION_COST_TOKENS)
                failure_text = "Не удалось скачать видео"
                break
            try:
                await message.answer_video(video=FSInputFile(video_path), caption="Готово!")
            finally:
                with suppress(OSError):
                    os.remove(video_path)
            await status_message.edit_text("Видео отправлено")
            return

        progress = status.progress or 0
        try:
            await status_message.edit_text(f"Генерация идёт…\nГотовность: {progress}%")
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise
        await asyncio.sleep(poll_interval)

    if failure_text:
        await status_message.edit_text(failure_text)
