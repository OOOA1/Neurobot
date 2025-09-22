# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import random
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

# --- Глобальный мягкий троттлинг сабмитов (чтобы меньше ловить 429) ---
_submit_lock = asyncio.Lock()
_last_submit_ts: float = 0.0
_MIN_SUBMIT_GAP = float(getattr(settings, "GEMINI_MIN_SUBMIT_GAP_S", 0.7))  # сек

async def _respect_submit_gap() -> None:
    """Гарантируем минимальный зазор между сабмитами в рамках процесса."""
    global _last_submit_ts
    async with _submit_lock:
        now = time.monotonic()
        wait = _MIN_SUBMIT_GAP - (now - _last_submit_ts)
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 0.2))
        _last_submit_ts = time.monotonic()


# ---------------- ПУЛ КЛЮЧЕЙ ----------------
class _KeyPool:
    """Простой пул с round-robin и cooldown по 429."""
    def __init__(self, keys: list[str], cooldown_default: float = 8.0) -> None:
        self._keys = keys
        self._cooldown = [0.0] * len(keys)   # ts до которого ключ «на паузе»
        self._idx = 0                        # текущий
        self._lock = asyncio.Lock()
        self._cooldown_default = float(cooldown_default)

    def __len__(self) -> int:
        return len(self._keys)

    async def pick(self) -> tuple[int, str]:
        """Вернёт (index, key) ближайший доступный к использованию."""
        async with self._lock:
            now = time.monotonic()
            # 1) если текущий свободен — используем его
            if self._cooldown[self._idx] <= now:
                return self._idx, self._keys[self._idx]
            # 2) ищем следующий доступный
            best_i = None
            best_ready = float("inf")
            for i, ready in enumerate(self._cooldown):
                if ready <= now:
                    self._idx = i
                    return i, self._keys[i]
                if ready < best_ready:
                    best_ready = ready
                    best_i = i
            # 3) все заняты — возьмём тот, который освободится раньше (зовущий код может поспать)
            self._idx = best_i if best_i is not None else self._idx
            return self._idx, self._keys[self._idx]

    async def mark_429(self, idx: int, retry_after_header: str | None = None) -> None:
        """Пометить ключ перегруженным: поставить cooldown и сдвинуть указатель."""
        async with self._lock:
            try:
                ra = float(retry_after_header or 0)
            except Exception:
                ra = 0.0
            cooldown = max(self._cooldown_default, ra)
            self._cooldown[idx] = time.monotonic() + cooldown
            self._idx = (idx + 1) % len(self._keys)

    async def current_index(self) -> int:
        async with self._lock:
            return self._idx


class Veo3Provider(VideoProvider):
    name = Provider.VEO3

    def __init__(self) -> None:
        # Поддержка нескольких ключей: GOOGLE_API_KEYS="k1;k2,k3 k4"
        raw_list = (
            getattr(settings, "GOOGLE_API_KEYS", None)
            or os.getenv("GOOGLE_API_KEYS")
        )
        keys: list[str] = []
        if isinstance(raw_list, str) and raw_list.strip():
            for part in re.split(r"[;,]\s*|\s+", raw_list.strip()):
                if part:
                    keys.append(part.strip())

        # Фолбэки — одиночный ключ (как раньше)
        if not keys:
            single = (
                os.getenv("GOOGLE_API_KEY")
                or os.getenv("VEO_API_KEY")
                or getattr(settings, "GEMINI_API_KEY", None)
                or os.getenv("GEMINI_API_KEY")
            )
            if single:
                keys = [str(single)]

        if not keys:
            log.warning("No API key (GOOGLE_API_KEYS/GOOGLE_API_KEY/VEO_API_KEY/GEMINI_API_KEY); Veo3 submissions will fail")

        self._api_key: Optional[str] = (keys[0] if keys else None)
        cooldown = float(getattr(settings, "GEMINI_COOLDOWN_ON_429_S", 8.0))
        self._pool: _KeyPool | None = _KeyPool(keys, cooldown_default=cooldown) if len(keys) > 1 else None

        # Маппинг: операция → индекс ключа (для последующих poll/download тем же ключом)
        self._job_key_index: dict[str, int] = {}

    def _ensure_key(self) -> str:
        if not self._api_key:
            raise RuntimeError("Veo API key is not configured")
        return self._api_key

    def _prepare_env_for_sdk(self, key: str) -> None:
        """
        Перед созданием клиента SDK приводим окружение к одному ключу:
        - ставим GOOGLE_API_KEY = выбранный ключ
        - убираем GEMINI_API_KEY, чтобы SDK не писал warning
        """
        os.environ["GOOGLE_API_KEY"] = key
        os.environ.pop("GEMINI_API_KEY", None)

    # ------------ SUBMIT ------------
    async def create_job(self, params: GenerationParams) -> JobId:
        """
        Photo→video: SDK (google-genai) — синхронный вызов в отдельном треде.
        Text→video: REST (predictLongRunning) — БЕЗ передачи imageBytes (эта ветка не поддерживает).
        """
        _ = self._ensure_key()  # проверим, что хоть один ключ есть

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
            Если пользователь просит 1080п при 9:16 — принудительно 720п (без ошибок).
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
            """
            Усиливаем ориентацию и отсутствие рамок БЕЗ буквальных «9:16/16:9»,
            чтобы модель не рисовала цифры/иконки на видео.
            Добавляем строгий negative на текст/логотипы/водяные знаки/оверлеи.
            """
            if aspect == "9:16":
                tail = (
                    "portrait orientation, vertical video, full-frame composition, "
                    f"{anti_borders}"
                )
            else:
                tail = (
                    "landscape orientation, widescreen video, full-frame composition, "
                    f"{anti_borders}"
                )

            strict_neg = (
                "no text, no numbers, no captions, no subtitles, no titles, "
                "no logos, no watermarks, no stickers, no badges, no overlays, "
                "no ui, no hud, no icons, no timecodes, no corner icons, "
                "no frame counters, no lower-thirds, "
                f"{anti_borders}"
            )
            merged_neg = (user_neg.strip() + ", " + strict_neg) if (user_neg and user_neg.strip()) else strict_neg
            body = prompt if prompt.endswith((".", "!", "?")) else prompt + "."
            return f"{body} ({tail}). Avoid: {merged_neg}."

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
            # Санитайзим окружение для SDK и выбираем ключ
            try:
                from google import genai
                from google.genai import types as genai_types
                try:
                    from google.genai.errors import ClientError as GenaiClientError
                except Exception:
                    GenaiClientError = Exception
            except Exception as exc:
                raise RuntimeError(
                    "Photo→video через REST не поддерживается этой моделью. "
                    "Установи пакет 'google-genai' (pip install google-genai) для SDK."
                ) from exc

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

            tried_downshift = False
            while True:
                # берём ключ из пула (или единственный)
                if self._pool:
                    key_idx, api_key = await self._pool.pick()
                else:
                    key_idx, api_key = 0, self._ensure_key()

                self._prepare_env_for_sdk(api_key)
                client = genai.Client(api_key=api_key)

                async def _sdk_call(curr_model: str):
                    await _respect_submit_gap()
                    return await asyncio.to_thread(
                        client.models.generate_videos,
                        model=curr_model,
                        prompt=prompt_for_sdk,
                        image=image_obj,
                        config=genai_types.GenerateVideosConfig(**gv_kwargs) if gv_kwargs else None,
                    )

                try:
                    operation = await _sdk_call(model_name)
                    op_name = getattr(operation, "name", None) or getattr(operation, "operation", None)
                    if not op_name:
                        raise RuntimeError("Veo3 SDK returned no operation name")
                    log.info("Google Veo operation (SDK-sync) started: %s via key#%s", op_name, key_idx)
                    # запомним, каким ключом создавали
                    self._job_key_index[str(op_name)] = key_idx
                    return op_name
                except GenaiClientError as exc:
                    code = getattr(exc, "status_code", None)
                    msg = str(exc)
                    if (code == 429 or "RESOURCE_EXHAUSTED" in msg or "Too Many Requests" in msg):
                        if self._pool:
                            await self._pool.mark_429(key_idx, None)
                        # один раз дауншифтнем модель на fast
                        if (not fast_flag) and (not tried_downshift):
                            log.warning("SDK 429; switching model to fast and retrying once")
                            model_name = "veo-3.0-fast-generate-001"
                            fast_flag = True
                            tried_downshift = True
                        await asyncio.sleep(1.0 + random.uniform(0, 0.5))
                        continue
                    raise

        # --------- ПУТЬ 2: текст → REST API (predictLongRunning) ---------
        def _rest_url(model: str) -> str:
            return f"{_API_BASE}/models/{model}:predictLongRunning"

        url_lro = _rest_url(model_name)

        # Правильная схема для REST: instances[] + parameters{}
        parameters: dict[str, Any] = {
            "aspectRatio": _map_ar(ar),
            "resolution": _map_resolution(desired_resolution, ar),
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
            "instances": [{"prompt": prompt_text}],
            "parameters": parameters,
        }

        tried_fast = bool(fast_flag)
        # Внешний цикл — смена ключа при 429
        while True:
            if self._pool:
                key_idx, api_key = await self._pool.pick()
            else:
                key_idx, api_key = 0, self._ensure_key()

            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }

            # один-два ретрая на сабмит для «мелких» сбоев
            for attempt in range(3):
                try:
                    await _respect_submit_gap()
                    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
                        r = await http.post(url_lro, headers=headers, json=payload)
                    if r.status_code >= 400:
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
                        if r.status_code == 429:
                            ra = r.headers.get("retry-after")
                            if self._pool:
                                await self._pool.mark_429(key_idx, ra)
                            # попробуем дауншифт на fast (один раз)
                            if not tried_fast:
                                log.warning("REST 429; switching model to fast and retrying")
                                model_name = "veo-3.0-fast-generate-001"
                                url_lro = _rest_url(model_name)
                                tried_fast = True
                            await asyncio.sleep(max(float(ra or 0), 1.0) + random.uniform(0, 0.5))
                            break  # к внешнему while → другой ключ
                        if _is_transient_status(r.status_code) and attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        log.error("Veo3 submit failed %s %s\nBody: %s", r.status_code, r.reason_phrase, r.text)
                        raise RuntimeError("Veo3 submission failed")

                    data = r.json()
                    op_name = data.get("name") or data.get("operation")
                    if not op_name:
                        raise RuntimeError("Veo3 submission succeeded but no operation name")
                    log.info("Google Veo operation started: %s via key#%s", op_name, key_idx)
                    self._job_key_index[str(op_name)] = key_idx
                    return op_name

                except _TRANSIENT_ERRORS as exc:
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    # после тяжёлого сетевого — попробуем другой ключ
                    if self._pool:
                        await self._pool.mark_429(key_idx, None)
                    break  # к внешнему while

    # ------------ POLL ------------
    async def poll(self, job_id: JobId) -> JobStatus:
        # стараемся использовать тот же ключ, что и при сабмите
        key_idx = self._job_key_index.get(str(job_id), 0)
        keys: list[str]
        if self._pool:
            keys = self._pool._keys  # список ключей
            if key_idx >= len(keys):
                key_idx = await self._pool.current_index()
        else:
            keys = [self._ensure_key()]

        # попробуем до 2 разных ключей: свой и ближайший сосед (если пул есть)
        hops = 2 if self._pool and len(keys) > 1 else 1
        for hop in range(hops):
            idx = (key_idx + hop) % len(keys)
            api_key = keys[idx]
            url = f"{_API_BASE}/{job_id}"
            headers = {"x-goog-api-key": api_key}

            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                        r = await http.get(url, headers=headers)
                    if r.status_code >= 400:
                        if r.status_code == 429 and self._pool:
                            await self._pool.mark_429(idx, r.headers.get("retry-after"))
                            break  # попробуем следующий ключ
                        if _is_transient_status(r.status_code) and attempt < 1:
                            await asyncio.sleep(1.0)
                            continue
                        return JobStatus(status="failed", error=f"Veo3 poll failed ({r.status_code})")
                    data = r.json()
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
                except _TRANSIENT_ERRORS:
                    if attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    break  # попробуем следующий ключ

        return JobStatus(status="pending")

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

        # Берём тот же ключ, что и при сабмите (если знаем)
        if self._pool:
            idx = self._job_key_index.get(str(job_id), await self._pool.current_index())
            api_key = self._pool._keys[idx % len(self._pool._keys)]
        else:
            api_key = self._ensure_key()

        headers = {"x-goog-api-key": api_key}

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
                            if self._pool and resp.status_code == 429:
                                # попробуем другой ключ
                                idx = self._job_key_index.get(str(job_id), 0)
                                await self._pool.mark_429(idx, resp.headers.get("retry-after"))
                                # обновим ключ и попробуем ещё раз
                                new_idx, new_key = await self._pool.pick()
                                headers["x-goog-api-key"] = new_key
                                continue
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
