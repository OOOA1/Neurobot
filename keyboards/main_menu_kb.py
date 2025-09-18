# -*- coding: utf-8 -*-
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🧩 Работа с видео", callback_data="menu:video"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Баланс", callback_data="menu:balance"),
        InlineKeyboardButton(text="📘 Инструкция", callback_data="menu:help"),
    )
    return builder.as_markup()

def back_to_main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Назад", callback_data="menu:back"))
    return builder.as_markup()

def video_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎬 Veo3", callback_data="menu:video:veo"),
        InlineKeyboardButton(text="✂️ Luma", callback_data="menu:video:luma"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"))
    return builder.as_markup()
