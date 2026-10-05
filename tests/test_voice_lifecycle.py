"""Voice lifecycle: joining, leaving and the races between them.

These drive the real `GuildPlayer` and the real `Music` cog. Only discord.py's voice layer is
faked, modelled on how 2.7.1 behaves on the paths that matter here:

* `abc.Connectable.connect` registers the new VoiceClient for the guild BEFORE the handshake,
  so during a handshake `guild.voice_client` exists but is not connected.
* `VoiceClient.disconnect` flips to disconnected at once, sends op4, and then waits for the
  gateway to echo VOICE_STATE_UPDATE(null). The echo dispatches `on_voice_state_update` while
  the client is still registered; `cleanup()` then pops the client registered under the
  GUILD id, whichever client that is.
"""
import asyncio
import functools
import itertools
import logging
import types

import discord

from bot.cogs.music import Music
from bot.core.player import GuildPlayer
from bot.core.ytdl import Track

BOT_ID = 999
_ids = itertools.count(100)


async def until(cond, timeout=2.0):
    """Wait for `cond()` without guessing a sleep long enough for a loaded machine."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


def track(name, duration=5):
    # duration <= 10 keeps _play_track's "ended after <3s" failure branch out of the way
    return Track(title=name, webpage_url=f"https://y/{name}", duration=duration)


class FakeVC:
    def __init__(self, guild, channel):
        self.guild, self.channel = guild, channel
        self._connected = False
        self.torn = False
        self.disconnects = 0
        self.disconnect_gate = None    # set to an Event to hold the wait for the gateway echo
        self.move_gate = None
        self._playing = False
        self._after = None

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self._playing, self._after = True, after

    def stop(self):
        self._playing = False
        if self._after:
            after, self._after = self._after, None
            after(None)

    async def move_to(self, channel):
        if self.move_gate:
            await self.move_gate.wait()
        self.channel = channel

    async def disconnect(self, *, force=False):
        self.disconnects += 1
        self.stop()
        was_in = self.channel
        self._connected = False
        self.torn = True
        await asyncio.sleep(0)                        # op4 goes out
        cog = self.guild.cog
        if cog is not None and was_in is not None:    # ...and Discord echoes it back
            self.guild.echoes.append(asyncio.get_running_loop().create_task(cog.on_voice_state_update(
                self.guild.me, types.SimpleNamespace(channel=was_in), types.SimpleNamespace(channel=None))))
        if self.disconnect_gate:
            await self.disconnect_gate.wait()
        await asyncio.sleep(0.01)                     # the echo releases this wait a few passes later
        self.guild.registry.pop(self.guild.id, None)  # VoiceProtocol.cleanup(): by guild id


def _member(*, bot=False, channel=None, guild=None):
    return types.SimpleNamespace(id=next(_ids), bot=bot, guild=guild, voice=types.SimpleNamespace(channel=channel),
                                 guild_permissions=types.SimpleNamespace(move_members=False))


class FakeChannel:
    def __init__(self, guild, name, *, humans=1, user_limit=0):
        self.guild, self.name, self.id = guild, name, next(_ids)
        self.user_limit = user_limit
        self.humans = [_member(channel=self, guild=guild) for _ in range(humans)]
        for m in self.humans:
            guild.members[m.id] = m
        self.perms = types.SimpleNamespace(connect=True, speak=True, move_members=False)
        self.handshake_gate = None
        self.connects = 0

    @property
    def members(self):
        # like VoiceChannel.members: built from voice states, minus anyone the cache lacks
        return [m for m in self.humans if self.guild.get_member(m.id) is not None]

    @property
    def voice_states(self):
        return {m.id: types.SimpleNamespace() for m in self.humans}

    def permissions_for(self, _):
        return self.perms

    async def connect(self, *, timeout, reconnect, self_deaf):
        self.connects += 1
        if self.guild.registry.get(self.guild.id):
            raise discord.ClientException("Already connected to a voice channel.")
        vc = FakeVC(self.guild, self)
        self.guild.registry[self.guild.id] = vc       # registered before the handshake
        if self.handshake_gate:
            await self.handshake_gate.wait()
        await asyncio.sleep(0)
        if vc.torn:                                    # disconnected mid-handshake
            raise TimeoutError()
        vc._connected = True
        return vc


class FakeGuild:
    id = 1
    name = "g"

    def __init__(self):
        self.registry = {}
        self.cog = None
        self.echoes = []
        self.me = types.SimpleNamespace(id=BOT_ID, bot=True, guild=self, voice=None)
        self.members = {BOT_ID: self.me}

    @property
    def voice_client(self):
        return self.registry.get(self.id)

    def get_member(self, uid):
        return self.members.get(uid)


class Sink:
    def __init__(self):
        self.sent = []

    @property
    def texts(self):
        return [m for m in self.sent if isinstance(m, str)]

    async def send(self, content=None, **kw):
        self.sent.append(content if content is not None else kw.get("embed"))
        return types.SimpleNamespace(delete=self._noop, edit=self._edit)

    async def _noop(self):
        pass

    async def _edit(self, **kw):
        pass


class FakeYTDL:
    def __init__(self):
        self.gate = None             # set to an Event to make resolve() slow
        self.queries = []
        self.error = None

    async def resolve(self, query, requester_id=None):
        self.queries.append(query)
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return [track(query)]

    async def fetch_stream(self, t):
        t.stream_url = "https://example.invalid/stream"

    def make_source(self, t, volume):
        return types.SimpleNamespace(cleanup=lambda: None, volume=volume)


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _env(monkeypatch):
    monkeypatch.setattr(GuildPlayer, "voice", property(lambda self: self.guild.voice_client))
    guild = FakeGuild()
    cfg = types.SimpleNamespace(max_queue_size=50, default_volume=0.5, idle_disconnect_seconds=300,
                                voice_reconnect_grace=45, max_song_duration=7200)
    bot = types.SimpleNamespace(loop=asyncio.get_running_loop(), user=types.SimpleNamespace(id=BOT_ID))
    cog = Music.__new__(Music)
    cog.bot, cog.cfg, cog.ytdl, cog.spotify = bot, cfg, FakeYTDL(), None
    cog.settings = types.SimpleNamespace(get=lambda *a, **k: None)
    cog.players, cog._alone_checks, cog._reconnect_checks = {}, set(), set()
    guild.cog = cog
    return types.SimpleNamespace(guild=guild, cog=cog, bot=bot)


def make_player(env, *, grace=45.0, idle=300):
    p = GuildPlayer(env.bot, env.guild, env.cog.ytdl, max_queue=50, default_volume=0.5,
                    idle_seconds=idle, reconnect_grace=grace)
    p.text_channel = Sink()
    # the real wait_for_reconnect, polling every 5 ms instead of every 0.5 s
    p.wait_for_reconnect = functools.partial(GuildPlayer.wait_for_reconnect, p, poll=0.005)
    env.cog.players[env.guild.id] = p
    return p


def make_ctx(env, channel, *, author=None):
    sink = Sink()
    if author is None:
        author = _member(channel=channel)
    ctx = types.SimpleNamespace(guild=env.guild, channel=sink, interaction=None, author=author,
                                send=sink.send, defer=lambda: asyncio.sleep(0), typing=_Typing)
    return ctx, sink


async def teardown(player):
    task = player._task
    await player.disconnect()
    if task and not task.done():
        task.cancel()


# ============================================ disconnect() vs an in-flight connect()
async def test_reconnect_waiter_lets_a_connect_in_progress_decide(monkeypatch):
    """A voice drop, then a /play near the end of the grace. The cog's waiter ran out
    mid-handshake and force-disconnected the half-built client, so /play failed with
    'Timed out connecting' on a connection that was working."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env, grace=0.05)
    g.registry[g.id] = FakeVC(g, a)                   # dropped; discord.py still retrying
    a.handshake_gate = asyncio.Event()
    try:
        waiter = asyncio.create_task(env.cog._handle_bot_left_voice(g, player))
        joining = asyncio.create_task(player.connect(a))       # /play's join
        await asyncio.sleep(0.15)                     # the grace runs out mid-handshake
        a.handshake_gate.set()
        await joining
        await waiter
        assert player.connected, "the waiter tore down the connection /play just made"
    finally:
        await teardown(player)


async def test_loop_holding_a_track_does_not_give_up_during_a_connect(monkeypatch):
    """The player loop's own waiter hit the same window: it posted 'Lost the voice
    connection' and exited while the reconnect was succeeding, so the held song never played."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env, grace=0.05)
    g.registry[g.id] = FakeVC(g, a)                   # dropped
    a.handshake_gate = asyncio.Event()
    try:
        player.enqueue([track("held")])
        await until(lambda: [t.title for t in player.queue] == ["held"] and player._task)
        joining = asyncio.create_task(player.connect(a))
        await asyncio.sleep(0.15)                     # well past the loop's grace
        a.handshake_gate.set()
        await joining
        await until(lambda: player.current is not None or player._task.done())
        assert not any("Lost the voice connection" in m for m in player.text_channel.texts)
        assert player.current and player.current.title == "held"
    finally:
        await teardown(player)


async def test_disconnect_waits_for_an_in_flight_connect(monkeypatch):
    """disconnect() never took the connect lock, so it force-disconnected a client that was
    still handshaking (discord.py registers it before the handshake starts)."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env)
    a.handshake_gate = asyncio.Event()
    joining = asyncio.create_task(player.connect(a))
    await until(lambda: g.voice_client is not None)
    vc = g.voice_client
    leaving = asyncio.create_task(player.disconnect())
    await asyncio.sleep(0.02)
    assert not vc.torn, "disconnect() tore down a handshake in progress"
    a.handshake_gate.set()
    await joining                                     # must not raise TimeoutError
    await leaving
    assert g.voice_client is None


async def test_a_loop_started_during_a_slow_disconnect_survives_it(monkeypatch):
    """disconnect() read `_task` only after awaiting the voice disconnect, which can wait
    30 s for the gateway echo, so it cancelled a loop that a /play started meanwhile."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env)
    await player.connect(a)
    player.ensure_loop()                              # idle after a song
    vc = g.voice_client
    vc.disconnect_gate = asyncio.Event()
    leaving = asyncio.create_task(player.disconnect())
    await until(lambda: vc.torn)                      # parked on the echo wait
    player.enqueue([track("new")])
    started = player._task
    vc.disconnect_gate.set()
    await leaving
    await until(lambda: started.done())
    assert not started.cancelled(), "the teardown cancelled a loop started after it began"


async def test_alone_check_defers_to_a_join_in_progress(monkeypatch):
    """The 'everyone left' check read the old, empty channel while a /play was moving the
    bot to another one, and then disconnected from the channel it had just moved into."""
    env = _env(monkeypatch)
    monkeypatch.setattr(Music, "ALONE_CHECK_DELAY", 0.02)
    g = env.guild
    a, b = FakeChannel(g, "A", humans=1), FakeChannel(g, "B", humans=1)
    player = make_player(env)
    await player.connect(a)
    vc = g.voice_client
    leaver = a.humans.pop()
    vc.move_gate = asyncio.Event()
    check = asyncio.create_task(env.cog.on_voice_state_update(
        leaver, types.SimpleNamespace(channel=a), types.SimpleNamespace(channel=None)))
    moving = asyncio.create_task(player.connect(b))   # /play from B
    await asyncio.sleep(0.06)                         # the check wakes during the move
    vc.move_gate.set()
    await moving
    await check
    try:
        assert player.connected and player.channel is b, "left the channel it was summoned to"
        assert not any("Everyone left" in m for m in player.text_channel.texts)
    finally:
        await teardown(player)


async def test_alone_check_stays_if_someone_returns_during_its_announcement(monkeypatch):
    env = _env(monkeypatch)
    monkeypatch.setattr(Music, "ALONE_CHECK_DELAY", 0.0)
    a = FakeChannel(env.guild, "A", humans=1)
    player = make_player(env)
    await player.connect(a)
    leaver = a.humans.pop()
    gate, announcing = asyncio.Event(), asyncio.Event()

    async def slow_send(content=None, **kw):
        announcing.set()
        await gate.wait()

    player.text_channel.send = slow_send
    check = asyncio.create_task(env.cog.on_voice_state_update(
        leaver, types.SimpleNamespace(channel=a), types.SimpleNamespace(channel=None)))
    try:
        await asyncio.wait_for(announcing.wait(), timeout=1)
        a.humans.append(leaver)                       # back before the goodbye was sent
        gate.set()
        await check
        assert player.connected, "left a channel that had a listener again"
    finally:
        gate.set()
        await teardown(player)


async def test_alone_check_stays_if_a_join_starts_during_its_announcement(monkeypatch):
    """The post-announce re-check must also see a /play or /join that began moving the bot
    while the goodbye was sending: disconnect() queues behind the move and then tears down
    the channel the bot was just summoned to."""
    env = _env(monkeypatch)
    monkeypatch.setattr(Music, "ALONE_CHECK_DELAY", 0.0)
    g = env.guild
    a, b = FakeChannel(g, "A", humans=1), FakeChannel(g, "B", humans=1)
    player = make_player(env)
    await player.connect(a)
    vc = g.voice_client
    leaver = a.humans.pop()
    gate, announcing = asyncio.Event(), asyncio.Event()

    async def slow_send(content=None, **kw):
        announcing.set()
        await gate.wait()

    player.text_channel.send = slow_send
    vc.move_gate = asyncio.Event()
    check = asyncio.create_task(env.cog.on_voice_state_update(
        leaver, types.SimpleNamespace(channel=a), types.SimpleNamespace(channel=None)))
    try:
        await asyncio.wait_for(announcing.wait(), timeout=1)
        moving = asyncio.create_task(player.connect(b))   # /play from B, still moving
        await until(lambda: player.connecting)
        gate.set()
        await asyncio.sleep(0.02)                     # the check re-reads A: still empty
        vc.move_gate.set()
        await moving
        await check
        assert player.connected and player.channel is b, "left the channel it was summoned to"
    finally:
        gate.set()
        vc.move_gate.set()
        await teardown(player)


# ============================================ our own leaves are not external drops
async def test_leave_is_not_logged_as_a_failed_recovery(monkeypatch, caplog):
    """Discord echoes our own leave exactly like a kick, so every /leave logged 'bot left
    voice (external)', then 'voice did not come back', and disconnected a second time."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env)
    await player.connect(a)
    vc = g.voice_client
    ctx, sink = make_ctx(env, a)
    with caplog.at_level(logging.DEBUG, logger="bot"):
        await Music.leave.callback(env.cog, ctx)
        await asyncio.gather(*g.echoes)
    msgs = [r.getMessage() for r in caplog.records]
    assert sink.sent == ["👋 Bye."]
    assert not any("external" in m or "did not come back" in m for m in msgs), msgs
    assert vc.disconnects == 1
    assert msgs.count("[g] disconnected") == 1


async def test_stale_client_cleanup_in_connect_is_not_an_external_drop(monkeypatch, caplog):
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env)
    g.registry[g.id] = FakeVC(g, a)                   # stale client left behind
    with caplog.at_level(logging.INFO, logger="bot"):
        await player.connect(a)
        await asyncio.gather(*g.echoes)
    assert g.echoes, "the fake should have echoed the stale client's disconnect"
    assert not any("external" in r.getMessage() for r in caplog.records)
    try:
        assert player.connected
    finally:
        await teardown(player)


async def test_a_real_kick_still_resets_promptly(monkeypatch, caplog):
    """The own-leave check must not swallow a real kick, nor slow it down."""
    env = _env(monkeypatch)
    g, a = env.guild, FakeChannel(env.guild, "A")
    player = make_player(env)
    await player.connect(a)
    player.queue.extend([track("x")])
    g.voice_client._connected = False
    with caplog.at_level(logging.INFO, logger="bot"):
        listener = asyncio.create_task(env.cog.on_voice_state_update(
            g.me, types.SimpleNamespace(channel=a), types.SimpleNamespace(channel=None)))
        await asyncio.sleep(0)
        g.registry.pop(g.id, None)                    # discord.py's own cleanup after a kick
        await asyncio.wait_for(listener, timeout=1)
    assert any("did not come back" in r.getMessage() for r in caplog.records)
    assert player.queue.is_empty


# ============================================ the idle timer vs /play and /join
async def test_idle_timer_does_not_leave_while_play_is_resolving(monkeypatch):
    """/play joins, then spends seconds resolving before it enqueues. An idle timer expiring
    in that window left the channel, and the confirmed song died on 'Lost the voice connection'."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env, idle=0.05)
    env.cog.ytdl.gate = asyncio.Event()
    ctx, _ = make_ctx(env, a)
    playing = asyncio.create_task(Music.play.callback(env.cog, ctx, query="song"))
    try:
        await until(lambda: env.cog.ytdl.queries)
        await asyncio.sleep(0.2)                      # four idle periods while resolving
        env.cog.ytdl.gate.set()
        await playing
        await until(lambda: player.current is not None or not player.connected)
        assert player.connected, f"left mid-/play: {player.text_channel.sent}"
        assert player.current.title == "song"
        assert not any("Nothing played" in m for m in player.text_channel.texts)
    finally:
        await teardown(player)


async def test_a_cancelled_play_does_not_hold_off_idle_leaves_for_good(monkeypatch):
    """The reservation must be released however /play ends. A cancelled /play (or one whose
    reply raised) that kept its count blocked every idle leave in that guild until restart."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env, idle=0.03)
    env.cog.ytdl.gate = asyncio.Event()
    ctx, _ = make_ctx(env, a)
    playing = asyncio.create_task(Music.play.callback(env.cog, ctx, query="song"))
    try:
        await until(lambda: env.cog.ytdl.queries)     # joined, resolving, reservation held
        playing.cancel()
        try:
            await playing
        except asyncio.CancelledError:
            pass
        await until(lambda: not player.connected, timeout=1)
        assert any("Nothing played" in m for m in player.text_channel.texts)
    finally:
        await teardown(player)


async def test_play_arriving_during_the_idle_announce_keeps_the_bot(monkeypatch):
    """The second check, after the '💤' message is sent, must also see a /play in flight."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env, idle=0.02)
    await player.connect(a)
    gate, announcing = asyncio.Event(), asyncio.Event()
    sent = player.text_channel.sent

    async def slow_send(content=None, **kw):
        announcing.set()
        await gate.wait()
        sent.append(content)

    player.text_channel.send = slow_send
    player.ensure_loop()
    try:
        await asyncio.wait_for(announcing.wait(), timeout=1)
        with player.reserve():                       # a /play starts while that is sending
            gate.set()
            await asyncio.sleep(0.01)
        assert player.connected, "the idle path left while a /play was in flight"
    finally:
        gate.set()
        await teardown(player)


async def test_idle_timer_does_not_leave_during_a_join_move(monkeypatch):
    """A /join that moves the bot holds no reservation; the in-flight connect has to count."""
    env = _env(monkeypatch)
    g = env.guild
    a, b = FakeChannel(g, "A"), FakeChannel(g, "B")
    player = make_player(env, idle=0.03)
    await player.connect(a)
    vc = g.voice_client
    vc.move_gate = asyncio.Event()
    moving = asyncio.create_task(player.connect(b))
    await until(lambda: player.connecting)
    player.ensure_loop()
    try:
        await asyncio.sleep(0.12)                     # several idle periods mid-move
        # Checked before the move ends: unguarded, the idle path announces here and then
        # queues its disconnect behind the move, tearing down the channel it just moved to.
        assert not any("Nothing played" in m for m in player.text_channel.texts)
        assert player.connecting and player.connected
        vc.move_gate.set()
        await moving
        assert player.channel is b
    finally:
        vc.move_gate.set()                            # else teardown queues behind the move
        await teardown(player)


async def test_join_alone_still_idles_out(monkeypatch):
    """/join never started the player loop, and the idle timer lives in it, so a bot that
    was summoned and never used stayed in voice indefinitely."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env, idle=0.03)
    ctx, sink = make_ctx(env, a)
    await Music.join.callback(env.cog, ctx)
    assert sink.sent == ["✅ Joined **A**."]
    try:
        await until(lambda: not player.connected, timeout=1)
        assert any("Nothing played" in m for m in player.text_channel.texts)
    finally:
        await teardown(player)


async def test_failed_play_still_idles_out(monkeypatch):
    """Every /play that failed after joining (private video, too long, ...) left the bot in
    voice with no loop and therefore no idle timer."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env, idle=0.03)
    env.cog.ytdl.error = LookupError("That video is private.")
    ctx, sink = make_ctx(env, a)
    await Music.play.callback(env.cog, ctx, query="https://y/private")
    assert sink.sent == ["❌ That video is private."]
    try:
        await until(lambda: not player.connected, timeout=1)
    finally:
        await teardown(player)


# ============================================ who may move or dismiss the bot
async def test_play_from_another_channel_cannot_steal_the_bot_during_a_drop(monkeypatch):
    """`connected` is False while discord.py recovers a drop, and the busy check was gated
    on it, so anyone elsewhere could move the bot away from its listeners and their queue."""
    env = _env(monkeypatch)
    g = env.guild
    a, b = FakeChannel(g, "A", humans=3), FakeChannel(g, "B")
    player = make_player(env)
    await player.connect(a)
    player.queue.extend([track("t1"), track("t2")])
    g.voice_client._connected = False                 # uplink blip, discord.py reconnecting
    ctx, sink = make_ctx(env, b)
    try:
        assert await Music._join_author_channel(env.cog, ctx, player) is False
        assert player.channel is a
        assert sink.sent and "busy in **A**" in sink.sent[0]
    finally:
        await teardown(player)


async def test_leave_from_outside_the_channel_is_refused_while_people_listen(monkeypatch):
    """/leave skipped the same-channel rule every other control command has, so anyone in
    the server could disconnect the bot from its listeners and wipe their queue."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A", humans=2)
    player = make_player(env)
    await player.connect(a)
    player.queue.extend([track("t1")])
    ctx, sink = make_ctx(env, None, author=_member(channel=None))
    await Music.leave.callback(env.cog, ctx)
    try:
        assert sink.sent == ["You need to be in **A** to make me leave."]
        assert player.connected and len(player.queue) == 1
    finally:
        await teardown(player)


async def test_leave_counts_listeners_the_member_cache_cannot_resolve(monkeypatch):
    """Without the members intent `channel.members` can come back empty."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A", humans=1)
    for m in a.humans:
        del env.guild.members[m.id]                   # in voice, but not in the member cache
    player = make_player(env)
    await player.connect(a)
    ctx, sink = make_ctx(env, None, author=_member(channel=None))
    await Music.leave.callback(env.cog, ctx)
    try:
        assert player.connected, sink.sent
    finally:
        await teardown(player)


async def test_leave_is_allowed_from_the_channel_for_moderators_or_when_nobody_listens(monkeypatch):
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A", humans=1)
    player = make_player(env)

    async def leave_as(author):
        await player.connect(a)
        ctx, sink = make_ctx(env, a, author=author)
        await Music.leave.callback(env.cog, ctx)
        return sink.sent, player.connected

    assert await leave_as(a.humans[0]) == (["👋 Bye."], False)     # a listener
    mod = _member(channel=None)
    mod.guild_permissions.move_members = True
    assert await leave_as(mod) == (["👋 Bye."], False)             # a moderator elsewhere
    a.humans.clear()
    assert await leave_as(_member(channel=None)) == (["👋 Bye."], False)   # nobody listening


# ============================================ joining a full channel, or a Stage
async def test_full_channel_is_refused_at_once(monkeypatch):
    """Discord never answers a join into a full channel without Move Members: connect() sat
    out its 30 s timeout and then blamed flaky voice servers."""
    env = _env(monkeypatch)
    duo = FakeChannel(env.guild, "Duo", humans=2, user_limit=2)
    player = make_player(env)
    ctx, sink = make_ctx(env, duo, author=duo.humans[0])
    assert await Music._join_author_channel(env.cog, ctx, player) is False
    assert duo.connects == 0, "it should not even try"
    assert sink.sent and "**Duo** is full" in sink.sent[0]


async def test_full_channel_counts_occupants_the_member_cache_cannot_resolve(monkeypatch):
    """Without the members intent `channel.members` drops uncached occupants, so a full
    channel read as having room and the 30 s connect timeout was back."""
    env = _env(monkeypatch)
    duo = FakeChannel(env.guild, "Duo", humans=2, user_limit=2)
    for m in duo.humans:
        del env.guild.members[m.id]                   # in voice, but not in the member cache
    player = make_player(env)
    ctx, sink = make_ctx(env, duo, author=duo.humans[0])
    assert await Music._join_author_channel(env.cog, ctx, player) is False
    assert duo.connects == 0
    assert sink.sent and "**Duo** is full" in sink.sent[0]


async def test_full_channel_is_fine_with_move_members_or_when_already_in_it(monkeypatch):
    env = _env(monkeypatch)
    duo = FakeChannel(env.guild, "Duo", humans=2, user_limit=2)
    player = make_player(env)
    ctx, sink = make_ctx(env, duo, author=duo.humans[0])
    duo.perms.move_members = True
    try:
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        duo.perms.move_members = False                # the bot is already in it now
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        assert sink.sent == []
    finally:
        await teardown(player)


class _Stage(discord.StageChannel):
    """A real StageChannel subclass (the cog tests isinstance) with the fake channel's voice."""

    def __init__(self, guild):
        self._fake = FakeChannel(guild, "Stage")
        self.guild, self.name, self.id, self.user_limit = guild, "Stage", self._fake.id, 0

    members = property(lambda self: self._fake.members)
    voice_states = property(lambda self: self._fake.voice_states)

    def permissions_for(self, obj):
        return self._fake.perms

    async def connect(self, **kw):
        vc = await self._fake.connect(**kw)
        vc.channel = self
        self.guild.me.voice = types.SimpleNamespace(channel=self, suppress=True)   # audience
        return vc


def _http_error(cls, status):
    return cls(types.SimpleNamespace(status=status, reason="nope"), "nope")


async def test_stage_join_asks_to_speak(monkeypatch):
    """Everyone joins a Stage suppressed. The bot never unsuppressed itself, so it 'played'
    to complete silence while /status said playing."""
    env = _env(monkeypatch)
    me, calls = env.guild.me, []

    async def edit(**kw):
        calls.append(("edit", kw))
        me.voice.suppress = False

    me.edit = edit
    stage = _Stage(env.guild)
    player = make_player(env)
    ctx, sink = make_ctx(env, stage)
    try:
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        assert calls == [("edit", {"suppress": False})]
        # a later /play there: already a speaker, so no PATCH (or 403 retry) on every command
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        assert calls == [("edit", {"suppress": False})]
        assert sink.sent == []
    finally:
        await teardown(player)


async def test_stage_join_without_moderator_rights_requests_to_speak_and_says_so(monkeypatch):
    env = _env(monkeypatch)
    me, calls = env.guild.me, []

    async def edit(**kw):
        raise _http_error(discord.Forbidden, 403)

    async def request_to_speak():
        calls.append("request")
        raise _http_error(discord.Forbidden, 403)    # may be denied too; the join still stands

    me.edit, me.request_to_speak = edit, request_to_speak
    stage = _Stage(env.guild)
    player = make_player(env)
    ctx, sink = make_ctx(env, stage)
    try:
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        assert calls == ["request"]
        assert len(sink.sent) == 1 and "Stage moderator" in sink.sent[0]
    finally:
        await teardown(player)


async def test_stage_unsuppress_failing_otherwise_does_not_fail_the_join(monkeypatch):
    env = _env(monkeypatch)

    async def edit(**kw):
        raise _http_error(discord.HTTPException, 500)

    env.guild.me.edit = edit
    stage = _Stage(env.guild)
    player = make_player(env)
    ctx, sink = make_ctx(env, stage)
    try:
        assert await Music._join_author_channel(env.cog, ctx, player) is True
        assert sink.sent == []
    finally:
        await teardown(player)


# ============================================ /play details
async def test_play_strips_discord_angle_brackets_from_links(monkeypatch):
    """`<url>` stops Discord unfurling a link; left on, resolve() saw no URL and searched
    YouTube for the literal text."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env)
    ctx, _ = make_ctx(env, a)
    try:
        await Music.play.callback(env.cog, ctx, query=" <https://soundcloud.com/a/b> ")
        assert env.cog.ytdl.queries == ["https://soundcloud.com/a/b"]
        await Music.play.callback(env.cog, ctx, query="<3 song")    # not a bracketed pair
        assert env.cog.ytdl.queries[-1] == "<3 song"
    finally:
        await teardown(player)


async def test_play_reports_the_real_position_while_the_first_track_resolves(monkeypatch):
    """`current` is None while a stream resolves, so a song queued behind 20 others was
    confirmed as '▶️ Playing next'."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env)
    await player.connect(a)
    player._loading = track("pl0")                    # the loop is resolving track 1
    player.queue.extend([track(f"pl{i}") for i in range(1, 20)])
    monkeypatch.setattr(player, "ensure_loop", lambda: None)   # keep the loop out of it
    ctx, sink = make_ctx(env, a)
    await Music.play.callback(env.cog, ctx, query="A")
    embed = sink.sent[-1]
    assert embed.title == "➕ Added to queue"
    assert {f.name: f.value for f in embed.fields}["Position"] == "20"
    player._loading = None
    await teardown(player)


async def test_play_reports_a_position_whenever_something_is_ahead_of_it(monkeypatch):
    """Either term alone misses a case: a stream resolving with nothing queued behind it, and
    tracks queued that the loop has not picked up yet (both with `current` None)."""
    env = _env(monkeypatch)
    a = FakeChannel(env.guild, "A")
    player = make_player(env)
    await player.connect(a)
    monkeypatch.setattr(player, "ensure_loop", lambda: None)   # keep the loop out of it
    ctx, sink = make_ctx(env, a)
    try:
        player._loading = track("resolving")          # the loop is resolving; queue empty
        await Music.play.callback(env.cog, ctx, query="A")
        assert sink.sent[-1].title == "➕ Added to queue"
        assert {f.name: f.value for f in sink.sent[-1].fields}["Position"] == "1"

        player._loading = None
        player.queue.clear()
        player.queue.extend([track("q1"), track("q2")])   # queued, loop not busy yet
        await Music.play.callback(env.cog, ctx, query="B")
        assert sink.sent[-1].title == "➕ Added to queue"
        assert {f.name: f.value for f in sink.sent[-1].fields}["Position"] == "3"
    finally:
        player._loading = None
        await teardown(player)


# ============================================ shutdown
async def test_unload_leaves_every_guild_concurrently_and_within_a_bound(monkeypatch):
    """With the gateway down every voice disconnect waits 30 s for an echo. One guild at a
    time, shutdown outran docker's stop_grace_period and the container was SIGKILLed."""
    env = _env(monkeypatch)
    monkeypatch.setattr(Music, "UNLOAD_TIMEOUT", 0.05)
    started, cancelled = [], []

    class _Hung:
        async def disconnect(self):
            started.append(self)
            try:
                await asyncio.Event().wait()          # an echo that never comes
            except asyncio.CancelledError:
                cancelled.append(self)
                raise

    env.cog.players = {1: _Hung(), 2: _Hung(), 3: _Hung()}
    try:
        await asyncio.wait_for(env.cog.cog_unload(), timeout=1)
    except TimeoutError:
        raise AssertionError(f"shutdown did not finish; {len(started)} of 3 guilds were asked to leave") from None
    assert len(started) == 3, "every guild should have been asked to leave at once"
    assert env.cog.players == {}
    # abandoned, not left running into the loop's shutdown
    await until(lambda: len(cancelled) == 3, timeout=0.5)
