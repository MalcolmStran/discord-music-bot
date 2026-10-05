"""Auto-convert Twitter/X and TikTok links into uploaded MP4s (+ /convert, /media-* commands)."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core import video
from ..core.settings import GuildSettings

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.I)
# A ||spoiler|| span, non-greedy and across lines. A stray "||" can make a link look
# spoilered when it is not, which only ever errs towards hiding it.
_SPOILER_RE = re.compile(r"\|\|.+?\|\|", re.S)
_ON_WORDS = ("on", "yes", "true", "1", "enable", "enabled", "start")
_OFF_WORDS = ("off", "no", "false", "0", "disable", "disabled", "stop")
# Trailing characters Discord markdown / prose commonly glues onto a link.
_TRAILING = ").,!?;:'\"|*_~`"
# Third-party front-ends whose whole purpose is to render a playable inline embed, mapped to
# the site each one fronts. Someone who posts one has already solved the embed problem, so
# auto-converting it just duplicates the video underneath their message. They stay in
# SUPPORTED so an explicit /convert still works — this only suppresses the automatic
# listener. One dict is the single home for the list: it used to be spelled out again in
# SUPPORTED and a third time in normalise()'s regex, and the regex copy fell out of step.
EMBED_FIXERS = {
    "fxtwitter.com": "twitter",      # FixTweet
    "fixupx.com": "twitter",
    "twittpr.com": "twitter",
    "vxtwitter.com": "twitter",      # BetterTwitFix
    "fixvx.com": "twitter",
    "vxtiktok.com": "tiktok",        # the TikTok equivalents
    "tnktok.com": "tiktok",
}

# Where a fixer link has to be rewritten to before yt-dlp sees it.
CANONICAL_HOST = {"twitter": "x.com", "tiktok": "www.tiktok.com"}

# The genuine sites. tiktok's vm./vt. shorteners are covered by the suffix match and are
# deliberately NOT fixers: they redirect to an ordinary post and still need converting.
_REAL_DOMAINS = {
    "tiktok": ("tiktok.com",),
    "twitter": ("twitter.com", "x.com"),
}

SUPPORTED = {
    kind: (*domains, *(f for f, k in EMBED_FIXERS.items() if k == kind))
    for kind, domains in _REAL_DOMAINS.items()
}

# Real TikTok hosts that yt-dlp's TikTok extractors only match as www.tiktok.com. Once
# video.download() stopped falling back to the generic extractor (which used to follow
# TikTok's redirect), these links failed outright unless rewritten.
_TIKTOK_ALIASES = {"tiktok.com", "m.tiktok.com"}


def _host(url: str) -> str:
    """Hostname of an http(s) URL, lowercased, or "" if it is not one we should touch.

    Everything host-based goes through here. Hand-rolling it used to be a hole: the old
    splitter only cut at "/" and ":", so a fragment or query could smuggle the allowlisted
    suffix past it and make the bot fetch anything —
    ``https://127.0.0.1#.x.com/`` classified as twitter and got downloaded.
    """
    try:
        parts = urlsplit(url.strip().rstrip(_TRAILING))
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    try:
        return (parts.hostname or "").lower().rstrip(".")
    except ValueError:      # malformed IPv6 literal / bad port
        return ""


def _matched_domain(host: str, domains) -> Optional[str]:
    """The entry of `domains` that `host` is, or is a subdomain of."""
    if not host:
        return None
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


def classify_host(host: str) -> Optional[str]:
    for kind, domains in SUPPORTED.items():
        if _matched_domain(host, domains):
            return kind
    return None


def classify(url: str) -> Optional[str]:
    """Return "tiktok"/"twitter" for a link we handle, else None."""
    return classify_host(_host(url))


def fixer_domain(url: str) -> Optional[str]:
    """The fixer domain this URL belongs to, or None. Subdomains count (d.fxtwitter.com)."""
    return _matched_domain(_host(url), EMBED_FIXERS)


def is_embed_fixer(url: str) -> bool:
    """True for a link that already embeds its own video, so we should leave it alone."""
    return fixer_domain(url) is not None


def normalise(url: str, kind: str) -> str:
    """Point a fixer link (or a bare / m. TikTok link) at the canonical host yt-dlp
    expects, keeping the path and query.

    Rewriting the host through the parser rather than a "www.-or-nothing" prefix regex is
    what makes subdomains work: that regex left ``d.fxtwitter.com`` — which
    classify() and is_embed_fixer() both accept — completely untouched, so an explicit
    /convert handed the third-party host to yt-dlp instead of x.com.
    """
    url = url.strip().rstrip(_TRAILING)
    if _host(url) in _TIKTOK_ALIASES:
        parts = urlsplit(url)
        return urlunsplit(("https", CANONICAL_HOST["tiktok"], parts.path, parts.query, ""))
    fixer = fixer_domain(url)
    if not fixer:
        return url
    target = CANONICAL_HOST.get(EMBED_FIXERS[fixer])
    if not target:
        return url
    parts = urlsplit(url)
    return urlunsplit(("https", target, parts.path, parts.query, ""))


def _post_key(url: str, kind: str) -> tuple[str, str, str]:
    """Which post a supported link points at, to spot the same one twice in a message.

    The raw string was not enough: x.com / twitter.com / mobile.twitter.com forms and
    ?s=20 share-tracking queries of one tweet each got converted and uploaded again. The
    query is dropped because the post id lives in the path on both sites.
    """
    parts = urlsplit(normalise(url, kind))
    host = CANONICAL_HOST["twitter"] if kind == "twitter" else (parts.hostname or "").lower()
    return kind, host, parts.path.rstrip("/")


class Media(commands.Cog):
    """Twitter/X & TikTok → MP4, compressed to fit the server's upload limit."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = bot.cfg                          # type: ignore[attr-defined]
        self.settings: GuildSettings = bot.settings  # type: ignore[attr-defined]
        self.workdir: Path = self.cfg.media_tmp_dir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = self.cfg.max_download_mb * 1024 * 1024
        self._inflight: set[int] = set()            # message ids being processed
        # Work-dir files each running convert_and_send still needs, one entry per job so two
        # jobs can never drop each other's paths. Cleanup skips them (see _in_use).
        self._busy: dict[object, set[Path]] = {}
        self.stats = {"ok": 0, "failed": 0, "compressed": 0, "gif": 0, "skipped": 0}
        video.configure(self.cfg.max_concurrent_encodes)
        self.cleanup_loop.start()

    def cog_unload(self):
        self.cleanup_loop.cancel()

    def _in_use(self) -> frozenset[Path]:
        """Snapshot of every running job's files, taken on the event loop: the cleanup
        thread must never iterate sets the loop is still changing."""
        return frozenset(p for paths in self._busy.values() for p in paths)

    @tasks.loop(minutes=30)
    async def cleanup_loop(self):
        # An unhandled exception here would stop the loop for the rest of the process
        # lifetime and the temp dir would grow forever, so swallow and keep going.
        try:
            # A job can outlive the hour (three rungs × two passes × the encode timeout,
            # plus queueing for a slot), so its source is skipped, not judged by age.
            n = await asyncio.to_thread(video.cleanup_dir, self.workdir, 3600, self._in_use())
        except Exception:
            log.exception("media cleanup failed")
            return
        if n:
            log.info("media cleanup removed %d stale files", n)

    @cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ listener
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Forget a guild we were removed from — the settings file only ever grew."""
        await asyncio.to_thread(self.settings.forget_guild, guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        if await self._is_command_invocation(message):
            return  # the command path handles it (/convert), don't convert twice
        if not self.settings.media_enabled(message.guild.id):
            return
        spoilers = [m.span() for m in _SPOILER_RE.finditer(message.content)]
        # One parse per URL: classify() and is_embed_fixer() each re-parsed it otherwise.
        links: dict[tuple[str, str, str], list] = {}   # post -> [url, kind, spoiler], first seen wins
        found: list[Optional[tuple[str, str, str]]] = []  # each URL's post, None if we don't convert it
        skipped, any_spoiler = 0, False
        for m in URL_RE.finditer(message.content):
            u = m.group()
            # Test where the link STARTS: URL_RE runs on through the closing "||", so the
            # match always ends past the spoiler span it sits in.
            spoiler = any(s <= m.start() < e for s, e in spoilers)
            any_spoiler = any_spoiler or spoiler
            found.append(None)
            host = _host(u)
            kind = classify_host(host)
            if not kind:
                continue
            if _matched_domain(host, EMBED_FIXERS):
                # already embeds its own video; converting would post the clip twice
                skipped += 1
                continue
            if not video.downloadable(normalise(u, kind)):
                # A profile, hashtag, live or Space link: download() can only refuse it, so
                # it must not flash ⏳, count as failed, or take a slot from a real post.
                # Its key stays None, so the message keeps its embeds.
                skipped += 1
                continue
            key = found[-1] = _post_key(u, kind)
            if key in links:
                links[key][2] = links[key][2] or spoiler    # spoilered anywhere → upload blurred
            else:
                links[key] = [u, kind, spoiler]
        self.stats["skipped"] += skipped
        if not links:
            return
        # Checked here rather than above: this is the only point where the answer matters,
        # and the lookup builds a set, which is wasted on every message with no link at all.
        if self.settings.is_media_optout(message.author.id):
            return  # this person asked us to leave their posts alone (/autoconvert off)
        # Don't spend a download and an encode slot on an upload Discord will refuse: in a
        # channel the bot may not post in, every link used to be fetched and compressed only
        # to fail with 403 on the final reply. attach_files is already False wherever the
        # bot can't send (discord.py applies that for threads too); a reply also needs Read
        # Message History. A thread whose parent isn't cached raises, so try as before. So
        # does any other channel type: discord.py hands over a PartialMessageable for a
        # channel or thread it hasn't cached, and its permissions_for() is always none(),
        # which read as "may not post" and silently dropped every link there.
        perms = None
        if isinstance(message.channel, (discord.abc.GuildChannel, discord.Thread)):
            try:
                perms = message.channel.permissions_for(message.guild.me)
            except discord.ClientException:
                pass
        if perms is not None and not (perms.attach_files and perms.read_message_history):
            log.debug("no permission to upload in channel %s; leaving its links alone", message.channel.id)
            return
        # at most 2 videos per message, and never process the same message twice
        if message.id in self._inflight:
            return
        self._inflight.add(message.id)
        try:
            done = set()
            for key, (url, kind, spoiler) in list(links.items())[:2]:
                if await self.convert_and_send(message, normalise(url, kind), kind, reply_errors=False,
                                               suppress_embeds=False, spoiler=spoiler):
                    done.add(key)
            # Discord's suppress flag removes EVERY embed on the message, so drop them only
            # once each link in it has been replaced by an upload. Suppressing per conversion
            # wiped the embeds of a YouTube link beside the tweet, of a third link past the
            # cap, of a link whose conversion failed, and of an embed-fixer link (which the
            # listener skipped precisely to keep its embed). A spoilered link's embed is the
            # blurred copy the poster chose, so a spoiler anywhere keeps them all.
            if not any_spoiler and all(k in done for k in found):
                try:
                    await message.edit(suppress=True)
                except discord.HTTPException:
                    pass
        finally:
            self._inflight.discard(message.id)

    async def _is_command_invocation(self, message: discord.Message) -> bool:
        """True if this message is a real command: a prefix the bot answers to AND a known
        command, which the command path handles (/convert), so don't convert twice.

        `commands.when_mentioned_or(...)` means the bot mention is a prefix as well as the
        configured one, so checking only cfg.prefix let `@Bot convert <link>` be converted
        twice. But a prefix alone was too broad: "!!! look <link>" or "@Bot what is this
        <link>" runs no command (CommandNotFound is ignored), so the link was silently
        dropped.
        """
        try:
            ctx = await self.bot.get_context(message)
        except Exception:
            return message.content.startswith(self.cfg.prefix)
        return ctx.valid

    # ---------------------------------------------------------------- core
    async def convert_and_send(self, message: discord.Message, url: str, kind: str, *,
                               reply_errors: bool, suppress_embeds: bool = True, spoiler: bool = False) -> bool:
        guild = message.guild
        assert guild is not None
        limit = guild.filesize_limit                     # honours server boost level
        status: Optional[discord.Message] = None

        async def progress(text: str):
            nonlocal status
            try:
                if status is None:
                    status = await message.reply(text, mention_author=False, silent=True)
                else:
                    await status.edit(content=text)
            except discord.HTTPException:
                pass

        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass
        src: Optional[Path] = None
        out: Optional[Path] = None
        held: set[Path] = set()
        job = object()
        self._busy[job] = held
        try:
            src = await video.download(url, self.workdir, self.max_bytes,
                                       cookies_file=self.cfg.ytdl_cookies_file, rapidapi_key=self.cfg.rapidapi_key)
            held.add(src)       # before any await: /media-cleanup must never see it unclaimed
            info = await video.probe(src)
            target = int(limit * 0.97)
            out = None
            # Which footer counter this job earns ("gif" / "compressed"). Counted only next to
            # "ok": bumping it up front counted a compression that then raised (too long, no
            # rung fit) or an upload Discord rejected as both compressed and failed.
            made_as: Optional[str] = None
            # A silent clip is what GIF is for, and Discord autoplays a GIF inline instead of
            # showing the click-to-play card a muted MP4 gets.
            if video.should_gif(info, self.cfg.max_gif_seconds):
                out = await video.to_gif(src, target, self.workdir, info=info,
                                         timeout=self.cfg.encode_timeout_seconds, progress=progress)
                if out is not None:
                    made_as = "gif"
            if out is None:                      # not silent, too long, or no rung fit
                if src.stat().st_size > limit:
                    out = await video.fit_under(src, target, self.workdir, info=info,
                                                timeout=self.cfg.encode_timeout_seconds, progress=progress)
                    made_as = "compressed"
                else:
                    out = src
            held.add(out)
            ext = out.suffix.lower().lstrip(".") or "mp4"
            # A link posted inside ||spoiler|| tags must not come back as a clip playing inline.
            await message.reply(file=discord.File(out, filename=f"{kind}.{ext}", spoiler=spoiler),
                                mention_author=False)
            self.stats["ok"] += 1
            if made_as:
                self.stats[made_as] += 1
            # Tidy: drop the original embed if we can. Discord's suppress flag applies to the
            # WHOLE message, which is why the listener passes False and decides once, after
            # all its links are done. An explicit /convert still suppresses straight away.
            if suppress_embeds:
                try:
                    await message.edit(suppress=True)
                except discord.HTTPException:
                    pass
            return True
        except video.VideoError as e:
            self.stats["failed"] += 1
            log.info("media convert failed for %s: %s", url, e)
            if reply_errors:
                await message.reply(f"❌ {e}", mention_author=False)
            return False
        except discord.HTTPException as e:
            self.stats["failed"] += 1
            log.warning("upload failed: %s", e)
            if reply_errors:
                await message.reply("❌ Discord rejected the upload (too large, or a network hiccup).",
                                    mention_author=False)
            return False
        except Exception:
            self.stats["failed"] += 1
            log.exception("media convert crashed for %s", url)
            if reply_errors:
                await message.reply("❌ Something went wrong converting that link; it's been logged.",
                                    mention_author=False)
            return False
        finally:
            for p in {src, out}:
                if p:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
            del self._busy[job]
            try:
                await message.remove_reaction("⏳", guild.me)
            except discord.HTTPException:
                pass
            if status:
                try:
                    await status.delete()
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------ commands
    @commands.hybrid_command(name="convert", description="Convert a Twitter/X or TikTok link to an MP4")
    @app_commands.describe(url="Twitter/X or TikTok link")
    @commands.guild_only()
    async def convert(self, ctx: commands.Context, url: str):
        url = url.strip().lstrip("<").rstrip(">")   # users paste <link> to suppress the embed
        if not self.settings.media_enabled(ctx.guild.id):  # type: ignore[union-attr]
            return await ctx.send("🚫 Media conversion is disabled on this server (`/media-toggle` to enable).")
        kind = classify(url)
        if not kind:
            return await ctx.send("❌ Only Twitter/X and TikTok links are supported.")
        if not video.downloadable(normalise(url, kind)):
            # a profile, hashtag, live or Space link: say so now, not after a download slot
            return await ctx.send("❌ That link isn't supported.")
        if ctx.interaction:
            await ctx.interaction.response.send_message(f"⏳ Converting {kind} link…", ephemeral=True)
            # for slash commands we attach to a fresh message so replies have an anchor
            anchor = await ctx.channel.send(f"🎬 Converting <{normalise(url, kind)}> for {ctx.author.mention}",
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            anchor = ctx.message
        await self.convert_and_send(anchor, normalise(url, kind), kind, reply_errors=True)

    @commands.hybrid_command(name="autoconvert",
                             description="Choose whether I auto-convert links you post")
    @app_commands.describe(state="on to let me convert your links, off to leave them alone")
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def autoconvert(self, ctx: commands.Context, state: Optional[str] = None):
        """Anyone can opt themselves out; it applies everywhere the bot is, not just here."""
        opted_out = self.settings.is_media_optout(ctx.author.id)
        if state is None:
            return await ctx.send(
                ("🚫 I currently **don't** auto-convert links you post. `/autoconvert on` to turn it back on."
                 if opted_out else
                 "✅ I currently auto-convert Twitter/X and TikTok links you post. `/autoconvert off` to stop."),
                ephemeral=True)
        value = state.strip().lower()
        if value in _OFF_WORDS:
            want_off = True
        elif value in _ON_WORDS:
            want_off = False
        else:
            # Anything unrecognised used to fall through to "on", so `!autoconvert of`
            # silently deleted an existing opt-out and reported success.
            return await ctx.send("Use `on` or `off`.", ephemeral=True)
        await self.settings.set_media_optout_async(ctx.author.id, want_off)
        await ctx.send(
            ("🚫 Done — I'll leave the links you post alone, in every server I'm in. "
             "`/convert <url>` still works if you want one on purpose."
             if want_off else
             "✅ Done — I'll auto-convert Twitter/X and TikTok links you post again."),
            ephemeral=True)

    @commands.hybrid_command(name="media-toggle", description="Enable/disable automatic link conversion here (admin)")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def media_toggle(self, ctx: commands.Context):
        gid = ctx.guild.id  # type: ignore[union-attr]
        new = not self.settings.media_enabled(gid)
        await self.settings.set_async(gid, "media_enabled", new)
        await ctx.send(f"{'✅ Enabled' if new else '🚫 Disabled'} automatic Twitter/TikTok conversion for this server.")

    @commands.hybrid_command(name="mediainfo", aliases=["media-status"], description="Media conversion status")
    @commands.guild_only()
    async def mediainfo(self, ctx: commands.Context):
        gid = ctx.guild.id  # type: ignore[union-attr]
        ff, fp = video.which_ffmpeg()
        e = discord.Embed(title="🎬 Media conversion", color=0x5865F2)
        e.add_field(name="This server", value="✅ enabled" if self.settings.media_enabled(gid) else "🚫 disabled", inline=True)
        e.add_field(name="Upload limit here", value=f"{ctx.guild.filesize_limit // 1048576} MB", inline=True)  # type: ignore[union-attr]
        e.add_field(name="Max download", value=f"{self.cfg.max_download_mb} MB", inline=True)
        e.add_field(name="TikTok fallback API", value="✅" if self.cfg.rapidapi_key else "— (yt-dlp only)", inline=True)
        e.add_field(name="ffmpeg", value="✅" if ff and fp else "❌ missing", inline=True)
        used = await asyncio.to_thread(video.dir_size, self.workdir)
        e.add_field(name="Temp usage", value=f"{used / 1048576:.1f} MB", inline=True)
        s = self.stats
        e.add_field(name="Your links",
                    value="🚫 not converted" if self.settings.is_media_optout(ctx.author.id) else "✅ converted",
                    inline=True)
        e.add_field(name="Links left alone", value=str(s["skipped"]), inline=True)
        e.add_field(name="Silent clips → GIF",
                    value=f"≤ {self.cfg.max_gif_seconds}s" if self.cfg.max_gif_seconds else "🚫 disabled",
                    inline=True)
        e.set_footer(text=f"session: {s['ok']} ok · {s['failed']} failed · "
                          f"{s['compressed']} compressed · {s['gif']} as GIF")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="media-cleanup", description="Delete temporary media files (admin)")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def media_cleanup(self, ctx: commands.Context):
        # The 60 s floor only protects files still being written (yt-dlp fragments, ffmpeg
        # output). A finished download is only read from then on, by every ffmpeg pass, so
        # it looked stale while a long encode still needed it: running jobs' files are
        # skipped explicitly. The periodic loop reclaims the rest.
        n = await asyncio.to_thread(video.cleanup_dir, self.workdir, 60, self._in_use())
        await ctx.send(f"🧹 Removed {n} temp file(s).")


async def setup(bot: commands.Bot):
    await bot.add_cog(Media(bot))
