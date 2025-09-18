# -*- coding: utf-8 -*-
"""Reply and inline keyboards used across the bot."""



from __future__ import annotations



from aiogram.filters.callback_data import CallbackData

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton

from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder





MAIN_BTNS = [

    ["?????+???'?? ?? ????????"],

    ["?'???>??????", "???????'??????O??"],

]



VIDEO_BTNS = [

    ["Veo-3", "Luma"],

    ["??????????"],

]



ASPECT_BTNS = [["16:9", "9:16"], ["???'???????"]]



ASPECT_OPTIONS = ("16:9", "9:16", "1:1")

RESOLUTION_OPTIONS = ("720p", "1080p")

DURATION_OPTIONS = ("4s", "8s")





class VeoCallback(CallbackData, prefix="veo"):

    """Callback payload for Veo wizard actions."""



    action: str

    value: str | None = None





def main_kb():

    b = ReplyKeyboardBuilder()

    for row in MAIN_BTNS:

        b.row(*[KeyboardButton(text=t) for t in row])

    return b.as_markup(resize_keyboard=True)





def video_kb():

    b = ReplyKeyboardBuilder()

    for row in VIDEO_BTNS:

        b.row(*[KeyboardButton(text=t) for t in row])

    return b.as_markup(resize_keyboard=True)





def aspect_kb():

    b = ReplyKeyboardBuilder()

    for row in ASPECT_BTNS:

        b.row(*[KeyboardButton(text=t) for t in row])

    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)





def veo_summary_kb(

    *,

    aspect: str | None,

    resolution: str | None,

    fast_mode: bool,

    duration: str | None,

    negative_enabled: bool,

) -> InlineKeyboardMarkup:

    """Inline keyboard summarising current Veo3 selections."""



    builder = InlineKeyboardBuilder()

    builder.button(

        text=f"AR: {aspect or '—'}",

        callback_data=VeoCallback(action="select_ar").pack(),

    )

    builder.button(

        text=f"Resolution: {resolution or '—'}",

        callback_data=VeoCallback(action="select_resolution").pack(),

    )

    builder.button(

        text=f"Скорость: {'Быстро' if fast_mode else 'Качество'}",

        callback_data=VeoCallback(action="flip_fast").pack(),

    )

    builder.button(

        text=f"Длительность: {duration or '—'}",

        callback_data=VeoCallback(action="select_duration").pack(),

    )

    builder.button(

        text=f"Negative prompt: {'Вкл' if negative_enabled else 'Выкл'}",

        callback_data=VeoCallback(action="toggle_negative").pack(),

    )

    builder.button(

        text="?? Промт",

        callback_data=VeoCallback(action="request_prompt").pack(),

    )

    builder.button(

        text="?? Генерировать (Veo3)",

        callback_data=VeoCallback(action="generate").pack(),

    )

    builder.adjust(1)

    return builder.as_markup()





def veo_options_kb(action: str, options: tuple[str, ...]) -> InlineKeyboardMarkup:

    """Inline keyboard for picking among provided options."""



    builder = InlineKeyboardBuilder()

    for option in options:

        builder.button(

            text=option,

            callback_data=VeoCallback(action=action, value=option).pack(),

        )

    builder.row(

        InlineKeyboardButton(

            text="?? Назад",

            callback_data=VeoCallback(action="summary").pack(),

        )

    )

    builder.adjust(2)

    return builder.as_markup()





def veo_fast_mode_kb() -> InlineKeyboardMarkup:

    """Keyboard for fast vs quality switch."""



    builder = InlineKeyboardBuilder()

    builder.row(

        InlineKeyboardButton(

            text="Быстро",

            callback_data=VeoCallback(action="set_fast", value="true").pack(),

        ),

        InlineKeyboardButton(

            text="Качество",

            callback_data=VeoCallback(action="set_fast", value="false").pack(),

        ),

    )

    builder.row(

        InlineKeyboardButton(

            text="?? Назад",

            callback_data=VeoCallback(action="summary").pack(),

        )

    )

    return builder.as_markup()

