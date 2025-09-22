# -*- coding: utf-8 -*-
from __future__ import annotations

"""
services/media_tools.py

Набор утилит для постпроцессинга видео:
- probe_video: получить (width, height, fps) видео через ffprobe
- probe_duration: получить длительность файла в секундах через ffprobe
- build_intro_from_image: сделать короткий mp4 из картинки нужного размера
- concat_two: склеить интро и основное видео без перехода (только видео)
- concat_with_crossfade: склеить с плавным переходом (кроссфейд), сохранить аудио из второго клипа (если есть)
- enforce_ar_no_bars: убрать чёрные поля (letterbox) кропом под заданное соотношение сторон и привести к точному размеру
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

# -------- настройки качества (можно переопределить в .env) --------
DEFAULT_CRF = int(os.getenv("VIDEO_CRF", "18"))
DEFAULT_PRESET = os.getenv("FFMPEG_PRESET", "slow").strip() or "slow"
LOG_CMD = os.getenv("FFMPEG_LOG_CMD", "0") in ("1", "true", "True", "YES", "yes")

# -------- поиск бинарников --------
def _ensure_path(p: str | Path) -> str:
    """Преобразует путь к строке для ffmpeg/ffprobe (поддержка Path)."""
    return str(Path(p))


def _normalize_env_path(value: str | None, name: str) -> str | None:
    """
    Нормализует путь из env:
    - Если указывает на файл — вернуть как есть
    - Если указывает на директорию — вернуть путь с добавленным бинарником
    - Если пусто/не существует — None
    """
    if not value:
        return None
    # Убираем возможные кавычки из .env
    p = Path(value.strip('"').strip("'"))
    if p.is_file():
        return str(p)
    if p.is_dir():
        # подставим имя бинарника (учтём .exe на Windows)
        bin_name = name
        if os.name == "nt" and not bin_name.lower().endswith(".exe"):
            bin_name += ".exe"
        candidate = p / bin_name
        if candidate.is_file():
            return str(candidate)
    return None


def _bin_path(name: str, env_var: str) -> str:
    """
    Возвращает путь к бинарнику name (ffmpeg/ffprobe).
    1) Берём из переменной окружения env_var (поддерживает путь к файлу или директории).
    2) Ищем в PATH через shutil.which.
    3) Пробуем типичные пути Windows.
    Если не найден — вернём просто имя (пусть subprocess попробует),
    а в случае ошибки дадим понятное сообщение.
    """
    # 1) Переменная окружения
    env_val = os.environ.get(env_var)
    p_env = _normalize_env_path(env_val, name)
    if p_env:
        return p_env

    # 2) PATH
    p2 = shutil.which(name)
    if p2:
        return p2
    if os.name == "nt" and not name.lower().endswith(".exe"):
        p2 = shutil.which(name + ".exe")
        if p2:
            return p2

    # 3) Типичные Windows пути
    if os.name == "nt":
        base_candidates = [
            r"C:\ffmpeg\bin",
            r"C:\Program Files\ffmpeg\bin",
            r"C:\Program Files (x86)\ffmpeg\bin",
        ]
        for base in base_candidates:
            candidate = Path(base) / (name if name.lower().endswith(".exe") else f"{name}.exe")
            if candidate.is_file():
                return str(candidate)

    return name  # даст шанс subprocess'у; в случае ошибки мы покажем понятный текст


def _ffprobe_path() -> str:
    return _bin_path("ffprobe", "FFPROBE_PATH")


def _ffmpeg_path() -> str:
    return _bin_path("ffmpeg", "FFMPEG_PATH")


# -------- внутренние синхронные helpers --------
def _run_sync(cmd: list[str]) -> None:
    """Запускает команду и кидает исключение при ненулевом коде возврата."""
    if LOG_CMD:
        print("[ffmpeg] CMD:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Executable not found: {cmd[0]}\n"
            f"Проверь .env (FFMPEG_PATH/FFPROBE_PATH) и доступность файла."
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
        )


def _run_probe(cmd: list[str]) -> subprocess.CompletedProcess:
    """Запускает команду probe (ffprobe) и возвращает процесс или кидает понятную ошибку."""
    if LOG_CMD:
        print("[ffprobe] CMD:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Executable not found: {cmd[0]}\n"
            f"Проверь .env (FFMPEG_PATH/FFPROBE_PATH) и доступность файла."
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr}")
    return proc


# -------- вспомогательные проверки --------
def _has_audio(path: str | Path) -> bool:
    """
    Возвращает True, если у файла есть хотя бы один аудиопоток.
    """
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(),
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json",
        path,
    ]
    proc = _run_probe(cmd)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    streams = (data.get("streams") or [])
    return len(streams) > 0


# -------- публичные утилиты --------
def probe_video(path: str | Path) -> Tuple[int, int, float]:
    """
    Возвращает (width, height, fps) первого видеопотока.
    Использует ffprobe. Бросает RuntimeError при ошибке.
    """
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
        "-of", "json",
        path,
    ]
    proc = _run_probe(cmd)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe returned invalid JSON: {e}")

    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")
    st = streams[0]
    w = int(st.get("width") or 0)
    h = int(st.get("height") or 0)

    # fps: попробуем r_frame_rate, затем avg_frame_rate
    fr = st.get("r_frame_rate") or st.get("avg_frame_rate") or "25/1"
    try:
        if "/" in fr:
            num_s, den_s = fr.split("/", 1)
            num = float(num_s)
            den = float(den_s) if float(den_s) != 0 else 1.0
            fps = num / den
        else:
            fps = float(fr or 25.0)
    except Exception:
        fps = 25.0

    if w <= 0 or h <= 0:
        raise RuntimeError(f"Invalid probe result: width={w}, height={h}, fps={fps}")
    return w, h, fps


def probe_duration(path: str | Path) -> float:
    """
    Возвращает длительность файла (в секундах) по ffprobe.
    Бросает RuntimeError при ошибке или нулевой длительности.
    """
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        path,
    ]
    proc = _run_probe(cmd)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe returned invalid JSON: {e}")

    dur = float((data.get("format") or {}).get("duration") or 0.0)
    if dur <= 0:
        raise RuntimeError("Could not determine media duration")
    return dur


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
    - качество: -crf 18, -preset slow (переопределяется переменными)
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
        _ffmpeg_path(),
        "-y",
        "-loop", "1",
        "-t", f"{max(0.05, float(duration))}",
        "-i", image_path,
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-crf", str(DEFAULT_CRF),
        "-preset", DEFAULT_PRESET,
        "-movflags", "+faststart",
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
    Качество: -crf 18, -preset slow.
    """
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    cmd = [
        _ffmpeg_path(),
        "-y",
        "-i", intro_path,
        "-i", video_path,
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
        "-map", "[outv]",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-crf", str(DEFAULT_CRF),
        "-preset", DEFAULT_PRESET,
        "-movflags", "+faststart",
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
    Если у второго клипа нет аудио — сохраняет только видео-дорожку.
    Оба ролика должны иметь одинаковые параметры (лучше сформировать интро build_intro_from_image).
    Качество: -crf 18, -preset slow.
    """
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    intro_dur = probe_duration(intro_path)
    fd = max(0.1, float(fade_duration))
    offset = max(0.0, intro_dur - fd)
    has_aud = _has_audio(video_path)

    # видео-кроссфейд через xfade
    video_chain = f"[0:v][1:v]xfade=transition=fade:duration={fd}:offset={offset},format=yuv420p[v]"

    if has_aud:
        # у второго клипа есть звук — берём его и задерживаем
        adelay_ms = int(round(offset * 1000))
        filter_complex = f"{video_chain};[1:a]adelay={adelay_ms}|{adelay_ms}[a]"
        cmd = [
            _ffmpeg_path(),
            "-y",
            "-i", intro_path,     # 0
            "-i", video_path,     # 1
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-crf", str(DEFAULT_CRF),
            "-preset", DEFAULT_PRESET,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-shortest",
            out_path,
        ]
    else:
        # звука нет — маппим только видео
        filter_complex = video_chain
        cmd = [
            _ffmpeg_path(),
            "-y",
            "-i", intro_path,
            "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-c:v", "libx264",
            "-crf", str(DEFAULT_CRF),
            "-preset", DEFAULT_PRESET,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-shortest",
            out_path,
        ]

    await asyncio.to_thread(_run_sync, cmd)


# -------- анти-рамки (удаление чёрных полос) --------
def enforce_ar_no_bars(src_path: str | Path, dst_path: str | Path, aspect: str) -> None:
    """
    Удаляет letterbox кропом под заданное соотношение сторон и
    ДОВОДИТ размер до точного целевого разрешения:
      - 16:9 → 1920x1080
      - 9:16 → 1080x1920

    Это гарантирует отсутствие чёрных полей в Telegram/мобильных плеерах,
    даже если исходник имеет нетипичную высоту (например, 1920x1088).

    Аудио (если есть) копируется без перекодирования.
    """
    src = _ensure_path(src_path)
    dst = _ensure_path(dst_path)
    has_aud = _has_audio(src)

    if aspect == "16:9":
        # Экранируем запятые в if()/gte() для ffmpeg (иначе они считаются разделителями аргументов фильтра).
        w_expr = r"floor(if(gte(iw/ih\,16/9)\,ih*16/9\,iw)/2)*2"
        h_expr = r"floor(if(gte(iw/ih\,16/9)\,ih\,iw*9/16)/2)*2"
        scale_part = "scale=1920:1080"
    elif aspect == "9:16":
        w_expr = r"floor(if(gte(iw/ih\,9/16)\,ih*9/16\,iw)/2)*2"
        h_expr = r"floor(if(gte(iw/ih\,9/16)\,ih\,iw*16/9)/2)*2"
        scale_part = "scale=1080:1920"
    else:
        raise ValueError("Unsupported aspect ratio. Use '16:9' or '9:16'.")

    # crop по расчётным выражениям → формат → финальный жёсткий scale
    vf = (
        f"crop={w_expr}:{h_expr}:(iw-{w_expr})/2:(ih-{h_expr})/2,"
        f"format=yuv420p,{scale_part}"
    )

    cmd = [
        _ffmpeg_path(),
        "-y",
        "-i", src,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", str(DEFAULT_CRF),
        "-preset", DEFAULT_PRESET,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

    if has_aud:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]

    cmd += [dst]
    _run_sync(cmd)
