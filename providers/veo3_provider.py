# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from httpx import TimeoutException

from config import settings
from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams

log = logging.getLogger("providers.veo3_provider")
_SANITIZE_JOB_ID = re.compile(r"[^a-zA-Z0-9_-]+")
_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


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

    # ------------ SUBMIT (REST) ------------
    async def create_job(self, params: GenerationParams) -> JobId:
        """
        Отправляем минимальный запрос ТОЛЬКО с prompt, как в методичке.
        Сначала пробуем :predictLongRunning, при INVALID_ARGUMENT пробуем :generateVideo.
        """
        api_key = self._ensure_key()
        model_name = params.model or (
            "veo-3.0-fast-generate-001" if params.fast_mode else "veo-3.0-generate-001"
        )
        qparams = {"key": api_key}
        payload = {
            "instances": [
                {
                    "prompt": params.prompt
                }
            ]
        }
        headers = {"Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
            # 1) predictLongRunning
            url_lro = f"{_API_BASE}/models/{model_name}:predictLongRunning"
            r = await http.post(url_lro, params=qparams, headers=headers, json=payload)
            if r.status_code >= 400:
                # Попробуем понять причину
                body_text = r.text
                try:
                    body_json = r.json()
                except Exception:
                    body_json = None
                log.error("Veo3 submit failed %s %s\nBody: %s", r.status_code, r.reason_phrase, body_text)

                # 2) generateVideo как фолбэк при INVALID_ARGUMENT и подобном
                status = ((body_json or {}).get("error") or {}).get("status")
                if status == "INVALID_ARGUMENT":
                    url_gen = f"{_API_BASE}/models/{model_name}:generateVideo"
                    r2 = await http.post(url_gen, params=qparams, headers=headers, json=payload)
                    if r2.status_code >= 400:
                        log.error("Veo3 submit (fallback generateVideo) failed %s %s\nBody: %s",
                                  r2.status_code, r2.reason_phrase, r2.text)
                        raise RuntimeError("Veo3 submission failed")
                    data2 = r2.json()
                    op_name2 = data2.get("name")
                    if not op_name2:
                        log.error("Veo3 submit fallback ok but no operation name. Body: %s", data2)
                        raise RuntimeError("Veo3 submission succeeded but no operation name returned")
                    log.info("Google Veo operation started (fallback): %s", op_name2)
                    return op_name2

                # Если не INVALID_ARGUMENT — сразу ошибка
                raise RuntimeError("Veo3 submission failed")

            data = r.json()
            op_name = data.get("name")
            if not op_name:
                log.error("Veo3 submit ok but no operation name. Body: %s", data)
                raise RuntimeError("Veo3 submission succeeded but no operation name returned")
            log.info("Google Veo operation started: %s", op_name)
            return op_name

    async def create_video(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        resolution: int,
        negative_prompt: str | None = None,
        fast: bool = False,
        reference_file_id: str | None = None,
    ) -> JobId:
        # Параметры aspect/negative/resolution REST не используем (недокументированы).
        # Оставляем для совместимости сигнатуры.
        params = GenerationParams(
            prompt=prompt,
            provider=Provider.VEO3,
            aspect_ratio=aspect_ratio,
            resolution=f"{resolution}p",
            negative_prompt=negative_prompt,
            fast_mode=fast,
            extras={"reference_file_id": reference_file_id} if reference_file_id else None,
        )
        return await self.create_job(params)

    # ------------ POLL (REST operations/<id>) ------------
    async def poll(self, job_id: JobId) -> JobStatus:
        api_key = self._ensure_key()
        url = f"{_API_BASE}/{job_id}"  # job_id вида "operations/...."
        qparams = {"key": api_key}

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            r = await http.get(url, params=qparams)
            if r.status_code >= 400:
                log.error("Veo3 poll failed %s %s\nBody: %s", r.status_code, r.reason_phrase, r.text)
                return JobStatus(status="failed", error="Veo3 poll failed")

            data = r.json()

        done = bool(data.get("done"))
        metadata = data.get("metadata") or {}
        progress = self._extract_progress(metadata)

        if not done:
            return JobStatus(status="running" if progress else "pending", progress=progress, extra={"operation": data})

        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            return JobStatus(status="failed", progress=progress, error=message, extra={"operation": data})

        video_url = self._extract_video_uri(data)
        if not video_url:
            return JobStatus(status="failed", progress=progress, error="No video URL returned", extra={"operation": data})

        return JobStatus(status="succeeded", progress=100, extra={"operation": data, "video_url": video_url})

    # ------------ DOWNLOAD (HTTP GET uri) ------------
    async def download(self, job_id: JobId) -> Path:
        status = await self.poll(job_id)
        video_url = (status.extra or {}).get("video_url") if status.extra else None
        if status.status != "succeeded" or not video_url:
            raise RuntimeError("Veo3 download called before generation finished")

        sanitized = _SANITIZE_JOB_ID.sub("_", job_id)
        target = Path.cwd() / f"veo3_{int(time.time())}_{sanitized}.mp4"
        # иногда ссылка закрыта — оставим заголовок с ключом на всякий
        headers = {"x-goog-api-key": self._ensure_key()}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0), follow_redirects=True, headers=headers) as http:
                r = await http.get(video_url)
                r.raise_for_status()
        except TimeoutException as exc:
            log.exception("Veo3 download timeout: %s", exc)
            raise RuntimeError("Veo3 download timed out") from exc
        except httpx.HTTPError as exc:
            log.exception("Veo3 download HTTP error: %s | Body: %s", exc, getattr(exc.response, "text", ""))
            raise RuntimeError("Veo3 download failed") from exc

        target.write_bytes(r.content)
        return target

    # ------------ helpers ------------
    def _extract_progress(self, meta: dict[str, Any]) -> int:
        for k in ("progress", "progress_percent", "progressPercent", "progress_percentage"):
            v = meta.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    def _extract_video_uri(self, data: dict[str, Any]) -> str | None:
        resp = data.get("response") or {}
        # Вариант из доки (Veo3 via Gemini):
        gvr = resp.get("generateVideoResponse") or {}
        samples = gvr.get("generatedSamples") or []
        if isinstance(samples, list) and samples:
            video = (samples[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("downloadUri")
            if uri:
                return uri
        # Альтернативные ответы некоторых ревизий:
        gv = resp.get("generated_videos")
        if isinstance(gv, list) and gv:
            video = (gv[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("download_uri")
            if uri:
                return uri
        return None


_default_provider = Veo3Provider()


async def create_job(
    prompt: str,
    aspect_ratio: str,
    resolution: int | str,
    *,
    fast: bool = False,
    negative_prompt: str | None = None,
    reference_file_id: str | None = None,
) -> JobId:
    resolution_int = int(str(resolution).rstrip("p"))
    return await _default_provider.create_video(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution_int,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
    )


async def poll(job_id: JobId) -> JobStatus:
    return await _default_provider.poll(job_id)


async def download(job_id: JobId) -> Path:
    return await _default_provider.download(job_id)