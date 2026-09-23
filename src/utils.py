from __future__ import annotations

import re


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
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", value.casefold()).split())


def match_score(title: str, author: str, duration_ms: int, candidate_title: str, candidate_author: str, candidate_duration_ms: int) -> float:
    wanted = set(normalize_text(f"{title} {author}").split())
    found = set(normalize_text(f"{candidate_title} {candidate_author}").split())
    text_score = len(wanted & found) / max(1, len(wanted | found))
    duration_delta = abs(duration_ms - candidate_duration_ms)
    duration_score = max(0.0, 1.0 - duration_delta / 30_000) if duration_ms and candidate_duration_ms else 0.5
    return text_score * 0.75 + duration_score * 0.25
