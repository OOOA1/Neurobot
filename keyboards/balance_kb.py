# -*- coding: utf-8 -*-
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def balance_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧪 Пробный: 2 токена — 60 ₽", callback_data="buy:trial")],
        [InlineKeyboardButton(text="📦 База: 12 токенов — 330 ₽", callback_data="buy:base")],
        [InlineKeyboardButton(text="🧠 Нейро: 30 токенов — 700 ₽", callback_data="buy:neuro")],
        [InlineKeyboardButton(text="💎 Вип: 120 токенов — 2300 ₽", callback_data="buy:vip")],
        [InlineKeyboardButton(text="👑 Топ: 600 токенов — 12000 ₽", callback_data="buy:top")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="balance:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
