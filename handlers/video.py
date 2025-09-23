# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional, Tuple

import asyncio
import httpx
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
    GENERATION_COST_TOKENS,  # оставляю импорт для совместимости, но не используем
)
from keyboards.main_menu_kb import main_menu_kb, balance_kb_placeholder
from keyboards.veo_kb import veo_options_kb, veo_post_gen_kb
from keyboards.luma_kb import luma_options_kb
from providers.base import Provider
from providers.models import GenerationParams
from services import generation_service
from services.moderation import check_text
from services.media_tools import (
    enforce_ar_no_bars,
    build_vertical_blurpad,
)
from texts import WELCOME, INSUFFICIENT_TOKENS

router = Router()
log = logging.getLogger(__name__)

# -------- admin helpers --------
def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id)

# -------- Veo states --------
class VeoWizardStates(StatesGroup):
    summary = State()
    prompt_input = State()
    negative_input = State()  # не используется клавиатурой, но можно оставить
    reference_input = State()

VEO_DEFAULT_STATE: dict[str, Any] = {
    "prompt": None,
    "negative_enabled": False,
    "negative_text": None,
    "ar": "16:9",
    "mode": "quality",
    "reference_file_id": None,
    "reference_url": None,
    "image_bytes": None,
    "image_mime": None,
}

SUMMARY_META_KEY = "veo_summary_message"
DATA_KEY = "veo_state"

# ---- стоимость (из settings / .env, с дефолтами) ----
def _veo_costs() -> tuple[float, float]:
    fast = getattr(settings, "VEO_COST_FAST_TOKENS", None)
    qual = getattr(settings, "VEO_COST_QUALITY_TOKENS", None)
    if fast is None:
        fast = float(os.getenv("VEO_COST_FAST_TOKENS", "2.0"))
    if qual is None:
        qual = float(os.getenv("VEO_COST_QUALITY_TOKENS", "10.0"))
    try:
        fast = float(fast)
    except Exception:
        fast = 2.0
    try:
        qual = float(qual)
    except Exception:
        qual = 10.0
    return float(fast), float(qual)

def _current_cost(state: dict[str, Any]) -> float:
    fast, qual = _veo_costs()
    mode = (state.get("mode") or "quality").lower()
    return fast if mode == "fast" else qual

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

# -------- summary text --------
def _render_summary(state: dict[str, Any]) -> str:
    prompt = (state.get("prompt") or "").strip()
    has_ref = bool(state.get("reference_file_id") or state.get("reference_url") or state.get("image_bytes"))
    ar = (state.get("ar") or "16:9")
    mode = (state.get("mode") or "quality").lower()
    mode_label = "Fast ⚡" if mode == "fast" else "Quality 🎬"
    cost = _current_cost(state)

    if not prompt and not has_ref:
        return "🚀 Режим генерации активирован. Пришлите промпт или фото, затем нажмите «Сгенерировать»."

    lines: list[str] = []
    if prompt:
        lines.append("✍️ Промпт:")
        lines.append(prompt)
    else:
        lines.append("✍️ Промпт: —")

    lines.append(f"\n🖼 Референс: {'добавлен' if has_ref else '—'}")

    ar_icon = "📱" if ar == "9:16" else "🖥️"
    lines.append("\n🧩 Параметры генерации:")
    lines.append(f"• Формат: {ar} {ar_icon}")
    lines.append(f"• Режим: {mode_label}")
    lines.append(f"• Промпт: {'есть 💪' if prompt else 'нет —'}")
    lines.append(f"• Референс: {'есть 🖼' if has_ref else 'нет —'}")
    lines.append(f"• Стоимость: {cost:.1f} токена(ов) 💰")
    return "\n".join(lines)

# ---- Рендер/обновление сводки с надёжным фоллбэком ----
async def _edit_summary(
    *,
    message: Message | None,
    bot,
    state: FSMContext,
    data: dict[str, Any],
    fallback: Message | None = None,  # сюда передаём msg, если редактируем по meta
) -> None:
    text = _render_summary(data)
    markup = veo_options_kb(data)

    # 1) Если передан сам message (обычно это и есть сводка) — пробуем редактировать его.
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                with suppress(TelegramBadRequest):
                    await message.edit_reply_markup(reply_markup=markup)
                return
            # Падает редактирование? Отправляем новое.
            if fallback is None:
                fallback = message
        except Exception:
            if fallback is None:
                fallback = message

    # 2) Путь через сохранённую meta (обычно для обновления «издалека»).
    meta = await _get_summary_meta(state)
    if meta:
        try:
            await bot.edit_message_text(
                chat_id=meta["chat_id"], message_id=meta["message_id"], text=text, reply_markup=markup
            )
            return
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                with suppress(TelegramBadRequest):
                    await bot.edit_message_reply_markup(
                        chat_id=meta["chat_id"], message_id=meta["message_id"], reply_markup=markup
                    )
                return
            # если редактирование не удалось — попробуем отправить новое сообщение ниже
        except Exception:
            pass

    # 3) Надёжный фоллбэк — присылаем новую сводку в чат пользователя.
    if fallback is not None:
        with suppress(Exception):
            new_msg = await fallback.answer(text, reply_markup=markup)
            await _store_summary(new_msg, state)

async def _ensure_summary_message(msg: Message, state: FSMContext) -> Message:
    data = await _get_data(state)
    sent = await msg.answer(_render_summary(data), reply_markup=veo_options_kb(data))
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
            await msg.bot.edit_message_reply_markup(
                chat_id=meta["chat_id"], message_id=meta["message_id"], reply_markup=None
            )
    await _set_data(state, VEO_DEFAULT_STATE.copy())
    await _ensure_summary_message(msg, state)

@router.message(Command("veo"))
async def cmd_veo(msg: Message, state: FSMContext) -> None:
    await start_veo_wizard(msg, state)

async def _file_id_to_url(bot, file_id: str) -> str | None:
    try:
        tg_file = await bot.get_file(file_id)
        file_path = tg_file.file_path
        token = settings.BOT_TOKEN
        if not token:
            log.warning("BOT_TOKEN is not set; can't build file URL for reference image")
            return None
        return f"https://api.telegram.org/file/bot{token}/{file_path}"
    except Exception as exc:
        log.exception("Failed to resolve file_id to URL: %s", exc)
        return None

async def _fetch_image_bytes(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not url:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            raw = resp.content
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower() or None
            if not mime or not mime.startswith("image/"):
                lower = url.lower()
                if lower.endswith(".png"):
                    mime = "image/png"
                elif lower.endswith(".jpg") or lower.endswith(".jpeg"):
                    mime = "image/jpeg"
                else:
                    mime = "image/jpeg"
            return raw, mime
    except Exception as exc:
        log.exception("Failed to fetch image bytes: %s", exc)
        return None, None

async def _stitch_if_needed(reference_url: str | None, video_path: Path) -> Path:
    return video_path

# --- фикс чёрных полос: асинхронная обёртка ---
async def _normalize_result(src: Path, aspect: str) -> Path:
    try:
        if aspect == "9:16":
            dst = src.with_name(src.stem + "_blurpad.mp4")
            await asyncio.to_thread(build_vertical_blurpad, str(src), str(dst))
        else:
            dst = src.with_name(src.stem + "_normalized.mp4")
            await asyncio.to_thread(enforce_ar_no_bars, str(src), str(dst), "16:9")
        return dst if dst.exists() else src
    except Exception as exc:
        log.exception("output normalization failed: %s", exc)
        return src

# ---------- прямой ввод в summary ----------
@router.message(VeoWizardStates.summary, F.text)
async def veo_summary_text_input(msg: Message, state: FSMContext) -> None:
    if (msg.text or "").strip().startswith("/"):
        return
    text = (msg.text or "").strip()
    if not text:
        return
    moderation = check_text(text)
    if not moderation.allow:
        await msg.answer(f"Промт отклонён модерацией: {moderation.reason}")
        return
    data = await _update_data(state, prompt=text)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data, fallback=msg)

@router.message(VeoWizardStates.summary, F.photo | (F.document & (F.document.mime_type.startswith("image/"))))
async def veo_summary_image_input(msg: Message, state: FSMContext) -> None:
    file_id: str | None = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        return
    url = await _file_id_to_url(msg.bot, file_id)
    img_bytes: Optional[bytes] = None
    img_mime: Optional[str] = None
    if url:
        fetched_bytes, fetched_mime = await _fetch_image_bytes(url)
        if fetched_bytes and fetched_mime:
            img_bytes, img_mime = fetched_bytes, fetched_mime
    data = await _update_data(
        state,
        reference_file_id=file_id,
        reference_url=url,
        image_bytes=img_bytes,
        image_mime=img_mime,
    )
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data, fallback=msg)

# ---------- Клавиатурные колбэки ----------
@router.callback_query(F.data.startswith("veo:"))
async def veo_callback(cb: CallbackQuery, state: FSMContext) -> None:
    message = cb.message
    if message is None or not cb.data:
        await cb.answer(); return

    action, value = _parse_callback(cb.data)
    data = await _get_data(state)

    if action == "ar":
        mapping = {"16_9": "16:9", "9_16": "9:16"}
        chosen = mapping.get(value or "")
        if chosen:
            data = await _update_data(state, ar=chosen)
            await cb.answer("Выбрано соотношение сторон")
            await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        else:
            await cb.answer()
        return

    if action == "res":
        await cb.answer(); return

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
        had_prompt = bool((data.get("prompt") or "").strip())
        if had_prompt:
            data = await _update_data(state, prompt=None)
            await message.answer("Текущий промпт очищен. Пришлите новый текст-промпт.")
            await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        else:
            await message.answer("Отправьте текст промпта")

        await state.set_state(VeoWizardStates.prompt_input)
        await cb.answer()
        return

    if action == "ref" and value == "attach":
        ref_exists = bool(
            data.get("reference_file_id") or
            data.get("reference_url") or
            data.get("image_bytes")
        )
        if ref_exists:
            data = await _update_data(
                state,
                reference_file_id=None,
                reference_url=None,
                image_bytes=None,
                image_mime=None,
            )
            await message.answer("Референс очищен. Пришлите новое фото (jpg/png).")
            await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        else:
            await message.answer("Пришлите изображение-референс (jpg/png).")

        await state.set_state(VeoWizardStates.reference_input)
        await cb.answer()
        return

    if action == "ref" and value == "clear":
        # Кнопки очистки сейчас нет в UI, но обработчик оставим на всякий случай.
        data = await _update_data(
            state, reference_file_id=None, reference_url=None, image_bytes=None, image_mime=None
        )
        await cb.answer("Референс удалён")
        await _edit_summary(message=message, bot=message.bot, state=state, data=data)
        return

    if action == "generate":
        prompt = (data.get("prompt") or "").strip()
        aspect = data.get("ar")
        has_ref = bool(data.get("image_bytes") or data.get("reference_url") or data.get("reference_file_id"))
        if (not prompt) and (not has_ref):
            await cb.answer("Добавьте промпт или референс", show_alert=True)
            return
        if not aspect:
            await cb.answer("Выберите соотношение сторон (16:9 / 9:16)", show_alert=True)
            return

        # Если промпт пустой, но есть фото — подставим безопасный дефолт,
        # чтобы Polza не вернула 400 и точно сделала photo->video.
        used_prompt = prompt or "Animate this image realistically with native audio; keep the subject and style consistent."

        resolution_first = 1080  # 1080p
        reference_file_id = data.get("reference_file_id")
        reference_url = data.get("reference_url")
        mode = (data.get("mode") or "quality").lower()
        negative_prompt = (data.get("negative_text") or None) if data.get("negative_enabled") else None
        is_admin = _is_admin(cb.from_user.id)

        async with connect() as db:
            await _prepare(db)
            await ensure_user(db, cb.from_user.id, cb.from_user.username, settings.FREE_TOKENS_ON_JOIN)

        expected_cost = _current_cost(data)

        if not is_admin:
            async with connect() as db:
                await _prepare(db)
                bal = await get_user_balance(db, cb.from_user.id)
            if bal < expected_cost:
                await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
                await cb.answer(); return

        if not is_admin:
            async with connect() as db:
                await _prepare(db)
                charged = await charge_user_tokens(db, cb.from_user.id, expected_cost)
            if not charged:
                await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder())
                await cb.answer(); return

        await cb.answer("Генерация запущена")
        status_message = await message.answer("Генерация началась")

        try:
            ref_value = (reference_url or reference_file_id) or None
            image_bytes: Optional[bytes] = data.get("image_bytes")
            image_mime: Optional[str] = data.get("image_mime")
            if (not image_bytes) and reference_url:
                fetched_bytes, fetched_mime = await _fetch_image_bytes(reference_url)
                if fetched_bytes and fetched_mime:
                    image_bytes, image_mime = fetched_bytes, fetched_mime
                    await _update_data(state, image_bytes=image_bytes, image_mime=image_mime)

            strict = True
            job_id_first = await generation_service.create_video(
                provider="veo3",
                prompt=used_prompt,
                aspect_ratio=aspect,
                resolution=resolution_first,
                negative_prompt=negative_prompt,
                fast=(mode == "fast"),
                reference_file_id=ref_value,
                strict_ar=strict,
                image_bytes=image_bytes,
                image_mime=image_mime,
            )
        except Exception as exc:
            if not is_admin:
                async with connect() as db:
                    await _prepare(db)
                    await refund_user_tokens(db, cb.from_user.id, expected_cost)
            log.exception("Veo3 submit failed: %s", exc)
            txt = str(exc)
            low = txt.lower()
            # Новая дружелюбная ветка для Polza
            if "INSUFFICIENT_BALANCE" in txt or "payment required" in low or "402" in low:
                await status_message.edit_text(
                    "❗ Не удалось начать генерацию: недостаточно средств в Polza.ai.\n"
                    "Пополните баланс/повысьте лимит ключа и попробуйте снова."
                )
            elif "resource_exhausted" in low or "quota" in low or "rate limit" in low:
                await status_message.edit_text(
                    "❗ Не удалось начать генерацию: превышен лимит/квота провайдера.\n"
                    "Попробуйте позже или переключите режим на Fast."
                )
            else:
                await status_message.edit_text("Не удалось начать генерацию")
            return

        poll_interval = max(3.0, settings.JOB_POLL_INTERVAL_SEC)
        interval_plan = [6.0, 10.0, 15.0]
        first_status = await generation_service.wait_for_completion(
            Provider.VEO3, job_id_first, interval_sec=poll_interval,
            timeout_sec=max(60.0, settings.JOB_MAX_WAIT_MIN * 60), interval_schedule=interval_plan
        )
        if first_status.status != "succeeded":
            if not is_admin:
                async with connect() as db:
                    await _prepare(db)
                    await refund_user_tokens(db, cb.from_user.id, expected_cost)
            await status_message.edit_text(first_status.error or "Генерация завершилась с ошибкой")
            return

        try:
            await status_message.edit_text("Генерация завершена, готовлю видео…")
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise

        try:
            video_path_first = await generation_service.download_video("veo3", job_id_first)
        except Exception as exc:
            log.exception("Veo3 download (first) failed: %s", exc)
            if not is_admin:
                async with connect() as db:
                    await _prepare(db)
                    await refund_user_tokens(db, cb.from_user.id, expected_cost)
            await status_message.edit_text("Не удалось скачать видео")
            return

        to_send_first = video_path_first
        to_send_first_fixed = await _normalize_result(Path(to_send_first), aspect)

        caption_first = "Ваше видео сгенерировано. Спасибо что пользуетесь нашим ботом"
        if not is_admin:
            async with connect() as db:
                await _prepare(db)
                left_balance = await get_user_balance(db, cb.from_user.id)
            caption_first = (
                f"Ваше видео сгенерировано. Остаток баланса - {left_balance} токенов. "
                f"Спасибо что пользуетесь нашим ботом"
            )

        try:
            await message.answer_video(
                video=FSInputFile(to_send_first_fixed),
                caption=caption_first,
                supports_streaming=True,
                reply_markup=veo_post_gen_kb(),
            )
        finally:
            with suppress(OSError):
                os.remove(video_path_first)
            if Path(to_send_first) != Path(video_path_first):
                with suppress(OSError):
                    os.remove(to_send_first)
            if Path(to_send_first_fixed) not in (Path(video_path_first), Path(to_send_first)):
                with suppress(OSError):
                    os.remove(to_send_first_fixed)

        # Если пользователь выбрал Quality — запускаем HQ-проход (и для 9:16 тоже!)
        if mode != "quality":
            await status_message.edit_text("Видео отправлено")
            return

        try:
            # Второй проход: форсируем качественную модель 'veo3' и 1080p
            params_hq = GenerationParams(
                prompt=used_prompt,
                provider=Provider.VEO3,
                aspect_ratio=aspect,          # 9:16 поддерживаем так же, как 16:9
                resolution="1080p",
                negative_prompt=negative_prompt,
                fast_mode=False,
                image_bytes=data.get("image_bytes"),
                image_mime=data.get("image_mime"),
                strict_ar=True,
                extras={**({"reference_file_id": (reference_url or reference_file_id)} if (reference_url or reference_file_id) else {})},
                model="veo3",                # качественная модель
            )
            job_id_hq = await generation_service.create_job(params_hq)
        except Exception as exc:
            log.exception("Veo3 submit (HQ) failed: %s", exc)
            await status_message.edit_text("Видео отправлено (HQ-версию начать не удалось)")
            return

        hq_status = await generation_service.wait_for_completion(
            Provider.VEO3, job_id_hq, interval_sec=poll_interval,
            timeout_sec=max(60.0, settings.JOB_MAX_WAIT_MIN * 60), interval_schedule=interval_plan
        )
        if hq_status.status != "succeeded":
            await status_message.edit_text("Видео отправлено (HQ-версию сгенерировать не удалось)")
            return

        try:
            video_path_hq = await generation_service.download_video("veo3", job_id_hq)
        except Exception as exc:
            log.exception("Veo3 download (HQ) failed: %s", exc)
            await status_message.edit_text("Видео отправлено (HQ-версию скачать не удалось)")
            return

        to_send_hq = video_path_hq
        to_send_hq_fixed = await _normalize_result(Path(to_send_hq), aspect)

        try:
            await message.answer_video(video=FSInputFile(to_send_hq_fixed), caption="Оригинал (HQ)")
        finally:
            with suppress(OSError):
                os.remove(video_path_hq)
            if Path(to_send_hq) != Path(video_path_hq):
                with suppress(OSError):
                    os.remove(to_send_hq)
            if Path(to_send_hq_fixed) not in (Path(video_path_hq), Path(to_send_hq)):
                with suppress(OSError):
                    os.remove(to_send_hq_fixed)

        await status_message.edit_text("Видео отправлено")
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
            async with connect() as db:
                await _prepare(db)
                bal = await get_user_balance(db, cb.from_user.id)
            await message.edit_text(text=WELCOME, reply_markup=main_menu_kb(bal))
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise
        return

    await cb.answer()

# ----- старые ручки -----
@router.message(VeoWizardStates.prompt_input)
async def prompt_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Промт не может быть пустым, попробуйте снова")
        return
    data = await _update_data(state, prompt=text)
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data, fallback=msg)
    await msg.answer("Промт обновлён")

@router.message(VeoWizardStates.negative_input)
async def negative_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Negative prompt не может быть пустым")
        return
    data = await _update_data(state, negative_text=text)
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data, fallback=msg)
    await msg.answer("Negative prompt сохранён")

@router.message(VeoWizardStates.reference_input, F.photo | F.document)
async def reference_input(msg: Message, state: FSMContext) -> None:
    file_id: str | None = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        await msg.answer("Пришлите изображение (фото) в качестве референса")
        return

    url = await _file_id_to_url(msg.bot, file_id)
    img_bytes: Optional[bytes] = None
    img_mime: Optional[str] = None
    if url:
        fetched_bytes, fetched_mime = await _fetch_image_bytes(url)
        if fetched_bytes and fetched_mime:
            img_bytes, img_mime = fetched_bytes, fetched_mime

    data = await _update_data(
        state,
        reference_file_id=file_id,
        reference_url=url,
        image_bytes=img_bytes,
        image_mime=img_mime,
    )
    await state.set_state(VeoWizardStates.summary)
    await _edit_summary(message=None, bot=msg.bot, state=state, data=data, fallback=msg)
    await msg.answer("Референс сохранён")

@router.message(VeoWizardStates.reference_input)
async def reference_input_invalid(msg: Message) -> None:
    await msg.answer("Пришлите изображение в качестве референса")

# --------------- Luma ---------------
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
    try:
        await bot.edit_message_text(chat_id=meta["chat_id"], message_id=meta["message_id"], text=text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if _not_modified(exc):
            try:
                await bot.edit_message_reply_markup(chat_id=meta["chat_id"], message_id=meta["message_id"], reply_markup=markup)
            except TelegramBadRequest as inner_exc:
                if not _not_modified(inner_exc):
                    raise
        else:
            raise

async def _luma_ensure_summary_message(msg: Message, state: FSMContext) -> Message:
    data = await _luma_get_data(state)
    sent = await msg.answer(_render_luma_summary(data), reply_markup=luma_options_kb(data))
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
        await cb.answer(); return
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
        if video_file_id and not prompt:
            await cb.answer("Добавьте промпт (для редактирования видео)", show_alert=True); return
        if not video_file_id and not prompt:
            await cb.answer("Добавьте промпт (генерация по тексту)", show_alert=True); return
        await cb.answer("Запуск…")
        await _run_luma_generation(message, data); return

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
            async with connect() as db:
                await _prepare(db)
                bal = await get_user_balance(db, cb.from_user.id)
            await message.edit_text(text=WELCOME, reply_markup=main_menu_kb(bal))
        except TelegramBadRequest as exc:
            if not _not_modified(exc):
                raise
        return

    await cb.answer()

@router.message(LumaWizardStates.prompt_input)
async def luma_prompt_input(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Промт не может быть пустым, попробуйте снова."); return
    moderation = check_text(text)
    if not moderation.allow:
        await msg.answer(f"Промт отклонён модерацией: {moderation.reason}"); return
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
        await msg.answer("Пришлите видео или mp4-файл."); return
    data = await _luma_update_data(state, video_file_id=file_id)
    await state.set_state(LumaWizardStates.summary)
    await _luma_update_view(message=None, bot=msg.bot, state=state, data=data)
    await msg.answer("Видео сохранено")

async def _run_luma_generation(message: Message, data: dict[str, Any]) -> None:
    prompt = (data.get("prompt") or "").strip()
    video_file_id = data.get("video_file_id")
    intensity = int(data.get("intensity") or 1)
    mode = "edit" if video_file_id else "generate"
    is_admin = _is_admin(message.from_user.id)

    async with connect() as db:
        await _prepare(db)
        await ensure_user(db, message.from_user.id, message.from_user.username, settings.FREE_TOKENS_ON_JOIN)

    if not is_admin:
        async with connect() as db:
            await _prepare(db)
            bal = await get_user_balance(db, message.from_user.id)
        if bal < GENERATION_COST_TOKENS:
            await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder()); return

    if not is_admin:
        async with connect() as db:
            await _prepare(db)
            charged = await charge_user_tokens(db, message.from_user.id, GENERATION_COST_TOKENS)
        if not charged:
            await message.answer(INSUFFICIENT_TOKENS, reply_markup=balance_kb_placeholder()); return

    status_message = await message.answer("Генерация началась…")
    extras: dict[str, Any] = {"intensity": int(max(1, min(3, intensity)))}
    if mode == "edit" and video_file_id:
        extras["video_file_id"] = video_file_id

    params = GenerationParams(prompt=prompt, provider=Provider.LUMA, model=None, extras=extras)

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
            if not is_admin:
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
            if not is_admin:
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
                if not is_admin:
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
