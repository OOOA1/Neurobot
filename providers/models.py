# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from providers.base import Provider


@dataclass(slots=True)
class GenerationParams:
    """Unified parameter set consumed by video providers."""

    prompt: str
    provider: Provider
    aspect_ratio: str | None = None
    resolution: str | None = None
    duration: str | None = None
    negative_prompt: str | None = None
    model: str | None = None
    fast_mode: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
