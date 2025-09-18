# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

StateDict = Mapping[str, object]

def _mark(label: str, *, selected: bool) -> str:
    return f"✅ {label}" if selected else label

def veo_options_kb(state: StateDict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    prompt_present = bool(state.get("prompt"))
    reference_present = bool(state.get("reference_file_id"))
    ar = (state.get("ar") or "16:9").lower()
    mode = (state.get("mode") or "quality").lower()
    resolution = (state.get("resolution") or "1080p").lower()

    # ВАЖНО: даём value, которые ждёт handlers/video.py
    builder.row(
        InlineKeyboardButton(
            text=_mark("🖼️ Референс", selected=reference_present),
            callback_data="veo:ref:attach",
        ),
        InlineKeyboardButton(
            text=_mark("✍️ Промт", selected=prompt_present),
            callback_data="veo:prompt:input",
        ),
    )

    # ВАЖНО: значения для AR с подчёркиванием (а не с двоеточием)
    builder.row(
        InlineKeyboardButton(
            text=_mark("16:9", selected=(ar == "16:9")),
            callback_data="veo:ar:16_9",
        ),
        InlineKeyboardButton(
            text=_mark("9:16", selected=(ar == "9:16")),
            callback_data="veo:ar:9_16",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text=_mark("720p", selected=(resolution == "720p")),
            callback_data="veo:res:720p",
        ),
        InlineKeyboardButton(
            text=_mark("1080p", selected=(resolution == "1080p")),
            callback_data="veo:res:1080p",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text=_mark("Quality", selected=(mode == "quality")),
            callback_data="veo:mode:quality",
        ),
        InlineKeyboardButton(
            text=_mark("Fast", selected=(mode == "fast")),
            callback_data="veo:mode:fast",
        ),
    )

    builder.row(
        InlineKeyboardButton(text="🚀 Сгенерировать", callback_data="veo:generate"),
    )
    builder.row(
        InlineKeyboardButton(text="🔁 Сброс", callback_data="veo:reset"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="veo:back"),
    )

    return builder.as_markup()
