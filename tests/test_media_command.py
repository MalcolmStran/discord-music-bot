"""/autoconvert: the per-user opt-out command's own contract.

Driven through the real callback, because the app_commands choices only constrain the slash
form — the prefix form accepts any string.
"""
from pathlib import Path

import pytest

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


async def test_the_command_only_affects_the_caller(cog):
    await run(cog, _Ctx(7), "off")
    assert cog.settings.media_optout() == {7}
    await run(cog, _Ctx(8), "off")
    assert cog.settings.media_optout() == {7, 8}
    await run(cog, _Ctx(7), "on")
    assert cog.settings.media_optout() == {8}
