"""Pure helpers from the music cog (no Discord objects needed)."""
from bot.cogs.music import split_too_long
from bot.core.ytdl import Track

MAX = 7200


def t(name, duration):
    return Track(title=name, webpage_url=f"https://y/{name}", duration=duration)


def test_splits_by_duration():
    playable, too_long = split_too_long([t("short", 100), t("long", 99999)], MAX)
    assert [x.title for x in playable] == ["short"]
    assert [x.title for x in too_long] == ["long"]


def test_unknown_duration_is_allowed_through():
    """Livestreams report duration 0/None; they must not be filtered out as 'too long'."""
    playable, too_long = split_too_long([t("live", 0)], MAX)
    assert [x.title for x in playable] == ["live"] and not too_long


def test_duplicates_are_preserved():
    """The old filter was `[t for t in tracks if t not in too_long]` — value equality over
    a mutable dataclass, quadratic in the playlist size."""
    dup = [t("same", 100), t("same", 100), t("same", 100)]
    playable, too_long = split_too_long(dup, MAX)
    assert len(playable) == 3 and not too_long


def test_order_is_preserved():
    tracks = [t("a", 10), t("big", 99999), t("b", 20), t("c", 30)]
    playable, too_long = split_too_long(tracks, MAX)
    assert [x.title for x in playable] == ["a", "b", "c"]
    assert [x.title for x in too_long] == ["big"]


def test_boundary_is_inclusive():
    playable, too_long = split_too_long([t("exact", MAX), t("over", MAX + 1)], MAX)
    assert [x.title for x in playable] == ["exact"]
    assert [x.title for x in too_long] == ["over"]


def test_empty_input():
    assert split_too_long([], MAX) == ([], [])


def test_scales_to_a_large_playlist():
    tracks = [t(f"s{i}", 100) for i in range(2000)] + [t(f"l{i}", 99999) for i in range(2000)]
    playable, too_long = split_too_long(tracks, MAX)
    assert len(playable) == 2000 and len(too_long) == 2000
