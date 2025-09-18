# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from providers.models import GenerationParams


class Provider(str, Enum):
    """Supported video generation providers."""

    LUMA = "luma"
    VEO3 = "veo3"


JobId = str


@dataclass(slots=True)
class JobStatus:
    """Represents a provider job state snapshot."""

    status: str
    progress: int = 0
    error: str | None = None
    extra: dict | None = None


class VideoProvider(Protocol):
    """Common contract for video generation providers."""

    name: Provider

    async def create_job(self, params: GenerationParams) -> JobId:
        """Submit a generation job and return provider job identifier."""

    async def poll(self, job_id: JobId) -> JobStatus:
        """Return latest job status supplied by the provider."""

    async def download(self, job_id: JobId) -> Path:
        """Download generated asset and return local filesystem path."""
