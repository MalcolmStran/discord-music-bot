"""Music commands (hybrid: work as `!play` and `/play`)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core.player import GuildPlayer, LoopMode
from ..core.ytdl import YTDL, fmt_duration, looks_like_url

log = logging.getLogger(__name__)


def _voice_channel_of(member: discord.Member):
    return member.voice.channel if member.voice else None


class Music(commands.Cog):
    """Play music from YouTube (and anything else yt-dlp can stream)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = bot.cfg                      # type: ignore[attr-defined]
        self.ytdl: YTDL = bot.ytdl              # type: ignore[attr-defined]
        self.players: dict[int, GuildPlayer] = {}

    # ------------------------------------------------------------ helpers
    def player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.players.get(guild.id)
        if p is None:
            p = GuildPlayer(self.bot, guild, self.ytdl, max_queue=self.cfg.max_queue_size,
                            default_volume=self.cfg.default_volume, idle_seconds=self.cfg.idle_disconnect_seconds)
            self.players[guild.id] = p
        return p

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
        if player.connected and player.channel != ch and player.is_playing:
            await ctx.send(f"🎧 I'm already playing in **{player.channel.name}** — join me there or `/stop` first.")  # type: ignore[union-attr]
            return False
        try:
            await player.connect(ch)
        except Exception as e:
            log.warning("voice connect failed in %s: %s", ctx.guild, e)
            await ctx.send(f"❌ Couldn't connect to **{ch.name}**: {e}")
            return False
        player.text_channel = ctx.channel
        return True

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
        if member.guild.id not in self.players:
            return
        player = self.players[member.guild.id]
        # bot itself disconnected (kicked / channel deleted)
        if member.id == self.bot.user.id and before.channel and not after.channel:  # type: ignore[union-attr]
            log.info("[%s] bot left voice (external); resetting player", member.guild.name)
            await player.disconnect()
            return
        # someone left the bot's channel → check if alone
        if before.channel and before.channel == player.channel and not member.bot:
            await asyncio.sleep(10)
            ch = player.channel
            if ch and not [m for m in ch.members if not m.bot]:
                await player._announce("👋 Everyone left, so I'll leave too.")
                await player.disconnect()

    # ----------------------------------------------------------- commands
    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song/URL or add it to the queue")
    @app_commands.describe(query="Song name, YouTube/SoundCloud/etc. URL, or playlist URL")
    async def play(self, ctx: commands.Context, *, query: str):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if player.queue.is_full:
            return await ctx.send(f"📦 Queue is full ({player.queue.max_size}).")
        if not await self._join_author_channel(ctx, player):
            return
        async with ctx.typing():   # defers the interaction for slash invocations
            try:
                tracks = await self.ytdl.resolve(query, requester_id=ctx.author.id)
            except LookupError as e:
                return await ctx.send(f"❌ {e}")
            except Exception as e:
                log.exception("resolve failed")
                return await ctx.send(f"❌ Couldn't load that: {e}")
        too_long = [t for t in tracks if t.duration and t.duration > self.cfg.max_song_duration]
        tracks = [t for t in tracks if t not in too_long]
        if not tracks:
            return await ctx.send(f"⏱️ Too long (max {self.cfg.max_song_duration // 60} min).")
        added = player.enqueue(tracks)
        if added == 1 and len(tracks) == 1:
            t = tracks[0]
            pos = len(player.queue) if player.current else 0
            e = discord.Embed(title="➕ Added to queue" if pos else "▶️ Playing next",
                              description=f"**[{t.title}]({t.webpage_url})**", color=0x5865F2)
            e.add_field(name="Duration", value=t.pretty_duration, inline=True)
            if pos:
                e.add_field(name="Position", value=str(pos), inline=True)
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
            await ctx.send(embed=e)
        else:
            msg = f"📃 Added **{added}** tracks"
            if added < len(tracks):
                msg += f" (queue full, {len(tracks) - added} dropped)"
            if too_long:
                msg += f", skipped {len(too_long)} too-long"
            await ctx.send(msg + ".")

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current track")
    async def skip(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        if not player.is_playing:
            return await ctx.send("Nothing is playing.")
        player.skip()
        await ctx.send("⏭️ Skipped.")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        player.stop()
        await ctx.send("⏹️ Stopped and cleared the queue.")

    @commands.hybrid_command(name="pause", description="Pause playback")
    async def pause(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        await ctx.send("⏸️ Paused." if player.pause() else "Nothing to pause.")

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Resume playback")
    async def resume(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not await self._require_same_channel(ctx, player):
            return
        await ctx.send("▶️ Resumed." if player.resume() else "Not paused.")

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Show or set the volume (0–150)")
    @app_commands.describe(level="0–150 (100 = normal)")
    async def volume(self, ctx: commands.Context, level: Optional[int] = None):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if level is None:
            return await ctx.send(f"🔊 Volume: {int(player.volume * 100)}%")
        if not 0 <= level <= 150:
            return await ctx.send("Volume must be 0–150.")
        player.set_volume(level / 100)
        await ctx.send(f"🔊 Volume set to {level}%.")

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the queue")
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
            e.add_field(name="Now playing", value=f"**{player.current.title}** `{player.current.pretty_duration}`", inline=False)
        lines = [f"`{i}.` **{t.title[:60]}** `{t.pretty_duration}`" for i, t in enumerate(items[start:start + per], start + 1)]
        e.description = "\n".join(lines) or "*(nothing queued)*"
        e.set_footer(text=f"{len(items)}/{player.queue.max_size} queued · total {fmt_duration(player.queue.total_duration)} · loop: {player.loop_mode.value}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="What's playing right now")
    async def nowplaying(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not player.current:
            return await ctx.send("Nothing is playing.")
        await ctx.send(embed=player.now_playing_embed())

    @commands.hybrid_command(name="loop", aliases=["repeat"], description="Loop mode: off, one (current track) or all (queue)")
    @app_commands.describe(mode="off / one / all (omit to cycle)")
    @app_commands.choices(mode=[app_commands.Choice(name=m.value, value=m.value) for m in LoopMode])
    async def loop(self, ctx: commands.Context, mode: Optional[str] = None):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if mode is None:
            order = [LoopMode.OFF, LoopMode.ONE, LoopMode.ALL]
            mode = order[(order.index(player.loop_mode) + 1) % 3].value
        try:
            player.loop_mode = LoopMode(mode.lower())
        except ValueError:
            return await ctx.send("Use `off`, `one` or `all`.")
        icon = {"off": "➡️", "one": "🔂", "all": "🔁"}[player.loop_mode.value]
        await ctx.send(f"{icon} Loop: **{player.loop_mode.value}**")

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if len(player.queue) < 2:
            return await ctx.send("Need at least 2 queued tracks.")
        player.queue.shuffle()
        await ctx.send("🔀 Shuffled.")

    @commands.hybrid_command(name="remove", aliases=["rm"], description="Remove a queued track by position")
    @app_commands.describe(position="Position as shown in /queue")
    async def remove(self, ctx: commands.Context, position: int):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        t = player.queue.remove(position - 1)
        await ctx.send(f"🗑️ Removed **{t.title}**." if t else "No track at that position.")

    @commands.hybrid_command(name="move", description="Move a queued track to another position")
    async def move(self, ctx: commands.Context, from_pos: int, to_pos: int):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        ok = player.queue.move(from_pos - 1, to_pos - 1)
        await ctx.send("↕️ Moved." if ok else "Invalid positions.")

    @commands.hybrid_command(name="clear", description="Clear the queue (keeps the current track)")
    async def clear(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        player.queue.clear()
        await ctx.send("🗑️ Queue cleared.")

    @commands.hybrid_command(name="join", aliases=["summon"], description="Join your voice channel")
    async def join(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if await self._join_author_channel(ctx, player):
            await ctx.send(f"✅ Joined **{player.channel.name}**.")  # type: ignore[union-attr]

    @commands.hybrid_command(name="leave", aliases=["dc", "disconnect"], description="Leave the voice channel")
    async def leave(self, ctx: commands.Context):
        player = self.player(ctx.guild)  # type: ignore[arg-type]
        if not player.connected:
            return await ctx.send("I'm not in a voice channel.")
        await player.disconnect()
        await ctx.send("👋 Bye.")

    @commands.hybrid_command(name="status", aliases=["voice-debug", "vdebug"], description="Voice/player status")
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
            e.add_field(name="Current", value=player.current.title[:80], inline=False)
        await ctx.send(embed=e)

    # ------------------------------------------------------------- errors
    async def cog_command_error(self, ctx: commands.Context, error: Exception):  # type: ignore[override]
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Music commands only work in a server.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")  # type: ignore[union-attr]
        elif isinstance(error, commands.BadArgument):
            await ctx.send("That argument doesn't look right — check `/help`.")
        else:
            log.exception("command %s failed", ctx.command, exc_info=error)
            await ctx.send(f"❌ {type(error).__name__}: {error}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
