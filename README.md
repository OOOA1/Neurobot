
# Neurobot — Telegram-бот для генерации видео (Veo 3 / Luma)

Лёгкий бот на **aiogram** c поддержкой:

-   генерации видео в двух режимах (_Quality_ / _Fast_);
    
-   промптов и референс-картинок;
    
-   учёта токенов и промокодов;
    
-   админ-меню (промокоды, список активных, рассылка).
    

----------

## 1) Быстрый старт

### Требования

-   Python **3.10+**
    
-   **FFmpeg**
    
-   Токен Telegram-бота (BotFather)
    
-   Проект в **Google Cloud** и API-ключ **Google AI Studio** (Gemini / Veo-3)
    

### Установка (venv + зависимости)

`git clone <URL_ВАШЕГО_РЕПО> neurobot cd neurobot`

`python -m venv .venv # macOS / Linux  source .venv/bin/activate # Windows (PowerShell) .\.venv\Scripts\Activate.ps1`

`pip install -U pip`
`pip install -r requirements.txt`

### FFmpeg — установка

`# macOS (Homebrew) brew install ffmpeg` 

`# Ubuntu/Debian sudo apt-get update && sudo apt-get install -y ffmpeg` 

`Windows:
1) Скачайте сборку (например, gyan.dev/ffmpeg/builds).
2) Распакуйте, добавьте папку "bin" в переменную окружения PATH.` 

Полезные ссылки:

-   Официальные сборки: [https://ffmpeg.org/download.html](https://ffmpeg.org/download.html)
    
-   Билды Windows: https://www.gyan.dev/ffmpeg/builds/
    

> Если FFmpeg не в `PATH` — можно явно указать пути в `.env` (`FFMPEG_PATH` / `FFPROBE_PATH`).

----------

## 2) Telegram Bot Token

1.  В Telegram откройте **@BotFather** ? `/newbot` ? следуйте инструкциям.
    
2.  Скопируйте выданный токен — он понадобится для `.env` (`BOT_TOKEN`).
    

----------

## 3) Google Cloud + Google AI Studio (API-ключ)

1.  **Создайте проект в Google Cloud**  
    https://console.cloud.google.com/ ? _Select a project_ ? **New Project**.
    
2.  **Получите API-ключ в Google AI Studio**  
    [https://ai.google.dev/](https://ai.google.dev/?utm_source=chatgpt.com) ? Sign in ? **Get API key** ? в выпадающем списке выберите **именно тот проект**, который создали на шаге 1 ? сгенерируйте ключ.
    
3.  **Сохраните ключ** — он нужен в `.env` как `GOOGLE_GENAI_API_KEY`.
    

----------

## 4) Конфигурация: `.env`

Создайте файл `.env` в корне и заполните:

`# --- Telegram ---
BOT_TOKEN=1234567890:ABCDEF_your_telegram_bot_token

GOOGLE_GENAI_API_KEY=your_google_ai_studio_api_key

VEO_COST_FAST_TOKENS=2.0
VEO_COST_QUALITY_TOKENS=10.0
FREE_TOKENS_ON_JOIN=10

JOB_POLL_INTERVAL_SEC=5
JOB_MAX_WAIT_MIN=15

FFMPEG_PATH=/usr/local/bin/ffmpeg
FFPROBE_PATH=/usr/local/bin/ffprobe

VIDEO_CRF=18
FFMPEG_PRESET=slow
FFMPEG_LOG_CMD=0` 



## 5) Запуск бота

Запускать **только** так (из корня проекта, с активированным venv):

`python bot.py` 

Бот подключится по `BOT_TOKEN` и будет готов к работе.



## 6) Команды (пользователь)

-   `/start` — приветствие, первичная настройка и вход в меню
    
-   `/menu` — открыть главное меню
    
-   `/help` — краткая справка по использованию
    
-   `/veo` — генерация **Veo 3** (текстовый промпт и/или референс-картинка)
    
-   `/luma` — генерация**Luma** (текстовый промпт или редактирование своего видео)
    
    

## 7) Команды (администратор)

-   `/admin` — открыть админ-меню:
    
    -   **Создать промо-коды** (генерация на заданное число токенов);
        
    -   **Список активных промо-кодов**;
        
    -   **Рассылка всем пользователям**.
        



## Благодарности

Если проект оказался полезен — поставьте :star: на GitHub и расскажите друзьям.