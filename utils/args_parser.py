# -*- coding: utf-8 -*-
"""Command-line style argument parser helpers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from providers.base import Provider
from providers.models import GenerationParams


@dataclass(slots=True)
class ParsedArgs:
    """Result of parsing a /veo invocation."""

    params: GenerationParams
    raw_prompt: str


class FlagParseError(ValueError):
    """Raised when provided flags cannot be parsed."""


def parse_veo_command(raw: str) -> ParsedArgs:
    """Parse a /veo command with flag modifiers into generation params."""

    tokens = shlex.split(raw)
    prompt_parts: list[str] = []
    aspect_ratio: str | None = None
    resolution: str | None = None
    duration: str | None = None
    negative_prompt: str | None = None
    fast_mode = False
    model: str | None = None

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]

        if token.startswith("/veo"):
            idx += 1
            continue

        if token.startswith("--") and "=" in token:
            flag, value = token.split("=", 1)
            tokens.insert(idx + 1, value)
            token = flag

        if token in {"--ar", "--aspect", "--aspect-ratio"}:
            idx += 1
            if idx >= len(tokens):
                raise FlagParseError("--ar flag requires a value")
            aspect_ratio = tokens[idx]
        elif token in {"--720p", "--1080p"}:
            resolution = token.lstrip("-")
        elif token == "--resolution":
            idx += 1
            if idx >= len(tokens):
                raise FlagParseError("--resolution flag requires a value")
            resolution = tokens[idx]
        elif token in {"--dur", "--duration"}:
            idx += 1
            if idx >= len(tokens):
                raise FlagParseError("--dur flag requires a value")
            duration = _normalize_duration(tokens[idx])
        elif token in {"--neg", "--negative"}:
            idx += 1
            if idx >= len(tokens):
                raise FlagParseError("--neg flag requires a value")
            negative_prompt = tokens[idx]
        elif token == "--fast":
            fast_mode = True
        elif token in {"--quality", "--slow"}:
            fast_mode = False
        elif token == "--model":
            idx += 1
            if idx >= len(tokens):
                raise FlagParseError("--model flag requires a value")
            model = tokens[idx]
        else:
            prompt_parts.append(token)

        idx += 1

    prompt = " ".join(prompt_parts).strip()
    params = GenerationParams(
        prompt=prompt,
        provider=Provider.VEO3,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        duration=duration,
        negative_prompt=negative_prompt,
        model=model,
        fast_mode=fast_mode,
    )
    return ParsedArgs(params=params, raw_prompt=prompt)


def _normalize_duration(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned.endswith("s"):
        return cleaned
    if cleaned.isdigit():
        return f"{cleaned}s"
    raise FlagParseError(f"Unsupported duration value: {value}")
