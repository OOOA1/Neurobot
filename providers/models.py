# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from providers.base import Provider


AspectRatio = Literal["16:9", "9:16"]
Resolution = Literal["720p", "1080p"]


@dataclass(slots=True)
class GenerationParams:
    """Unified parameter set consumed by video providers."""

    # Обязательное
    prompt: str
    provider: "Provider"

    # Видео-настройки
    aspect_ratio: Optional[AspectRatio] = None   # если None — провайдер подставит "16:9"
    # По умолчанию целимся в 1080p (стабильнее для Telegram и HQ)
    resolution: Optional[Resolution] = "1080p"

    # Старое поле (лучше использовать duration_seconds)
    duration: Optional[str] = None               # legacy/unused, сохраняем для совместимости
    duration_seconds: Optional[int] = None       # предпочтительное поле для длительности (обычно 8)

    negative_prompt: Optional[str] = None
    seed: Optional[int] = None                   # детерминизм, если поддерживается моделью

    # Модель/вариант
    model: Optional[str] = None
    fast_mode: bool = False

    # Изображение для photo->video
    image_bytes: Optional[bytes] = None          # «сырые» байты изображения
    image_mime: Optional[str] = None             # "image/jpeg" | "image/png"

    # Строгое соблюдение AR (по умолчанию включено)
    strict_ar: bool = True

    # Прочее/расширения (reference_file_id, кастомные параметры и т.п.)
    extras: dict[str, Any] = field(default_factory=dict)
