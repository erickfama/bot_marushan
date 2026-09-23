from src.youtube import YoutubeStreamResolver


def test_recognizes_youtube_urls() -> None:
    assert YoutubeStreamResolver.is_youtube_url("https://www.youtube.com/watch?v=abc")
    assert YoutubeStreamResolver.is_youtube_url("https://youtu.be/abc")
    assert YoutubeStreamResolver.is_youtube_url("https://music.youtube.com/watch?v=abc")


def test_does_not_rewrite_direct_audio_urls() -> None:
    assert not YoutubeStreamResolver.is_youtube_url("https://cdn.example.com/audio.opus")
    assert not YoutubeStreamResolver.is_youtube_url(None)


def test_removes_radio_mix_from_video_url() -> None:
    url = "https://www.youtube.com/watch?v=mQ_hn6YKx5M&list=RDmQ_hn6YKx5M&start_radio=1&index=2"
    assert YoutubeStreamResolver.without_radio(url) == "https://www.youtube.com/watch?v=mQ_hn6YKx5M"


def test_keeps_explicit_playlist_and_normal_video_urls() -> None:
    playlist = "https://www.youtube.com/playlist?list=PL123"
    video = "https://youtu.be/abc123?t=30"
    assert YoutubeStreamResolver.without_radio(playlist) == playlist
    assert YoutubeStreamResolver.without_radio(video) == video
