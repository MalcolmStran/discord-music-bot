"""Per-guild player: owns the voice connection, the queue and the playback loop.

The playback loop is a single task per guild:
    wait for a track → resolve stream → play → wait for "finished" event → repeat
No recursion through the `after=` callback (that was the v1 design and it made error
handling and loop modes fragile). Skip/stop just stop the voice client, which fires the
finished event. Idle → disconnect after `idle_seconds`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Optional

import discord

from .queue import TrackQueue
from .ytdl import Track, YTDL

log = logging.getLogger(__name__)


class LoopMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


class GuildPlayer:
    def __init__(self, bot: discord.Client, guild: discord.Guild, ytdl: YTDL, *,
                 max_queue: int, default_volume: float, idle_seconds: int):
        self.bot = bot
        self.guild = guild
        self.ytdl = ytdl
        self.queue = TrackQueue(max_size=max_queue)
        self.volume = default_volume
        self.loop_mode = LoopMode.OFF
        self.idle_seconds = idle_seconds

        self.current: Optional[Track] = None
        self.started_at: float = 0.0
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.now_playing_msg: Optional[discord.Message] = None

        self._source: Optional[discord.PCMVolumeTransformer] = None
        self._wake = asyncio.Event()      # set when something is added to the queue
        self._finished = asyncio.Event()  # set when the current track ends
        self._task: Optional[asyncio.Task] = None
        self._skip_requested = False
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- voice
    @property
    def voice(self) -> Optional[discord.VoiceClient]:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def connected(self) -> bool:
        vc = self.voice
        return bool(vc and vc.is_connected())

    @property
    def channel(self) -> Optional[discord.VoiceChannel | discord.StageChannel]:
        vc = self.voice
        return vc.channel if vc else None  # type: ignore[return-value]

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Connect or move to `channel`. Raises on failure."""
        async with self._lock:
            vc = self.voice
            if vc and vc.is_connected():
                if vc.channel != channel:
                    await vc.move_to(channel)
                return
            if vc:  # stale client object
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
            # discord.py handles reconnects/session resumes itself; keep this minimal
            # (lesson from 2026-03-11: extra retry loops caused 4006/4017 errors)
            await channel.connect(timeout=30, reconnect=True, self_deaf=True)
            log.info("[%s] connected to %s", self.guild.name, channel.name)

    async def disconnect(self) -> None:
        self.queue.clear()
        self.loop_mode = LoopMode.OFF
        self._stop_current()
        vc = self.voice
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                log.debug("disconnect error: %s", e)
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.current = None
        log.info("[%s] disconnected", self.guild.name)

    # ------------------------------------------------------------- controls
    def enqueue(self, tracks: list[Track]) -> int:
        n = self.queue.extend(tracks)
        self._wake.set()
        self.ensure_loop()
        return n

    def ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = self.bot.loop.create_task(self._player_loop(), name=f"player:{self.guild.id}")

    def skip(self) -> None:
        self._skip_requested = True
        self._stop_current()

    def stop(self) -> None:
        self.queue.clear()
        self.loop_mode = LoopMode.OFF
        self._skip_requested = True
        self._stop_current()

    def pause(self) -> bool:
        vc = self.voice
        if vc and vc.is_playing():
            vc.pause()
            return True
        return False

    def resume(self) -> bool:
        vc = self.voice
        if vc and vc.is_paused():
            vc.resume()
            return True
        return False

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(2.0, volume))
        if self._source:
            self._source.volume = self.volume   # live change

    @property
    def is_playing(self) -> bool:
        vc = self.voice
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    @property
    def is_paused(self) -> bool:
        vc = self.voice
        return bool(vc and vc.is_paused())

    @property
    def position(self) -> float:
        if not self.current or not self.started_at:
            return 0.0
        return time.monotonic() - self.started_at

    def _stop_current(self) -> None:
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()          # triggers the after-callback → _finished
        else:
            self._finished.set()

    # ----------------------------------------------------------- main loop
    async def _player_loop(self) -> None:
        log.debug("[%s] player loop started", self.guild.name)
        try:
            while True:
                track = await self._next_track()
                if track is None:          # idle timeout
                    await self._announce("💤 Nothing played for a while — leaving the voice channel.")
                    await self.disconnect()
                    return
                if not self.connected:
                    log.warning("[%s] not connected; dropping %s", self.guild.name, track.title)
                    self.queue.push_front(track)
                    await asyncio.sleep(2)
                    if not self.connected:
                        await self._announce("❌ Lost the voice connection. Use `/play` again to reconnect.")
                        self.current = None
                        return
                    continue
                await self._play_track(track)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] player loop crashed", self.guild.name)
            await self._announce("❌ Player crashed; try `/play` again.")
            self.current = None

    async def _next_track(self) -> Optional[Track]:
        """Pick the next track honouring loop mode; wait (with idle timeout) if the queue is empty."""
        if self.loop_mode is LoopMode.ONE and self.current and not self._skip_requested:
            return self.current
        if self.loop_mode is LoopMode.ALL and self.current and not self._skip_requested:
            self.queue.add(self.current)
        if self.loop_mode is LoopMode.ALL and self.current and self._skip_requested:
            self.queue.add(self.current)  # skipping in loop-all still cycles it
        self._skip_requested = False
        while self.queue.is_empty:
            self.current = None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
            except asyncio.TimeoutError:
                return None
        return self.queue.pop_next()

    async def _play_track(self, track: Track) -> None:
        vc = self.voice
        if not vc:
            return
        try:
            await self.ytdl.fetch_stream(track)
            self._source = self.ytdl.make_source(track, self.volume)
        except Exception as e:
            log.warning("[%s] cannot play %s: %s", self.guild.name, track.title, e)
            await self._announce(f"⚠️ Skipping **{track.title}** — {e}")
            if self.loop_mode is LoopMode.ONE:
                self.loop_mode = LoopMode.OFF
            return

        self._finished.clear()
        loop = self.bot.loop

        def _after(err: Optional[Exception]) -> None:
            if err:
                log.warning("[%s] playback error: %s", self.guild.name, err)
            loop.call_soon_threadsafe(self._finished.set)

        self.current = track
        self.started_at = time.monotonic()
        try:
            vc.play(self._source, after=_after)
        except discord.ClientException as e:
            log.warning("[%s] play() failed: %s", self.guild.name, e)
            self._finished.set()
            return
        await self._announce_now_playing(track)
        await self._finished.wait()
        self._source = None

    # --------------------------------------------------------------- output
    async def _announce(self, text: str) -> None:
        if not self.text_channel:
            return
        try:
            await self.text_channel.send(text)
        except Exception as e:
            log.debug("announce failed: %s", e)

    async def _announce_now_playing(self, track: Track) -> None:
        if not self.text_channel:
            return
        embed = self.now_playing_embed(track)
        # delete the previous "now playing" to keep the channel tidy
        if self.now_playing_msg:
            try:
                await self.now_playing_msg.delete()
            except Exception:
                pass
        try:
            self.now_playing_msg = await self.text_channel.send(embed=embed)
        except Exception as e:
            log.debug("now playing announce failed: %s", e)

    def now_playing_embed(self, track: Optional[Track] = None) -> discord.Embed:
        track = track or self.current
        title = "🔁 Repeating" if self.loop_mode is LoopMode.ONE else "🎵 Now playing"
        if not track:
            return discord.Embed(title="Nothing playing", color=0x99AAB5)
        e = discord.Embed(title=title, description=f"**[{track.title}]({track.webpage_url})**", color=0x1DB954)
        if track.uploader:
            e.add_field(name="Uploader", value=track.uploader, inline=True)
        e.add_field(name="Duration", value=track.pretty_duration, inline=True)
        if self.position and track.duration:
            e.add_field(name="Position", value=_progress_bar(self.position, track.duration), inline=False)
        if len(self.queue):
            nxt = self.queue.peek()
            e.add_field(name=f"Up next ({len(self.queue)} queued)", value=nxt.title[:80] if nxt else "—", inline=False)
        if track.requester_id:
            m = self.guild.get_member(track.requester_id)
            who = m.display_name if m else str(track.requester_id)
            e.set_footer(text=f"Requested by {who} · volume {int(self.volume * 100)}%")
        if track.thumbnail:
            e.set_thumbnail(url=track.thumbnail)
        return e


def _progress_bar(pos: float, total: float, width: int = 18) -> str:
    frac = max(0.0, min(1.0, pos / total)) if total else 0.0
    filled = int(frac * width)
    from .ytdl import fmt_duration
    return f"`{fmt_duration(pos)}` {'▬' * filled}🔘{'▬' * (width - filled)} `{fmt_duration(total)}`"
