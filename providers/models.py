# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from providers.base import Provider

# Допустимые значения
AspectRatio = Literal["16:9", "9:16"]
Resolution = Literal["720p", "1080p"]


@dataclass(slots=True)
class GenerationParams:
    """Unified parameter set consumed by video providers."""

    # Обязательное
    prompt: str
    provider: "Provider"

    # Видео-настройки
    # Если None — провайдер подставит "16:9"
    aspect_ratio: Optional[AspectRatio] = None
    # По умолчанию целимся в 1080p (стабильнее для Telegram и HQ)
    resolution: Optional[Resolution] = "1080p"

    # Старое поле (лучше использовать duration_seconds)
    duration: Optional[str] = None               # legacy/unused, оставлено для совместимости
    duration_seconds: Optional[int] = None       # предпочтительное поле для длительности (обычно 8)

    negative_prompt: Optional[str] = None
    seed: Optional[int] = None                   # детерминизм, если поддерживается моделью

    # Модель/вариант
    model: Optional[str] = None
    fast_mode: bool = False                      # актуальное поле
    fast: Optional[bool] = None                  # бэко-совместимый алиас (если где-то в старом коде)

    # Изображение для photo->video
    image_bytes: Optional[bytes] = None          # «сырые» байты изображения
    image_mime: Optional[str] = None             # "image/jpeg" | "image/png"

    # Строгое соблюдение AR (по умолчанию включено)
    strict_ar: bool = True

    # Прочее/расширения (reference_file_id/reference_url, кастомные параметры и т.п.)
    extras: dict[str, Any] = field(default_factory=dict)

    # ---------------------------
    # Нормализация и утилиты
    # ---------------------------
    def __post_init__(self) -> None:
        # --- нормализация aspect_ratio ---
        if self.aspect_ratio not in ("16:9", "9:16"):
            # допускаем None и любые другие значения -> дефолт "16:9"
            self.aspect_ratio = "16:9"

        # --- нормализация resolution ---
        # принимаем варианты: 1080, "1080", "1080p" / 720, "720", "720p"
        self.resolution = self._normalize_resolution(self.resolution)

        # duration_seconds должны быть > 0, иначе None
        if isinstance(self.duration_seconds, int) and self.duration_seconds <= 0:
            self.duration_seconds = None

        # подчистим negative_prompt
        if isinstance(self.negative_prompt, str):
            np = self.negative_prompt.strip()
            self.negative_prompt = np or None

        # extras — гарантированно dict
        if not isinstance(self.extras, dict):
            self.extras = {}

    @staticmethod
    def _normalize_resolution(value: Any) -> Resolution | None:
        if value is None:
            return "1080p"
        # числа 1080/720
        if isinstance(value, int):
            return "1080p" if value >= 1080 else ("720p" if value == 720 else "1080p")
        # строки "1080", "1080p", "720", "720p"
        if isinstance(value, str):
            v = value.strip().lower()
            if v.endswith("p"):
                v = v[:-1]
            if v.isdigit():
                n = int(v)
                if n >= 1080:
                    return "1080p"
                if n == 720:
                    return "720p"
            # любые другие строки — свернём к 1080p
            return "1080p"
        # неизвестный тип — безопасно к 1080p
        return "1080p"

    @property
    def effective_fast(self) -> bool:
        """
        Единая точка истины для «быстрого» режима:
        - если передан старый флаг fast — используем его
        - иначе — fast_mode
        """
        return bool(self.fast if self.fast is not None else self.fast_mode)

    @property
    def aspect_or_default(self) -> AspectRatio:
        """Возвращает заданный AR или '16:9' по умолчанию."""
        return (self.aspect_ratio or "16:9")  # type: ignore[return-value]

    @property
    def resolution_or_default(self) -> Resolution:
        """Возвращает заданное разрешение в стандартизированном виде ('720p'/'1080p')."""
        return self.resolution or "1080p"  # type: ignore[return-value]
