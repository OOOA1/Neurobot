# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Tuple

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

        # Флаг «админов не чарджим» можно задавать и через settings, и через ENV
        self._admin_bypass: bool = str(
            getattr(settings, "ADMIN_TOKENS_BYPASS", os.getenv("ADMIN_TOKENS_BYPASS", "1"))
        ).lower() in ("1", "true", "yes", "y")

    # ---------------------- TOKEN / ADMIN HELPERS ----------------------

    def _is_admin(self, user_id: int) -> bool:
        """Проверка: является ли пользователь админом.
        Сначала пробуем settings.is_admin, затем читаем ADMIN_USER_IDS из settings/env.
        """
        try:
            is_admin_attr = getattr(settings, "is_admin", None)
            if callable(is_admin_attr):
                return bool(is_admin_attr(user_id))
        except Exception:
            pass

        raw_ids = getattr(settings, "ADMIN_USER_IDS", None)
        if raw_ids is None:
            raw_ids = os.getenv("ADMIN_USER_IDS", "")

        try:
            if isinstance(raw_ids, (list, tuple, set)):
                return int(user_id) in {int(x) for x in raw_ids}
            ids: set[int] = set()
            for tok in re.split(r"[,\s]+", str(raw_ids).strip()):
                if tok:
                    try:
                        ids.add(int(tok))
                    except Exception:
                        pass
            return int(user_id) in ids
        except Exception:
            return False

    def _extract_user_id(self, params: GenerationParams) -> Optional[int]:
        """
        Пытаемся достать Telegram user id из разных возможных мест,
        не ломая обратную совместимость проекта.
        """
        candidates: list[Any] = []

        # Прямые атрибуты модели параметров
        for attr in ("user_id", "tg_user_id", "telegram_user_id", "author_id", "chat_id"):
            if hasattr(params, attr):
                candidates.append(getattr(params, attr))

        # Словарные поля (в том числе extras!)
        for attr in ("meta", "extra", "extras", "context"):
            if hasattr(params, attr):
                v = getattr(params, attr)
                if isinstance(v, dict):
                    candidates.extend(
                        [
                            v.get("user_id"),
                            v.get("tg_user_id"),
                            v.get("telegram_user_id"),
                            v.get("author_id"),
                            v.get("chat_id"),  # иногда хендлер кладёт chat_id
                        ]
                    )

        # Пробуем привести первое подходящее значение к int
        for c in candidates:
            try:
                if c is None:
                    continue
                return int(c)
            except Exception:
                continue
        return None

    def _extract_precharged(self, params: GenerationParams) -> bool:
        """
        Узнаём, проставил ли верхний слой отметку о предоплате.
        Понимаем несколько ключей для совместимости.
        """
        try:
            ext = getattr(params, "extras", None)
            if isinstance(ext, dict):
                if ext.get("precharged") or ext.get("charged_already") or ext.get("skip_charge"):
                    return True
        except Exception:
            pass

        # На всякий случай посмотрим и в другие словари, если используются
        for key in ("extra", "meta", "context"):
            try:
                v = getattr(params, key, None)
                if isinstance(v, dict):
                    if v.get("precharged") or v.get("charged_already") or v.get("skip_charge"):
                        return True
            except Exception:
                continue
        return False

    def _derive_quality(self, params: GenerationParams, model: str) -> str:
        """
        Определяем 'fast' | 'quality' по параметрам/модели.
        """
        q = getattr(params, "quality", None)
        if isinstance(q, str) and q.strip().lower() in {"fast", "quality"}:
            return q.strip().lower()

        m = (model or "").lower()
        # Простая эвристика: самую новую модель считаем "quality"
        if m in {"dream-machine-1.5"}:
            return "quality"
        return "fast"

    def _token_cost(self, quality: str) -> float:
        # Читаем стоимость из settings (см. config.py)
        return settings.token_cost(provider="luma", quality=quality)

    def _should_charge(self, user_id: Optional[int], precharged: bool) -> bool:
        """
        Нужно ли списывать токены на стороне провайдера.
        Если верхний слой указал precharged=True — не списываем.
        Для админов при включённом _admin_bypass — не списываем.
        """
        if precharged:
            return False
        if user_id is None:
            # если не знаем пользователя, перестрахуемся и НЕ будем списывать на провайдере
            # (верхний слой отвечает за списание)
            return False
        if self._admin_bypass and self._is_admin(int(user_id)):
            return False
        return settings.should_charge_tokens(user_id)

    def _try_import_token_service(self):
        """
        Отложенный импорт, чтобы не создавать жёсткой зависимости,
        если сервис токенов не подключён.
        """
        try:
            from services import token_service  # type: ignore
            return token_service
        except Exception:
            log.debug("token_service not available; skipping provider-side charging")
            return None

    async def _charge_if_needed(self, user_id: Optional[int], quality: str, precharged: bool) -> Tuple[bool, float]:
        """
        Если нужно — атомарно списываем токены через token_service.
        Возвращает (charged: bool, cost: float).
        """
        if not self._should_charge(user_id, precharged):
            return (False, 0.0)

        ts = self._try_import_token_service()
        if ts is None:
            # Нет сервиса — оставляем списание на верхний слой
            return (False, 0.0)

        if user_id is None:
            # не знаем пользователя — не списываем, оставляем верхнему слою
            return (False, 0.0)

        cost = self._token_cost(quality)
        ok = bool(ts.check_and_consume_tokens(int(user_id), float(cost)))
        if not ok:
            cur = ts.get_tokens(int(user_id))
            raise RuntimeError(f"Недостаточно токенов: нужно {cost}, на балансе {cur if cur is not None else 0}.")
        return (True, cost)

    def _refund_if_needed(self, charged: bool, user_id: Optional[int], cost: float) -> None:
        """Попытка вернуть токены при ошибке сабмита/сетевого сбоя."""
        if not charged or cost <= 0 or user_id is None:
            return
        ts = self._try_import_token_service()
        if ts is None:
            return
        try:
            ts.add_tokens(int(user_id), float(cost))
        except Exception as e:
            log.error("Failed to refund tokens after Luma error: %s", e)

    # -------------------------- PROVIDER API ---------------------------

    async def create_job(self, params: GenerationParams) -> JobId:
        """Submit a new Dream Machine job and return provider identifier."""
        model = (params.model or "").strip().lower()
        if model not in _ALLOWED_MODELS:
            model = _DEFAULT_MODEL

        # --- Учет токенов / админов ---
        user_id = self._extract_user_id(params)
        precharged = self._extract_precharged(params)

        # На всякий случай: если это админ и включён байпас — считаем, что предоплата уже была
        if user_id is not None and self._admin_bypass and self._is_admin(int(user_id)):
            precharged = True

        quality = self._derive_quality(params, model)
        charged = False
        cost = 0.0

        try:
            charged, cost = await self._charge_if_needed(user_id, quality, precharged)
        except Exception as e:
            log.warning("Luma: token check/charge failed for user %s: %s", user_id, e)
            raise

        payload: dict[str, Any] = {
            "prompt": params.prompt,
            "model": model,
        }
        if params.aspect_ratio:
            payload["aspect_ratio"] = params.aspect_ratio

        try:
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
                        self._refund_if_needed(charged, user_id, cost)
                        raise RuntimeError(f"Luma submit failed with status {resp.status}")
                    data = await self._safe_json(resp, text)
        except Exception:
            self._refund_if_needed(charged, user_id, cost)
            raise

        job_id = data.get("id") or (data.get("generation") or {}).get("id")
        if not job_id:
            self._refund_if_needed(charged, user_id, cost)
            raise RuntimeError("Luma submit succeeded but no job id returned")

        return job_id

    async def poll(self, job_id: JobId) -> JobStatus:
        """
        Опрос статуса. 4xx — фатальная ошибка; 5xx и сетевые сбои — транзиентные:
        возвращаем pending (с ретраями), чтобы внешний цикл продолжал опрос.
        """
        trust_env = os.getenv("HTTP_TRUST_ENV", "1").lower() in ("1", "true", "yes")
        retries = 3
        backoff_base = 1.5

        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession(trust_env=trust_env) as session:
                    async with session.get(
                        f"{self._base_url}/generations/{job_id}",
                        headers=self._headers_get,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        text = await resp.text()

                        # 5xx — транзиентно
                        if 500 <= resp.status < 600:
                            log.warning("Luma poll transient %s: %s", resp.status, text)
                            if attempt < retries - 1:
                                await asyncio.sleep(backoff_base * (2 ** attempt))
                                continue
                            return JobStatus(status="pending", progress=0, extra={"state": "transient", "http": resp.status})

                        # 4xx — клиентская ошибка
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

            except aiohttp.ClientError as e:
                log.warning("Luma poll network error: %s (attempt %d)", e, attempt + 1)
                if attempt < retries - 1:
                    await asyncio.sleep(backoff_base * (2 ** attempt))
                    continue
                return JobStatus(status="pending", progress=0, extra={"state": "transient", "error": str(e)})

        # На всякий случай
        return JobStatus(status="pending", progress=0, extra={"state": "unknown"})

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
