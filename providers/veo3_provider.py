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
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from httpx import TimeoutException

from config import settings
from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams

log = logging.getLogger("providers.veo3_provider")
_SANITIZE_JOB_ID = re.compile(r"[^a-zA-Z0-9_-]+")


class Veo3Provider(VideoProvider):
    name = Provider.VEO3

    def __init__(self) -> None:
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            log.warning("GEMINI_API_KEY is missing; Veo3 submissions will fail")
            self._client: Optional[genai.Client] = None
        else:
            self._client = genai.Client(api_key=api_key)

    def _ensure_client(self) -> genai.Client:
        if not self._client:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        return self._client

    async def create_job(self, params: GenerationParams) -> JobId:
        client = self._ensure_client()
        model_name = params.model or (
            "veo-3.0-fast-generate-001" if params.fast_mode else "veo-3.0-generate-001"
        )
        cfg = types.GenerateVideosConfig(
            aspect_ratio=params.aspect_ratio or "16:9",
            resolution=params.resolution or "1080p",
            negative_prompt=(params.negative_prompt or None),
        )
        async def _submit(model: str):
            return await asyncio.to_thread(
                client.models.generate_videos,
                model=model,
                prompt=params.prompt,
                config=cfg,
            )
        attempts = [model_name]
        if model_name != "veo-3.0-fast-generate-001":
            attempts.append("veo-3.0-fast-generate-001")
        last_exc: Exception | None = None
        for i, mdl in enumerate(attempts):
            try:
                if i > 0:
                    await asyncio.sleep(3 * i)
                op = await _submit(mdl)
                name = getattr(op, "name", None) or getattr(op, "id", None)
                if not name:
                    data = self._operation_to_dict(op)
                    name = data.get("name") or data.get("id")
                if not name:
                    raise RuntimeError("Veo3 submission succeeded but no operation name returned")
                return name
            except ClientError as ce:
                if self._is_quota(ce) and i + 1 < len(attempts):
                    log.warning("Veo3 quota hit on %s, retrying...", mdl)
                    last_exc = ce
                    continue
                log.exception("Veo3 create_job failed: %s", ce)
                raise RuntimeError("Veo3 submission failed") from ce
            except Exception as exc:
                log.exception("Veo3 create_job failed: %s", exc)
                last_exc = exc
                break
        raise RuntimeError("Veo3 submission failed") from last_exc

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

    async def poll(self, job_id: JobId) -> JobStatus:
        client = self._ensure_client()
        try:
            op = await asyncio.to_thread(client.operations.get, name=str(job_id))
        except TypeError:
            op = await asyncio.to_thread(client.operations.get, str(job_id))
        except Exception as exc:
            log.exception("Veo3 poll failed: %s", exc)
            return JobStatus(status="failed", error="Veo3 poll failed")
        data = self._operation_to_dict(op)
        done = data.get("done", False)
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

    async def download(self, job_id: JobId) -> Path:
        status = await self.poll(job_id)
        video_url = (status.extra or {}).get("video_url") if status.extra else None
        if status.status != "succeeded" or not video_url:
            raise RuntimeError("Veo3 download called before generation finished")
        sanitized = _SANITIZE_JOB_ID.sub("_", job_id)
        target = Path.cwd() / f"veo3_{int(time.time())}_{sanitized}.mp4"
        headers = {"x-goog-api-key": settings.GEMINI_API_KEY} if settings.GEMINI_API_KEY else {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0), follow_redirects=True, headers=headers) as http:
                r = await http.get(video_url)
                r.raise_for_status()
        except TimeoutException as exc:
            log.exception("Veo3 download timeout: %s", exc)
            raise RuntimeError("Veo3 download timed out") from exc
        except httpx.HTTPError as exc:
            log.exception("Veo3 download HTTP error: %s", exc)
            raise RuntimeError("Veo3 download failed") from exc
        target.write_bytes(r.content)
        return target

    def _is_quota(self, ce: ClientError) -> bool:
        try:
            j = ce.response_json
        except Exception:
            return False
        return (j or {}).get("error", {}).get("status") == "RESOURCE_EXHAUSTED"

    def _operation_to_dict(self, op: Any) -> dict[str, Any]:
        if isinstance(op, dict):
            return op
        if hasattr(op, "to_dict"):
            try:
                return op.to_dict()
            except Exception:
                pass
        if hasattr(op, "to_json"):
            try:
                return json.loads(op.to_json())
            except Exception:
                pass
        if hasattr(op, "_pb"):
            try:
                return dict(op._pb)
            except Exception:
                pass
        if hasattr(op, "__dict__"):
            return {k: v for k, v in op.__dict__.items() if not k.startswith("_")}
        return {}

    def _extract_progress(self, meta: dict[str, Any]) -> int:
        for k in ("progress", "progress_percent", "progressPercent", "progress_percentage"):
            v = meta.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    def _extract_video_uri(self, data: dict[str, Any]) -> str | None:
        resp = data.get("response") or {}
        gv = resp.get("generated_videos")
        if isinstance(gv, list) and gv:
            video = (gv[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("download_uri")
            if uri:
                return uri
        gvr = resp.get("generateVideoResponse") or {}
        samples = gvr.get("generatedSamples") or []
        if isinstance(samples, list) and samples:
            video = (samples[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("downloadUri")
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
