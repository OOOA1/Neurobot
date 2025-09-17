from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import KeyboardButton


MAIN_BTNS = [
["Работа с видео"],
["Баланс", "Инструкция"],
]


VIDEO_BTNS = [
["Veo-3", "Luma"],
["Назад"]
]


ASPECT_BTNS = [["16:9", "9:16"], ["Отмена"]]


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