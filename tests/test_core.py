"""Offline unit tests (no Discord, no network). Run: python -m pytest -q"""
import asyncio
from pathlib import Path

from bot.core.queue import TrackQueue
from bot.core.settings import GuildSettings
from bot.core.ytdl import Track, fmt_duration, looks_like_playlist, looks_like_url
from bot.cogs.media import classify, normalise


def t(name, d=100):
    return Track(title=name, webpage_url=f"https://x/{name}", duration=d)


def test_queue_basics():
    q = TrackQueue(max_size=3)
    assert q.add(t("a")) and q.add(t("b")) and q.add(t("c"))
    assert not q.add(t("d")) and q.is_full
    assert q.pop_next().title == "a"
    assert q.peek().title == "b"
    assert q.move(0, 1) and [x.title for x in q] == ["c", "b"]
    assert q.remove(5) is None and q.remove(0).title == "c"
    assert q.total_duration == 100
    q.clear(); assert q.is_empty


def test_settings_roundtrip_and_v1_compat(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"1": false, "2": true}')            # v1 format
    s = GuildSettings(p, media_default=True)
    assert s.media_enabled(1) is False and s.media_enabled(2) is True and s.media_enabled(3) is True
    s.set_media_enabled(3, False)
    s2 = GuildSettings(p)
    assert s2.media_enabled(3) is False and s2.media_enabled(1) is False


def test_helpers():
    assert fmt_duration(65) == "1:05" and fmt_duration(3600) == "1:00:00" and fmt_duration(0) == "live/unknown"
    assert looks_like_url("https://youtu.be/x") and not looks_like_url("never gonna")
    assert looks_like_playlist("https://www.youtube.com/playlist?list=PL1") and not looks_like_playlist("https://youtu.be/x")
    assert classify("https://x.com/a/status/1") == "twitter"
    assert classify("https://vm.tiktok.com/ZM1/") == "tiktok"
    assert classify("https://youtube.com/watch?v=1") is None
    assert normalise("https://fxtwitter.com/a/status/1).", "twitter") == "https://x.com/a/status/1"


def test_spotify_parse():
    from bot.core.spotify import parse, is_spotify
    assert parse("https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8?si=abc") == ("track", "4PTG3Z6ehGkBFwjybzWkR8")
    assert parse("https://open.spotify.com/intl-de/album/5ht7ItJgpBH7W6vJ5BqpPr") == ("album", "5ht7ItJgpBH7W6vJ5BqpPr")
    assert parse("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert not is_spotify("https://youtu.be/x")
