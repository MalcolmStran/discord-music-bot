"""on_message gating: which links the auto-converter picks up, and whose.

Drives the real Media.on_message with stand-ins for Discord and for the conversion itself,
because this is where both the embed-fixer rule and the per-user opt-out actually apply.
"""
from pathlib import Path

import discord
import pytest

from bot.cogs.media import Media
from bot.core.settings import GuildSettings


class _Author:
    def __init__(self, uid, is_bot=False):
        self.id, self.bot = uid, is_bot


class _Guild:
    id = 7
    name = "guild"
    me = object()


class _Channel(discord.abc.GuildChannel):
    """A guild text channel as far as the permission pre-check can tell."""

    def __init__(self, perms):
        self.perms = perms
        self.id = 42

    def permissions_for(self, member):
        assert member is _Guild.me
        if isinstance(self.perms, Exception):
            raise self.perms
        return self.perms


# What a bot that may post in the channel has.
_CAN_POST = discord.Permissions(view_channel=True, send_messages=True, attach_files=True,
                                read_message_history=True, add_reactions=True)


class _Message:
    _seq = 0

    def __init__(self, content, uid=100, is_bot=False, guild=True, perms=_CAN_POST, channel=None):
        _Message._seq += 1
        self.id = _Message._seq
        self.content = content
        self.author = _Author(uid, is_bot)
        self.guild = _Guild() if guild else None
        self.channel = channel or _Channel(perms)
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


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
    c.suppressed = []
    c.spoilered = []
    c.failing = set()           # URLs whose conversion should fail
    c.last = None
    c.stats = {"ok": 0, "failed": 0, "compressed": 0, "gif": 0, "skipped": 0}

    async def _convert(message, url, kind, *, reply_errors, suppress_embeds=True, spoiler=False):
        c.converted.append((url, kind))
        c.suppressed.append(suppress_embeds)
        c.spoilered.append(spoiler)
        return url not in c.failing

    async def _not_a_command(message):
        return False

    c.convert_and_send = _convert
    c._is_command_invocation = _not_a_command
    return c


async def urls(cog, content, uid=100, **kw):
    cog.converted.clear()
    cog.last = _Message(content, uid=uid, **kw)
    await cog.on_message(cog.last)
    return [u for u, _ in cog.converted]


def suppressed(cog) -> bool:
    """Whether the listener hid the message's embeds."""
    return {"suppress": True} in cog.last.edits


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


@pytest.mark.parametrize("perms", [
    # restricted to a #media channel: can read here, not post
    discord.Permissions(view_channel=True, read_message_history=True, add_reactions=True),
    # may post text but not files
    discord.Permissions(view_channel=True, send_messages=True, read_message_history=True),
    # may post files, but a reply (message_reference) also needs Read Message History
    discord.Permissions(view_channel=True, send_messages=True, attach_files=True),
])
async def test_nothing_is_downloaded_where_the_upload_would_be_refused(cog, perms):
    """Every link was downloaded and compressed, holding the encode slots, only for the
    final reply to fail with 403."""
    assert await urls(cog, "https://x.com/a/status/1", perms=perms) == []


async def test_an_uncached_thread_parent_does_not_stop_conversion(cog):
    """Thread.permissions_for raises when the parent channel isn't cached; that must not
    turn into an error on every message in the thread."""
    got = await urls(cog, "https://x.com/a/status/1", perms=discord.ClientException("Parent channel not found"))
    assert got == ["https://x.com/a/status/1"]


async def test_a_channel_discord_py_has_not_cached_is_tried_anyway(cog):
    """For a channel or thread missing from its cache discord.py hands over a
    PartialMessageable, whose permissions_for() is always none(): read as "may not post",
    every link there was dropped without a word, though replying works fine."""
    channel = discord.PartialMessageable(state=None, id=123, guild_id=_Guild.id)
    assert await urls(cog, "https://x.com/a/status/1", channel=channel) == ["https://x.com/a/status/1"]


async def test_unsupported_links_are_ignored(cog):
    assert await urls(cog, "https://youtube.com/watch?v=1 https://example.com/a") == []


async def test_a_fixer_link_in_the_message_stops_us_suppressing_its_embed(cog):
    """Discord's suppress flag covers the WHOLE message. Suppressing after converting the
    x.com link would destroy the fxtwitter embed the listener skipped that link to keep —
    exactly the outcome this feature exists to prevent."""
    await urls(cog, "https://fxtwitter.com/a/1 and https://x.com/b/status/2")
    assert cog.converted == [("https://x.com/b/status/2", "twitter")]
    assert not suppressed(cog)


async def test_embeds_are_still_suppressed_when_no_fixer_is_present(cog):
    await urls(cog, "https://x.com/b/status/2")
    assert suppressed(cog)
    assert cog.last.edits == [{"suppress": True}], "once, not once per link"


async def test_embeds_are_suppressed_once_after_every_link_converted(cog):
    await urls(cog, "https://x.com/a/status/1 https://www.tiktok.com/@u/video/2")
    assert len(cog.converted) == 2
    assert cog.suppressed == [False, False], "the listener decides once, after the loop"
    assert cog.last.edits == [{"suppress": True}]


# Discord's suppress flag hides every embed on the message, so it may only be set once each
# link in it has been replaced by an upload.
async def test_another_sites_embed_beside_the_tweet_is_kept(cog):
    await urls(cog, "tweet https://x.com/a/status/1 and the song https://youtube.com/watch?v=abc")
    assert cog.converted == [("https://x.com/a/status/1", "twitter")]
    assert not suppressed(cog)


async def test_a_third_link_past_the_cap_keeps_the_embeds(cog):
    await urls(cog, " ".join(f"https://x.com/u/status/{i}" for i in range(3)))
    assert len(cog.converted) == 2
    assert not suppressed(cog)


async def test_a_failed_conversion_keeps_the_embeds(cog):
    cog.failing = {"https://x.com/b/status/2"}
    await urls(cog, "https://x.com/a/status/1 https://x.com/b/status/2")
    assert len(cog.converted) == 2
    assert not suppressed(cog)


async def test_trailing_punctuation_does_not_stop_suppression(cog):
    """Both sides go through the same normalisation, so "(…/1)." still counts as converted."""
    await urls(cog, "see (https://x.com/a/status/1).")
    assert suppressed(cog)


# ------------------------------------------------------------------- ||spoilers||
async def test_a_spoilered_link_is_uploaded_as_a_spoiler_and_keeps_its_blurred_embed(cog):
    await urls(cog, "ending spoiler ||https://x.com/a/status/1||")
    assert cog.converted == [("https://x.com/a/status/1", "twitter")]
    assert cog.spoilered == [True]
    assert not suppressed(cog)


async def test_one_spoilered_link_keeps_every_embed(cog):
    """Suppression is per message: hiding the plain link's embed would hide the blurred
    one too."""
    await urls(cog, "https://x.com/a/status/1 and ||the twist\nhttps://x.com/b/status/2 ||")
    assert cog.spoilered == [False, True]
    assert not suppressed(cog)


async def test_a_link_after_a_closed_spoiler_is_not_spoilered(cog):
    await urls(cog, "||no peeking|| https://x.com/a/status/1")
    assert cog.spoilered == [False]
    assert suppressed(cog)


# ------------------------------------------------------------- the same post twice
@pytest.mark.parametrize("second", [
    "https://x.com/a/status/1",
    "https://x.com/a/status/1).",
    "https://twitter.com/a/status/1",
    "https://mobile.twitter.com/a/status/1?s=20",
    "https://www.x.com/a/status/1/",
])
async def test_the_same_post_twice_is_converted_once(cog, second):
    await urls(cog, f"lol https://x.com/a/status/1 {second}")
    assert cog.converted == [("https://x.com/a/status/1", "twitter")]
    assert suppressed(cog), "every link in the message is the converted post"


async def test_the_cap_counts_distinct_posts(cog):
    """A repeat used to take one of the two slots, silently dropping a different link."""
    await urls(cog, "https://x.com/a/status/1 https://twitter.com/a/status/1 https://x.com/b/status/2")
    assert [u for u, _ in cog.converted] == ["https://x.com/a/status/1", "https://x.com/b/status/2"]


async def test_a_repeat_inside_a_spoiler_blurs_the_upload(cog):
    await urls(cog, "https://x.com/a/status/1 ||https://x.com/a/status/1||")
    assert cog.spoilered == [True]
    assert not suppressed(cog)


async def test_skipped_fixer_links_are_counted(cog):
    await urls(cog, "https://fxtwitter.com/a/1 https://vxtwitter.com/b/2 https://x.com/c/status/3")
    assert cog.stats["skipped"] == 2


# ------------------------------------------------- links no allowed extractor can fetch
@pytest.mark.parametrize("link", [
    "https://www.tiktok.com/@me",
    "https://tiktok.com/@me",
    "https://www.tiktok.com/@me/live",
    "https://www.tiktok.com/tag/cats",
    "https://x.com/me",
    "https://x.com/i/spaces/1zqKVPlQNApJB",
])
async def test_a_profile_link_does_not_take_a_real_posts_slot(cog, link):
    """It used to get ⏳, fail inside yt-dlp, count as failed, and push the second real
    post past the two-per-message cap so it was never converted."""
    got = await urls(cog, f"follow me {link} latest https://www.tiktok.com/@me/video/7300000000000000001 "
                          "and https://x.com/me/status/1800000000000000000")
    assert got == ["https://www.tiktok.com/@me/video/7300000000000000001", "https://x.com/me/status/1800000000000000000"]
    assert cog.stats["skipped"] == 1
    assert not suppressed(cog), "the profile link's own embed is kept"


async def test_a_message_with_only_a_profile_link_starts_nothing(cog):
    assert await urls(cog, "https://www.tiktok.com/@me") == []
    assert cog.stats["skipped"] == 1
    assert cog.last.edits == []


async def test_an_opted_out_user_does_not_bump_the_skip_counter_twice(cog):
    cog.settings.set_media_optout(555, True)
    await urls(cog, "https://x.com/a/status/1", uid=555)
    assert cog.stats["skipped"] == 0
