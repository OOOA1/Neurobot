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
from services.media_tools import (
    enforce_ar_no_bars,     # 16:9 → cover+crop до 1920x1080 (без внутренних рамок)
    build_vertical_blurpad, # 9:16 → вертикальный 1080x1920 канвас с блюр-подложкой
)

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
    interval_schedule: list[float] | None = None,
) -> JobStatus:
    """
    Poll provider periodically until job completes or times out.
    Добавлен retry для временных сетевых ошибок.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    schedule = interval_schedule or [interval_sec]
    step = 0

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

        sleep_for = schedule[min(step, len(schedule) - 1)]
        step += 1
        await asyncio.sleep(sleep_for)


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

    # Нормализуем resolution к виду '720p'/'1080p'
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


# ------------------------------
# НОВОЕ: нормализация результата
# ------------------------------
def _norm_out_path(src: Path, aspect: str) -> Path:
    stem = src.stem
    suffix = src.suffix or ".mp4"
    tag = "16x9" if aspect == "16:9" else "9x16" if aspect == "9:16" else aspect.replace(":", "x")
    return src.with_name(f"{stem}.normalized_{tag}{suffix}")


async def _normalize_to_aspect(src_path: Path, out_path: Path, aspect: str) -> None:
    """
    Единая точка нормализации:
    - 16:9 → cover+crop до 1920x1080 (убираем любые внутренние рамки)
    - 9:16 → вертикальный 1080x1920 канвас с блюр-подложкой (как на референс-скрине)
    """
    if aspect == "9:16":
        await asyncio.to_thread(build_vertical_blurpad, str(src_path), str(out_path))
    else:
        await asyncio.to_thread(enforce_ar_no_bars, str(src_path), str(out_path), "16:9")


async def download_and_normalize_video(provider: str, job_id: JobId, aspect: str) -> Path:
    """
    Скачивает ролик у провайдера и гарантированно приводит к:
      - 16:9 → full-frame 1920x1080 без чёрных полос,
      - 9:16 → 1080x1920 с размытыми боками.
    Возвращает путь к нормализованному файлу.
    """
    provider_enum = _to_provider_enum(provider)
    src_path = await download_job(provider_enum, job_id)
    out_path = _norm_out_path(Path(src_path), aspect)

    log.info("Normalizing AR to %s (no bars/blurpad): %s -> %s", aspect, src_path, out_path)
    await _normalize_to_aspect(Path(src_path), out_path, aspect)
    return out_path


async def normalize_existing_video(src_path: Path, aspect: str) -> Path:
    """
    Нормализует уже существующий локальный файл:
      - 16:9 → cover+crop 1920x1080,
      - 9:16 → вертикальный blurpad 1080x1920.
    """
    out_path = _norm_out_path(Path(src_path), aspect)
    log.info("Normalizing existing file to %s: %s -> %s", aspect, src_path, out_path)
    await _normalize_to_aspect(Path(src_path), out_path, aspect)
    return out_path


async def generate_wait_download_normalized(
    *,
    prompt: str,
    aspect_ratio: str,
    resolution: Union[int, str] = "1080p",
    negative_prompt: Optional[str] = None,
    fast: bool = False,
    reference_file_id: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_mime: Optional[str] = None,
    seed: Optional[int] = None,
    duration_seconds: Optional[int] = None,
    poll_interval: float = 8.0,
    poll_timeout: float = 20 * 60.0,
) -> Path:
    """
    Удобный «всё-в-одном» пайплайн под Veo3:
    создать → дождаться → скачать → нормализовать под 16:9/9:16 (без чёрных полос; для 9:16 — blurpad).
    """
    job_id = await create_video(
        provider="veo3",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=True,
    )

    status = await wait_for_completion(Provider.VEO3, job_id, interval_sec=poll_interval, timeout_sec=poll_timeout)
    if status.status != "succeeded":
        raise RuntimeError(f"Generation failed: {status.error or status.status}")

    return await download_and_normalize_video("veo3", job_id, aspect_ratio)
