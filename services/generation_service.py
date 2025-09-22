# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Union, Optional

from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams
from providers.luma_provider import LumaProvider
from providers.veo3_provider import Veo3Provider

log = logging.getLogger("services.generation_service")

_PROVIDER_FACTORIES: dict[Provider, Callable[[], VideoProvider]] = {
    Provider.LUMA: LumaProvider,
    Provider.VEO3: Veo3Provider,
}
_provider_cache: dict[Provider, VideoProvider] = {}


def get_provider(provider: Provider) -> VideoProvider:
    """Return a singleton provider instance for the requested backend."""
    try:
        factory = _PROVIDER_FACTORIES[provider]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider: {provider}") from exc

    if provider not in _provider_cache:
        _provider_cache[provider] = factory()
    return _provider_cache[provider]


async def create_job(params: GenerationParams) -> JobId:
    """Submit a generation request via the selected provider."""
    provider = get_provider(params.provider)
    return await provider.create_job(params)


async def poll_job(provider: Provider, job_id: JobId) -> JobStatus:
    """Retrieve current status of a job from provider."""
    return await get_provider(provider).poll(job_id)


async def download_job(provider: Provider, job_id: JobId) -> Path:
    """Download rendered asset for a completed job."""
    return await get_provider(provider).download(job_id)


async def wait_for_completion(
    provider: Provider,
    job_id: JobId,
    *,
    interval_sec: float = 8.0,
    timeout_sec: float = 20 * 60.0,
    max_retries: int = 3,
) -> JobStatus:
    """
    Poll provider periodically until job completes or times out.
    Добавлен retry для временных сетевых ошибок.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec

    attempt = 0
    while True:
        try:
            status = await poll_job(provider, job_id)
        except Exception as exc:
            attempt += 1
            if attempt <= max_retries:
                backoff = min(5.0, interval_sec * attempt)
                log.warning(
                    "poll_job failed (attempt %s/%s): %s. Retrying in %.1fs",
                    attempt, max_retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            log.error("poll_job failed permanently after %s retries: %s", max_retries, exc)
            return JobStatus(status="failed", error=str(exc))

        # сброс счётчика после успешного poll
        attempt = 0

        if status.status in {"succeeded", "failed"}:
            return status

        if loop.time() > deadline:
            return JobStatus(status="failed", error="timeout")

        await asyncio.sleep(interval_sec)


def _to_provider_enum(provider: Union[str, Provider]) -> Provider:
    return Provider(provider) if not isinstance(provider, Provider) else provider


async def create_video(
    *,
    provider: str,
    prompt: str,
    aspect_ratio: str,
    resolution: Union[int, str],
    negative_prompt: Optional[str] = None,
    fast: bool = False,
    reference_file_id: Optional[str] = None,
    strict_ar: bool = True,
    # Новые параметры для photo->video:
    image_bytes: Optional[bytes] = None,
    image_mime: Optional[str] = None,  # "image/jpeg" | "image/png"
    # Дополнительно:
    seed: Optional[int] = None,
    duration_seconds: Optional[int] = None,
) -> JobId:
    """
    Convenience helper used by the Veo3 wizard.

    Теперь умеет принимать сырые байты изображения (image_bytes) и их mime (image_mime).
    Если также указан reference_file_id (URL/Gemini files/локальный путь), провайдер решит,
    чем именно воспользоваться: приоритет обычно за image_bytes/image_mime.
    """
    provider_enum = _to_provider_enum(provider)
    if provider_enum is not Provider.VEO3:
        raise ValueError(f"Unsupported provider for video creation: {provider_enum}")

    # Нормализуем resolution
    if isinstance(resolution, int):
        resolution_str = f"{resolution}p"
    else:
        resolution_str = str(resolution).lower()
        if not resolution_str.endswith("p"):
            resolution_str += "p"

    params = GenerationParams(
        prompt=prompt.strip(),
        provider=provider_enum,
        aspect_ratio=aspect_ratio,
        resolution=resolution_str,
        negative_prompt=negative_prompt,
        fast_mode=fast,
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=strict_ar,
        extras={**({"reference_file_id": reference_file_id} if reference_file_id else {})},
    )
    return await create_job(params)


async def poll_video(provider: str, job_id: JobId) -> JobStatus:
    """Poll helper that accepts string provider identifiers."""
    provider_enum = _to_provider_enum(provider)
    return await poll_job(provider_enum, job_id)


async def download_video(provider: str, job_id: JobId) -> Path:
    """Download helper that accepts string provider identifiers."""
    provider_enum = _to_provider_enum(provider)
    return await download_job(provider_enum, job_id)
