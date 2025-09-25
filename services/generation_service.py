# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Union, Optional, Tuple

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
    reference_url: Optional[str] = None,   # ← важно для image→video по URL
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

    Теперь умеет принимать:
      - сырые байты изображения (image_bytes) + их mime (image_mime);
      - reference_file_id (tg file_id/url/локальный путь);
      - reference_url (прямая HTTP-ссылка) — критично для Polza/KIE (image→video).
    Если указаны и image_bytes, и reference_url — провайдер сам решит приоритет.
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

    extras: dict = {}
    if reference_file_id:
        extras["reference_file_id"] = reference_file_id
    if reference_url:
        extras["reference_url"] = reference_url  # донесём до провайдера

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
        extras=extras,
    )

    # Если у модели параметров есть поле image_url — проставим его для совместимости.
    if reference_url and getattr(params, "image_url", None) in (None, ""):
        try:
            setattr(params, "image_url", reference_url)
        except Exception:
            pass

    return await create_job(params)


# ----------------------------------------------------------------------
# ПАРА «Оригинал + HQ» (для 16:9 и 9:16 одинаково)
# ----------------------------------------------------------------------
async def create_video_pair(
    *,
    prompt: str,
    aspect_ratio: str,
    # базовый проход:
    resolution: Union[int, str] = "720p",
    fast: bool = True,
    # HQ проход:
    send_hq: bool = True,
    hq_resolution: Union[int, str] = "1080p",
    # общее:
    negative_prompt: Optional[str] = None,
    reference_file_id: Optional[str] = None,
    reference_url: Optional[str] = None,     # ← добавлено
    strict_ar: bool = True,
    image_bytes: Optional[bytes] = None,
    image_mime: Optional[str] = None,
    seed: Optional[int] = None,
    duration_seconds: Optional[int] = None,
) -> Tuple[JobId, Optional[JobId]]:
    """
    Создаёт два задания:
      1) «Оригинал» (обычно fast/720p),
      2) «HQ» (медленнее, лучше — 1080p, работает и для 9:16).
    Возвращает (job_id_original, job_id_hq | None).
    """
    # 1) первый проход
    job_id_first = await create_video(
        provider="veo3",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
        reference_url=reference_url,      # прокинули
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=strict_ar,
    )

    if not send_hq:
        return job_id_first, None

    # 2) HQ-проход — 1080p, fast=False
    if isinstance(hq_resolution, int):
        hq_res_str = f"{hq_resolution}p"
    else:
        hq_res_str = str(hq_resolution).lower()
        if not hq_res_str.endswith("p"):
            hq_res_str += "p"

    extras_hq: dict = {}
    if reference_file_id:
        extras_hq["reference_file_id"] = reference_file_id
    if reference_url:
        extras_hq["reference_url"] = reference_url
    extras_hq["model"] = "veo3"  # подсказка провайдеру на quality-модель

    params_hq = GenerationParams(
        prompt=prompt.strip(),
        provider=Provider.VEO3,
        aspect_ratio=aspect_ratio,
        resolution=hq_res_str,
        negative_prompt=negative_prompt,
        fast_mode=False,
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=strict_ar,
        extras=extras_hq,
    )

    # Опционально — если у GenerationParams есть поле model/image_url.
    try:
        setattr(params_hq, "model", "veo3")
    except Exception:
        pass
    if reference_url and getattr(params_hq, "image_url", None) in (None, ""):
        try:
            setattr(params_hq, "image_url", reference_url)
        except Exception:
            pass

    provider = get_provider(Provider.VEO3)
    job_id_hq = await provider.create_job(params_hq)

    return job_id_first, job_id_hq


# ------------------------------
# НОРМАЛИЗАЦИЯ ВЫХОДА
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
    - 9:16 → вертикальный 1080x1920 канвас с блюр-подложкой
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
    reference_url: Optional[str] = None,    # ← добавлено
    image_bytes: Optional[bytes] = None,
    image_mime: Optional[str] = None,
    seed: Optional[int] = None,
    duration_seconds: Optional[int] = None,
    poll_interval: float = 8.0,
    poll_timeout: float = 20 * 60.0,
) -> Path:
    """
    Удобный «всё-в-одном» пайплайн под Veo3:
    создать → дождаться → скачать → нормализовать под 16:9/9:16.
    """
    job_id = await create_video(
        provider="veo3",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
        reference_url=reference_url,       # прокинули
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=True,
    )

    status = await wait_for_completion(Provider.VEO3, job_id, interval_sec=poll_interval, timeout_sec=poll_timeout)
    if status.status != "succeeded":
        raise RuntimeError(f"Generation failed: {status.error or {status.status}}")

    return await download_and_normalize_video("veo3", job_id, aspect_ratio)


# --------- «Оригинал + HQ» полноциклово ----------
async def generate_wait_download_normalized_pair(
    *,
    prompt: str,
    aspect_ratio: str,
    # базовый проход:
    resolution: Union[int, str] = "720p",
    fast: bool = True,
    # HQ проход:
    send_hq: bool = True,
    hq_resolution: Union[int, str] = "1080p",
    # общее:
    negative_prompt: Optional[str] = None,
    reference_file_id: Optional[str] = None,
    reference_url: Optional[str] = None,     # ← добавлено
    image_bytes: Optional[bytes] = None,
    image_mime: Optional[str] = None,
    seed: Optional[int] = None,
    duration_seconds: Optional[int] = None,
    poll_interval: float = 8.0,
    poll_timeout: float = 20 * 60.0,
) -> Tuple[Path, Optional[Path]]:
    """
    Полный цикл для пары «Оригинал + HQ»:
    - одинаково работает для 16:9 и 9:16,
    - HQ-прогон всегда идёт в 1080p (если send_hq=True).
    Возвращает (path_original, path_hq | None).
    """
    job_id_first, job_id_hq = await create_video_pair(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        fast=fast,
        send_hq=send_hq,
        hq_resolution=hq_resolution,
        negative_prompt=negative_prompt,
        reference_file_id=reference_file_id,
        reference_url=reference_url,       # прокинули
        image_bytes=image_bytes,
        image_mime=image_mime,
        seed=seed,
        duration_seconds=duration_seconds,
        strict_ar=True,
    )

    # ждём первый
    st1 = await wait_for_completion(Provider.VEO3, job_id_first, interval_sec=poll_interval, timeout_sec=poll_timeout)
    if st1.status != "succeeded":
        raise RuntimeError(f"Original generation failed: {st1.error or st1.status}")
    path1 = await download_and_normalize_video("veo3", job_id_first, aspect_ratio)

    path2: Optional[Path] = None
    if job_id_hq:
        st2 = await wait_for_completion(Provider.VEO3, job_id_hq, interval_sec=poll_interval, timeout_sec=poll_timeout)
        if st2.status != "succeeded":
            log.error("HQ generation failed: %s", st2.error or st2.status)
            path2 = None
        else:
            path2 = await download_and_normalize_video("veo3", job_id_hq, aspect_ratio)

    return path1, path2


# ------------------------------
# BACKWARD-COMPAT WRAPPERS
# ------------------------------
async def poll_video(provider: str, job_id: JobId) -> JobStatus:
    """String-friendly wrapper, keeps backward compatibility."""
    provider_enum = _to_provider_enum(provider)
    return await poll_job(provider_enum, job_id)

async def download_video(provider: str, job_id: JobId) -> Path:
    """String-friendly wrapper, keeps backward compatibility."""
    provider_enum = _to_provider_enum(provider)
    return await download_job(provider_enum, job_id)
