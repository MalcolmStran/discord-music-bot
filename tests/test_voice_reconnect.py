"""Voice-drop recovery — the parts of commit 8f04630 that shipped without tests.

That commit's own five tests all stop at `GuildPlayer.wait_for_reconnect()`. Everything
around it had no coverage, and mutation testing showed what that cost: collapsing the cog's
guard back to an unconditional `disconnect()` — i.e. restoring the exact bug the commit
fixes — left the suite green, and so did deleting the `disconnect()` that handles a real
kick. These tests drive the real cog method, the real player loop and the real
config -> player wiring.
"""
import asyncio
import types

from bot.cogs.music import Music
from bot.config import Config
from bot.core.player import GuildPlayer
from bot.core.ytdl import Track


class _Guild:
    id = 1
    name = "test-guild"

    def __init__(self):
        self.voice_client = None


def track(name, duration=5):
    # duration <= 10 keeps _play_track's "ended after <3s" stream-failure branch out of the
    # way; these tests are about the hold path, not about failure counting.
    return Track(title=name, webpage_url=f"https://y/{name}", duration=duration)


# --------------------------------------------- the cog's recovered-vs-kick decision
class _StubPlayer:
    """Records what the cog does to it. `wait_for_reconnect` is the input to the decision
    under test, so it is stubbed; the decision itself is the real `Music` method."""

    def __init__(self, recovered: bool, grace: float = 45.0):
        self._recovered = recovered
        self.reconnect_grace = grace
        self.waits = 0
        self.disconnects = 0

    async def wait_for_reconnect(self):
        self.waits += 1
        return self._recovered

    async def disconnect(self):
        self.disconnects += 1


def _cog():
    cog = Music.__new__(Music)
    cog._reconnect_checks = set()
    return cog


async def test_recovered_connection_keeps_the_player():
    """The entire point of the fix. Collapsing the guard so `disconnect()` runs regardless —
    the pre-8f04630 behaviour that turned every uplink blip into a lost song — must fail here."""
    cog, player = _cog(), _StubPlayer(recovered=True)
    await Music._handle_bot_left_voice(cog, _Guild(), player)
    assert player.waits == 1, "the cog has to consult wait_for_reconnect at all"
    assert player.disconnects == 0, "a drop that came back must not clear the queue"


async def test_connection_that_stays_down_resets_the_player():
    """The other half: a real kick or a deleted channel still has to reset the player.
    Deleting the `disconnect()` call leaves a live player with a stale queue and no voice."""
    cog, player = _cog(), _StubPlayer(recovered=False)
    await Music._handle_bot_left_voice(cog, _Guild(), player)
    assert player.waits == 1 and player.disconnects == 1


async def test_concurrent_drop_events_share_one_wait():
    """discord.py's reconnect emits more than one VOICE_STATE_UPDATE echo per drop. Without
    the `_reconnect_checks` guard every echo starts its own multi-second waiter, and the
    losers each call `disconnect()` — tearing down a connection the winner just recovered."""
    cog, guild = _cog(), _Guild()
    parked, release = asyncio.Event(), asyncio.Event()

    class _Slow(_StubPlayer):
        async def wait_for_reconnect(self):
            self.waits += 1
            parked.set()
            await release.wait()
            return False

    player = _Slow(recovered=False)
    first = asyncio.create_task(Music._handle_bot_left_voice(cog, guild, player))
    await parked.wait()                       # the first waiter is parked inside the grace
    try:
        # The second echo must short-circuit on the guard. Without it this call starts its own
        # waiter and parks on `release` too, so bound it rather than hang the suite.
        await asyncio.wait_for(Music._handle_bot_left_voice(cog, guild, player), timeout=1)
    except TimeoutError:
        raise AssertionError("the duplicate drop event started its own waiter") from None
    finally:
        release.set()
        await first
    assert player.waits == 1, "the duplicate drop event started a second waiter"
    assert player.disconnects == 1, "the drop that never recovered still has to reset the player"


async def test_the_guard_is_released_so_later_drops_are_still_handled():
    """A leaked `_reconnect_checks` entry would make that guild deaf to every future drop."""
    cog, guild, player = _cog(), _Guild(), _StubPlayer(recovered=True)
    await Music._handle_bot_left_voice(cog, guild, player)
    assert guild.id not in cog._reconnect_checks
    await Music._handle_bot_left_voice(cog, guild, player)
    assert player.waits == 2, "the guard was never released"


# --------------------------------------------- config -> player wiring
class _Settings(dict):
    def get(self, guild_id, key, default=None):   # matches GuildSettings.get
        return dict.get(self, (guild_id, key), default)


def _cog_with_cfg(**overrides):
    cfg = types.SimpleNamespace(max_queue_size=50, default_volume=0.5,
                                idle_disconnect_seconds=300, voice_reconnect_grace=45)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cog = Music.__new__(Music)
    cog.bot = None
    cog.cfg = cfg
    cog.ytdl = None
    cog.settings = _Settings()
    cog.players = {}
    return cog


async def test_configured_grace_reaches_the_player():
    """VOICE_RECONNECT_GRACE only exists if it survives the trip into GuildPlayer. Dropping
    `reconnect_grace=` from the construction in `Music.player()` silently pins every guild to
    the hard-coded default and makes the documented setting — including the `0` escape
    hatch — do nothing at all."""
    cog = _cog_with_cfg(voice_reconnect_grace=7)
    player = Music.player(cog, _Guild())
    assert player.reconnect_grace == 7


async def test_zero_grace_survives_the_trip_to_the_player():
    """0 is the documented "reset immediately, like before" value; it must not be swallowed
    by an `or` / truthiness test somewhere along the way."""
    cog = _cog_with_cfg(voice_reconnect_grace=0)
    assert Music.player(cog, _Guild()).reconnect_grace == 0


async def test_default_grace_outlasts_discord_py_reconnect_window():
    """discord.py waits up to 30 s for a new voice server after a forced close, so the grace
    has to outlast that or we give up while the library is still trying.

    Built through the real `__init__`, and tied to the config default: the version of this
    test that shipped with 8f04630 read `make_player().reconnect_grace`, which the test
    harness never sets, so it only ever saw the class-attribute fallback and would have
    passed even if the two defaults had drifted apart.
    """
    player = GuildPlayer(None, _Guild(), None, max_queue=50, default_volume=0.5, idle_seconds=300)
    assert player.reconnect_grace > 30
    assert player.reconnect_grace == Config.voice_reconnect_grace, \
        "GuildPlayer's default and VOICE_RECONNECT_GRACE's default must not drift apart"


# --------------------------------------------- the player loop's hold path
class _FakeVoice:
    def __init__(self):
        self.connected = True
        self._playing = False
        self._after = None

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self._playing = True
        self._after = after

    def stop(self):
        """End the current track the way discord.py does — via the after-callback."""
        self._playing = False
        if self._after:
            after, self._after = self._after, None
            after(None)

    async def disconnect(self, *, force=False):
        self.connected = False


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content if content is not None else "<embed>")
        return types.SimpleNamespace(delete=self._noop, edit=self._edit)

    async def _noop(self):
        pass

    async def _edit(self, **kw):
        pass


class _YTDL:
    async def fetch_stream(self, track):
        track.stream_url = "https://example.invalid/stream"

    def make_source(self, track, volume):
        return types.SimpleNamespace(cleanup=lambda: None, volume=volume)


def _loop_player(monkeypatch, grace):
    """A GuildPlayer whose real `_player_loop` can be run against a fake voice client."""
    guild = _Guild()
    player = GuildPlayer(types.SimpleNamespace(loop=asyncio.get_running_loop()), guild, _YTDL(),
                         max_queue=50, default_volume=0.5, idle_seconds=300, reconnect_grace=grace)
    vc = _FakeVoice()
    guild.voice_client = vc
    player.text_channel = _Channel()
    monkeypatch.setattr(GuildPlayer, "voice", property(lambda self: self.guild.voice_client))
    return player, vc


async def test_loop_holds_the_track_across_a_drop_and_resumes(monkeypatch):
    """A drop that recovers inside the grace must cost a few seconds of silence, not the song.
    Skipping the wait (announce and exit immediately) reverts the second half of the fix."""
    player, vc = _loop_player(monkeypatch, grace=5)
    player.enqueue([track("a"), track("b")])
    try:
        await asyncio.sleep(0.05)
        assert player.current and player.current.title == "a"

        vc.connected = False      # the voice websocket drops...
        vc.stop()                 # ...and the current track ends, so the loop takes the next
        await asyncio.sleep(0.1)
        assert [t.title for t in player.queue] == ["b"], "the next track must go back to the front"
        assert not any("Lost the voice connection" in m for m in player.text_channel.sent), \
            "the loop gave up instead of holding the track for the grace"
        assert not player._task.done(), "the loop exited during the grace"

        vc.connected = True       # discord.py gets the connection back
        await asyncio.sleep(1.2)  # one 0.5 s poll, plus slack for a loaded machine
        assert player.current and player.current.title == "b", "playback did not resume"
        assert player.queue.is_empty
    finally:
        await player.disconnect()


async def test_loop_gives_up_once_the_grace_expires(monkeypatch):
    """The other direction: a connection that never comes back must end the loop with one
    message. Never giving up leaves `_player_loop` re-pushing the same track forever."""
    player, vc = _loop_player(monkeypatch, grace=0.1)
    vc.connected = False
    player.enqueue([track("a")])
    try:
        await asyncio.sleep(1.5)
        said = [m for m in player.text_channel.sent if "Lost the voice connection" in m]
        assert len(said) == 1, f"expected exactly one give-up message, got {player.text_channel.sent}"
        assert player._task.done(), "the loop must exit rather than spin on the held track"
        assert player.current is None
    finally:
        if player._task and not player._task.done():
            player._task.cancel()
