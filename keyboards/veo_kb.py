# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

StateDict = Mapping[str, object]


def _mark(label: str, *, selected: bool) -> str:
    """
    Помечаем выбранные пункты в тексте кнопки.
    Используем '✅' в конце, чтобы визуально не мешало основному лейблу.
    """
    return f"{label} ✅" if selected else label


def _norm_mode(val: object) -> str:
    v = (str(val or "quality")).lower()
    return "fast" if v == "fast" else "quality"


def veo_options_kb(state: StateDict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    prompt_present = bool(str(state.get("prompt") or "").strip())
    reference_present = bool(
        state.get("reference_file_id")
        or state.get("reference_url")
        or state.get("image_bytes")
    )

    ar_val = (state.get("ar") or "16:9")
    ar = str(ar_val).strip().lower()  # '16:9' | '9:16'
    mode = _norm_mode(state.get("mode"))

    # ВЕРХНИЕ ШИРОКИЕ КНОПКИ (каждая на своей строке — размер меню стабильный)
    builder.row(
        InlineKeyboardButton(
            text=("🔁 Референс" if reference_present else "🖼 Референс"),
            callback_data="veo:ref:attach",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=("🔁 Изменить промпт" if prompt_present else "📝 Добавить промпт"),
            callback_data="veo:prompt:input",
        )
    )

    if reference_present:
        builder.row(
            InlineKeyboardButton(text="❌ Убрать референс", callback_data="veo:ref:clear")
        )

    # Соотношение сторон
    builder.row(
        InlineKeyboardButton(text=_mark("16:9", selected=(ar == "16:9")), callback_data="veo:ar:16_9"),
        InlineKeyboardButton(text=_mark("9:16", selected=(ar == "9:16")), callback_data="veo:ar:9_16"),
    )

    # Режим
    builder.row(
        InlineKeyboardButton(
            text=_mark("🎬 Quality", selected=(mode == "quality")),
            callback_data="veo:mode:quality",
        ),
        InlineKeyboardButton(
            text=_mark("⚡ Fast", selected=(mode == "fast")),
            callback_data="veo:mode:fast",
        ),
    )

    # Действия
    builder.row(InlineKeyboardButton(text="🚀 Сгенерировать", callback_data="veo:generate"))
    builder.row(
        InlineKeyboardButton(text="🔄 Начать заново", callback_data="veo:reset"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="veo:back"),
    )

    return builder.as_markup()


def veo_post_gen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Сгенерировать ещё", callback_data="menu:video:veo")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:back")],
        ]
    )
