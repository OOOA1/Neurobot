# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp

from config import settings
from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams

log = logging.getLogger(__name__)

# допустимые идентификаторы моделей Luma (обновите при необходимости)
_ALLOWED_MODELS = {
    "ray-2",
    "dream-machine-1",
    "dream-machine-1.5",
}
_DEFAULT_MODEL = "ray-2"


class LumaProvider(VideoProvider):
    """Video generation provider backed by Luma Dream Machine."""

    name = Provider.LUMA

    def __init__(self) -> None:
        self._base_url = "https://api.lumalabs.ai/dream-machine/v1"
        self._api_key = settings.LUMA_API_KEY
        if not self._api_key:
            log.warning("LUMA_API_KEY is not configured; provider will fail on submit")

    async def create_job(self, params: GenerationParams) -> JobId:
        """Submit a new Dream Machine job and return provider identifier."""
        model = (params.model or "").strip().lower()
        if model not in _ALLOWED_MODELS:
            model = _DEFAULT_MODEL

        payload: dict[str, Any] = {
            "prompt": params.prompt,
            "model": model,
        }
        if params.aspect_ratio:
            payload["aspect_ratio"] = params.aspect_ratio

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                f"{self._base_url}/generations",
                headers=self._headers_json,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    log.error("Luma create_job failed %s: %s", resp.status, text)
                    raise RuntimeError(f"Luma submit failed with status {resp.status}")
                data = await self._safe_json(resp, text)

        job_id = data.get("id") or (data.get("generation") or {}).get("id")
        if not job_id:
            raise RuntimeError("Luma submit succeeded but no job id returned")
        return job_id

    async def poll(self, job_id: JobId) -> JobStatus:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                f"{self._base_url}/generations/{job_id}",
                headers=self._headers_get,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    log.error("Luma poll failed %s: %s", resp.status, text)
                    raise RuntimeError(f"Luma poll failed with status {resp.status}")
                data = await self._safe_json(resp, text)

        state = data.get("state") or "pending"
        video_url = (data.get("assets") or {}).get("video")
        mapped_status = self._map_state(state)
        progress = 100 if mapped_status == "succeeded" and video_url else 0
        extra = {"video_url": video_url, "state": state}
        return JobStatus(status=mapped_status, progress=progress, extra=extra)

    async def download(self, job_id: JobId) -> Path:
        """Скачать готовое видео в кросс-платформенную temp-папку и вернуть путь."""
        status = await self.poll(job_id)
        video_url = (status.extra or {}).get("video_url") if status.extra else None
        if not video_url:
            raise RuntimeError("Luma download requested before video is ready")

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode(errors="ignore") if body else ""
                    log.error("Luma download failed %s: %s", resp.status, text)
                    raise RuntimeError(f"Luma download failed with status {resp.status}")

        # Кросс-платформенный путь (Windows/Linux/macOS)
        safe_job = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(job_id))
        tmpdir = Path(tempfile.gettempdir()) / "luma_cache"
        tmpdir.mkdir(parents=True, exist_ok=True)

        output_path = tmpdir / f"luma_{int(time.time())}_{safe_job}.mp4"
        output_path.write_bytes(body)
        return output_path

    def _map_state(self, state: str) -> str:
        lowered = (state or "").lower()
        if lowered in {"pending", "queued", "starting"}:
            return "pending"
        if lowered in {"dreaming", "processing", "running", "generating"}:
            return "running"
        if lowered in {"completed", "succeeded", "success"}:
            return "succeeded"
        if lowered in {"failed", "error", "cancelled"}:
            return "failed"
        return "pending"

    @property
    def _headers_json(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @property
    def _headers_get(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    async def _safe_json(self, resp: aiohttp.ClientResponse, text: str) -> dict[str, Any]:
        try:
            return await resp.json()
        except Exception as exc:
            log.error("Luma response non-json: %s", text)
            raise RuntimeError("Luma returned invalid JSON") from exc
