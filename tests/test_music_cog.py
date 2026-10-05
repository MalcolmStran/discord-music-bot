"""Helpers and command output of the music cog (no Discord connection needed)."""
import logging
import types

from discord.ext import commands
from discord.ext.commands.view import StringView

from bot.cogs.music import Music, split_too_long
from bot.core.player import GuildPlayer
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
    """Uses GuildPlayer's REAL set_volume. Re-implementing the clamp here meant the clamp
    test asserted on the stand-in and a broken GuildPlayer.set_volume went unnoticed."""

    from bot.core.player import GuildPlayer as _GP

    set_volume = _GP.set_volume

    def __init__(self):
        from bot.core.player import LoopMode
        self.volume = 0.5
        self.loop_mode = LoopMode.OFF
        self._source = None


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
    """Exercises GuildPlayer.set_volume itself."""
    p = restore({(1, "volume"): 99.0})
    assert p.volume == 2.0


def test_restored_negative_volume_is_clamped():
    p = restore({(1, "volume"): -5.0})
    assert p.volume == 0.0


# ------------------------------------------------- command output (real Music commands)
class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content if content is not None else kw.get("embed"))


def _cog_and_player():
    """The real Music cog and a real GuildPlayer, with no voice connection."""
    guild = types.SimpleNamespace(id=1, name="g", voice_client=None, get_member=lambda uid: None)
    player = GuildPlayer(None, guild, None, max_queue=50, default_volume=0.5, idle_seconds=300)
    cog = Music.__new__(Music)
    cog.players = {guild.id: player}
    sink = _Sink()
    ctx = types.SimpleNamespace(guild=guild, send=sink.send, prefix="!", command=None)
    return cog, player, ctx, sink


async def test_nowplaying_shows_the_track_being_resolved_not_the_finished_one():
    """While the next stream resolves, `current` is still the track that just ended, so
    /nowplaying reported it as "Now playing" with a stale progress bar."""
    cog, player, ctx, sink = _cog_and_player()
    player.current, player._loading = t("finished", 100), t("next up", 100)
    await Music.nowplaying.callback(cog, ctx)
    assert isinstance(sink.sent[-1], str), "no now-playing embed for a track that has ended"
    assert "Loading" in sink.sent[-1] and "next up" in sink.sent[-1]
    assert "finished" not in sink.sent[-1]


async def test_nowplaying_during_the_first_resolve_is_not_nothing_playing():
    cog, player, ctx, sink = _cog_and_player()
    player._loading = t("first", 100)
    await Music.nowplaying.callback(cog, ctx)
    assert "Loading" in sink.sent[-1] and "first" in sink.sent[-1]


async def test_queue_lists_the_track_being_resolved():
    """It had left the queue and was not `current`, so it appeared nowhere."""
    cog, player, ctx, sink = _cog_and_player()
    player.current, player._loading = t("finished", 100), t("next up", 100)
    player.queue.extend([t("later", 100)])
    await Music.queue.callback(cog, ctx, page=1)
    embed = sink.sent[-1]
    assert not isinstance(embed, str)
    top = embed.fields[0]
    assert "Loading" in top.name and "next up" in top.value
    assert all("finished" not in f.value for f in embed.fields)
    assert "later" in embed.description


async def test_queue_during_the_first_resolve_is_not_empty():
    cog, player, ctx, sink = _cog_and_player()
    player._loading = t("first", 100)
    await Music.queue.callback(cog, ctx, page=1)
    assert sink.sent[-1] != "Queue is empty."
    assert "first" in sink.sent[-1].fields[0].value


async def test_remove_escapes_markdown_in_the_title(monkeypatch):
    cog, player, ctx, sink = _cog_and_player()

    async def same_channel(ctx, player):
        return True

    monkeypatch.setattr(cog, "_require_same_channel", same_channel)
    player.queue.extend([t("**LIVE** ||spoiler|| __2024__", 100)])
    await Music.remove.callback(cog, ctx, position=1)
    assert sink.sent[-1] == "🗑️ Removed **\\*\\*LIVE\\*\\* \\|\\|spoiler\\|\\| \\_\\_2024\\_\\_**."


async def test_volume_reads_back_the_level_that_was_set():
    """`/volume 29` stored 0.29 and a bare `/volume` then said 28% (int truncation)."""
    cog, player, ctx, sink = _cog_and_player()
    player.set_volume(29 / 100)
    await Music.volume.callback(cog, ctx, level=None)
    assert sink.sent[-1] == "🔊 Volume: 29%"


async def test_an_unbalanced_quote_gets_the_argument_hint_not_an_internal_error(caplog):
    """`!remove "1` raises ExpectedClosingQuoteError, a UserInputError but not a
    BadArgument: it fell through to "Something went wrong" and an ERROR traceback."""
    cog, _, ctx, sink = _cog_and_player()
    try:
        StringView('"1').get_quoted_word()           # how discord.py parses `!remove "1`
    except commands.ExpectedClosingQuoteError as e:
        error = e
    else:
        raise AssertionError("discord.py no longer rejects an unbalanced quote")
    with caplog.at_level(logging.ERROR, logger="bot.cogs.music"):
        await Music.cog_command_error(cog, ctx, error)
    assert sink.sent == ["That argument doesn't look right — check `/help`."]
    assert not caplog.records, "a typo must not be logged as an internal error"


async def test_status_shows_the_volume_that_was_set():
    """/status had its own `int(volume * 100)` and still said 28% after `/volume 29`."""
    cog, player, ctx, sink = _cog_and_player()
    player.set_volume(29 / 100)
    await Music.status.callback(cog, ctx)
    assert {f.name: f.value for f in sink.sent[-1].fields}["Volume"] == "29%"
