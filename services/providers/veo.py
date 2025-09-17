# services/providers/veo.py
import aiohttp
import logging
from typing import Any
from config import settings

logger = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"

# по умолчанию стабильная модель; можно переключить в .env через VEO_MODEL_NAME
DEFAULT_MODEL = "veo-3.0-generate-001"  # или "veo-3.0-fast-generate-001"


def _aspect(a: str) -> str:
    a = (a or "").strip()
    mapping = {"1:1": "1:1", "9:16": "9:16", "16:9": "16:9"}
    return mapping.get(a, "16:9")


async def _post(session: aiohttp.ClientSession, url: str, payload: dict, api_key: str) -> dict:
    async with session.post(
        url, json=payload, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}
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


async def submit(prompt: str, aspect: str, speed: str) -> dict[str, Any]:
    """
    Запускает генерацию видео Veo (Gemini API).
    Возвращает {"job_id": <operation_name>}
    """
    api_key = settings.VEO_API_KEY or getattr(settings, "GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("Не задан GOOGLE_API_KEY / VEO_API_KEY для Google Veo")

    model = getattr(settings, "VEO_MODEL_NAME", DEFAULT_MODEL) or DEFAULT_MODEL
    url = f"{BASE}/models/{model}:predictLongRunning"

    # Параметры можно класть в config — SDK делает именно так; в REST они читаются из instances[0]
    payload = {
        "instances": [
            {
                "prompt": prompt,
                "config": {
                    "aspectRatio": _aspect(aspect),
                    # при желании: "resolution": "720p" | "1080p", "negativePrompt": "...", "seed": 123
                },
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        data = await _post(session, url, payload, api_key)
        op_name = data.get("name")
        if not op_name:
            raise ValueError(f"Не удалось получить имя операции из ответа: {data}")
        logger.info("Google Veo operation started: %s", op_name)
        return {"job_id": op_name}


async def poll(job_id: str) -> dict[str, Any]:
    """
    Опрос долгой операции. Возвращает {"status": in_progress|completed|failed, "file_id": url|None}
    """
    api_key = settings.VEO_API_KEY or getattr(settings, "GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("Не задан GOOGLE_API_KEY / VEO_API_KEY для Google Veo")

    url = f"{BASE}/{job_id}"  # job_id приходит как 'operations/…'

    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, api_key)

    if not data.get("done"):
        return {"status": "in_progress", "file_id": None}

    # По гайду URL лежит тут:
    # response.generateVideoResponse.generatedSamples[0].video.uri
    resp = data.get("response") or {}
    gvr = (resp.get("generateVideoResponse") or {})
    samples = gvr.get("generatedSamples") or []
    video_url = None
    if samples and isinstance(samples, list):
        first = samples[0] or {}
        video = first.get("video") or {}
        video_url = video.get("uri")

    return {"status": "completed", "file_id": video_url}
