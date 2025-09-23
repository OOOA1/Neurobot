
# Neurobot � Telegram-��� ��� ��������� ����� (Veo 3 / Luma)

˸���� ��� �� **aiogram** c ����������:

-   ��������� ����� � ���� ������� (_Quality_ / _Fast_);
    
-   �������� � ��������-��������;
    
-   ����� ������� � ����������;
    
-   �����-���� (���������, ������ ��������, ��������).
    

----------

## 1) ������� �����

### ����������

-   Python **3.10+**
    
-   **FFmpeg**
    
-   ����� Telegram-���� (BotFather)
    
-   ������ � **Google Cloud** � API-���� **Google AI Studio** (Gemini / Veo-3)
    

### ��������� (venv + �����������)

`git clone <URL_������_����> neurobot cd neurobot`

`python -m venv .venv # macOS / Linux  source .venv/bin/activate # Windows (PowerShell) .\.venv\Scripts\Activate.ps1`

`pip install -U pip`
`pip install -r requirements.txt`

### FFmpeg � ���������

`# macOS (Homebrew) brew install ffmpeg` 

`# Ubuntu/Debian sudo apt-get update && sudo apt-get install -y ffmpeg` 

`Windows:
1) �������� ������ (��������, gyan.dev/ffmpeg/builds).
2) ����������, �������� ����� "bin" � ���������� ��������� PATH.` 

�������� ������:

-   ����������� ������: [https://ffmpeg.org/download.html](https://ffmpeg.org/download.html)
    
-   ����� Windows: https://www.gyan.dev/ffmpeg/builds/
    

> ���� FFmpeg �� � `PATH` � ����� ���� ������� ���� � `.env` (`FFMPEG_PATH` / `FFPROBE_PATH`).

----------

## 2) Telegram Bot Token

1.  � Telegram �������� **@BotFather** ? `/newbot` ? �������� �����������.
    
2.  ���������� �������� ����� � �� ����������� ��� `.env` (`BOT_TOKEN`).
    

----------

## 3) Google Cloud + Google AI Studio (API-����)

1.  **�������� ������ � Google Cloud**  
    https://console.cloud.google.com/ ? _Select a project_ ? **New Project**.
    
2.  **�������� API-���� � Google AI Studio**  
    [https://ai.google.dev/](https://ai.google.dev/?utm_source=chatgpt.com) ? Sign in ? **Get API key** ? � ���������� ������ �������� **������ ��� ������**, ������� ������� �� ���� 1 ? ������������ ����.
    
3.  **��������� ����** � �� ����� � `.env` ��� `GOOGLE_GENAI_API_KEY`.
    

----------

## 4) ������������: `.env`

�������� ���� `.env` � ����� � ���������:

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



## 5) ������ ����

��������� **������** ��� (�� ����� �������, � �������������� venv):

`python bot.py` 

��� ����������� �� `BOT_TOKEN` � ����� ����� � ������.



## 6) ������� (������������)

-   `/start` � �����������, ��������� ��������� � ���� � ����
    
-   `/menu` � ������� ������� ����
    
-   `/help` � ������� ������� �� �������������
    
-   `/veo` � ��������� **Veo 3** (��������� ������ �/��� ��������-��������)
    
-   `/luma` � ���������**Luma** (��������� ������ ��� �������������� ������ �����)
    
    

## 7) ������� (�������������)

-   `/admin` � ������� �����-����:
    
    -   **������� �����-����** (��������� �� �������� ����� �������);
        
    -   **������ �������� �����-�����**;
        
    -   **�������� ���� �������������**.
        



## �������������

���� ������ �������� ������� � ��������� :star: �� GitHub � ���������� �������.