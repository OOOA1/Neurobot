# -*- coding: utf-8 -*-
# services/providers/veo.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"

# Модели по умолчанию (можно переопределить через .env VEO_MODEL_NAME)
DEFAULT_MODEL_QUALITY = "veo-3.0-generate-001"
DEFAULT_MODEL_FAST = "veo-3.0-fast-generate-001"


def _api_key() -> str:
    """
    Единая точка получения ключа: сначала Settings.GEMINI_API_KEY,
    затем переменная окружения GOOGLE_API_KEY.
    """
    key = getattr(settings, "GEMINI_API_KEY", "") or ""
    if not key:
        # на всякий случай — SDK/Google тоже часто читает GOOGLE_API_KEY
        import os

        key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise RuntimeError("Не задан GEMINI_API_KEY/GOOGLE_API_KEY для Google Veo")
    return key


def _choose_model(speed: str) -> str:
    """
    Выбор модели с учётом режима speed и возможной переопределённой переменной.
    Если пользователь явно указал VEO_MODEL_NAME — уважаем её.
    Иначе выбираем fast/quality дефолты.
    """
    custom = getattr(settings, "VEO_MODEL_NAME", "") or ""
    if custom:
        return custom
    s = (speed or "").lower()
    return DEFAULT_MODEL_FAST if s == "fast" else DEFAULT_MODEL_QUALITY


def _aspect(a: str) -> str:
    """
    Поддерживаем только 16:9 и 9:16.
    Всё остальное мягко сводим к 16:9.
    """
    a = (a or "").strip()
    if a in ("16:9", "9:16"):
        return a
    return "16:9"


_ANTI_BORDERS = (
    "no device frame, no smartphone frame, no UI mockup, "
    "no borders, no black bars, no letterboxing, no pillarboxing, "
    "edge-to-edge content, fill the entire frame"
)


def _strong_ar_prompt(prompt: str, aspect: str) -> str:
    """
    Мягко усиливаем ориентацию и отсутствие рамок прямо в тексте промпта.
    Это помогает модели возвращать кадр без «полей» уже на генерации.
    """
    base = (prompt or "").strip()
    if not base.endswith((".", "!", "?")):
        base += "."
    if aspect == "9:16":
        tail = f"(VERTICAL 9:16 FULL-FRAME, {_ANTI_BORDERS}, not landscape, not 16:9)."
    else:
        tail = f"(WIDESCREEN 16:9 FULL-FRAME, {_ANTI_BORDERS}, not vertical, not 9:16)."
    # Можно дополнительно перечислить «Avoid», но без пользовательского negative_prompt держим кратко
    return f"{base} {tail}"


async def _post(session: aiohttp.ClientSession, url: str, payload: dict, api_key: str) -> dict:
    async with session.post(
        url,
        json=payload,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
    ) as r:
        text = await r.text()
        if r.status >= 400:
            logger.error("Veo(Google) POST %s failed %s: %s", url, r.status, text)
            raise ValueError(f"Veo(Google) POST {url} failed {r.status}: {text}")
        return await r.json()


async def _get(session: aiohttp.ClientSession, url: str, api_key: str) -> dict:
    async with session.get(url, headers={"x-goog-api-key": api_key}) as r:
        text = await r.text()
        if r.status >= 400:
            logger.error("Veo(Google) GET %s failed %s: %s", url, r.status, text)
            raise ValueError(f"Veo(Google) GET {url} failed {r.status}: {text}")
        return await r.json()


def _extract_video_uri(lro_response: dict) -> Optional[str]:
    """
    Достаём ссылку на видео из ответа LRO с разными вариантами расположения.
    """
    resp = (lro_response or {}).get("response") or {}

    # Основной ожидаемый формат
    gvr = resp.get("generateVideoResponse") or {}
    samples = gvr.get("generatedSamples") or []
    if samples:
        video = (samples[0] or {}).get("video") or {}
        uri = video.get("uri") or video.get("downloadUri")
        if uri:
            return uri

    # Упрощённые варианты
    uri = resp.get("uri") or resp.get("downloadUri")
    if uri:
        return uri

    video = resp.get("video") or {}
    if isinstance(video, dict):
        uri = video.get("uri") or video.get("downloadUri")
        if uri:
            return uri

    # Иногда прилетает files API
    resources = resp.get("resources") or []
    for it in resources:
        if isinstance(it, dict):
            uri = it.get("uri") or it.get("downloadUri")
            if uri:
                return uri

    return None


async def submit(prompt: str, aspect: str, speed: str) -> dict[str, Any]:
    """
    Запускает генерацию видео Veo (Gemini API).
    Возвращает {"job_id": <operation_name>}
    """
    api_key = _api_key()
    model = _choose_model(speed)
    url = f"{BASE}/models/{model}:predictLongRunning"

    # Усиливаем ориентацию/безрамочность в тексте промпта (strict-ish AR).
    prompt_text = _strong_ar_prompt(prompt, _aspect(aspect))

    # Параметры кладём в config — REST читает их из instances[0].
    # По умолчанию целимся в 1080p (лучше для Telegram/HQ).
    payload = {
        "instances": [
            {
                "prompt": prompt_text,
                "config": {
                    "aspectRatio": _aspect(aspect),
                    "resolution": "1080p",
                    # При необходимости можно добавить:
                    # "negativePrompt": "...",
                    # "durationSeconds": 8,
                    # "seed": 123,
                },
            }
        ]
    }

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _post(session, url, payload, api_key)
        op_name = data.get("name") or data.get("operation")
        if not op_name:
            raise ValueError(f"Не удалось получить имя операции из ответа: {data}")
        logger.info("Google Veo operation started: %s", op_name)
        return {"job_id": op_name}


async def poll(job_id: str) -> dict[str, Any]:
    """
    Опрос долгой операции. Возвращает
      {"status": in_progress|completed|failed, "file_id": url|None}
    """
    api_key = _api_key()
    url = f"{BASE}/{job_id}"  # job_id приходит как 'operations/...'

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(session, url, api_key)

    if not data.get("done"):
        return {"status": "in_progress", "file_id": None}

    error = data.get("error")
    if error:
        # прокинем сообщение об ошибке в логи
        logger.error("Veo operation error: %s", error)
        return {"status": "failed", "file_id": None}

    video_url = _extract_video_uri(data)
    return {"status": "completed", "file_id": video_url}
