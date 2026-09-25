"""on_message gating: which links the auto-converter picks up, and whose.

Drives the real Media.on_message with stand-ins for Discord and for the conversion itself,
because this is where both the embed-fixer rule and the per-user opt-out actually apply.
"""
from pathlib import Path

import pytest

from bot.cogs.media import Media
from bot.core.settings import GuildSettings


class _Author:
    def __init__(self, uid, is_bot=False):
        self.id, self.bot = uid, is_bot


class _Guild:
    id = 7
    name = "guild"


class _Message:
    _seq = 0

    def __init__(self, content, uid=100, is_bot=False, guild=True):
        _Message._seq += 1
        self.id = _Message._seq
        self.content = content
        self.author = _Author(uid, is_bot)
        self.guild = _Guild() if guild else None


class _Cfg:
    prefix = "!"


@pytest.fixture
def cog(tmp_path: Path):
    """A Media cog with only the attributes on_message touches."""
    c = Media.__new__(Media)
    c.cfg = _Cfg()
    c.settings = GuildSettings(tmp_path / "s.json", media_default=True)
    c._inflight = set()
    c.converted = []

    async def _convert(message, url, kind, *, reply_errors):
        c.converted.append((url, kind))
        return True

    async def _not_a_command(message):
        return False

    c.convert_and_send = _convert
    c._is_command_invocation = _not_a_command
    return c


async def urls(cog, content, uid=100, **kw):
    cog.converted.clear()
    await cog.on_message(_Message(content, uid=uid, **kw))
    return [u for u, _ in cog.converted]


async def test_a_real_post_is_converted(cog):
    assert await urls(cog, "look https://x.com/a/status/1") == ["https://x.com/a/status/1"]


@pytest.mark.parametrize("link", [
    "https://fxtwitter.com/a/status/1",
    "https://vxtwitter.com/a/status/1",
    "https://fixupx.com/a/status/1",
    "https://fixvx.com/a/status/1",
    "https://twittpr.com/a/status/1",
    "https://vxtiktok.com/@u/video/1",
    "https://tnktok.com/@u/video/1",
])
async def test_embed_fixer_links_are_skipped(cog, link):
    """The poster already arranged a working embed; converting duplicates the video."""
    assert await urls(cog, f"check this {link}") == []


@pytest.mark.parametrize("link", [
    "https://vm.tiktok.com/ZM1/",
    "https://vt.tiktok.com/ZS1/",
])
async def test_official_tiktok_shorteners_are_still_converted(cog, link):
    """These redirect to a normal post — they are not embed front-ends."""
    assert await urls(cog, link) == [link]


async def test_a_fixer_link_does_not_suppress_a_real_one_in_the_same_message(cog):
    got = await urls(cog, "https://fxtwitter.com/a/1 and https://x.com/b/status/2")
    assert got == ["https://x.com/b/status/2"]


async def test_opted_out_user_is_left_alone(cog):
    assert await urls(cog, "https://x.com/a/status/1", uid=555) == ["https://x.com/a/status/1"]
    cog.settings.set_media_optout(555, True)
    assert await urls(cog, "https://x.com/a/status/1", uid=555) == []


async def test_the_opt_out_is_per_user(cog):
    cog.settings.set_media_optout(555, True)
    assert await urls(cog, "https://x.com/a/status/1", uid=555) == []
    assert await urls(cog, "https://x.com/a/status/1", uid=777) == ["https://x.com/a/status/1"]


async def test_opting_back_in_restores_conversion(cog):
    cog.settings.set_media_optout(555, True)
    cog.settings.set_media_optout(555, False)
    assert await urls(cog, "https://x.com/a/status/1", uid=555) == ["https://x.com/a/status/1"]


async def test_the_opt_out_survives_a_restart(cog, tmp_path: Path):
    cog.settings.set_media_optout(555, True)
    cog.settings = GuildSettings(tmp_path / "s.json")     # as if the process restarted
    assert await urls(cog, "https://x.com/a/status/1", uid=555) == []


async def test_guild_level_disable_still_wins(cog):
    cog.settings.set_media_enabled(_Guild.id, False)
    assert await urls(cog, "https://x.com/a/status/1") == []


async def test_bots_dms_and_empty_messages_are_ignored(cog):
    assert await urls(cog, "https://x.com/a/status/1", is_bot=True) == []
    assert await urls(cog, "https://x.com/a/status/1", guild=False) == []
    assert await urls(cog, "") == []


async def test_at_most_two_links_per_message(cog):
    many = " ".join(f"https://x.com/u/status/{i}" for i in range(5))
    assert len(await urls(cog, many)) == 2


async def test_unsupported_links_are_ignored(cog):
    assert await urls(cog, "https://youtube.com/watch?v=1 https://example.com/a") == []
