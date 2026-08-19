"""Auto-convert Twitter/X and TikTok links into uploaded MP4s (+ /convert, /media-* commands)."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core import video
from ..core.settings import GuildSettings

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.I)
SUPPORTED = {
    "tiktok": re.compile(r"(^|\.)(tiktok\.com)$", re.I),
    "twitter": re.compile(r"(^|\.)(twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)$", re.I),
}


def classify(url: str) -> Optional[str]:
    try:
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].split(":")[0].lower()
    except Exception:
        return None
    for kind, rx in SUPPORTED.items():
        if rx.search(host):
            return kind
    return None


def normalise(url: str, kind: str) -> str:
    if kind == "twitter":
        url = re.sub(r"https?://(www\.)?(fxtwitter|vxtwitter|fixupx)\.com", "https://x.com", url, flags=re.I)
    return url.rstrip(").,!?")


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
        n = video.cleanup_dir(self.workdir, older_than_seconds=3600)
        if n:
            log.info("media cleanup removed %d stale files", n)

    # ------------------------------------------------------------ listener
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        if message.content.startswith(self.cfg.prefix):
            return  # commands handle themselves (/convert)
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
            await message.reply(file=discord.File(out, filename=f"{kind}.mp4"), mention_author=False)
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
                await message.reply(f"❌ Upload failed: {e.text or e}", mention_author=False)
            return False
        except Exception as e:
            self.stats["failed"] += 1
            log.exception("media convert crashed for %s", url)
            if reply_errors:
                await message.reply(f"❌ {type(e).__name__}: {e}", mention_author=False)
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
    @commands.guild_only()
    async def media_toggle(self, ctx: commands.Context):
        gid = ctx.guild.id  # type: ignore[union-attr]
        new = not self.settings.media_enabled(gid)
        self.settings.set_media_enabled(gid, new)
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
        e.add_field(name="Temp usage", value=f"{video.dir_size(self.workdir) / 1048576:.1f} MB", inline=True)
        s = self.stats
        e.set_footer(text=f"session: {s['ok']} ok · {s['failed']} failed · {s['compressed']} needed compression")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="media-cleanup", description="Delete temporary media files (admin)")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def media_cleanup(self, ctx: commands.Context):
        n = video.cleanup_dir(self.workdir, older_than_seconds=0)
        await ctx.send(f"🧹 Removed {n} temp file(s).")


async def setup(bot: commands.Bot):
    await bot.add_cog(Media(bot))
