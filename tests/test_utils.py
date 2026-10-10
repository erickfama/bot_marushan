import pytest

from src.utils import format_time, match_score, parse_time


@pytest.mark.parametrize(("value", "expected"), [("90", 90), ("1:30", 90), ("1:02:03", 3723)])
def test_parse_time(value: str, expected: int) -> None:
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "1:60", "1:2:99", "1:2:3:4"])
def test_parse_time_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_time(value)


def test_format_time() -> None:
    assert format_time(90_000) == "1:30"
    assert format_time(3_723_000) == "1:02:03"


def test_spotify_match_prefers_same_song_and_duration() -> None:
    exact = match_score("Creep", "Radiohead", 238_000, "Creep", "Radiohead", 239_000)
    wrong = match_score("Creep", "Radiohead", 238_000, "No Surprises", "Radiohead", 229_000)
    assert exact > 0.9
    assert exact > wrong


def test_spotify_match_accepts_platform_noise_and_accents() -> None:
    score = match_score(
        "Canción",
        "Artista",
        200_000,
        "CANCION (Official Audio)",
        "Artista - Topic",
        201_000,
    )
    assert score > 0.9
