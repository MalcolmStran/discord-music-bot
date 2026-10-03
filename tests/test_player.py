"""Playback state-machine tests: the loop-mode logic used to be entirely uncovered, and
two real bugs lived in it."""
import asyncio
import types

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
    p._connecting = 0
    p._leaving = 0
    p._pending = 0
    # mirrors __init__; without it every attribute read here falls back to the class
    # attribute, which is how the shipped grace test passed without touching the wiring
    p.reconnect_grace = 45.0
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


# ------------------------------------------------- dropped voice connection (2026-09-27)
class _FlakyVoice:
    """Stands in for discord.VoiceClient: disconnected until `back_after` polls have passed."""

    def __init__(self, back_after):
        self.back_after = back_after
        self.polls = 0

    def is_connected(self):
        self.polls += 1
        return self.back_after is not None and self.polls > self.back_after


def _with_voice(p, vc, monkeypatch):
    monkeypatch.setattr(GuildPlayer, "voice", property(lambda self: vc))
    return p


async def test_wait_for_reconnect_returns_true_once_voice_is_back(monkeypatch):
    """A Starlink blip: discord.py reconnects a few seconds later; the player must survive."""
    p = _with_voice(make_player(), _FlakyVoice(back_after=3), monkeypatch)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await p.wait_for_reconnect(grace=5, poll=0.001) is True
    # three polls at 1 ms, not at the 0.5 s default: `poll` has to be honoured or a
    # recovering connection is noticed up to half a second late.
    assert loop.time() - t0 < 0.3


async def test_wait_for_reconnect_gives_up_after_the_grace(monkeypatch):
    p = _with_voice(make_player(), _FlakyVoice(back_after=None), monkeypatch)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await p.wait_for_reconnect(grace=0.05, poll=0.01) is False
    assert loop.time() - t0 < 1


async def test_wait_for_reconnect_returns_at_once_when_discord_dropped_the_client(monkeypatch):
    """A real kick: discord.py has already torn its voice client down, so don't sit out the grace."""
    p = _with_voice(make_player(), None, monkeypatch)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await p.wait_for_reconnect(grace=60, poll=0.01) is False
    assert loop.time() - t0 < 0.5


async def test_zero_grace_keeps_the_old_immediate_behaviour(monkeypatch):
    """VOICE_RECONNECT_GRACE=0 is documented as "reset immediately, like before", so this has
    to check the *immediacy*, not just the return value — asserting only `is False` made it a
    duplicate of the timeout test above and let a `max(0.5, grace)` floor through."""
    vc = _FlakyVoice(back_after=None)
    p = _with_voice(make_player(), vc, monkeypatch)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await p.wait_for_reconnect(grace=0, poll=0.01) is False
    assert vc.polls == 1, "grace=0 must check once and return, not enter the poll loop"
    assert loop.time() - t0 < 0.05, "grace=0 must not sleep at all"


# The default-grace check lives in tests/test_voice_reconnect.py: it has to go through the real
# GuildPlayer.__init__ to mean anything, and make_player() deliberately bypasses __init__.


# ------------------------------------------------- playback edge cases (review fixes)
class _StreamYTDL:
    """Resolves every track; `during` (if set) runs inside fetch_stream, i.e. mid-resolve."""

    def __init__(self, during=None):
        self.during = during

    async def fetch_stream(self, track):
        if self.during:
            await self.during()
        track.stream_url = "https://example.invalid/stream"

    def make_source(self, track, volume):
        return types.SimpleNamespace(cleanup=lambda: None, volume=volume)


async def test_a_drained_queue_does_not_hand_its_failure_streak_to_the_next_play(monkeypatch):
    """A queue that ended on four dead tracks left _failures at 4, so the first bad track of
    the next /play hit the cap and wiped the whole fresh playlist."""
    p = make_player()
    p.ytdl = _FailingYTDL()
    _, said = _wire(p, monkeypatch)
    p._failures = 4                                   # what the previous queue left behind

    waiting = asyncio.create_task(p._next_track())    # idles on the empty queue
    await asyncio.sleep(0)
    p.queue.extend([track("age-restricted")] + [track(f"good{i}") for i in range(5)])
    p._wake.set()
    nxt = await asyncio.wait_for(waiting, timeout=1)
    await p._play_track(nxt)

    assert p._failures == 1
    assert len(p.queue) == 5, "one bad track must not wipe a freshly queued playlist"
    assert not any("Too many tracks failed" in m for m in said)


class _DroppingVC(_VC):
    """discord.VoiceClient whose connection drops while a stream is still resolving.
    play() raises like the real one does when not connected."""

    def __init__(self):
        super().__init__()
        self.connected = True

    def is_connected(self):
        return self.connected

    def play(self, source, after=None):
        import discord
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        super().play(source, after)


async def test_a_voice_drop_during_the_resolve_holds_the_track_instead_of_skipping_it(monkeypatch):
    """play() raised "Not connected to voice." and the track was announced as "Couldn't
    start", counted as a failure and lost, while the next one was held for the reconnect."""
    p = make_player()
    p.bot = types.SimpleNamespace(loop=asyncio.get_running_loop())
    vc = _DroppingVC()
    monkeypatch.setattr(type(p), "voice", property(lambda self: vc))
    said = []

    async def _announce(text):
        said.append(text)

    monkeypatch.setattr(p, "_announce", _announce)

    async def drop():
        vc.connected = False

    p.ytdl = _StreamYTDL(during=drop)
    p.queue.add(track("B"))

    await p._play_track(track("A"))

    assert [t.title for t in p.queue] == ["A", "B"], "A must go back to the front, ahead of B"
    assert p._failures == 0, "a dropped connection is not a broken track"
    assert not said, f"nothing should be announced as skipped: {said}"
    assert p.current is None and p._source is None


async def test_a_play_error_while_connected_still_counts_as_a_failure(monkeypatch):
    """The hold above is only for a dropped connection; any other ClientException is a
    track we could not start and must still feed the failure cap."""
    import discord

    p = make_player()
    p.bot = types.SimpleNamespace(loop=asyncio.get_running_loop())
    vc, said = _wire(p, monkeypatch)

    def refuse(source, after=None):
        raise discord.ClientException("something else")

    vc.play = refuse
    p.ytdl = _StreamYTDL()
    await p._play_track(track("A"))
    assert p._failures == 1
    assert p.queue.is_empty
    assert said and "Couldn't start" in said[0]


async def test_a_skip_during_the_resolve_keeps_the_track_in_the_loop_all_rotation(monkeypatch):
    """A second /skip landing while the next track resolved dropped that track from the
    loop-all rotation for good: only a track that had started was cycled to the back."""
    p = make_player()
    p.loop_mode = LoopMode.ALL
    _wire(p, monkeypatch)
    p.queue.extend([track("C"), track("A")])          # A just played and was cycled back

    async def user_skips():
        p.skip()

    p.ytdl = _StreamYTDL(during=user_skips)
    await p._play_track(track("B"))

    assert [t.title for t in p.queue] == ["C", "A", "B"]
    assert p.current is None


async def test_a_skip_during_the_resolve_is_not_requeued_outside_loop_all(monkeypatch):
    p = make_player()
    _wire(p, monkeypatch)
    for mode in (LoopMode.OFF, LoopMode.ONE):
        p.loop_mode = mode

        async def user_skips():
            p.skip()

        p.ytdl = _StreamYTDL(during=user_skips)
        await p._play_track(track("B"))
        assert p.queue.is_empty, mode


@pytest.mark.parametrize("how", ["stop", "disconnect"])
async def test_a_stop_during_the_resolve_does_not_requeue_under_loop_all(monkeypatch, how):
    """/stop and a teardown clear the queue; re-adding the abandoned track would leave it
    behind for the next /play in that guild."""
    p = make_player()
    p.loop_mode = LoopMode.ALL
    _wire(p, monkeypatch)
    p.queue.extend([track("C")])

    async def user_stops():
        if how == "stop":
            p.stop()
        else:
            await p.disconnect()

    p.ytdl = _StreamYTDL(during=user_stops)
    await p._play_track(track("B"))
    assert p.queue.is_empty


def test_loading_exposes_the_track_being_resolved():
    p = make_player()
    assert p.loading is None
    p._loading = track("x")
    assert p.loading.title == "x"


def test_volume_percent_reads_back_every_level_the_user_can_set():
    """int(0.29 * 100) is 28: seven of the 151 accepted levels displayed one lower."""
    import json

    p = make_player()
    for level in range(151):
        p.set_volume(level / 100)
        assert p.volume_percent == level
        p.set_volume(json.loads(json.dumps(p.volume)))    # the settings-file round trip
        assert p.volume_percent == level


def test_now_playing_footer_shows_the_volume_that_was_set():
    p = make_player()
    p.guild.get_member = lambda uid: None
    p.set_volume(0.29)
    t = track("a")
    t.requester_id = 42
    footer = p.now_playing_embed(t).footer.text
    assert footer.endswith("volume 29%"), footer


# --------------------------------------- a dead discord.py AudioPlayer after a voice drop
class _VoiceConnection:
    """Stands in for discord.py's VoiceConnectionState, the only part of the voice stack
    faked here: VoiceClient.play/stop/is_playing and the AudioPlayer thread are real."""

    def __init__(self):
        self.connected = True
        self.timeout = 30.0

        async def speak(_state):
            pass

        self.ws = types.SimpleNamespace(speak=speak)

    def is_connected(self):
        return self.connected

    def wait(self, timeout=None):
        # The real one blocks up to `timeout` (30 s) for a reconnect; give up at once, as it
        # does when that runs out or when disconnect(cleanup=False) "flips" the event.
        return self.connected


class _Opus:
    """An endless, already-opus source, so the real AudioPlayer needs no encoder or ffmpeg."""

    volume = 1.0

    def __init__(self):
        import discord

        self.cleaned = 0
        self._frame = discord.player.OPUS_SILENCE

    def read(self):
        return self._frame

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleaned += 1


async def test_a_dead_audio_player_left_by_a_voice_drop_does_not_wipe_the_queue(monkeypatch):
    """discord.py's AudioPlayer gives up on a dropped connection WITHOUT setting its end
    flag (player.py: `if self._end.is_set() or not connected: return`), and its reconnect
    path calls disconnect(cleanup=False), which never calls VoiceClient.stop(). So
    `vc.is_playing()` stayed True for good, every later play() raised "Already playing
    audio.", and the fifth such "failure" cleared the whole queue."""
    import functools

    import discord

    class _AudioSource(_Opus, discord.AudioSource):
        pass

    loop = asyncio.get_running_loop()
    conn = _VoiceConnection()
    vc = discord.VoiceClient.__new__(discord.VoiceClient)
    vc._connection, vc._player, vc.encoder = conn, None, None
    vc.client = types.SimpleNamespace(loop=loop)
    vc.send_audio_packet = lambda data, encode=True: None

    class _YT:
        async def fetch_stream(self, t):
            t.stream_url = "https://example.invalid/stream"

        def make_source(self, t, volume):
            return _AudioSource()

    guild = types.SimpleNamespace(id=1, name="g", voice_client=vc, get_member=lambda uid: None)
    p = GuildPlayer(types.SimpleNamespace(loop=loop), guild, _YT(), max_queue=50, default_volume=0.5,
                    idle_seconds=300, reconnect_grace=5)
    p.wait_for_reconnect = functools.partial(GuildPlayer.wait_for_reconnect, p, poll=0.005)
    said = []

    async def _announce(text):
        said.append(text)

    monkeypatch.setattr(p, "_announce", _announce)
    monkeypatch.setattr(p, "_announce_now_playing", lambda t: asyncio.sleep(0))

    async def until(cond, timeout=2.0):
        deadline = loop.time() + timeout
        while not cond():
            assert loop.time() < deadline, "condition never became true"
            await asyncio.sleep(0.005)

    # duration <= 10 keeps the "ended after <3s" stream-failure branch out of the way
    songs = [Track(title=f"song{i}", webpage_url=f"https://y/{i}", duration=5) for i in range(4)]
    p.enqueue(songs)
    try:
        await until(lambda: p.current is songs[0] and vc._player is not None)
        dead = vc._player

        conn.connected = False                        # the uplink drops mid-song
        await until(lambda: not dead.is_alive())      # the audio thread aborts...
        assert vc.is_playing(), "precondition: discord.py's dead player still reports playing"
        conn.connected = True                         # ...and discord.py reconnects

        await until(lambda: p.current is songs[1] or said)
        assert not said, f"tracks were failed against the dead player: {said}"
        assert p.current is songs[1]
        assert vc._player is not dead and vc._player.is_alive()
        assert [t.title for t in p.queue] == ["song2", "song3"]
    finally:
        task, p._task = p._task, None
        if task:
            task.cancel()
        player = vc._player
        vc.stop()
        if player is not None:
            player.join(1)
