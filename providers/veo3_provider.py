# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Optional

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

# НЕ допускаем слэши в имени файла, только буквы/цифры/._-
_SANITIZE_JOB_ID = re.compile(r"[^a-zA-Z0-9._-]+")
_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

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


class Veo3Provider(VideoProvider):
    name = Provider.VEO3

    def __init__(self) -> None:
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            log.warning("GEMINI_API_KEY is missing; Veo3 submissions will fail")
        self._api_key: Optional[str] = api_key or None

    def _ensure_key(self) -> str:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        return self._api_key

    # ------------ SUBMIT ------------
    async def create_job(self, params: GenerationParams) -> JobId:
        """
        Photo→video: SDK (google-genai) — синхронный вызов в отдельном треде.
        Text→video: REST (predictLongRunning) — БЕЗ передачи imageBytes (эта ветка не поддерживает).
        """
        api_key = self._ensure_key()

        base_prompt = (params.prompt or "").strip()
        ar = (params.aspect_ratio or "16:9").strip()
        strict_ar = bool(getattr(params, "strict_ar", False))

        # поддержка и params.fast, и params.fast_mode
        fast_flag = getattr(params, "fast", None)
        if fast_flag is None:
            fast_flag = getattr(params, "fast_mode", False)
        desired_resolution = getattr(params, "resolution", None) or "1080p"

        # --- собрать байты картинки, если есть (fallback по URL) ---
        image_bytes = getattr(params, "image_bytes", None)
        image_mime = getattr(params, "image_mime", None)
        if not image_bytes:
            ref_url = None
            if params.extras and isinstance(params.extras, dict):
                ref_url = params.extras.get("reference_url") or params.extras.get("reference_file_id")
            if isinstance(ref_url, str) and ref_url.startswith(("http://", "https://")):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                        rimg = await http.get(ref_url)
                        rimg.raise_for_status()
                        image_bytes = rimg.content
                    image_mime = (mimetypes.guess_type(ref_url)[0]) or "image/jpeg"
                except Exception as exc:
                    log.exception("Failed to fetch reference image: %s", exc)

        # --- выбор модели ---
        model_name = params.model or (
            "veo-3.0-fast-generate-001" if fast_flag else "veo-3.0-generate-001"
        )

        # --- утилиты ---
        def _map_ar(val: str) -> str:
            v = (val or "").strip()
            return v if v in {"16:9", "9:16"} else "16:9"

        def _map_resolution(res: Optional[str], aspect: str) -> str:
            """
            В Gemini API 1080p гарантирован для 16:9; при 9:16 используем 720p.
            Если пользователь просит 1080p при 9:16 — принудительно 720p (без ошибок).
            """
            ar_eff = _map_ar(aspect)
            if ar_eff == "9:16":
                return "720p"
            if not res:
                return "1080p"
            r = str(res).lower().rstrip("p")
            return "1080p" if r.startswith("1080") else "720p"

        anti_borders = (
            "no device frame, no smartphone frame, no UI mockup, "
            "no borders, no black bars, no letterboxing, no pillarboxing, "
            "edge-to-edge content, fill the entire frame"
        )

        def _strong_ar_prompt(prompt: str, aspect: str, user_neg: Optional[str]) -> str:
            # мягко усиливаем ориентацию и отсутствие рамок
            if aspect == "9:16":
                tail = f"(VERTICAL 9:16 FULL-FRAME, {anti_borders}, not landscape, not 16:9)"
            else:
                tail = f"(WIDESCREEN 16:9 FULL-FRAME, {anti_borders}, not vertical, not 9:16)"
            merged_neg = (user_neg.strip() + ", " + anti_borders) if user_neg else anti_borders
            body = prompt if prompt.endswith((".", "!", "?")) else prompt + "."
            return f"{body} {tail}. Avoid: {merged_neg}."

        extras = params.extras if isinstance(params.extras, dict) else {}
        negative_prompt = (getattr(params, "negative_prompt", None) or extras.get("negative_prompt") or None)
        duration_seconds = getattr(params, "duration_seconds", None)
        if duration_seconds is None:
            duration_seconds = extras.get("duration_seconds")
        seed = getattr(params, "seed", None)
        if seed is None:
            seed = extras.get("seed")

        # --------- ПУТЬ 1: есть картинка → SDK (google-genai) ---------
        if image_bytes:
            try:
                from google import genai
                from google.genai import types as genai_types
            except Exception as exc:
                raise RuntimeError(
                    "Photo→video через REST не поддерживается этой моделью. "
                    "Установи пакет 'google-genai' (pip install google-genai) для SDK."
                ) from exc

            client = genai.Client(api_key=api_key)

            gv_kwargs: dict[str, Any] = {
                "aspect_ratio": _map_ar(ar),
                "resolution": _map_resolution(desired_resolution, ar),
            }
            if negative_prompt:
                gv_kwargs["negative_prompt"] = negative_prompt
            if duration_seconds:
                gv_kwargs["duration_seconds"] = int(duration_seconds)
            if seed is not None:
                gv_kwargs["seed"] = int(seed)

            prompt_for_sdk = base_prompt if not strict_ar else _strong_ar_prompt(base_prompt, ar, negative_prompt)
            image_obj = {"image_bytes": image_bytes, "mime_type": image_mime or "image/jpeg"}

            # SDK синхронный — гоняем в отдельном треде
            operation = await asyncio.to_thread(
                client.models.generate_videos,
                model=model_name,
                prompt=prompt_for_sdk,
                image=image_obj,
                config=genai_types.GenerateVideosConfig(**gv_kwargs) if gv_kwargs else None,
            )
            op_name = getattr(operation, "name", None) or getattr(operation, "operation", None)
            if not op_name:
                raise RuntimeError("Veo3 SDK returned no operation name")
            log.info("Google Veo operation (SDK-sync) started: %s", op_name)
            return op_name

        # --------- ПУТЬ 2: текст → REST API (predictLongRunning) ---------
        # NB: Никаких imageBytes/медиа не отправляем в этой ветке.
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        url_lro = f"{_API_BASE}/models/{model_name}:predictLongRunning"

        # Правильная схема для REST: instances[] + parameters{}
        parameters: dict[str, Any] = {
            "aspectRatio": _map_ar(ar),
            "resolution": _map_resolution(desired_resolution, ar),
            # Ровным счётом это опционально, но явно разрешим генерацию людей в text→video,
            # чтобы снизить неожиданные блокировки (см. доку по Veo 3).
            "personGeneration": "allow_all",
        }
        if negative_prompt:
            parameters["negativePrompt"] = negative_prompt
        if duration_seconds:
            parameters["durationSeconds"] = int(duration_seconds)
        if seed is not None:
            parameters["seed"] = int(seed)

        prompt_text = _strong_ar_prompt(base_prompt, ar, negative_prompt) if strict_ar else base_prompt
        payload = {
            "instances": [
                {
                    "prompt": prompt_text,
                }
            ],
            "parameters": parameters,
        }

        # один-два ретрая на сабмит
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
                    r = await http.post(url_lro, headers=headers, json=payload)
                if r.status_code >= 400:
                    # распространённые ошибки схемы полезно подсветить в логе
                    if r.status_code == 400:
                        txt = (r.text or "").strip()
                        if "Unknown name" in txt and ("contents" in txt or "generationConfig" in txt):
                            log.error("REST 400: payload must use instances[] + parameters{}, not contents/generationConfig")
                        if "imageBytes" in txt:
                            log.error("REST 400: imageBytes isn't supported by this model via REST")
                            raise RuntimeError(
                                "Photo→video через REST не поддерживается (imageBytes). "
                                "Нужно использовать SDK (google-genai)."
                            )
                    if _is_transient_status(r.status_code) and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    log.error("Veo3 submit failed %s %s\nBody: %s", r.status_code, r.reason_phrase, r.text)
                    raise RuntimeError("Veo3 submission failed")
                data = r.json()
                op_name = data.get("name") or data.get("operation")
                if not op_name:
                    raise RuntimeError("Veo3 submission succeeded but no operation name")
                log.info("Google Veo operation started: %s", op_name)
                return op_name
            except _TRANSIENT_ERRORS as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Veo3 submission transport error: {exc}") from exc

        # теоретически недостижимо
        raise RuntimeError("Veo3 submission failed after retries")

    # ------------ POLL ------------
    async def poll(self, job_id: JobId) -> JobStatus:
        api_key = self._ensure_key()
        url = f"{_API_BASE}/{job_id}"
        headers = {"x-goog-api-key": api_key}

        # лёгкие ретраи, чтобы не срывать весь пайплайн из-за единичного сбоя
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    r = await http.get(url, headers=headers)
                if r.status_code >= 400:
                    if _is_transient_status(r.status_code) and attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    return JobStatus(status="failed", error=f"Veo3 poll failed ({r.status_code})")
                data = r.json()
                break
            except _TRANSIENT_ERRORS as exc:
                if attempt < 1:
                    await asyncio.sleep(1.0)
                    continue
                return JobStatus(status="pending", error=str(exc))
        else:
            return JobStatus(status="pending")

        done = bool(data.get("done"))
        metadata = data.get("metadata") or {}
        progress = self._extract_progress(metadata)

        if not done:
            return JobStatus(status="running" if progress else "pending", progress=progress)

        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            return JobStatus(status="failed", progress=progress, error=message or "generation failed")

        video_url = self._extract_video_uri(data)
        if not video_url:
            return JobStatus(status="failed", progress=progress, error="No video URL returned")

        return JobStatus(status="succeeded", progress=100, extra={"video_url": video_url})

    # ------------ DOWNLOAD ------------
    async def download(self, job_id: JobId) -> Path:
        status = await self.poll(job_id)
        video_url = (status.extra or {}).get("video_url")
        if status.status != "succeeded" or not video_url:
            raise RuntimeError("Veo3 download called before generation finished")

        # Берём только хвост operation id (последний сегмент) и санитизируем
        short_id = str(job_id).split("/")[-1]
        sanitized = _SANITIZE_JOB_ID.sub("_", short_id)
        target = Path.cwd() / f"veo3_{int(time.time())}_{sanitized}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)

        headers = {"x-goog-api-key": self._ensure_key()}

        # несколько попыток на скачивание, stream + tmp → rename (атомарнее, не едим память)
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(300.0),
                    follow_redirects=True,
                    headers=headers,
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
                raise RuntimeError("Veo3 download timed out") from exc
            except HTTPError as exc:
                # не-временные сетевые ошибки
                raise RuntimeError(f"Veo3 download failed: {exc}") from exc

        # теоретически недостижимо
        raise RuntimeError("Veo3 download failed after retries")

    # ------------ helpers ------------
    def _extract_progress(self, meta: dict[str, Any]) -> int:
        for k in ("progress", "progressPercent", "progress_percentage", "progress_percent"):
            v = meta.get(k)
            if isinstance(v, (int, float)):
                try:
                    return max(0, min(100, int(v)))
                except Exception:
                    pass
        # иногда в metadata кладут state: "PENDING"/"RUNNING"
        state = str(meta.get("state") or "").upper()
        if state == "RUNNING":
            return 1
        return 0

    def _extract_video_uri(self, data: dict[str, Any]) -> str | None:
        """
        Извлекаем ссылку на видео из ответа LRO:
        - response.generateVideoResponse.generatedSamples[0].video.uri | downloadUri
        - response.video / response.uri
        - response.resources[*].uri
        """
        resp = data.get("response") or {}

        # основной ожидаемый формат
        gvr = resp.get("generateVideoResponse") or {}
        samples = gvr.get("generatedSamples") or []
        if samples:
            video = (samples[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("downloadUri")
            if uri:
                return uri

        # упрощённые варианты
        uri = resp.get("uri") or resp.get("downloadUri")
        if uri:
            return uri

        video = resp.get("video") or {}
        if isinstance(video, dict):
            uri = video.get("uri") or video.get("downloadUri")
            if uri:
                return uri

        # иногда прилетает files API
        resources = resp.get("resources") or []
        for it in resources:
            if isinstance(it, dict):
                uri = it.get("uri") or it.get("downloadUri")
                if uri:
                    return uri

        return None


_default_provider = Veo3Provider()
