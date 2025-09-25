# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Mapping
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

State = Mapping[str, object]

def _prompt_label(has_prompt: bool) -> str:
    # начальный смайлик как на 1-м скрине, после добавления — стрелочка как на 3-м
    return "📝 Добавить промпт" if not has_prompt else "↩️ Изменить промпт"

def _video_label(has_video: bool) -> str:
    return "🎬 Добавить видео" if not has_video else "↩️ Заменить видео"

def luma_options_kb(state: State) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    has_video = bool(state.get("video_file_id"))
    has_prompt = bool((state.get("prompt") or "").strip())
    intensity = int(state.get("intensity") or 1)

    # независимые опции
    builder.row(
        InlineKeyboardButton(text=_prompt_label(has_prompt), callback_data="luma:prompt:input"),
        InlineKeyboardButton(text=_video_label(has_video),  callback_data="luma:video:attach"),
    )

    # переключатель интенсивности (для редактирования; можно нажимать в любой момент)
    builder.row(
        InlineKeyboardButton(text=f"🎚️ Интенсивность: x{intensity}", callback_data="luma:intensity:cycle"),
    )

    builder.row(InlineKeyboardButton(text="🚀 Запустить", callback_data="luma:generate"))
    builder.row(
        InlineKeyboardButton(text="🔁 Сброс", callback_data="luma:reset"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="luma:back"),
    )
    return builder.as_markup()
