from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def parse_time(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if not re.fullmatch(r"\d{1,2}(?::\d{1,2}){1,2}", value):
        raise ValueError("Usa segundos, MM:SS o HH:MM:SS")
    parts = [int(part) for part in value.split(":")]
    if any(part >= 60 for part in parts[1:]):
        raise ValueError("Los minutos y segundos deben ser menores a 60")
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def normalize_text(value: str) -> str:
    # Keep accented titles comparable with YouTube variants ("canción" and
    # "cancion") instead of deleting the accented character entirely.
    ascii_value = unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", ascii_value).split())


def match_score(title: str, author: str, duration_ms: int, candidate_title: str, candidate_author: str, candidate_duration_ms: int) -> float:
    noise = {
        "audio",
        "official",
        "video",
        "lyrics",
        "lyric",
        "visualizer",
        "topic",
        "hq",
        "hd",
    }

    def similarity(wanted_value: str, found_value: str) -> float:
        wanted_normalized = normalize_text(wanted_value)
        found_normalized = normalize_text(found_value)
        wanted_tokens = set(wanted_normalized.split()) - noise
        found_tokens = set(found_normalized.split()) - noise
        if not wanted_tokens or not found_tokens:
            return 0.0
        coverage = len(wanted_tokens & found_tokens) / len(wanted_tokens)
        sequence = SequenceMatcher(None, wanted_normalized, found_normalized).ratio()
        return max(coverage, sequence)

    title_score = similarity(title, candidate_title)
    author_score = similarity(author, f"{candidate_author} {candidate_title}")
    duration_delta = abs(duration_ms - candidate_duration_ms)
    duration_score = max(0.0, 1.0 - duration_delta / 30_000) if duration_ms and candidate_duration_ms else 0.5
    return title_score * 0.55 + author_score * 0.25 + duration_score * 0.20
