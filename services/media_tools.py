# -*- coding: utf-8 -*-
from __future__ import annotations

"""
services/media_tools.py

Набор утилит для постпроцессинга видео:
- probe_video: получить (width, height, fps) видео через ffprobe
- probe_duration: получить длительность файла в секундах через ffprobe
- build_intro_from_image: сделать короткий mp4 из картинки нужного размера (cover: без паддингов)
- concat_two: склеить интро и основное видео без перехода (только видео)
- concat_with_crossfade: склеить с плавным переходом (кроссфейд), сохранить аудио из второго клипа (если есть)
- enforce_ar_no_bars: нормализация без чёрных полос (включая «впаянные» letterbox)
- build_vertical_blurpad: вертикальный 1080x1920 с размытой подложкой (как Reels/TikTok)
"""

import asyncio
import json
import os
import shutil
import subprocess
import math
from pathlib import Path
from typing import Tuple, Optional

# -------- настройки качества (можно переопределить в .env) --------
DEFAULT_CRF = int(os.getenv("VIDEO_CRF", "18"))
DEFAULT_PRESET = os.getenv("FFMPEG_PRESET", "slow").strip() or "slow"
LOG_CMD = os.getenv("FFMPEG_LOG_CMD", "0") in ("1", "true", "True", "YES", "yes")

# -------- утилиты --------
def _ratio_str(w: int, h: int) -> str:
    """Вернёт сокращённую строку вида '16/9' для setdar."""
    g = math.gcd(max(1, int(w)), max(1, int(h)))
    return f"{int(w)//g}/{int(h)//g}"

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
    p = Path(value.strip('"').strip("'"))
    if p.is_file():
        return str(p)
    if p.is_dir():
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
    Если не найден — возвращаем просто имя (пусть subprocess попробует),
    а в случае ошибки дадим понятное сообщение.
    """
    env_val = os.environ.get(env_var)
    p_env = _normalize_env_path(env_val, name)
    if p_env:
        return p_env

    p2 = shutil.which(name)
    if p2:
        return p2
    if os.name == "nt" and not name.lower().endswith(".exe"):
        p2 = shutil.which(name + ".exe")
        if p2:
            return p2

    if os.name == "nt":
        for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin", r"C:\Program Files (x86)\ffmpeg\bin"):
            candidate = Path(base) / (name if name.lower().endswith(".exe") else f"{name}.exe")
            if candidate.is_file():
                return str(candidate)

    return name  # даст шанс subprocess'у; в случае ошибки покажем понятный текст

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
            f"Executable not found: {cmd[0]}\nПроверь .env (FFMPEG_PATH/FFPROBE_PATH) и доступность файла."
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")

def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """Запуск команды с возвратом stdout/stderr (для cropdetect и т.п.)."""
    if LOG_CMD:
        print("[ffmpeg-capture] CMD:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Executable not found: {cmd[0]}\nПроверь .env (FFMPEG_PATH/FFPROBE_PATH) и доступность файла."
        ) from e
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr}")
    return proc

def _run_probe(cmd: list[str]) -> subprocess.CompletedProcess:
    """Запускает команду probe (ffprobe)."""
    if LOG_CMD:
        print("[ffprobe] CMD:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Executable not found: {cmd[0]}\nПроверь .env (FFMPEG_PATH/FFPROBE_PATH) и доступность файла."
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr}")
    return proc

# -------- вспомогательные проверки --------
def _has_audio(path: str | Path) -> bool:
    """True, если у файла есть хотя бы один аудиопоток."""
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(), "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json", path,
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
    """Возвращает (width, height, fps) первого видеопотока по ffprobe."""
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
        "-of", "json", path,
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
    """Возвращает длительность файла (в секундах) по ffprobe."""
    path = _ensure_path(path)
    cmd = [
        _ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
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
    Короткий mp4 из картинки с cover-кропом под точные размеры (без паддингов).
    Фиксируем SAR/DAR, используем yuv420p.
    """
    image_path = _ensure_path(image_path)
    out_path = _ensure_path(out_path)
    fps_i = max(1, int(round(fps)))
    dar = _ratio_str(width, height)

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"setsar=1,setdar={dar},"
        f"fps={fps_i},format=yuv420p"
    )

    cmd = [
        _ffmpeg_path(), "-y",
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

async def concat_two(intro_path: str | Path, video_path: str | Path, out_path: str | Path) -> None:
    """Склейка двух роликов без перехода (только видео)."""
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    cmd = [
        _ffmpeg_path(), "-y",
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
    """Склейка с кроссфейдом. Если у второго клипа есть звук — переносим его."""
    intro_path = _ensure_path(intro_path)
    video_path = _ensure_path(video_path)
    out_path = _ensure_path(out_path)

    intro_dur = probe_duration(intro_path)
    fd = max(0.1, float(fade_duration))
    offset = max(0.0, intro_dur - fd)
    has_aud = _has_audio(video_path)

    video_chain = f"[0:v][1:v]xfade=transition=fade:duration={fd}:offset={offset},format=yuv420p[v]"

    if has_aud:
        adelay_ms = int(round(offset * 1000))
        filter_complex = f"{video_chain};[1:a]adelay={adelay_ms}|{adelay_ms}[a]"
        cmd = [
            _ffmpeg_path(), "-y",
            "-i", intro_path, "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
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
        cmd = [
            _ffmpeg_path(), "-y",
            "-i", intro_path, "-i", video_path,
            "-filter_complex", video_chain,
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

# -------- анти-рамки (детект + удаление «впаянных» чёрных полос) --------
def _parse_crop_from_stderr(stderr: str) -> Tuple[int, int, int, int] | None:
    """
    Парсит последнюю подсказку 'crop=w:h:x:y' из stderr ffmpeg (cropdetect).
    Возвращает (w, h, x, y) либо None.
    """
    last = None
    for line in (stderr or "").splitlines():
        line = line.strip()
        if "crop=" in line:
            idx = line.rfind("crop=")
            seg = line[idx + 5:].strip()
            parts = seg.split(":")
            if len(parts) >= 4:
                try:
                    cw = int(parts[0]); ch = int(parts[1]); cx = int(parts[2]); cy = int(parts[3])
                    last = (cw, ch, cx, cy)
                except Exception:
                    pass
    return last

def _detect_letterbox_crop(src_path: str | Path, *, sample_frames: int = 120) -> Optional[Tuple[int, int, int, int]]:
    """
    Прогоняет cropdetect на первых N кадрах и возвращает подсказку (w,h,x,y),
    если реально есть «впаянные» чёрные поля (>~2% площади).
    """
    src = _ensure_path(src_path)
    iw, ih, _ = probe_video(src)

    # cropdetect логирует в stderr (info). limit=24 — порог чувствительности.
    cmd = [
        _ffmpeg_path(), "-hide_banner", "-v", "info",
        "-i", src,
        "-vf", "cropdetect=limit=24:round=2:reset=0",
        "-frames:v", str(max(30, int(sample_frames))),
        "-f", "null", "-"
    ]
    proc = _run_capture(cmd)
    hint = _parse_crop_from_stderr(proc.stderr or "")
    if not hint:
        return None
    cw, ch, cx, cy = hint

    # подсказка почти равна исходнику — считаем, что полос нет
    area_ratio = (cw * ch) / float(iw * ih)
    if area_ratio >= 0.98:  # <2% экономии — игнорируем
        return None
    if cw <= 0 or ch <= 0 or cw > iw or ch > ih:
        return None
    return cw, ch, cx, cy

def _even(val: int) -> int:
    return val if val % 2 == 0 else val - 1

def enforce_ar_no_bars(src_path: str | Path, dst_path: str | Path, aspect: str) -> None:
    """
    Нормализация кадра без рамок:
      1) если внутри есть letterbox — предварительно вырежем его (cropdetect),
      2) затем cover+crop к целевым размерам и фиксация DAR/SAR.
    16:9 -> 1920x1080, DAR=16/9; 9:16 -> 1080x1920, DAR=9/16.
    Аудио копируем как есть. Работает на любых сборках FFmpeg.
    """
    src = _ensure_path(src_path)
    dst = _ensure_path(dst_path)
    has_aud = _has_audio(src)

    if aspect == "9:16":
        target_w, target_h = 1080, 1920
        dar = "9/16"
    else:
        target_w, target_h = 1920, 1080
        dar = "16/9"

    # 1) авто-кроп «впаянных» полос (если есть)
    pre_crop = ""
    try:
        hint = _detect_letterbox_crop(src)
    except Exception:
        hint = None
    if hint:
        cw, ch, cx, cy = hint
        pre_crop = f"crop={cw}:{ch}:{cx}:{cy},"

    # 2) нормализация под целевой AR
    vf = (
        f"{pre_crop}"
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"setsar=1,setdar={dar},format=yuv420p"
    )

    cmd = [
        _ffmpeg_path(), "-y",
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

def build_vertical_blurpad(src_path: str | Path, dst_path: str | Path) -> None:
    """
    Формирует вертикальный ролик 1080x1920 с размытым фоном (TikTok/Reels style).
    Перед тем, как собирать фон/фореграунд, вырезает «впаянные» чёрные полосы (cropdetect).
    """
    src = _ensure_path(src_path)
    dst = _ensure_path(dst_path)
    has_aud = _has_audio(src)

    crop_stage = ""
    try:
        hint = _detect_letterbox_crop(src)
    except Exception:
        hint = None
    if hint:
        cw, ch, cx, cy = hint
        crop_stage = f"crop={cw}:{ch}:{cx}:{cy},"

    filter_complex = (
        f"[0:v]{crop_stage}scale=1080:1920,boxblur=20:1[bg];"
        f"[0:v]{crop_stage}scale=-2:1920[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,setdar=9/16,format=yuv420p[vout]"
    )

    cmd = [
        _ffmpeg_path(), "-y",
        "-i", src,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264",
        "-crf", str(DEFAULT_CRF),
        "-preset", DEFAULT_PRESET,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if has_aud:
        cmd += ["-map", "0:a?", "-c:a", "copy"]
    else:
        cmd += ["-an"]
    cmd += [dst]
    _run_sync(cmd)
