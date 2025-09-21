# -*- coding: utf-8 -*-
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu_kb(balance: float | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Блок работы с видео
    builder.row(
        InlineKeyboardButton(text="🧩 Работа с видео", callback_data="menu:video"),
    )

    # Баланс (с отображением числа токенов, если передан balance)
    balance_label = f"💳 Баланс: {balance:.1f}" if balance is not None else "💳 Баланс"
    builder.row(
        InlineKeyboardButton(text=balance_label, callback_data="menu:balance"),
    )

    # Промо и подарки
    builder.row(
        InlineKeyboardButton(text="🏷️ Промокод", callback_data="menu:promo"),
        InlineKeyboardButton(text="🎁 Подарить", callback_data="menu:gift"),
    )

    # Рефералка и примеры
    builder.row(
        InlineKeyboardButton(text="👥 Рефералка", callback_data="menu:ref"),
        InlineKeyboardButton(text="📚 Примеры", callback_data="menu:examples"),
    )

    # Инструкция и поддержка (URL-кнопки)
    builder.row(
        InlineKeyboardButton(text="📘 Инструкция", url="https://t.me/ablinov18"),
        InlineKeyboardButton(text="🛟 Тех.поддержка", url="https://t.me/ablinov18"),
    )

    return builder.as_markup()


def back_to_main_menu_kb(balance: float | None = None) -> InlineKeyboardMarkup:
    """
    Кнопка возврата в главное меню. Можно сразу отрисовать главное меню с балансом.
    """
    return main_menu_kb(balance)


def video_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎬 Veo3", callback_data="menu:video:veo"),
        InlineKeyboardButton(text="✂️ Luma", callback_data="menu:video:luma"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"))
    return builder.as_markup()


def balance_kb_placeholder() -> InlineKeyboardMarkup:
    """
    Раньше была заглушка пополнения.
    Теперь возвращаем реальную клавиатуру тарифов, чтобы не ломать старые импорты.
    """
    from keyboards.balance_kb import balance_kb  # абсолютный импорт, избегаем относительного
    return balance_kb()
