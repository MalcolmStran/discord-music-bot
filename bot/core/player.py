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
from discord.utils import escape_markdown

from .queue import TrackQueue
from .ytdl import YTDL, Track

log = logging.getLogger(__name__)


class LoopMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


class GuildPlayer:
    # consecutive unplayable tracks before the player gives up instead of spamming
    MAX_CONSECUTIVE_FAILURES = 5

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
        self._paused_at: float = 0.0
        self._paused_total: float = 0.0
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.now_playing_msg: Optional[discord.Message] = None

        self._source: Optional[discord.PCMVolumeTransformer] = None
        self._wake = asyncio.Event()      # set when something is added to the queue
        self._finished = asyncio.Event()  # set when the current track ends
        self._task: Optional[asyncio.Task] = None
        self._np_task: Optional[asyncio.Task] = None   # live "now playing" updater
        self._skip_requested = False
        self._stop_requested = False   # /stop raised while a stream was still resolving
        self._failures = 0             # consecutive tracks that would not play
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
        self._skip_requested = True     # so the teardown is not mistaken for a dead stream
        self._stop_current()
        self._cancel_np()
        self._release_source()
        vc = self.voice
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                log.debug("disconnect error: %s", e)
        task, self._task = self._task, None
        # `disconnect()` is also called from inside `_player_loop` (idle timeout). Cancelling
        # the task we are running in leaves it in a half-cancelled state for no benefit.
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self.current = None
        log.info("[%s] disconnected", self.guild.name)

    def _cancel_np(self) -> None:
        """Stop the live now-playing updater. It used to be cancelled only on the normal
        end-of-track path, so a disconnect left it editing a finished track's embed."""
        if self._np_task and not self._np_task.done():
            self._np_task.cancel()
        self._np_task = None

    def _release_source(self) -> None:
        """Tear down the ffmpeg child behind the current source.

        discord.py cleans up a source it is playing, but one built and never handed to
        `vc.play()` (or left over after an error) keeps its ffmpeg process alive forever.
        """
        src, self._source = self._source, None
        if src is not None:
            try:
                src.cleanup()
            except Exception as e:
                log.debug("source cleanup failed: %s", e)

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
        self._stop_requested = True
        self._stop_current()

    def pause(self) -> bool:
        vc = self.voice
        if vc and vc.is_playing():
            vc.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        vc = self.voice
        if vc and vc.is_paused():
            vc.resume()
            if self._paused_at:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = 0.0
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
        now = self._paused_at or time.monotonic()
        return max(0.0, now - self.started_at - self._paused_total)

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
                    if not self.queue.is_empty:
                        continue           # something arrived as the timer expired
                    await self._announce("💤 Nothing played for a while — leaving the voice channel.")
                    if not self.queue.is_empty:
                        # queued while that message was in flight; disconnect() would clear
                        # it and leave no loop running to play it
                        continue
                    await self.disconnect()
                    return
                if not self.connected:
                    log.warning("[%s] not connected; dropping %s", self.guild.name, track.title)
                    self.queue.push_front(track)
                    self.current = None    # so loop-all does not re-queue the previous track
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
        finally:
            self._cancel_np()
            self._release_source()

    async def _next_track(self) -> Optional[Track]:
        """Pick the next track honouring loop mode; wait (with idle timeout) if the queue is empty.

        `self.current` is None whenever the last track did not actually play (it failed to
        resolve, or the voice connection went away). That is what keeps a broken track out
        of the loop-all rotation instead of re-queueing whatever played before it — the old
        code only assigned `current` on success, so a failed track duplicated its
        predecessor and evicted its successor.
        """
        if self.current:
            if self.loop_mode is LoopMode.ONE and not self._skip_requested:
                return self.current
            if self.loop_mode is LoopMode.ALL:
                # skipping under loop-all still cycles the track to the back
                if not self.queue.add(self.current):
                    log.info("[%s] queue full; %s dropped from the loop", self.guild.name, self.current.title)
        self._skip_requested = False
        self._stop_requested = False
        while self.queue.is_empty:
            self.current = None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
            except TimeoutError:
                return None
            # a flag raised while we were idle refers to a track that is already gone
            self._skip_requested = False
            self._stop_requested = False
        return self.queue.pop_next()

    async def _play_track(self, track: Track) -> None:
        vc = self.voice
        if not vc:
            self.current = None
            return
        # Clear *before* the slow fetch below: a /stop or /skip issued while the stream is
        # still resolving used to be swallowed by this clear and the track played anyway.
        self._finished.clear()
        self._skip_requested = False
        self._stop_requested = False
        try:
            await self.ytdl.fetch_stream(track)
            source = self.ytdl.make_source(track, self.volume)
        except Exception as e:
            log.warning("[%s] cannot play %s: %s", self.guild.name, track.title, e)
            self.current = None          # never leave the previous track as "current"
            await self._on_track_failed(
                f"⚠️ Skipping **{escape_markdown(track.title)}** — {escape_markdown(str(e)[:150])}")
            return

        if self._stop_requested or self._skip_requested:
            # the user gave up while we were resolving; honour it instead of playing
            try:
                source.cleanup()
            except Exception:
                pass
            self.current = None
            return

        self._source = source
        loop = self.bot.loop

        def _after(err: Optional[Exception]) -> None:
            if err:
                log.warning("[%s] playback error: %s", self.guild.name, err)
            loop.call_soon_threadsafe(self._finished.set)

        self.current = track
        self.started_at = time.monotonic()
        self._paused_at = 0.0
        self._paused_total = 0.0
        try:
            vc.play(self._source, after=_after)
        except discord.ClientException as e:
            log.warning("[%s] play() failed: %s", self.guild.name, e)
            self._release_source()       # otherwise the ffmpeg child outlives the track
            self.current = None
            self._finished.set()
            await self._on_track_failed(f"⚠️ Couldn't start **{escape_markdown(track.title)}**. Skipping.")
            return
        await self._announce_now_playing(track)
        self._cancel_np()
        self._np_task = self.bot.loop.create_task(self._update_now_playing())
        try:
            await self._finished.wait()
        finally:
            self._cancel_np()
            self._release_source()
        await self._finalize_now_playing()
        # ffmpeg dying immediately (403 on the CDN url, geo-block, etc.) looks like a 1-second track
        played = time.monotonic() - self.started_at
        if played < 3 and not self._skip_requested and not self._stop_requested and (track.duration or 0) > 10:
            log.warning("[%s] %s ended after <3s — stream probably failed", self.guild.name, track.title)
            self.current = None          # keep the dead track out of the loop-all rotation
            await self._on_track_failed(
                f"⚠️ Couldn't stream **{escape_markdown(track.title)}** (source rejected the connection). Skipping.")
        else:
            self._failures = 0

    async def _on_track_failed(self, message: str) -> None:
        """Announce a track we could not play, and give up if nothing is playable.

        Without the counter a permanently-broken track under loop-all — or a whole playlist
        of dead links — retried forever, one channel message per attempt.
        """
        self._failures += 1
        if self._failures <= self.MAX_CONSECUTIVE_FAILURES:
            await self._announce(message)
        if self._failures >= self.MAX_CONSECUTIVE_FAILURES:
            log.warning("[%s] %d tracks failed in a row; stopping", self.guild.name, self._failures)
            self.queue.clear()
            self.loop_mode = LoopMode.OFF
            self._failures = 0
            await self._announce("🛑 Too many tracks failed in a row — stopping. "
                                 "YouTube may be blocking the bot; try again later or set `YTDL_COOKIES_FILE`.")

    # --------------------------------------------------------------- output
    async def announce(self, text: str) -> None:
        """Public entry point for cogs (they used to reach into `_announce`)."""
        await self._announce(text)

    async def _announce(self, text: str) -> None:
        if not self.text_channel:
            return
        try:
            await self.text_channel.send(text[:1900])
        except discord.Forbidden:
            log.warning("[%s] cannot post in the bound text channel; muting announcements",
                        self.guild.name)
            self.text_channel = None
        except Exception as e:
            log.warning("[%s] announce failed: %s", self.guild.name, e)

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

    async def _update_now_playing(self) -> None:
        """Edit the announce message every 15 s so its position bar is live."""
        try:
            while True:
                await asyncio.sleep(15)
                if not self.now_playing_msg or not self.current:
                    return
                try:
                    await self.now_playing_msg.edit(embed=self.now_playing_embed())
                except discord.NotFound:
                    self.now_playing_msg = None
                    return
                except discord.HTTPException as e:
                    log.debug("now playing edit failed: %s", e)
        except asyncio.CancelledError:
            pass

    async def _finalize_now_playing(self) -> None:
        """Freeze the announce as 'Played' when the track ends (so old messages don't lie)."""
        msg, track = self.now_playing_msg, self.current
        if not msg or not track:
            return
        try:
            e = discord.Embed(title="✅ Played",
                              description=f"**[{escape_markdown(track.title)}]({track.link})**", color=0x99AAB5)
            e.add_field(name="Duration", value=track.pretty_duration, inline=True)
            await msg.edit(embed=e)
        except discord.HTTPException:
            pass

    def now_playing_embed(self, track: Optional[Track] = None) -> discord.Embed:
        track = track or self.current
        paused = self.is_paused
        title = "⏸️ Paused" if paused else "🔁 Repeating" if self.loop_mode is LoopMode.ONE else "🎵 Now playing"
        if not track:
            return discord.Embed(title="Nothing playing", color=0x99AAB5)
        e = discord.Embed(title=title,
                          description=f"**[{escape_markdown(track.title)}]({track.link})**", color=0x1DB954)
        if track.uploader:
            e.add_field(name="Uploader", value=track.uploader, inline=True)
        e.add_field(name="Duration", value=track.pretty_duration, inline=True)
        if self.position and track.duration:
            e.add_field(name="Position", value=_progress_bar(self.position, track.duration), inline=False)
        if len(self.queue):
            nxt = self.queue.peek()
            e.add_field(name=f"Up next ({len(self.queue)} queued)",
                        value=escape_markdown(nxt.title[:80]) if nxt else "—", inline=False)
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
