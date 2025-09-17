# AI Video Bot

Telegram бот для генерации видео с использованием AI.

## Установка и запуск

1. Клонировать репозиторий:
```bash
git clone <repository-url>
cd ai_tgbot
```

2. Создать и активировать виртуальное окружение:
```bash
# Создать виртуальное окружение
python -m venv venv

# Активировать
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate
```

3. Установить зависимости:
```bash
pip install -r requirements.txt
```

4. Настроить конфигурацию:
```bash
# Создать файл .env из примера
copy ".env .example" .env  # Windows
# или
cp ".env .example" .env   # Linux/Mac

# Отредактировать .env и установить:
# - TG_BOT_TOKEN (получить у @BotFather)
# - Другие параметры при необходимости
```

5. Запустить бота:
```bash
python bot.py
```

## Основные функции

- Генерация видео с помощью AI
- Поддержка различных моделей (Veo-3, Luma)
- Выбор соотношения сторон (16:9, 9:16)
- Система модерации промптов
- Управление лимитами пользователей

## Структура проекта

- `bot.py` - основной файл бота
- `config.py` - конфигурация и настройки
- `db.py` - работа с базой данных
- `models.py` - модели данных
- `keyboards.py` - клавиатуры Telegram
- `handlers/` - обработчики команд
- `services/` - сервисы и провайдеры

## Требования

- Python 3.8+
- Зависимости из requirements.txt
- Telegram Bot Token