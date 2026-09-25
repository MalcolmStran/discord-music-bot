"""Offline unit tests (no Discord, no network). Run: python -m pytest"""
from pathlib import Path

import pytest

from bot.cogs.media import EMBED_FIXERS, classify, is_embed_fixer, normalise
from bot.core.queue import TrackQueue
from bot.core.settings import GuildSettings
from bot.core.spotify import is_spotify, parse
from bot.core.ytdl import Track, fmt_duration, looks_like_playlist, looks_like_url


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
    q.clear()
    assert q.is_empty


def test_queue_index_edges():
    """remove()/move() take raw user input (position-1), so negatives must not wrap."""
    q = TrackQueue(max_size=5)
    q.extend([t("a"), t("b"), t("c")])
    assert q.remove(-1) is None            # would otherwise delete the last track
    assert q.move(-1, 0) is False
    assert q.move(0, 99) is False
    assert [x.title for x in q] == ["a", "b", "c"]
    assert q.extend([t("d"), t("e"), t("f")]) == 2    # stops at max_size
    assert len(q) == 5


def test_queue_history_records_played():
    q = TrackQueue(max_size=3, history_size=2)
    q.extend([t("a"), t("b"), t("c")])
    q.pop_next(), q.pop_next(), q.pop_next()
    assert [x.title for x in q.history] == ["b", "c"]     # bounded


def test_settings_roundtrip_and_v1_compat(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"1": false, "2": true}')            # v1 format
    s = GuildSettings(p, media_default=True)
    assert s.media_enabled(1) is False and s.media_enabled(2) is True and s.media_enabled(3) is True
    s.set_media_enabled(3, False)
    s2 = GuildSettings(p)
    assert s2.media_enabled(3) is False and s2.media_enabled(1) is False


@pytest.mark.parametrize("body", ["[1, 2, 3]", '"a string"', "42", "not json at all", ""])
def test_settings_survives_a_bad_file(tmp_path: Path, body: str):
    """A valid-JSON-but-wrong-shape file used to raise AttributeError inside __init__ and
    take the whole bot down at startup."""
    p = tmp_path / "s.json"
    p.write_text(body)
    s = GuildSettings(p, media_default=True)
    assert s.media_enabled(1) is True                   # fell back to the default
    assert list(tmp_path.glob("s.corrupt-*.json"))      # the old file was kept


def test_settings_keeps_every_corrupt_copy(tmp_path: Path):
    """Every rescue copy must survive. A fixed name clobbered the previous one; a
    second-resolution timestamp alone still collided for failures inside one second, which
    a `>= 1` assertion happily accepted while 3 of 4 backups were being destroyed."""
    p = tmp_path / "s.json"
    rounds = 4
    for i in range(rounds):
        p.write_text(f"{{{{{{ {i}")
        GuildSettings(p)
    kept = sorted(tmp_path.glob("s.corrupt-*.json"))
    assert len(kept) == rounds, f"expected {rounds} rescue copies, kept {len(kept)}"
    assert len({q.read_text() for q in kept}) == rounds, "rescue copies overwrote each other"


def test_settings_forget_guild(tmp_path: Path):
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    s.set_media_enabled(1, False)
    s.set_media_enabled(2, False)
    s.forget_guild(1)
    assert set(GuildSettings(p).all()) == {2}


def test_helpers():
    assert fmt_duration(65) == "1:05" and fmt_duration(3600) == "1:00:00" and fmt_duration(0) == "live/unknown"
    assert looks_like_url("https://youtu.be/x") and not looks_like_url("never gonna")
    assert looks_like_playlist("https://www.youtube.com/playlist?list=PL1") and not looks_like_playlist("https://youtu.be/x")


def test_spotify_parse():
    assert parse("https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8?si=abc") == ("track", "4PTG3Z6ehGkBFwjybzWkR8")
    assert parse("https://open.spotify.com/intl-de/album/5ht7ItJgpBH7W6vJ5BqpPr") == ("album", "5ht7ItJgpBH7W6vJ5BqpPr")
    assert parse("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert not is_spotify("https://youtu.be/x")


# --------------------------------------------------------------- media links
@pytest.mark.parametrize("url,kind", [
    ("https://x.com/a/status/1", "twitter"),
    ("https://twitter.com/a/status/1?s=20", "twitter"),
    ("https://mobile.twitter.com/a/status/1", "twitter"),
    ("https://X.COM/a/status/1", "twitter"),
    ("https://x.com:443/a/status/1", "twitter"),
    ("https://vm.tiktok.com/ZM1/", "tiktok"),
    ("https://www.tiktok.com/@u/video/1", "tiktok"),
    ("https://youtube.com/watch?v=1", None),
    ("https://x.com.evil.com/a", None),
    ("https://evilx.com/a", None),
    ("not a url", None),
])
def test_classify(url, kind):
    assert classify(url) == kind


@pytest.mark.parametrize("url", [
    "https://evil.com#.x.com/",
    "https://127.0.0.1#.x.com/",
    "https://evil.com?a=.tiktok.com/",
    "https://169.254.169.254/#.x.com",
    "file:///etc/passwd#.x.com",
    "ftp://x.com/a",
])
def test_classify_rejects_host_smuggling(url):
    """The old splitter only cut at "/" and ":", so a fragment or query could smuggle an
    allowlisted suffix past the check and get an arbitrary URL handed to yt-dlp."""
    assert classify(url) is None


@pytest.mark.parametrize("raw,expected", [
    ("https://fxtwitter.com/a/status/1).", "https://x.com/a/status/1"),
    ("https://vxtwitter.com/a/1", "https://x.com/a/1"),
    ("https://fixupx.com/a/1", "https://x.com/a/1"),
    ("https://x.com/a/status/1||", "https://x.com/a/status/1"),      # ||spoiler||
    ("https://x.com/a/status/1,", "https://x.com/a/status/1"),
    ("https://x.com/a/1?s=20", "https://x.com/a/1?s=20"),            # query is preserved
])
def test_normalise(raw, expected):
    assert normalise(raw, "twitter") == expected


# ----------------------------------------------------- persisted guild settings
def test_volume_and_loop_round_trip(tmp_path: Path):
    """They used to be in-memory only, so every restart silently reset them."""
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    s.set(42, "volume", 0.75)
    s.set(42, "loop_mode", "all")
    reloaded = GuildSettings(p)
    assert reloaded.get(42, "volume") == 0.75
    assert reloaded.get(42, "loop_mode") == "all"
    assert reloaded.get(99, "volume") is None      # untouched guilds keep the default


def test_settings_reserves_guild_zero_for_bookkeeping(tmp_path: Path):
    """The bot stores its own state (the slash-command signature) under guild id 0."""
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    s.set(0, "command_signature", "abc")
    s.set(1, "media_enabled", False)
    assert GuildSettings(p).get(0, "command_signature") == "abc"
    assert GuildSettings(p).media_enabled(1) is False


# ------------------------------------------------- embed-fixer links are left alone
@pytest.mark.parametrize("url", [
    "https://fxtwitter.com/a/status/1",
    "https://vxtwitter.com/a/status/1",
    "https://fixupx.com/a/status/1",
    "https://fixvx.com/a/status/1",
    "https://twittpr.com/a/status/1",
    "https://d.fxtwitter.com/a/status/1",       # the direct-media subdomain
    "https://www.vxtwitter.com/a/status/1",
    "https://vxtiktok.com/@u/video/1",
    "https://tnktok.com/@u/video/1",
])
def test_embed_fixers_are_recognised(url):
    """These front-ends already render a playable embed, so auto-converting them would post
    the same clip twice under the message."""
    assert is_embed_fixer(url) is True
    assert classify(url) is not None, "still classified, so an explicit /convert works"


@pytest.mark.parametrize("url", [
    "https://x.com/a/status/1",
    "https://twitter.com/a/status/1",
    "https://www.tiktok.com/@u/video/1",
    "https://vm.tiktok.com/ZM1/",      # official shortener, NOT a fixer
    "https://vt.tiktok.com/ZS1/",      # official shortener, NOT a fixer
])
def test_real_posts_are_not_treated_as_fixers(url):
    assert is_embed_fixer(url) is False
    assert classify(url) is not None


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=1",
    "https://evil.com#.fxtwitter.com/",     # the host-smuggling shape
    "https://fxtwitter.com.evil.com/a",     # suffix, not the domain
    "ftp://fxtwitter.com/a",
    "not a url",
])
def test_fixer_check_is_not_foolable(url):
    assert is_embed_fixer(url) is False


def test_every_fixer_is_also_a_supported_domain():
    """A fixer that classify() does not recognise would make /convert reject it outright."""
    for domain in EMBED_FIXERS:
        assert classify(f"https://{domain}/a/status/1") is not None


@pytest.mark.parametrize("raw,expected", [
    ("https://fixvx.com/a/status/1", "https://x.com/a/status/1"),
    ("https://twittpr.com/a/status/1", "https://x.com/a/status/1"),
    ("https://vxtiktok.com/@u/video/1", "https://www.tiktok.com/@u/video/1"),
    ("https://tnktok.com/@u/video/1", "https://www.tiktok.com/@u/video/1"),
])
def test_normalise_rewrites_the_newer_fixers(raw, expected):
    """An explicit /convert on a fixer link must still reach the real site."""
    assert normalise(raw, classify(raw)) == expected


# ----------------------------------------------- per-user auto-convert opt-out
def test_media_optout_round_trip(tmp_path: Path):
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    assert s.is_media_optout(42) is False
    s.set_media_optout(42, True)
    s.set_media_optout(99, True)
    assert GuildSettings(p).media_optout() == {42, 99}
    s.set_media_optout(42, False)
    assert GuildSettings(p).media_optout() == {99}


def test_media_optout_is_idempotent(tmp_path: Path):
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    for _ in range(3):
        s.set_media_optout(7, True)
    assert s.media_optout() == {7}
    for _ in range(3):
        s.set_media_optout(7, False)
    assert s.media_optout() == set()


def test_media_optout_does_not_disturb_guild_settings(tmp_path: Path):
    """The opt-out lives under the reserved guild id 0, so it must not collide with a
    real guild's media_enabled flag."""
    p = tmp_path / "s.json"
    s = GuildSettings(p)
    s.set_media_enabled(1, False)
    s.set_media_optout(42, True)
    reloaded = GuildSettings(p)
    assert reloaded.media_enabled(1) is False
    assert reloaded.media_optout() == {42}
    assert reloaded.media_enabled(42) is True, "a user id must not be read as a guild id"


def test_media_optout_survives_a_hand_mangled_list(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"0": {"media_optout": [1, "2", null, "abc", 3.9, [], {}]}}')
    assert GuildSettings(p).media_optout() == {1, 2, 3}


def test_media_optout_defaults_to_empty_when_absent(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"0": {"command_signature": "abc"}}')
    assert GuildSettings(p).media_optout() == set()
    assert GuildSettings(p).is_media_optout(1) is False
