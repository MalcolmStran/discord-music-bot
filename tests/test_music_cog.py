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


# ------------------------------------------------- restoring persisted settings
class _Settings(dict):
    def get(self, guild_id, key, default=None):   # matches GuildSettings.get
        return dict.get(self, (guild_id, key), default)


class _Player:
    def __init__(self):
        from bot.core.player import LoopMode
        self.volume = 0.5
        self.loop_mode = LoopMode.OFF

    def set_volume(self, v):
        self.volume = max(0.0, min(2.0, v))


def restore(stored):
    """Drive Music._restore without building a real cog."""
    from bot.cogs.music import Music
    cog = Music.__new__(Music)
    cog.settings = _Settings(stored)
    p = _Player()
    Music._restore(cog, 1, p)
    return p


def test_restores_saved_volume_and_loop():
    from bot.core.player import LoopMode
    p = restore({(1, "volume"): 0.75, (1, "loop_mode"): "all"})
    assert p.volume == 0.75 and p.loop_mode is LoopMode.ALL


def test_missing_settings_leave_defaults():
    from bot.core.player import LoopMode
    p = restore({})
    assert p.volume == 0.5 and p.loop_mode is LoopMode.OFF


def test_corrupt_settings_do_not_break_player_creation():
    """A hand-edited settings file must not stop the guild's player from being created."""
    from bot.core.player import LoopMode
    p = restore({(1, "volume"): "loud", (1, "loop_mode"): "sideways"})
    assert p.volume == 0.5 and p.loop_mode is LoopMode.OFF


def test_restored_volume_is_clamped():
    p = restore({(1, "volume"): 99.0})
    assert p.volume <= 2.0
