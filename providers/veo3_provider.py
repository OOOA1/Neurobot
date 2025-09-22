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
        api_key = settings.GEMINI_API_KEY
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
        Photo→video: через SDK (google-genai), sync в отдельном треде.
        Text→video: через REST API.
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
            # extras может содержать reference_file_id как URL (в твоём пайплайне ты его иногда передаёшь)
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

        def _map_resolution(res: Optional[str]) -> str:
            if not res:
                return "1080p"
            r = str(res).lower().rstrip("p")
            return "720p" if r.startswith("720") else "1080p"

        anti_borders = (
            "no device frame, no smartphone frame, no UI mockup, "
            "no borders, no black bars, no letterboxing, no pillarboxing, "
            "edge-to-edge content, fill the entire frame"
        )

        def _strong_ar_prompt(prompt: str, aspect: str, user_neg: Optional[str]) -> str:
            # добавляем мягкие ограничения на ориентацию и отсутствие рамок
            if aspect == "9:16":
                tail = f"(VERTICAL 9:16 FULL-FRAME, {anti_borders}, not landscape, not 16:9)"
            else:
                tail = f"(WIDESCREEN 16:9 FULL-FRAME, {anti_borders}, not vertical, not 9:16)"
            merged_neg = (user_neg.strip() + ", " + anti_borders) if user_neg else anti_borders
            # избегаем двойной точки
            body = prompt if prompt.endswith((".", "!", "?")) else prompt + "."
            return f"{body} {tail}. Avoid: {merged_neg}."

        negative_prompt = (getattr(params, "negative_prompt", None) or None)
        duration_seconds = getattr(params, "duration_seconds", None)
        seed = getattr(params, "seed", None)

        # --------- ПУТЬ 1: есть картинка → SDK (google-genai) ---------
        if image_bytes:
            try:
                from google import genai
            except Exception as exc:
                raise RuntimeError(
                    "Photo→video через REST не поддерживается. "
                    "Установи 'google-genai' (pip install google-genai)."
                ) from exc

            client = genai.Client(api_key=api_key)

            gv_kwargs: dict[str, Any] = {
                "aspect_ratio": _map_ar(ar),
                "resolution": _map_resolution(desired_resolution),
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
                config=gv_kwargs or None,
            )
            op_name = getattr(operation, "name", None) or getattr(operation, "operation", None)
            if not op_name:
                raise RuntimeError("Veo3 SDK returned no operation name")
            log.info("Google Veo operation (SDK-sync) started: %s", op_name)
            return op_name

        # --------- ПУТЬ 2: текст → REST API (predictLongRunning) ---------
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        url_lro = f"{_API_BASE}/models/{model_name}:predictLongRunning"

        config: dict[str, Any] = {
            "aspectRatio": _map_ar(ar),
            "resolution": _map_resolution(desired_resolution),
        }
        if negative_prompt:
            config["negativePrompt"] = negative_prompt
        if duration_seconds:
            config["durationSeconds"] = int(duration_seconds)
        if seed is not None:
            config["seed"] = int(seed)

        prompt_text = _strong_ar_prompt(base_prompt, ar, negative_prompt) if strict_ar else base_prompt
        payload_cfg = {
            "instances": [
                {
                    "prompt": prompt_text,
                    "config": config,
                }
            ]
        }

        # один-два ретрая на сабмит
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
                    r = await http.post(url_lro, headers=headers, json=payload_cfg)
                if r.status_code >= 400:
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
        # На всякий случай создадим родительскую папку (обычно это CWD)
        target.parent.mkdir(parents=True, exist_ok=True)

        headers = {"x-goog-api-key": self._ensure_key()}

        # несколько попыток на скачивание, с редиректами
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(180.0),
                    follow_redirects=True,
                    headers=headers,
                ) as http:
                    r = await http.get(video_url)
                    if r.status_code >= 400:
                        if _is_transient_status(r.status_code) and attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        r.raise_for_status()
                    # пишем атомарнее (через tmp), чтобы не оставлять битые файлы
                    tmp = target.with_suffix(".tmp")
                    tmp.write_bytes(r.content)
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
        - иногда бывает response.video or response.uri
        - в редких случаях — files API url в response.resources
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
