"""ytdl helpers: shell quoting, header handling, format picking, error mapping."""
import shlex

import pytest

from bot.core.ytdl import (
    FFMPEG_BEFORE,
    Track,
    _audio_format,
    _clean_header,
    _friendly,
    _shq,
    fmt_duration,
    looks_like_playlist,
    looks_like_url,
)


@pytest.mark.parametrize("value", [
    "User-Agent: Mozilla/5.0 (X11)\r\n",
    "Cookie: a='b'; c=\"d\"\r\n",
    "X: back\\slash and 'quote'\r\n",
    "",
    "spaces   and\ttabs",
])
def test_shq_survives_shlex(value):
    """discord.py shlex-splits before_options, so a header blob has to come back byte for
    byte or ffmpeg gets a mangled (or extra) argument."""
    argv = shlex.split(f"{FFMPEG_BEFORE} -headers {_shq(value)}")
    assert argv[-1] == value
    assert argv[-2] == "-headers"


def test_shq_cannot_inject_an_extra_argument():
    argv = shlex.split("x " + _shq("a' -evil-flag '"))
    assert len(argv) == 2 and argv[1] == "a' -evil-flag '"


@pytest.mark.parametrize("raw,expected", [
    ("Mozilla/5.0", "Mozilla/5.0"),
    ("Mozilla\r\nX-Injected: 1", "MozillaX-Injected: 1"),
    ("  padded  ", "padded"),
])
def test_clean_header_strips_crlf(raw, expected):
    """Header values are concatenated into ffmpeg's -headers blob; a CR/LF inside one
    would start a new header line."""
    assert _clean_header(raw) == expected


def test_audio_format_prefers_the_audio_stream():
    """requested_formats[0] is the *video* half of a merged selection, so taking it
    blindly handed the audio player a video-only URL."""
    info = {"requested_formats": [
        {"acodec": "none", "vcodec": "avc1", "url": "VIDEO"},
        {"acodec": "opus", "vcodec": "none", "url": "AUDIO"},
    ]}
    assert _audio_format(info)["url"] == "AUDIO"


def test_audio_format_falls_back_to_the_first_entry():
    info = {"requested_formats": [{"vcodec": "avc1", "url": "ONLY"}]}
    assert _audio_format(info)["url"] == "ONLY"


def test_audio_format_handles_a_single_format():
    assert _audio_format({"url": "direct"}) is None


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: Private video. Sign in if you've been granted access", "That video is private."),
    ("ERROR: Video unavailable", "That video is unavailable."),
    ("ERROR: Sign in to confirm your age", "That video is age-restricted (cookies needed)."),
    ("ERROR: Unsupported URL: https://example.com/x", "Unsupported URL."),
    ("ERROR: [youtube] x: Sign in to confirm your age", "That video is age-restricted (cookies needed)."),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "That source requires a login (cookies needed)."),
    ("ERROR: Requested format is not available", "No playable audio format for that video."),
])
def test_friendly_known_cases(raw, expected):
    assert _friendly(raw) == expected


def test_friendly_strips_the_ytdlp_prefix_and_bounds_length():
    out = _friendly("ERROR: [youtube] dQw4w9WgXcQ: " + "x" * 500)
    assert not out.startswith("ERROR")
    assert len(out) <= 200


def test_friendly_never_returns_empty():
    assert _friendly("") and _friendly("ERROR: ")


@pytest.mark.parametrize("q,is_url,is_playlist", [
    ("https://youtu.be/x", True, False),
    ("https://www.youtube.com/playlist?list=PL1", True, True),
    ("https://www.youtube.com/watch?v=a&list=PL1", True, True),
    ("https://soundcloud.com/u/sets/mix", True, True),
    ("never gonna give you up", False, False),
    ("  https://x/  ", True, False),
])
def test_url_shape_helpers(q, is_url, is_playlist):
    assert looks_like_url(q) is is_url
    assert looks_like_playlist(q) is is_playlist


@pytest.mark.parametrize("secs,text", [
    (None, "live/unknown"), (0, "live/unknown"), (9, "0:09"),
    (65, "1:05"), (600, "10:00"), (3661, "1:01:01"),
])
def test_fmt_duration(secs, text):
    assert fmt_duration(secs) == text


def test_track_link_prefers_the_webpage_url():
    assert Track(title="t", webpage_url="https://y/1", source_url="https://s/1").link == "https://y/1"
    assert Track(title="t", webpage_url="", source_url="https://s/1").link == "https://s/1"
    assert Track(title="t", webpage_url="").link == ""
