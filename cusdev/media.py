"""Файлы: хэш, длительность, нормализация аудио в .m4a (ARCHITECTURE.md, Р8).

ffmpeg есть только в Docker-образе на VPS. Локально (на Маке его нет) аудио сохраняется
как есть — MacWhisper всё равно читает m4a/mp3/wav/ogg.
"""

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".oga", ".aac", ".mp4"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".heic"}
TEXT_EXT = {".txt"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Хэш вставленного текста: без учёта пробелов по краям и переносов строк."""
    return sha256_bytes(" ".join(text.split()).encode())


def kind_by_ext(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    return None


def probe_duration(path: Path) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        return round(float(json.loads(proc.stdout)["format"]["duration"]))
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def normalize_audio(src: Path, dst_stem: Path) -> Path:
    """Перекодирует в моно .m4a рядом с исходником. Без ffmpeg — возвращает исходник."""
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg не найден, аудио остаётся как есть: %s", src.name)
        return src
    dst = dst_stem.with_suffix(".m4a")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-c:a", "aac",
         "-b:a", "64k", str(dst)],
        capture_output=True,
        text=True,
        timeout=600,
    )  # fmt: skip
    if proc.returncode != 0:
        raise ValueError(f"не удалось прочитать аудио: {proc.stderr.strip()[:300]}")
    return dst
