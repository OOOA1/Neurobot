# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import logging
import mimetypes
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
        Отправка задачи в Veo3 (операция LRO).

        Режимы:
        - Обычный: пробуем instances[0].config (aspectRatio / resolution / negativePrompt).
          Если модель не поддерживает config (400 INVALID_ARGUMENT) — повторяем без config,
          но усиливаем промпт (full-frame 9:16 и пр.).
        - strict_ar: всегда пропускаем config и сразу шлём усиленный промпт,
          чтобы максимально жёстко удержать нужный формат.
        """
        api_key = self._ensure_key()
        model_name = params.model or (
            "veo-3.0-fast-generate-001" if params.fast_mode else "veo-3.0-generate-001"
        )
        qparams = {"key": api_key}

        # ---- Базовый промпт / аспект ----
        base_prompt = (params.prompt or "").strip()
        ar = (params.aspect_ratio or "").strip()
        strict_ar = bool((params.extras or {}).get("strict_ar"))

        # ---- Референс (inlineData) ----
        image_obj: dict[str, Any] | None = None
        ref = (params.extras or {}).get("reference_file_id") if params.extras else None
        if isinstance(ref, str) and ref.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    rimg = await http.get(ref)
                    rimg.raise_for_status()
                    img_bytes = rimg.content
                mime = (mimetypes.guess_type(ref)[0]) or "image/jpeg"
                b64 = base64.b64encode(img_bytes).decode("ascii")
                image_obj = {"inlineData": {"mimeType": mime, "data": b64}}
            except Exception as exc:
                log.exception("Failed to fetch reference image: %s", exc)
                image_obj = None

        def _map_ar(val: str) -> str:
            v = (val or "").strip()
            return v if v in {"16:9", "9:16", "1:1"} else "16:9"

        def _map_resolution(res: Optional[str]) -> Optional[str]:
            if not res:
                return None
            r = str(res).lower().rstrip("p")
            if r in {"720", "720p"}:
                return "720p"
            if r in {"1080", "1080p"}:
                return "1080p"
            return None

        anti_borders = (
            "no device frame, no smartphone frame, no UI mockup, "
            "no borders, no black bars, no letterboxing, no pillarboxing, "
            "edge-to-edge content, fill the entire frame"
        )

        def _strong_ar_prompt(prompt: str, aspect: str, resolution_hint: Optional[str], user_neg: Optional[str]) -> str:
            rh = (resolution_hint or "").lower().rstrip("p")
            res_int = 1080 if rh == "1080" else 720  # дефолт 720

            if aspect == "9:16":
                w, h = (720, 1280) if res_int <= 720 else (1080, 1920)
                tail = (
                    f" (VERTICAL 9:16 FULL-FRAME, portrait composition, {w}x{h}, {anti_borders}, "
                    f"not landscape, not widescreen, not 16:9)"
                )
            elif aspect == "1:1":
                s = 1080 if res_int >= 1080 else 720
                tail = (
                    f" (SQUARE 1:1 FULL-FRAME, {s}x{s}, {anti_borders}, "
                    f"not landscape, not widescreen, not 16:9, not 9:16)"
                )
            else:
                w, h = (1280, 720) if res_int <= 720 else (1920, 1080)
                tail = (
                    f" (WIDESCREEN 16:9 FULL-FRAME, landscape composition, {w}x{h}, {anti_borders}, "
                    f"not vertical, not portrait, not 9:16, not 1:1)"
                )

            merged_neg = (user_neg.strip() + ", " + anti_borders) if user_neg else anti_borders
            return prompt + tail + f". Avoid: {merged_neg}."

        headers = {"Content-Type": "application/json"}
        url_lro = f"{_API_BASE}/models/{model_name}:predictLongRunning"

        # ---------- Строгий режим: сразу шлём усиленный промпт (без config) ----------
        if strict_ar:
            prompt_strict = _strong_ar_prompt(base_prompt, ar, params.resolution, params.negative_prompt)
            instance_fb: dict[str, Any] = {"prompt": prompt_strict}
            if image_obj:
                instance_fb["image"] = image_obj
            payload_fb: dict[str, Any] = {"instances": [instance_fb]}

            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
                r2 = await http.post(url_lro, params=qparams, headers=headers, json=payload_fb)

            if r2.status_code >= 400:
                log.error("Veo3 submit (strict) failed %s %s\nBody: %s", r2.status_code, r2.reason_phrase, r2.text)
                raise RuntimeError("Veo3 submission failed")

            data2 = r2.json()
            op_name2 = data2.get("name")
            if not op_name2:
                log.error("Veo3 submit strict ok but no operation name. Body: %s", data2)
                raise RuntimeError("Veo3 submission succeeded but no operation name returned")
            log.info("Google Veo operation (strict) started: %s", op_name2)
            return op_name2

        # ---------- Попытка №1: с config ----------
        config: dict[str, Any] = {"aspectRatio": _map_ar(ar)}
        res = _map_resolution(params.resolution)
        if res:
            config["resolution"] = res
        if params.negative_prompt:
            config["negativePrompt"] = params.negative_prompt

        # Мягкий текстовый префикс (доп. сигнал ориентации)
        prompt_cfg = base_prompt
        if ar == "9:16":
            prompt_cfg = (
                "VERTICAL 9:16 full-frame video. Edge-to-edge content. "
                "No device/phone frame, no borders/letterboxing/pillarboxing. "
            ) + prompt_cfg
        elif ar == "1:1":
            prompt_cfg = (
                "SQUARE 1:1 full-frame video. Edge-to-edge content. "
                "No borders/letterboxing/pillarboxing. "
            ) + prompt_cfg

        instance_cfg: dict[str, Any] = {"prompt": prompt_cfg, "config": config}
        if image_obj:
            instance_cfg["image"] = image_obj

        payload_cfg: dict[str, Any] = {"instances": [instance_cfg]}

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
            r = await http.post(url_lro, params=qparams, headers=headers, json=payload_cfg)

        # Если всё ок — возвращаем operation name
        if r.status_code < 400:
            data = r.json()
            op_name = data.get("name")
            if not op_name:
                log.error("Veo3 submit ok but no operation name. Body: %s", data)
                raise RuntimeError("Veo3 submission succeeded but no operation name returned")
            log.info("Google Veo operation started: %s", op_name)
            return op_name

        # Если модель не поддерживает config — повторяем без него (fallback)
        body_text = r.text
        msg = body_text.lower()
        config_unsupported = (
            "`config` isn't supported" in body_text
            or "config isn't supported" in msg
            or "unknown field \"config\"" in msg
            or ("invalid_argument" in msg and "config" in msg)
        )
        if not config_unsupported:
            log.error("Veo3 submit failed %s %s\nBody: %s", r.status_code, r.reason_phrase, body_text)
            raise RuntimeError("Veo3 submission failed")

        # ---------- Попытка №2: без config, но с ЖЁСТКИМ AR-хинтом ----------
        prompt_fallback = _strong_ar_prompt(base_prompt, ar, params.resolution, params.negative_prompt)

        instance_fb: dict[str, Any] = {"prompt": prompt_fallback}
        if image_obj:
            instance_fb["image"] = image_obj
        payload_fb: dict[str, Any] = {"instances": [instance_fb]}

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
            r2 = await http.post(url_lro, params=qparams, headers=headers, json=payload_fb)

        if r2.status_code >= 400:
            log.error("Veo3 submit (fallback) failed %s %s\nBody: %s", r2.status_code, r2.reason_phrase, r2.text)
            raise RuntimeError("Veo3 submission failed")

        data2 = r2.json()
        op_name2 = data2.get("name")
        if not op_name2:
            log.error("Veo3 submit fallback ok but no operation name. Body: %s", data2)
            raise RuntimeError("Veo3 submission succeeded but no operation name returned")
        log.info("Google Veo operation (fallback) started: %s", op_name2)
        return op_name2

    async def create_video(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        resolution: int,
        negative_prompt: str | None = None,
        fast: bool = False,
        reference_file_id: str | None = None,
        strict_ar: bool = False,  # <--- новый параметр
    ) -> JobId:
        # resolution/negativePrompt/AR прокидываются в submit
        extras: dict[str, Any] = {}
        if reference_file_id:
            extras["reference_file_id"] = reference_file_id
        if strict_ar:
            extras["strict_ar"] = True

        params = GenerationParams(
            prompt=prompt,
            provider=Provider.VEO3,
            aspect_ratio=aspect_ratio,
            resolution=f"{resolution}p",
            negative_prompt=negative_prompt,
            fast_mode=fast,
            extras=extras or None,
        )
        return await self.create_job(params)

    # ------------ POLL (REST operations/<id>) ------------
    async def poll(self, job_id: JobId) -> JobStatus:
        api_key = self._ensure_key()
        url = f"{_API_BASE}/{job_id}"
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
        gvr = resp.get("generateVideoResponse") or {}
        samples = gvr.get("generatedSamples") or []
        if isinstance(samples, list) and samples:
            video = (samples[0] or {}).get("video") or {}
            uri = video.get("uri") or video.get("downloadUri")
            if uri:
                return uri
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
    strict_ar: bool = False,   # <--- пробрасываем наружу
) -> JobId:
    resolution_int = int(str(resolution).rstrip("p"))
    return await _default_provider.create_video(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution_int,
        negative_prompt=negative_prompt,
        fast=fast,
        reference_file_id=reference_file_id,
        strict_ar=strict_ar,
    )


async def poll(job_id: JobId) -> JobStatus:
    return await _default_provider.poll(job_id)


async def download(job_id: JobId) -> Path:
    return await _default_provider.download(job_id)
