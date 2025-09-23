# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx
from httpx import (
    HTTPError,
    TimeoutException,
    RemoteProtocolError,
    ConnectError,
    ReadTimeout,
    ProxyError,
)

from config import settings
from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams

log = logging.getLogger("providers.veo3_provider")

# --- Polza.ai ---
POLZA_API_KEY = os.getenv("POLZA_API_KEY")
POLZA_BASE_URL = os.getenv("POLZA_BASE_URL", "https://api.polza.ai/api/v1")

# совместимость: если раньше был один POLZA_MODEL — используем как DEFAULT
POLZA_MODEL_DEFAULT = os.getenv("POLZA_MODEL_DEFAULT", os.getenv("POLZA_MODEL", "veo3-fast"))
POLZA_MODEL_FAST    = os.getenv("POLZA_MODEL_FAST", "veo3-fast")
POLZA_MODEL_QUALITY = os.getenv("POLZA_MODEL_QUALITY", "veo3")  # «качественный» проход

# НЕ допускаем слэши в имени файла, только буквы/цифры/._-
_SANITIZE_JOB_ID = re.compile(r"[^a-zA-Z0-9._-]+")

# сетевые ошибки, которые считаем временными и ретраим
_TRANSIENT_ERRORS = (
    TimeoutException,
    RemoteProtocolError,
    ConnectError,
    ReadTimeout,
    ProxyError,
)

def _is_transient_status(status_code: int) -> bool:
    # 5xx, 425/429/499 — временные
    return status_code >= 500 or status_code in (425, 429, 499)

# --- Глобальный мягкий троттлинг сабмитов (чтобы меньше ловить 429) ---
_submit_lock = asyncio.Lock()
_last_submit_ts: float = 0.0
_MIN_SUBMIT_GAP = float(getattr(settings, "GEMINI_MIN_SUBMIT_GAP_S", 0.7))  # сек (reuse из настроек)

async def _respect_submit_gap() -> None:
    """Гарантируем минимальный зазор между сабмитами в рамках процесса."""
    global _last_submit_ts
    async with _submit_lock:
        now = time.monotonic()
        wait = _MIN_SUBMIT_GAP - (now - _last_submit_ts)
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 0.2))
        _last_submit_ts = time.monotonic()

def _auth_headers() -> dict:
    if not POLZA_API_KEY:
        raise RuntimeError("POLZA_API_KEY is not set")
    return {
        "Authorization": f"Bearer {POLZA_API_KEY}",
        "Content-Type": "application/json",
    }

def _coalesce(*vals):
    for v in vals:
        if v not in (None, "", []):
            return v
    return None

def _is_http_url(u: Optional[str]) -> bool:
    return isinstance(u, str) and u.lower().startswith(("http://", "https://"))

def _map_ar(val: Optional[str]) -> str:
    v = (val or "").strip()
    return v if v in {"16:9", "9:16", "1:1"} else "16:9"

def _map_resolution(res: Optional[str]) -> str:
    if not res:
        return "720p"
    r = str(res).lower().rstrip("p")
    if r.startswith("1080"):
        return "1080p"
    if r.startswith("720"):
        return "720p"
    return "720p"

def _strong_ar_prompt(prompt: str, aspect: str, user_neg: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Усиливаем ориентацию и отсутствие рамок, добавляем негативы против текста/рамок.
    Возвращаем (prompt, negative_prompt).
    """
    anti_borders = (
        "no device frame, no smartphone frame, no UI mockup, "
        "no borders, no black bars, no letterboxing, no pillarboxing, "
        "edge-to-edge content, fill the entire frame"
    )
    if aspect == "9:16":
        tail = "portrait orientation, vertical video, full-frame composition, " + anti_borders
    elif aspect == "1:1":
        tail = "square composition, full-frame content, " + anti_borders
    else:
        tail = "landscape orientation, widescreen video, full-frame composition, " + anti_borders

    strict_neg = (
        "no text, no numbers, no captions, no subtitles, no titles, "
        "no logos, no watermarks, no stickers, no badges, no overlays, "
        "no ui, no hud, no icons, no timecodes, no corner icons, "
        "no frame counters, no lower-thirds, "
        f"{anti_borders}"
    )

    merged_neg = (user_neg.strip() + ", " + strict_neg) if (user_neg and str(user_neg).strip()) else strict_neg
    body = prompt if prompt.endswith((".", "!", "?")) else prompt + "."
    return f"{body} ({tail}).", merged_neg

def _build_polza_input(p: GenerationParams) -> dict:
    """
    Маппинг GenerationParams -> Polza input (внутренний объект input)
    """
    extras = p.extras if isinstance(p.extras, dict) else {}
    negative_prompt = _coalesce(getattr(p, "negative_prompt", None), extras.get("negative_prompt"))
    aspect = _map_ar(_coalesce(getattr(p, "aspect_ratio", None), getattr(p, "aspect", None)))
    strict_ar = bool(getattr(p, "strict_ar", False))

    # референс
    image_bytes = getattr(p, "image_bytes", None)
    image_mime = getattr(p, "image_mime", None)
    image_url = _coalesce(
        getattr(p, "image_url", None),
        getattr(p, "image", None),
        extras.get("reference_url"),
        # ВНИМАНИЕ: reference_file_id может быть просто file_id TG, он не HTTP — не отправляем его как URL.
        extras.get("reference_file_id"),
    )
    if not _is_http_url(image_url):
        image_url = None

    # промпт — не должен быть пустым, если есть референс
    base_prompt = (p.prompt or "").strip()
    has_ref = bool(image_url or (image_bytes and image_mime))
    if not base_prompt:
        if has_ref:
            base_prompt = "Animate this image realistically with native audio; keep the subject and style consistent."
        else:
            base_prompt = "Cinematic shot, natural motion and lighting."

    # усиление ориентации/негативов
    if strict_ar:
        base_prompt, negative_prompt = _strong_ar_prompt(base_prompt, aspect, negative_prompt)

    # duration/length
    length = _coalesce(getattr(p, "length", None), getattr(p, "duration", None))
    duration_seconds = _coalesce(getattr(p, "duration_seconds", None), extras.get("duration_seconds"))
    if not length and duration_seconds:
        try:
            length = int(duration_seconds)
        except Exception:
            length = None
    if not length:
        length = 8

    resolution = _map_resolution(_coalesce(getattr(p, "resolution", None), "720p"))
    # звук включён по умолчанию
    with_audio = bool(_coalesce(getattr(p, "with_audio", None), extras.get("with_audio"), True))
    seed = _coalesce(getattr(p, "seed", None), extras.get("seed"))

    payload_input: dict[str, Any] = {
        "prompt": base_prompt,            # всегда непустой
        "length": int(length),            # совместимость
        "duration": int(length),          # Polza использует duration
        "aspect_ratio": aspect,
        "aspectRatio": aspect,
        "resolution": resolution,
        "generate_audio": with_audio,
        "generateAudio": with_audio,
    }
    if negative_prompt:
        payload_input["negative_prompt"]  = negative_prompt
        payload_input["negativePrompt"]   = negative_prompt
    if seed is not None:
        try:
            payload_input["seed"] = int(seed)
        except Exception:
            pass

    if image_url:
        # несколько алиасов — совместимость c разными роутерами на стороне агрегатора
        payload_input["image"]       = image_url
        payload_input["image_url"]   = image_url
        payload_input["reference"]   = image_url
    elif image_bytes and image_mime:
        # при необходимости можно добавить поддержку base64:
        # payload_input["image_base64"] = base64.b64encode(image_bytes).decode()
        # payload_input["image_mime"] = image_mime
        pass

    # удалить None/пустые
    return {k: v for k, v in payload_input.items() if v not in (None, "", [])}

def _flatten_for_polza_top_level(inp: dict) -> dict:
    """
    Поля, которые Polza ждёт в корне запроса (иначе 400 'prompt is missing').
    """
    top = {
        "prompt":         inp.get("prompt"),
        "duration":       inp.get("duration") or inp.get("length"),
        "resolution":     inp.get("resolution"),
        "aspectRatio":    inp.get("aspectRatio") or inp.get("aspect_ratio"),
        "generateAudio":  inp.get("generateAudio") or inp.get("generate_audio"),
        "negativePrompt": inp.get("negativePrompt") or inp.get("negative_prompt"),
        "seed":           inp.get("seed"),
    }
    # алиасы для изображения и на верхнем уровне тоже
    img = inp.get("image") or inp.get("image_url") or inp.get("reference")
    if img:
        top["image"] = img
        top["image_url"] = img
        top["reference"] = img
    return {k: v for k, v in top.items() if v not in (None, "", [])}

def _extract_video_url(data: dict[str, Any]) -> Optional[str]:
    """
    Унифицированный парсинг ответа статуса на Polza:
    ищем url в output/result/videos[0].url, video.url, url, resources[*].url и т.п.
    """
    candidates = []

    # прямые поля
    for k in ("url", "downloadUrl", "download_uri", "downloadUri"):
        v = data.get(k)
        if isinstance(v, str):
            candidates.append(v)

    # вложенные структуры
    for parent_key in ("output", "result", "response"):
        parent = data.get(parent_key) or {}
        if isinstance(parent, dict):
            for k in ("url", "downloadUrl", "download_uri", "downloadUri"):
                v = parent.get(k)
                if isinstance(v, str):
                    candidates.append(v)
            video = parent.get("video") or {}
            if isinstance(video, dict):
                vv = _coalesce(video.get("url"), video.get("downloadUrl"), video.get("downloadUri"))
                if isinstance(vv, str):
                    candidates.append(vv)
            videos = parent.get("videos") or []
            if isinstance(videos, list) and videos:
                first = videos[0] or {}
                vv = _coalesce(first.get("url"), first.get("downloadUrl"), first.get("downloadUri"))
                if isinstance(vv, str):
                    candidates.append(vv)
            resources = parent.get("resources") or []
            if isinstance(resources, list):
                for it in resources:
                    if isinstance(it, dict):
                        vv = _coalesce(it.get("url"), it.get("downloadUrl"), it.get("downloadUri"))
                        if isinstance(vv, str):
                            candidates.append(vv)

    return candidates[0] if candidates else None

def _normalize_status(val: Any) -> str:
    s = str(val or "").lower()
    # распространённые варианты
    if s in ("queued", "pending", "processing", "running"):
        return "pending"
    if s in ("succeed", "completed", "success", "done"):
        return "succeeded"
    if s in ("failed", "error", "canceled", "cancelled"):
        return "failed"
    return s or "pending"

def _pick_model(p: GenerationParams) -> str:
    """
    Выбор модели по флагам проекта:
    - fast / fast_mode → POLZA_MODEL_FAST
    - hq / quality / quality_mode → POLZA_MODEL_QUALITY
    - явное p.model имеет приоритет
    - иначе POLZA_MODEL_DEFAULT
    """
    if getattr(p, "model", None):
        return str(p.model)
    if any(bool(getattr(p, f, False)) for f in ("quality", "hq", "quality_mode")):
        return POLZA_MODEL_QUALITY
    if any(bool(getattr(p, f, False)) for f in ("fast", "fast_mode")):
        return POLZA_MODEL_FAST
    return POLZA_MODEL_DEFAULT

class Veo3Provider(VideoProvider):
    """
    Drop-in адаптация под Polza.ai.
    Сохраняем имя класса и интерфейс, чтобы остальной проект не менять.
    """
    name = Provider.VEO3

    def __init__(self) -> None:
        if not POLZA_API_KEY:
            log.warning("POLZA_API_KEY is not set; submissions will fail")
        # карта: job_id -> (последний известный статус, видео-URL)
        self._jobs_cache: dict[str, dict] = {}

    # ------------ SUBMIT (Polza) ------------
    async def create_job(self, params: GenerationParams) -> JobId:
        """
        POST /api/v1/videos/generations
        body: { model: str, prompt: str, duration: int, ... , input: {...} }
        resp: { id | requestId | taskId, ... }
        """
        inp = _build_polza_input(params)
        payload = {
            "model": _pick_model(params),
            **_flatten_for_polza_top_level(inp),  # <-- дублируем ключевые поля в корень
            "input": inp,                          # и оставляем nested-форму (совместимость)
        }

        headers = _auth_headers()
        await _respect_submit_gap()

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
            r = await http.post(f"{POLZA_BASE_URL}/videos/generations", headers=headers, json=payload)

        if r.status_code == 402:
            # дружелюбная ошибка «недостаточно средств»
            try:
                err = r.json().get("error", {})
                msg = err.get("message") or "Insufficient balance"
                code = err.get("code") or "INSUFFICIENT_BALANCE"
            except Exception:
                msg, code = "Insufficient balance", "INSUFFICIENT_BALANCE"
            raise RuntimeError(f"{code}: {msg}")

        if r.status_code >= 400:
            log.error("Polza submit failed %s %s\nBody: %s", r.status_code, r.reason_phrase, r.text)
            raise RuntimeError(f"Polza submission failed ({r.status_code})")

        data = r.json()
        job_id = (
            data.get("id")
            or data.get("requestId")
            or data.get("request_id")
            or data.get("taskId")
            or data.get("task_id")
        )
        if not job_id:
            raise RuntimeError(f"Polza: cannot find job id in response: {data}")

        # небольшое кэширование
        self._jobs_cache[str(job_id)] = {"status": "pending", "video_url": None}
        log.info("Polza Veo submit ok: job_id=%s", job_id)
        return job_id

    # ------------ POLL (Polza) ------------
    async def poll(self, job_id: JobId) -> JobStatus:
        """
        GET /api/v1/videos/{id}
        ожидаем { status, output: { url }, ... }
        """
        headers = _auth_headers()

        # 2 попытки на временные сетевые
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    r = await http.get(f"{POLZA_BASE_URL}/videos/{job_id}", headers=headers)
                if r.status_code >= 400:
                    if _is_transient_status(r.status_code) and attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    return JobStatus(status="failed", error=f"Polza status failed ({r.status_code})")

                data = r.json()
                status_raw = data.get("status") or data.get("state")
                status = _normalize_status(status_raw)

                if status == "pending":
                    # Иногда приходит прогресс числом/процентом
                    progress = 0
                    for k in ("progress", "progressPercent", "progress_percentage", "progress_percent"):
                        v = data.get(k) or (data.get("metadata", {}) or {}).get(k)
                        if isinstance(v, (int, float)):
                            try:
                                progress = max(0, min(100, int(v)))
                                break
                            except Exception:
                                pass
                    return JobStatus(status="pending" if progress == 0 else "running", progress=progress)

                if status == "failed":
                    err = _coalesce(
                        data.get("error"),
                        (data.get("output") or {}).get("error"),
                        (data.get("result") or {}).get("error"),
                    )
                    return JobStatus(status="failed", error=str(err) if err else "generation failed")

                # succeeded
                video_url = _extract_video_url(data) or _extract_video_url(data.get("output") or {})
                if not video_url:
                    # иногда ссылка прилетает чуть позже статуса "succeed" — вернём running без URL
                    return JobStatus(status="running", progress=95)
                # кэш
                self._jobs_cache[str(job_id)] = {"status": "succeeded", "video_url": video_url}
                return JobStatus(status="succeeded", progress=100, extra={"video_url": video_url})

            except _TRANSIENT_ERRORS:
                if attempt < 1:
                    await asyncio.sleep(1.0)
                    continue
                return JobStatus(status="pending")

        return JobStatus(status="pending")

    # ------------ DOWNLOAD (Polza) ------------
    async def download(self, job_id: JobId) -> Path:
        """
        Скачиваем готовое видео по URL из статуса.
        """
        status = await self.poll(job_id)
        video_url = (status.extra or {}).get("video_url")
        if status.status != "succeeded" or not video_url:
            raise RuntimeError("download called before generation finished or without URL")

        # Берём только хвост id и санитизируем
        short_id = str(job_id).split("/")[-1]
        sanitized = _SANITIZE_JOB_ID.sub("_", short_id)
        target = Path.cwd() / f"veo3_{int(time.time())}_{sanitized}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)

        # несколько попыток на скачивание, stream + tmp → rename
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(300.0),
                    follow_redirects=True,
                ) as http:
                    async with http.stream("GET", video_url) as resp:
                        if resp.status_code >= 400:
                            if _is_transient_status(resp.status_code) and attempt < 2:
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            resp.raise_for_status()
                        tmp = target.with_suffix(".tmp")
                        with tmp.open("wb") as f:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                if chunk:
                                    f.write(chunk)
                        tmp.replace(target)
                        return target
            except _TRANSIENT_ERRORS as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError("download timed out") from exc
            except HTTPError as exc:
                raise RuntimeError(f"download failed: {exc}") from exc

        raise RuntimeError("download failed after retries")


_default_provider = Veo3Provider()
