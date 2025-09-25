# -*- coding: utf-8 -*-
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _format_balance(balance: float | int | None) -> str:
    """
    Форматируем баланс так, чтобы:
      - 0 показывался как '0.0'
      - None / NaN / отрицательные значения -> '0.0'
    Никаких подстановок из настроек, только фактическое значение.
    """
    try:
        val = 0.0 if balance is None else float(balance)
        if val != val:  # NaN
            val = 0.0
        if val < 0:
            val = 0.0
        return f"{val:.1f}"
    except Exception:
        return "0.0"


def main_menu_kb(balance: float | int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Блок работы с видео
    builder.row(
        InlineKeyboardButton(text="🧩 Работа с видео", callback_data="menu:video"),
    )

    # Баланс — всегда показываем число, даже если balance=None
    balance_label = f"💳 Баланс: {_format_balance(balance)}"
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


def back_to_main_menu_kb(balance: float | int | None = None) -> InlineKeyboardMarkup:
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
