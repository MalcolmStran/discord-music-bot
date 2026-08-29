"""Auto-convert Twitter/X and TikTok links into uploaded MP4s (+ /convert, /media-* commands)."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core import video
from ..core.settings import GuildSettings

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.I)
# Trailing characters Discord markdown / prose commonly glues onto a link.
_TRAILING = ").,!?;:'\"|*_~`"
SUPPORTED = {
    "tiktok": ("tiktok.com",),
    "twitter": ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"),
}


def classify(url: str) -> Optional[str]:
    """Return "tiktok"/"twitter" for a link we handle, else None.

    The host is taken from a real URL parse. Hand-rolling this used to be a hole: the old
    splitter only cut at "/" and ":", so a fragment or query could smuggle the allowlisted
    suffix past it and make the bot fetch anything —
    ``https://127.0.0.1#.x.com/`` classified as twitter and got downloaded.
    """
    try:
        parts = urlsplit(url.strip().rstrip(_TRAILING))
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    try:
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:      # malformed IPv6 literal / bad port
        return None
    if not host:
        return None
    for kind, domains in SUPPORTED.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return kind
    return None


def normalise(url: str, kind: str) -> str:
    url = url.strip().rstrip(_TRAILING)
    if kind == "twitter":
        url = re.sub(r"https?://(www\.)?(fxtwitter|vxtwitter|fixupx)\.com", "https://x.com", url, flags=re.I)
    return url


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
        self.stats = {"ok": 0, "failed": 0, "compressed": 0}
        video.configure(self.cfg.max_concurrent_encodes)
        self.cleanup_loop.start()

    def cog_unload(self):
        self.cleanup_loop.cancel()

    @tasks.loop(minutes=30)
    async def cleanup_loop(self):
        # An unhandled exception here would stop the loop for the rest of the process
        # lifetime and the temp dir would grow forever, so swallow and keep going.
        try:
            n = await asyncio.to_thread(video.cleanup_dir, self.workdir, 3600)
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
        links = [(u, k) for u in URL_RE.findall(message.content) if (k := classify(u))]
        if not links:
            return
        # at most 2 videos per message, and never process the same message twice
        if message.id in self._inflight:
            return
        self._inflight.add(message.id)
        try:
            for url, kind in links[:2]:
                await self.convert_and_send(message, normalise(url, kind), kind, reply_errors=False)
        finally:
            self._inflight.discard(message.id)

    async def _is_command_invocation(self, message: discord.Message) -> bool:
        """True if this message starts with any prefix the bot answers to.

        `commands.when_mentioned_or(...)` means the bot mention is a prefix as well as the
        configured one, so checking only cfg.prefix let `@Bot convert <link>` be converted
        twice — once here and once by the command.
        """
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            prefixes = self.cfg.prefix
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        return any(p and message.content.startswith(p) for p in prefixes)

    # ---------------------------------------------------------------- core
    async def convert_and_send(self, message: discord.Message, url: str, kind: str, *, reply_errors: bool) -> bool:
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
        try:
            src = await video.download(url, self.workdir, self.max_bytes,
                                       cookies_file=self.cfg.ytdl_cookies_file, rapidapi_key=self.cfg.rapidapi_key)
            if src.stat().st_size > limit:
                self.stats["compressed"] += 1
                out = await video.fit_under(src, int(limit * 0.97), self.workdir,
                                            timeout=self.cfg.encode_timeout_seconds, progress=progress)
            else:
                out = src
            ext = out.suffix.lower().lstrip(".") or "mp4"
            await message.reply(file=discord.File(out, filename=f"{kind}.{ext}"), mention_author=False)
            self.stats["ok"] += 1
            # tidy: drop the original embed if we can
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
        if ctx.interaction:
            await ctx.interaction.response.send_message(f"⏳ Converting {kind} link…", ephemeral=True)
            # for slash commands we attach to a fresh message so replies have an anchor
            anchor = await ctx.channel.send(f"🎬 Converting <{normalise(url, kind)}> for {ctx.author.mention}",
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            anchor = ctx.message
        await self.convert_and_send(anchor, normalise(url, kind), kind, reply_errors=True)

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
        e.set_footer(text=f"session: {s['ok']} ok · {s['failed']} failed · {s['compressed']} needed compression")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="media-cleanup", description="Delete temporary media files (admin)")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def media_cleanup(self, ctx: commands.Context):
        # older_than_seconds=0 would also delete files a conversion is still writing, so
        # keep a small floor; the periodic loop reclaims the rest.
        n = await asyncio.to_thread(video.cleanup_dir, self.workdir, 60)
        await ctx.send(f"🧹 Removed {n} temp file(s).")


async def setup(bot: commands.Bot):
    await bot.add_cog(Media(bot))
