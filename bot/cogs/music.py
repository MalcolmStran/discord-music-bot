"""Music commands (hybrid: work as `!play` and `/play`)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from ..core.player import GuildPlayer, LoopMode
from ..core.spotify import Spotify, is_spotify
from ..core.ytdl import YTDL, fmt_duration

log = logging.getLogger(__name__)


def _voice_channel_of(member: discord.Member):
    return member.voice.channel if member.voice else None


def split_too_long(tracks: list, max_seconds: int) -> tuple[list, list]:
    """Partition into (playable, too_long). Tracks of unknown length (livestreams) pass."""
    playable, too_long = [], []
    for t in tracks:
        (too_long if t.duration and t.duration > max_seconds else playable).append(t)
    return playable, too_long


class Music(commands.Cog):
    """Play music from YouTube (and anything else yt-dlp can stream)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = bot.cfg                      # type: ignore[attr-defined]
        self.ytdl: YTDL = bot.ytdl              # type: ignore[attr-defined]
        self.spotify: Spotify = bot.spotify     # type: ignore[attr-defined]
        self.settings = bot.settings            # type: ignore[attr-defined]
        self.players: dict[int, GuildPlayer] = {}
        self._alone_checks: set[int] = set()   # guilds with a pending "is anyone left?" check

    # ------------------------------------------------------------ helpers
    def player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.players.get(guild.id)
        if p is None:
            p = GuildPlayer(self.bot, guild, self.ytdl, max_queue=self.cfg.max_queue_size,
                            default_volume=self.cfg.default_volume, idle_seconds=self.cfg.idle_disconnect_seconds)
            self._restore(guild.id, p)
            self.players[guild.id] = p
        return p

    def _restore(self, guild_id: int, player: GuildPlayer) -> None:
        """Reapply settings saved by a previous run — they used to reset on every restart."""
        volume = self.settings.get(guild_id, "volume")
        if isinstance(volume, (int, float)):
            player.set_volume(float(volume))
        mode = self.settings.get(guild_id, "loop_mode")
        try:
            player.loop_mode = LoopMode(mode)
        except ValueError:
            pass

    @staticmethod
    def _thinking(ctx: commands.Context):
        """Show a typing indicator for prefix invocations.

        `ctx.typing()` *is* the defer for an interaction, so using it after `ctx.defer()`
        would raise InteractionResponded — hence the split.
        """
        return contextlib.nullcontext() if ctx.interaction else ctx.typing()

    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def _join_author_channel(self, ctx: commands.Context, player: GuildPlayer) -> bool:
        ch = _voice_channel_of(ctx.author)  # type: ignore[arg-type]
        if ch is None:
            await ctx.send("🔇 Join a voice channel first.")
            return False
        perms = ch.permissions_for(ctx.guild.me)  # type: ignore[union-attr]
        if not (perms.connect and perms.speak):
            await ctx.send(f"❌ I can't connect/speak in **{ch.name}** (missing permissions).")
            return False
        if player.connected and player.channel != ch and self._busy_elsewhere(player):
            await ctx.send(f"🎧 I'm already busy in **{player.channel.name}** — join me there or `/stop` first.")  # type: ignore[union-attr]
            return False
        try:
            await player.connect(ch)
        except TimeoutError:
            log.warning("voice connect timed out in %s", ctx.guild)
            await ctx.send(f"❌ Timed out connecting to **{ch.name}** — Discord's voice servers may be flaky, try again.")
            return False
        except Exception:
            log.exception("voice connect failed in %s", ctx.guild)
            await ctx.send(f"❌ Couldn't connect to **{ch.name}**.")
            return False
        player.text_channel = ctx.channel
        return True

    @staticmethod
    def _busy_elsewhere(player: GuildPlayer) -> bool:
        """In use by other people right now — playing, queued, or with listeners present.

        Checking `is_playing` alone let anyone steal the bot during the gap between two
        tracks, cutting off a channel full of listeners.
        """
        if player.is_playing or len(player.queue) or player.current:
            return True
        ch = player.channel
        return bool(ch and any(not m.bot for m in ch.members))

    async def _require_same_channel(self, ctx: commands.Context, player: GuildPlayer) -> bool:
        if not player.connected:
            await ctx.send("I'm not in a voice channel.")
            return False
        mine = player.channel
        yours = _voice_channel_of(ctx.author)  # type: ignore[arg-type]
        if yours != mine:
            await ctx.send(f"You need to be in **{mine.name}** to control playback.")  # type: ignore[union-attr]
            return False
        return True

    # ------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Auto-leave when the bot is alone; clean up if the bot gets kicked."""
        player = self.players.get(member.guild.id)
        if player is None:
            return
        # bot itself disconnected (kicked / channel deleted)
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            log.info("[%s] bot left voice (external); resetting player", member.guild.name)
            await player.disconnect()
            return
        # somebody actually left the bot's channel (a mute/deafen keeps before == after)
        if member.bot or before.channel == after.channel:
            return
        if before.channel is None or before.channel != player.channel:
            return
        if member.guild.id in self._alone_checks:
            return          # a check is already pending for this guild
        self._alone_checks.add(member.guild.id)
        try:
            await asyncio.sleep(10)
            ch = player.channel
            if ch and not any(not m.bot for m in ch.members):
                await player.announce("👋 Everyone left, so I'll leave too.")
                await player.disconnect()
        finally:
            self._alone_checks.discard(member.guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Drop the player when the bot is removed from a guild — the dict never shrank."""
        await self._drop_player(guild.id)

    async def _drop_player(self, guild_id: int) -> None:
        player = self.players.pop(guild_id, None)
        if player is not None:
            try:
                await player.disconnect()
            except Exception:
                log.debug("cleanup disconnect failed for guild %s", guild_id, exc_info=True)

    async def cog_unload(self) -> None:
        for gid in list(self.players):
            await self._drop_player(gid)

    # ----------------------------------------------------------- commands
    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song/URL or add it to the queue")
    @commands.guild_only()
    @app_commands.describe(query="Song name, YouTube/SoundCloud URL, playlist URL, or Spotify track/album/playlist link")
    async def play(self, ctx: commands.Context, *, query: str):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        # Defer first: joining voice can take up to 30 s and a slash command has 3 s to
        # acknowledge, so connecting before this left the interaction dead.
        await ctx.defer()
        if player.queue.is_full:
            return await ctx.send(f"📦 Queue is full ({player.queue.max_size}).")
        if not await self._join_author_channel(ctx, player):
            return
        try:
            async with self._thinking(ctx):
                if is_spotify(query):
                    tracks = await self.spotify.resolve(query, requester_id=ctx.author.id)
                else:
                    tracks = await self.ytdl.resolve(query, requester_id=ctx.author.id)
        except LookupError as e:
            return await ctx.send(f"❌ {e}")
        except Exception:
            log.exception("resolve failed for %r", query)
            return await ctx.send("❌ Couldn't load that; it's been logged.")
        if not tracks:
            return await ctx.send("❌ Nothing playable at that link.")
        limit = self.cfg.max_song_duration
        playable, too_long = split_too_long(tracks, limit)
        if not playable:
            return await ctx.send(f"⏱️ Too long (max {limit // 60} min).")
        added = player.enqueue(playable)
        if added == 0:
            return await ctx.send(f"📦 Queue is full ({player.queue.max_size}).")
        if added == 1 and len(playable) == 1:
            t = playable[0]
            pos = len(player.queue) if player.current else 0
            e = discord.Embed(title="➕ Added to queue" if pos else "▶️ Playing next",
                              description=f"**[{escape_markdown(t.title)}]({t.link})**", color=0x5865F2)
            if t.extractor == "spotify":
                e.set_footer(text="Spotify link → playing the YouTube match")
            e.add_field(name="Duration", value=t.pretty_duration, inline=True)
            if pos:
                e.add_field(name="Position", value=str(pos), inline=True)
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
            await ctx.send(embed=e)
        else:
            msg = f"📃 Added **{added}** tracks" + (" from Spotify (YouTube matches found as they play)" if playable[0].extractor == "spotify" else "")
            if added < len(playable):
                msg += f" (queue full, {len(playable) - added} dropped)"
            if too_long:
                msg += f", skipped {len(too_long)} too-long"
            await ctx.send(msg + ".")

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current track")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        if not player.is_busy:
            return await ctx.send("Nothing is playing.")
        player.skip()
        await ctx.send("⏭️ Skipped.")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        player.stop()
        await ctx.send("⏹️ Stopped and cleared the queue.")

    @commands.hybrid_command(name="pause", description="Pause playback")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        await ctx.send("⏸️ Paused." if player.pause() else "Nothing to pause.")

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Resume playback")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        await ctx.send("▶️ Resumed." if player.resume() else "Not paused.")

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Show or set the volume (0–150)")
    @commands.guild_only()
    @app_commands.describe(level="0–150 (100 = normal)")
    async def volume(self, ctx: commands.Context, level: Optional[int] = None):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if level is None:
            return await ctx.send(f"🔊 Volume: {int(player.volume * 100)}%")
        if not await self._require_same_channel(ctx, player):
            return
        if not 0 <= level <= 150:
            return await ctx.send("Volume must be 0–150.")
        player.set_volume(level / 100)
        await self.settings.set_async(ctx.guild.id, "volume", player.volume)  # type: ignore[union-attr]
        await ctx.send(f"🔊 Volume set to {level}%.")

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the queue")
    @commands.guild_only()
    @app_commands.describe(page="Page number")
    async def queue(self, ctx: commands.Context, page: int = 1):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        items = player.queue.snapshot()
        if not items and not player.current:
            return await ctx.send("Queue is empty.")
        per = 10
        pages = max(1, (len(items) + per - 1) // per)
        page = max(1, min(page, pages))
        start = (page - 1) * per
        e = discord.Embed(title=f"📋 Queue — page {page}/{pages}", color=0x5865F2)
        if player.current:
            e.add_field(name="Now playing",
                        value=f"**{escape_markdown(player.current.title[:200])}** `{player.current.pretty_duration}`",
                        inline=False)
        lines = [f"`{i}.` **{escape_markdown(t.title[:60])}** `{t.pretty_duration}`"
                 for i, t in enumerate(items[start:start + per], start + 1)]
        e.description = "\n".join(lines) or "*(nothing queued)*"
        e.set_footer(text=f"{len(items)}/{player.queue.max_size} queued · total {fmt_duration(player.queue.total_duration)} · loop: {player.loop_mode.value}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="What's playing right now")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not player.current:
            return await ctx.send("Nothing is playing.")
        await ctx.send(embed=player.now_playing_embed())

    @commands.hybrid_command(name="loop", aliases=["repeat"], description="Loop mode: off, one (current track) or all (queue)")
    @commands.guild_only()
    @app_commands.describe(mode="off / one / all (omit to cycle)")
    @app_commands.choices(mode=[app_commands.Choice(name=m.value, value=m.value) for m in LoopMode])
    async def loop(self, ctx: commands.Context, mode: Optional[str] = None):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        if mode is None:
            order = [LoopMode.OFF, LoopMode.ONE, LoopMode.ALL]
            mode = order[(order.index(player.loop_mode) + 1) % 3].value
        try:
            player.loop_mode = LoopMode(mode.lower())
        except ValueError:
            return await ctx.send("Use `off`, `one` or `all`.")
        await self.settings.set_async(ctx.guild.id, "loop_mode", player.loop_mode.value)  # type: ignore[union-attr]
        icon = {"off": "➡️", "one": "🔂", "all": "🔁"}[player.loop_mode.value]
        await ctx.send(f"{icon} Loop: **{player.loop_mode.value}**")

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        if len(player.queue) < 2:
            return await ctx.send("Need at least 2 queued tracks.")
        player.queue.shuffle()
        await ctx.send("🔀 Shuffled.")

    @commands.hybrid_command(name="remove", aliases=["rm"], description="Remove a queued track by position")
    @commands.guild_only()
    @app_commands.describe(position="Position as shown in /queue")
    async def remove(self, ctx: commands.Context, position: int):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        t = player.queue.remove(position - 1)
        await ctx.send(f"🗑️ Removed **{t.title}**." if t else "No track at that position.")

    @commands.hybrid_command(name="move", description="Move a queued track to another position")
    @commands.guild_only()
    async def move(self, ctx: commands.Context, from_pos: int, to_pos: int):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        ok = player.queue.move(from_pos - 1, to_pos - 1)
        await ctx.send("↕️ Moved." if ok else "Invalid positions.")

    @commands.hybrid_command(name="clear", description="Clear the queue (keeps the current track)")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        player.queue.clear()
        await ctx.send("🗑️ Queue cleared.")

    @commands.hybrid_command(name="join", aliases=["summon"], description="Join your voice channel")
    @commands.guild_only()
    async def join(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        await ctx.defer()          # the connect below can take up to 30 s
        if await self._join_author_channel(ctx, player):
            await ctx.send(f"✅ Joined **{player.channel.name}**.")  # type: ignore[union-attr]

    @commands.hybrid_command(name="leave", aliases=["dc", "disconnect"], description="Leave the voice channel")
    @commands.guild_only()
    async def leave(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not player.connected:
            return await ctx.send("I'm not in a voice channel.")
        await player.disconnect()
        await ctx.send("👋 Bye.")

    @commands.hybrid_command(name="status", aliases=["voice-debug", "vdebug"], description="Voice/player status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        vc = player.voice
        e = discord.Embed(title="🔊 Player status", color=0x57F287 if player.connected else 0xED4245)
        e.add_field(name="Connected", value=f"{'✅ ' + player.channel.name if player.connected else '❌ no'}", inline=True)  # type: ignore[union-attr]
        e.add_field(name="State", value="playing" if vc and vc.is_playing() else "paused" if vc and vc.is_paused() else "idle", inline=True)
        e.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)
        e.add_field(name="Queue", value=f"{len(player.queue)}/{player.queue.max_size}", inline=True)
        e.add_field(name="Loop", value=player.loop_mode.value, inline=True)
        e.add_field(name="Latency", value=f"{vc.latency * 1000:.0f} ms" if vc and vc.latency else "—", inline=True)
        if player.current:
            e.add_field(name="Current", value=escape_markdown(player.current.title[:80]), inline=False)
        await ctx.send(embed=e)

    # ------------------------------------------------------------- errors
    async def cog_command_error(self, ctx: commands.Context, error: Exception):  # type: ignore[override]
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Music commands only work in a server.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")  # type: ignore[union-attr]
        elif isinstance(error, commands.BadArgument):
            await ctx.send("That argument doesn't look right — check `/help`.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("🚫 You can't use that here.")
        else:
            log.exception("command %s failed", ctx.command, exc_info=error)
            # never echo `error` itself: it carries filesystem paths and signed CDN URLs,
            # and an over-long one makes this very send fail with a 400
            await ctx.send("❌ Something went wrong; it's been logged.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
