"""/autoconvert: the per-user opt-out command's own contract.

Driven through the real callback, because the app_commands choices only constrain the slash
form — the prefix form accepts any string. Also: which messages count as a command
invocation, so the auto-converter leaves them to the command path.
"""
from pathlib import Path

import discord
import pytest
from discord.ext import commands

from bot.cogs.media import Media
from bot.core.settings import GuildSettings


class _Author:
    def __init__(self, uid):
        self.id = uid


class _Ctx:
    def __init__(self, uid=100):
        self.author = _Author(uid)
        self.replies = []

    async def send(self, content=None, **kw):
        self.replies.append(content)
        return None


@pytest.fixture
def cog(tmp_path: Path):
    c = Media.__new__(Media)
    c.settings = GuildSettings(tmp_path / "s.json")
    return c


async def run(cog, ctx, state):
    await Media.autoconvert.callback(cog, ctx, state)
    return ctx.replies[-1]


@pytest.mark.parametrize("word", ["off", "OFF", " off ", "no", "false", "0", "disable", "stop"])
async def test_off_words_opt_the_user_out(cog, word):
    ctx = _Ctx(7)
    await run(cog, ctx, word)
    assert cog.settings.is_media_optout(7) is True


@pytest.mark.parametrize("word", ["on", "ON", " on ", "yes", "true", "1", "enable", "start"])
async def test_on_words_opt_the_user_back_in(cog, word):
    cog.settings.set_media_optout(7, True)
    ctx = _Ctx(7)
    await run(cog, ctx, word)
    assert cog.settings.is_media_optout(7) is False


@pytest.mark.parametrize("junk", ["of", "offf", "status", "", "   ", "maybe", "toggle", "-1"])
async def test_an_unrecognised_value_changes_nothing(cog, junk):
    """A typo used to fall through to "on": it deleted an existing opt-out and replied that
    it had succeeded, silently revoking the user's choice."""
    cog.settings.set_media_optout(7, True)
    ctx = _Ctx(7)
    reply = await run(cog, ctx, junk)
    assert cog.settings.is_media_optout(7) is True, "the opt-out must survive a typo"
    assert "on" in reply.lower() and "off" in reply.lower(), "and it must say what is valid"


async def test_no_argument_reports_the_current_state_without_changing_it(cog):
    ctx = _Ctx(7)
    reply = await run(cog, ctx, None)
    assert cog.settings.is_media_optout(7) is False
    assert "auto-convert" in reply

    cog.settings.set_media_optout(7, True)
    ctx2 = _Ctx(7)
    reply2 = await run(cog, ctx2, None)
    assert cog.settings.is_media_optout(7) is True, "reporting must not flip the setting"
    assert reply2 != reply


# ------------------------------------------- which messages the listener leaves to commands
BOT_ID = 999


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    _state = None

    def __init__(self, content):
        self.content = content
        self.author = _User(100)
        self.guild = None


@pytest.fixture
async def real_bot():
    """A real commands.Bot with the production prefix setup and the commands that take a
    link, so get_context resolves exactly as it does in MusicBot."""
    bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=discord.Intents.default(),
                       help_command=None)
    bot._connection.user = _User(BOT_ID)

    @bot.command(name="convert")
    async def convert(ctx, url: str): ...

    @bot.command(name="play")
    async def play(ctx, *, query: str): ...

    yield bot
    await bot.close()


class _Cfg:
    prefix = "!"


@pytest.mark.parametrize("content,is_command", [
    ("!convert https://x.com/a/status/1", True),
    (f"<@{BOT_ID}> convert https://x.com/a/status/1", True),
    ("!play https://x.com/a/status/1", True),
    # a prefix but no command: nothing else will handle these, so the listener must
    ("!!! look at this https://x.com/someone/status/123", False),
    (f"<@{BOT_ID}> what is this https://www.tiktok.com/@a/video/1", False),
    ("! https://x.com/a/status/1", False),
    ("look https://x.com/a/status/1", False),
])
async def test_only_a_real_command_is_left_to_the_command_path(real_bot, content, is_command):
    c = Media.__new__(Media)
    c.bot, c.cfg = real_bot, _Cfg()
    assert await c._is_command_invocation(_Msg(content)) is is_command


# ------------------------------------------------------------------------- /convert
class _Guild:
    id = 7


class _ConvertCtx(_Ctx):
    interaction = None
    guild = _Guild()
    message = object()


@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@someartist",
    "https://x.com/i/spaces/1zqKVPlQNApJB",
    "<https://tiktok.com/@someartist/live>",
])
async def test_convert_refuses_a_link_no_extractor_can_fetch_straight_away(cog, url):
    """It used to queue for a download slot only for yt-dlp to refuse it."""
    started = []

    async def convert_and_send(*a, **kw):
        started.append(a)

    cog.settings.set_media_enabled(_Guild.id, True)
    cog.convert_and_send = convert_and_send
    ctx = _ConvertCtx()
    await Media.convert.callback(cog, ctx, url)
    assert ctx.replies == ["❌ That link isn't supported."]
    assert started == []


async def test_convert_still_converts_a_post(cog):
    started = []

    async def convert_and_send(anchor, url, kind, **kw):
        started.append((url, kind))

    cog.settings.set_media_enabled(_Guild.id, True)
    cog.convert_and_send = convert_and_send
    await Media.convert.callback(cog, _ConvertCtx(), "https://tiktok.com/@u/video/7123456789012345678")
    assert started == [("https://www.tiktok.com/@u/video/7123456789012345678", "tiktok")]


async def test_the_command_only_affects_the_caller(cog):
    await run(cog, _Ctx(7), "off")
    assert cog.settings.media_optout() == {7}
    await run(cog, _Ctx(8), "off")
    assert cog.settings.media_optout() == {7, 8}
    await run(cog, _Ctx(7), "on")
    assert cog.settings.media_optout() == {8}
