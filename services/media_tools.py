# -*- coding: utf-8 -*-
from __future__ import annotations

"""
services/media_tools.py

Набор утилит для постпроцессинга видео:
- probe_video: получить (width, height, fps) видео через ffprobe
- build_intro_from_image: сделать короткий mp4 из картинки нужного размера
- concat_two: склеить интро и основное видео без перехода
- concat_with_crossfade: склеить с плавным переходом (кроссфейд)

Требуется установленный ffmpeg/ffprobe в PATH.
"""

import asyncio
import json
import shlex
import subprocess
from pathlib import Path
from typing import Tuple


# -------- внутренние синхронныеhelpers --------
def _run_sync(cmd: list[str]) -> None:
    """Запускает команду и кидает исключение при ненулевом коде возврата."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        raise RuntimeError(f"Command failed ({proc.returncode}): {cmd_str}\n{proc.stderr}")


def _ensure_path(p: str | Path) -> str:
    """Преобразует путь к строке для ffmpeg/ffprobe (поддержка Path)."""
    return str(Path(p))


# -------- публичные утилиты --------
def probe_video(path: str | Path) -> Tuple[int, int, float]:
    """
    Возвращает (width, height, fps) первого видеопотока.
    Использует ffprobe. Бросает RuntimeError при ошибке.
    """
    path = _ensure_path(path)
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json",
        path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{proc.stderr}")

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")
    st = streams[0]
    w = int(st.get("width") or 0)
    h = int(st.get("height") or 0)

    fr = st.get("r_frame_rate") or "25/1"
    if "/" in fr:
        num, den = fr.split("/", 1)
        fps = float(num) / (float(den) if float(den) != 0 else 1.0)
    else:
        fps = float(fr or 25.0)

    if w <= 0 or h <= 0:
        raise RuntimeError(f"Invalid probe result: width={w}, height={h}, fps={fps}")
    return w, h, fps


async def build_intro_from_image(
    image_path: str | Path,
    out_path: str | Path,
    *,
    width: int,
    height: int,
    duration: float = 0.8,
    fps: float = 25.0,
) -> None:
    """
    Делает короткий mp4 из картинки под нужный размер:
    - масштабирование с сохранением пропорций
    - паддинг до точного размера
    - yuv420p для совместимости
    """
    image_path = _ensure_path(image_path)
    out_path = _ensure_path(out_path)
    fps_i = max(1, int(round(fps)))

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p,fps={fps_i}"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-t", f"{max(0.05, float(duration))}",
        "-i", image_path,
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-r", f"{fps_i}",
        out_path,
    ]
    await asyncio.to_thread(_run_sync, cmd)


async def concat_two(
    intro_path: str | Path,
    video_path: str | Path,
    out_path: str | Path,
) -> None:
    """
    Склеивает два ролика (видео без аудио) последовательно.
    Переход — резкий. Для плавного см. concat_with_crossfade.
    """
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", intro_path,
        "-i", video_path,
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
        "-map", "[outv]",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        out_path,
    ]
    await asyncio.to_thread(_run_sync, cmd)


async def concat_with_crossfade(
    intro_path: str | Path,
    video_path: str | Path,
    out_path: str | Path,
    *,
    fade_duration: float = 0.4,
) -> None:
    """
    Склеивает с плавным переходом (кроссфейд) между концом интро и началом видео.
    Оба ролика должны иметь одинаковые параметры (лучше сформировать интро build_intro_from_image).
    """
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    # xfade считает время в секундах; первый клип должен быть длиннее fade_duration
    cmd = [
        "ffmpeg",
        "-y",
        "-i", intro_path,
        "-i", video_path,
        "-filter_complex",
        f"[0:v][1:v]xfade=transition=fade:duration={max(0.1, float(fade_duration))}:offset=prev_out,format=yuv420p[outv]",
        "-map", "[outv]",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        out_path,
    ]
    await asyncio.to_thread(_run_sync, cmd)
