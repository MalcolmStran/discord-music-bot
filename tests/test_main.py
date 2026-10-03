"""bot/__main__.py: process lifecycle, the slash-sync fingerprint and the shared error handler.

Driven through the real MusicBot. Nothing here logs in: the gateway is faked by patching
is_ready/latency/start, and the watchdog gets a tiny limit and an injected exit function
so it can fire in milliseconds without ending the test process.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

import discord
import pytest
from discord import app_commands
from discord.ext import commands

import bot.__main__ as entry
from bot.__main__ import MusicBot, build_help, start_watchdog
from bot.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(token="x", download_dir=tmp_path / "dl", log_dir=tmp_path / "logs")


@pytest.fixture
def mbot(tmp_path):
    return MusicBot(_cfg(tmp_path))


# --- watchdog -----------------------------------------------------------------------------

class _Exit:
    def __init__(self):
        self.codes = []
        self.called = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self.called.set()


def test_watchdog_exits_once_the_gateway_has_been_dead_too_long(mbot, caplog):
    """A failing HEALTHCHECK only marks the container unhealthy; Docker restarts nothing
    until the process exits. The watchdog is what makes a dead gateway end the process."""
    dead = _Exit()
    mbot.last_beat = time.monotonic() - 5
    with caplog.at_level(logging.CRITICAL, logger="bot"):
        t = start_watchdog(mbot, limit=1, poll=0.005, exit_fn=dead)
        assert dead.called.wait(2), "a stale heartbeat must end the process"
        t.join(1)
    assert dead.codes == [1]
    assert t.daemon, "a daemon thread, so it never keeps a finished process alive"
    assert any("gateway dead" in r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL)


def test_watchdog_is_not_armed_before_the_first_good_heartbeat(mbot):
    """A slow login or first READY must never count as a dead gateway."""
    dead = _Exit()
    assert mbot.last_beat is None
    t = start_watchdog(mbot, limit=0, poll=0.005, exit_fn=dead)
    assert not dead.called.wait(0.1), "fired before the gateway was ever alive"
    mbot.last_beat = time.monotonic() - 1     # now arm it, which also ends the thread
    assert dead.called.wait(2)
    t.join(1)


def test_watchdog_leaves_a_live_gateway_alone(mbot):
    dead = _Exit()
    mbot.last_beat = time.monotonic()
    t = start_watchdog(mbot, limit=60, poll=0.005, exit_fn=dead)
    assert not dead.called.wait(0.1), "a fresh heartbeat must not trigger an exit"
    mbot.last_beat = time.monotonic() - 120
    assert dead.called.wait(2)
    t.join(1)


async def _beat_once(mbot, monkeypatch, *, ready=True):
    monkeypatch.setattr(MusicBot, "is_ready", lambda self: ready)
    monkeypatch.setattr(MusicBot, "latency", property(lambda self: 0.05))
    task = asyncio.create_task(mbot._heartbeat())
    try:
        for _ in range(100):
            if mbot.last_beat is not None:
                break
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_heartbeat_feeds_the_watchdog_and_touches_the_health_file(mbot, monkeypatch):
    before = time.monotonic()
    await _beat_once(mbot, monkeypatch)
    assert mbot.last_beat is not None and mbot.last_beat >= before
    assert (mbot.cfg.log_dir / "healthy").exists()


async def test_heartbeat_does_not_feed_the_watchdog_while_the_gateway_is_down(mbot, monkeypatch):
    await _beat_once(mbot, monkeypatch, ready=False)
    assert mbot.last_beat is None


async def test_an_unwritable_logs_dir_does_not_starve_the_watchdog(tmp_path, monkeypatch):
    """The health file is for Docker's status display; failing to write it must not get a
    perfectly connected bot killed every ten minutes."""
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory")
    b = MusicBot(Config(token="x", download_dir=tmp_path / "dl", log_dir=blocker / "sub"))
    await _beat_once(b, monkeypatch)
    assert b.last_beat is not None


# --- main(): watchdog gating and the privileged-intent exit -------------------------------

@pytest.fixture
def fake_main(tmp_path, monkeypatch):
    """main() with a bot whose login fails the way discord.py fails on close code 4014."""
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: _cfg(tmp_path)))
    monkeypatch.setattr(entry, "setup_logging", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(entry, "start_watchdog", lambda b, **k: started.append(b))

    async def start(self, token, **kw):
        raise discord.PrivilegedIntentsRequired(None)

    monkeypatch.setattr(MusicBot, "start", start)
    return started


async def test_missing_message_content_intent_exits_with_one_clear_line(fake_main, monkeypatch):
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    with pytest.raises(SystemExit) as exc:
        await entry.main()
    msg = str(exc.value.code)
    assert "Message Content" in msg and "Privileged Gateway Intents" in msg


@pytest.mark.parametrize(("env", "expect"), [(None, False), ("true", True), ("false", False)])
async def test_watchdog_runs_only_under_docker(fake_main, monkeypatch, env, expect):
    """A bare `python -m bot` has no supervisor to bring it back, so it must never be
    killed for riding out a long Discord outage."""
    if env is None:
        monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("DOCKER_CONTAINER", env)
    with pytest.raises((SystemExit, discord.PrivilegedIntentsRequired)):
        await entry.main()
    assert bool(fake_main) is expect


# --- command_signature --------------------------------------------------------------------

def _bot_with(tmp_path, *, describe="the mode", choices=("off", "one"), app_id=None):
    b = MusicBot(_cfg(tmp_path))
    build_help(b)

    @b.hybrid_command(name="extra", description="an extra command")
    @app_commands.describe(mode=describe)
    @app_commands.choices(mode=[app_commands.Choice(name=c, value=c) for c in choices])
    async def extra(ctx, mode: str):
        pass

    b._connection.application_id = app_id
    return b


def test_signature_is_stable_for_an_identical_surface(tmp_path):
    assert _bot_with(tmp_path).command_signature() == _bot_with(tmp_path).command_signature()


def test_signature_sees_a_changed_option_description(tmp_path):
    assert (_bot_with(tmp_path, describe="old").command_signature()
            != _bot_with(tmp_path, describe="NEW").command_signature())


def test_signature_sees_changed_choices(tmp_path):
    """A new LoopMode value changes /loop's choices; the old fingerprint ignored them, so
    the sync was skipped and the new mode could never be picked from slash."""
    assert (_bot_with(tmp_path, choices=("off", "one")).command_signature()
            != _bot_with(tmp_path, choices=("off", "one", "all")).command_signature())


def test_signature_sees_a_different_application(tmp_path):
    """The same data volume pointed at another bot must still get that bot synced."""
    assert (_bot_with(tmp_path, app_id=111).command_signature()
            != _bot_with(tmp_path, app_id=222).command_signature())


# --- on_command_error ---------------------------------------------------------------------

class _Cmd:
    qualified_name = "remove"
    signature = "<index>"


class _Ctx:
    cog = None
    prefix = "!"
    command = _Cmd()

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


@pytest.mark.parametrize("error", [
    commands.ExpectedClosingQuoteError('"'),
    commands.UnexpectedQuoteError('"'),
    commands.InvalidEndOfQuotedStringError("x"),
    commands.TooManyArguments(),
    commands.BadArgument(),
])
async def test_a_typo_gets_the_usage_line_not_an_internal_error(mbot, caplog, error):
    """`!remove "1` raises ExpectedClosingQuoteError, a UserInputError but not a
    BadArgument; it used to be reported as a crash and logged with an ERROR traceback."""
    ctx = _Ctx()
    with caplog.at_level(logging.ERROR, logger="bot"):
        await mbot.on_command_error(ctx, error)
    assert ctx.sent == ["Usage: `!remove <index>`"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- wiring -------------------------------------------------------------------------------
def test_song_duration_cap_reaches_the_resolver(tmp_path):
    """MAX_SONG_DURATION was only checked against resolve-time metadata, which flat playlist
    entries and Spotify matches don't have. The resolver needs the cap to enforce it at play time."""
    cfg = Config(token="x", download_dir=tmp_path / "dl", log_dir=tmp_path / "logs", max_song_duration=600)
    assert MusicBot(cfg).ytdl.max_duration == 600
