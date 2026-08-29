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
