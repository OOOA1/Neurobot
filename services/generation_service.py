# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Union

from providers.base import JobId, JobStatus, Provider, VideoProvider
from providers.models import GenerationParams
from providers.luma_provider import LumaProvider
from providers.veo3_provider import Veo3Provider


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
) -> JobStatus:
    """Poll provider periodically until job completes or times out."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    while True:
        status = await poll_job(provider, job_id)
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
    resolution: int,
    negative_prompt: str | None = None,
    fast: bool = False,
    reference_file_id: str | None = None,
) -> JobId:
    """
    Convenience helper used by the Veo3 wizard.
    Duration убрана из публичного интерфейса — используется дефолт модели (Veo3).
    """
    provider_enum = _to_provider_enum(provider)
    if provider_enum is not Provider.VEO3:
        raise ValueError(f"Unsupported provider for video creation: {provider_enum}")

    provider_impl = get_provider(provider_enum)
    if not isinstance(provider_impl, Veo3Provider):
        raise RuntimeError("Configured provider is not Veo3Provider")

    return await provider_impl.create_video(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
    )


async def poll_video(provider: str, job_id: JobId) -> JobStatus:
    """Poll helper that accepts string provider identifiers."""
    provider_enum = _to_provider_enum(provider)
    return await poll_job(provider_enum, job_id)


async def download_video(provider: str, job_id: JobId) -> Path:
    """Download helper that accepts string provider identifiers."""
    provider_enum = _to_provider_enum(provider)
    return await download_job(provider_enum, job_id)
