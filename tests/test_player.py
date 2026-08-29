"""Playback state-machine tests: the loop-mode logic used to be entirely uncovered, and
two real bugs lived in it."""
import asyncio

import pytest

from bot.core.player import GuildPlayer, LoopMode
from bot.core.queue import TrackQueue
from bot.core.ytdl import Track


class _Guild:
    id = 1
    name = "test-guild"
    voice_client = None      # never connected in these tests


def make_player(max_queue: int = 50, idle_seconds: int = 300) -> GuildPlayer:
    """A GuildPlayer with just the state _next_track touches — no Discord, no voice."""
    p = GuildPlayer.__new__(GuildPlayer)
    p.bot = None
    p.guild = _Guild()
    p.ytdl = None
    p.queue = TrackQueue(max_size=max_queue)
    p.volume = 0.5
    p.loop_mode = LoopMode.OFF
    p.idle_seconds = idle_seconds
    p.current = None
    p.started_at = 0.0
    p._paused_at = 0.0
    p._paused_total = 0.0
    p.text_channel = None
    p.now_playing_msg = None
    p._source = None
    p._wake = asyncio.Event()
    p._finished = asyncio.Event()
    p._task = None
    p._np_task = None
    p._skip_requested = False
    p._stop_requested = False
    p._loading = None
    p._failures = 0
    p._lock = asyncio.Lock()
    return p


def track(name, duration=100):
    return Track(title=name, webpage_url=f"https://y/{name}", duration=duration)


async def play_out(player, rounds, failing=()):
    """Drive _next_track `rounds` times, simulating _play_track: `current` is set on
    success and left as None for a track that could not be played."""
    played = []
    for _ in range(rounds):
        t = await player._next_track()
        if t is None:
            played.append(None)
            continue
        if t.title in failing:
            played.append(f"{t.title}!")
            player.current = None
        else:
            played.append(t.title)
            player.current = t
    return played


async def test_loop_off_plays_each_track_once():
    p = make_player()
    p.queue.extend([track("a"), track("b"), track("c")])
    assert await play_out(p, 3) == ["a", "b", "c"]
    assert p.queue.is_empty


async def test_loop_all_cycles():
    p = make_player()
    p.loop_mode = LoopMode.ALL
    p.queue.extend([track("a"), track("b")])
    assert await play_out(p, 6) == ["a", "b", "a", "b", "a", "b"]


async def test_loop_one_repeats_until_skipped():
    p = make_player()
    p.loop_mode = LoopMode.ONE
    p.queue.extend([track("a"), track("b")])
    assert await play_out(p, 3) == ["a", "a", "a"]
    p._skip_requested = True
    assert await play_out(p, 1) == ["b"]


async def test_loop_all_drops_a_failing_track_without_duplicating_its_predecessor():
    """Regression: `current` was only assigned after the stream resolved, so a track that
    failed left the *previous* track as `current`. Under loop-all that re-queued the
    predecessor an extra time and evicted the successor from the rotation — the queue
    filled up with copies of one track."""
    p = make_player()
    p.loop_mode = LoopMode.ALL
    p.queue.extend([track("a"), track("b"), track("c")])
    played = await play_out(p, 9, failing={"b"})
    assert played[0:3] == ["a", "b!", "c"]
    assert played[3:] == ["a", "c", "a", "c", "a", "c"]   # even rotation, b gone
    assert len(p.queue) <= 2                              # and the queue does not grow


async def test_loop_all_never_grows_the_queue_past_its_cap():
    p = make_player(max_queue=2)
    p.loop_mode = LoopMode.ALL
    p.queue.extend([track("a"), track("b")])
    await play_out(p, 20)
    assert len(p.queue) <= 2


async def test_skip_flag_raised_while_idle_does_not_eat_the_next_track():
    """A /skip with nothing playing set _skip_requested; the flag survived the idle wait
    and consumed the first track queued afterwards."""
    p = make_player(idle_seconds=5)

    async def later():
        await asyncio.sleep(0.01)
        p._skip_requested = True
        p._stop_requested = True
        await asyncio.sleep(0.01)
        p.queue.add(track("z"))
        p._wake.set()

    feeder = asyncio.create_task(later())
    try:
        got = await p._next_track()
    finally:
        await feeder
    assert got.title == "z"
    assert p._skip_requested is False and p._stop_requested is False


async def test_idle_timeout_returns_none_and_clears_current():
    p = make_player(idle_seconds=0.05)
    p.current = track("a")
    assert await p._next_track() is None
    assert p.current is None


async def test_position_freezes_while_paused():
    """While paused, `position` reads from _paused_at instead of the wall clock, so the
    now-playing bar stops advancing."""
    p = make_player()
    p.current = track("a")
    p.started_at = 100.0
    p._paused_total = 0.0
    p._paused_at = 130.0         # paused 30s in
    assert p.position == pytest.approx(30.0)


async def test_position_excludes_time_spent_paused():
    p = make_player()
    p.current = track("a")
    p.started_at = 100.0
    p._paused_at = 0.0
    p._paused_total = 10.0
    import time as _t
    assert p.position == pytest.approx(_t.monotonic() - 110.0, abs=1.0)


def test_no_track_means_no_position():
    p = make_player()
    assert p.position == 0.0


# ------------------------------------------------- resolve window and failure streak
async def test_is_busy_covers_the_stream_resolve_window():
    """`is_playing` is False while yt-dlp resolves a URL, so a guard built on it alone
    answered "Nothing is playing" and threw the user's /skip away."""
    p = make_player()
    assert p.is_busy is False
    p._loading = track("resolving")
    assert p.is_busy is True
    p._loading = None
    assert p.is_busy is False


async def test_stop_clears_the_failure_streak():
    """_failures was only reset on a successful play, so four earlier failures plus one
    in a brand-new queue tripped the cap and wiped that queue."""
    p = make_player()
    p._failures = 4
    p.stop()
    assert p._failures == 0


async def test_disconnect_clears_the_failure_streak_and_loading():
    p = make_player()
    p._failures = 3
    p._loading = track("half-resolved")
    await p.disconnect()
    assert p._failures == 0
    assert p._loading is None


async def test_stop_does_not_mutate_the_persisted_loop_setting():
    """loop_mode is a per-guild setting written by /loop and restored at startup, so an
    unrelated command silently flipping it left memory and disk disagreeing."""
    p = make_player()
    p.loop_mode = LoopMode.ALL
    p.stop()
    assert p.loop_mode is LoopMode.ALL


async def test_disconnect_does_not_mutate_the_persisted_loop_setting():
    p = make_player()
    p.loop_mode = LoopMode.ONE
    await p.disconnect()
    assert p.loop_mode is LoopMode.ONE


async def test_loop_all_with_an_empty_queue_is_inert_after_a_failure_stop():
    """The failure cap clears the queue instead of forcing loop off; with nothing queued
    and no current track, loop-all must simply idle rather than spin."""
    p = make_player(idle_seconds=0.05)
    p.loop_mode = LoopMode.ALL
    p.current = None
    assert await p._next_track() is None


# --------------------------------------------- _play_track's own contract (not the harness)
class _FailingYTDL:
    """fetch_stream always raises, like an unplayable / geo-blocked track."""

    async def fetch_stream(self, track):
        raise LookupError("That video is unavailable.")

    def make_source(self, track, volume):      # never reached
        raise AssertionError("make_source must not run after fetch_stream raised")


class _VC:
    """Minimal stand-in for discord.VoiceClient (GuildPlayer.voice is patched to return it)."""

    def __init__(self):
        self.played = []

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self.played.append(source)


def _wire(p, monkeypatch):
    """Give the player a usable voice client and capture its announcements."""
    vc = _VC()
    monkeypatch.setattr(type(p), "voice", property(lambda self: vc))
    said = []

    async def _announce(text):
        said.append(text)

    monkeypatch.setattr(p, "_announce", _announce)
    return vc, said


async def test_play_track_nulls_current_when_the_stream_fails(monkeypatch):
    """THE regression, asserted against the source rather than a simulation.

    `current` used to be assigned only after the stream resolved, so a failed track left the
    PREVIOUS track as `current` and loop-all re-queued that predecessor. The loop-mode tests
    above model that behaviour in their harness, so they cannot catch a regression here.
    """
    p = make_player()
    p.ytdl = _FailingYTDL()
    p.current = track("previous")          # what used to wrongly survive
    vc, said = _wire(p, monkeypatch)

    await p._play_track(track("broken"))

    assert p.current is None, "a track that never played must not leave a stale `current`"
    assert p._loading is None, "the resolve marker must be cleared on the failure path"
    assert not vc.played, "nothing should have been handed to the voice client"
    assert said and "broken" in said[0]


async def test_play_track_counts_the_failure(monkeypatch):
    p = make_player()
    p.ytdl = _FailingYTDL()
    _wire(p, monkeypatch)
    await p._play_track(track("broken"))
    assert p._failures == 1


async def test_repeated_failures_stop_the_player_and_clear_the_queue(monkeypatch):
    """Five unplayable tracks in a row must stop rather than spam one message per attempt."""
    p = make_player()
    p.ytdl = _FailingYTDL()
    _, said = _wire(p, monkeypatch)
    p.queue.extend([track(f"bad{i}") for i in range(10)])

    for _ in range(p.MAX_CONSECUTIVE_FAILURES):
        await p._play_track(track("bad"))

    assert p.queue.is_empty, "the player should give up rather than keep grinding"
    assert p._failures == 0, "the streak resets once it has tripped"
    assert any("Too many tracks failed" in m for m in said)
    assert len(said) <= p.MAX_CONSECUTIVE_FAILURES + 1, "one message per attempt is spam"
