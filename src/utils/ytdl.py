"""
YouTube-DL source handling for the music bot
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import discord
import yt_dlp

from yt_dlp.utils import DownloadError

import config

logger = logging.getLogger(__name__)



class MissingJSRuntimeError(RuntimeError):
    """Raised when the required JavaScript runtime is unavailable."""


_JS_RUNTIME_READY = False


def _ensure_js_runtime() -> None:
    """Ensure yt-dlp has a JavaScript runtime available for signature solving."""
    global _JS_RUNTIME_READY

    if _JS_RUNTIME_READY:
        return

    if os.environ.get('YT_DLP_JS_RUNTIME'):
        _JS_RUNTIME_READY = True
        logger.debug("YT_DLP_JS_RUNTIME already set: %s", os.environ['YT_DLP_JS_RUNTIME'])
        return

    runtime_name = (config.JS_RUNTIME or '').strip()
    runtime_path = (config.JS_RUNTIME_PATH or '').strip()

    runtime_spec = None

    if runtime_path:
        candidate = Path(runtime_path).expanduser()
        if not candidate.exists():
            raise MissingJSRuntimeError(
                f"Configured JS runtime path '{candidate}' does not exist. "
                "Install Deno or adjust JS_RUNTIME_PATH in your .env file."
            )
        runtime_spec = f"{runtime_name}={candidate}" if runtime_name else str(candidate)
    else:
        # Try configured runtime name, then fallback to deno
        search_names = [runtime_name] if runtime_name else []
        if 'deno' not in search_names:
            search_names.append('deno')

        for name in filter(None, search_names):
            resolved = shutil.which(name)
            if resolved:
                runtime_spec = f"{name}={resolved}"
                runtime_name = name
                break

    if not runtime_spec:
        raise MissingJSRuntimeError(
            "No JavaScript runtime found for yt-dlp. Install Deno from "
            "https://deno.land/#installation or set JS_RUNTIME/JS_RUNTIME_PATH in your .env file."
        )

    os.environ['YT_DLP_JS_RUNTIME'] = runtime_spec
    _JS_RUNTIME_READY = True
    logger.info("Configured yt-dlp JavaScript runtime: %s", runtime_spec)
# YouTube-DL options
YTDL_FORMAT_OPTIONS: Dict[str, Any] = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': False,
}

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail')
        self.uploader = data.get('uploader')
        self.view_count = data.get('view_count')
        
    @classmethod
    async def create_source(
        cls,
        ctx,
        search: str,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        playlist_items: Optional[int | str] = None,
    ) -> Any:
        """Create a source from a search query or URL"""
        loop = loop or asyncio.get_event_loop()

        _ensure_js_runtime()

        ytdl_opts: Dict[str, Any] = dict(YTDL_FORMAT_OPTIONS)
        if playlist_items:
            ytdl_opts['playliststart'] = 1
            ytdl_opts['playlistend'] = int(playlist_items)

        try:
            if any(keyword in search.lower() for keyword in ['playlist', 'list=']):
                ytdl_opts['extract_flat'] = True
                playlist_ytdl = yt_dlp.YoutubeDL(ytdl_opts)  # type: ignore[arg-type]

                partial_result = await loop.run_in_executor(
                    None, lambda: playlist_ytdl.extract_info(search, download=False)
                )
                partial_data = dict(partial_result or {})
                if not partial_data:
                    raise Exception("Could not extract playlist information")

                entries_raw = partial_data.get('entries')
                if not isinstance(entries_raw, Iterable):
                    raise Exception("Playlist is empty or unavailable")

                entries = [dict(entry) for entry in entries_raw if entry]
                if not entries:
                    raise Exception("Playlist is empty or unavailable")

                if len(entries) > 50:
                    entries = entries[:50]

                ytdl_opts['extract_flat'] = False
                detail_ytdl = yt_dlp.YoutubeDL(ytdl_opts)  # type: ignore[arg-type]
                processed_entries = []

                for entry in entries:
                    try:
                        entry_url = entry.get('url') or (
                            f"https://www.youtube.com/watch?v={entry.get('id')}"
                        )
                        if not entry_url:
                            continue

                        entry_result = await loop.run_in_executor(
                            None, lambda url=entry_url: detail_ytdl.extract_info(url, download=False)
                        )

                        if entry_result:
                            processed_entries.append(
                                cls._format_song_data(dict(entry_result))
                            )
                    except Exception as exc:
                        logger.error(f"Error processing playlist entry: {exc}")
                        continue

                return processed_entries

            single_ytdl = yt_dlp.YoutubeDL(ytdl_opts)  # type: ignore[arg-type]
            result = await loop.run_in_executor(
                None, lambda: single_ytdl.extract_info(search, download=False)
            )
            data = dict(result or {})

            if not data:
                raise Exception("Could not extract video information")

            entries_raw = data.get('entries')
            if isinstance(entries_raw, Iterable):
                entries = [dict(entry) for entry in entries_raw if entry]
                if entries:
                    return cls._format_song_data(entries[0])
                raise Exception("No results found")

            return cls._format_song_data(data)

        except MissingJSRuntimeError:
            raise
        except DownloadError as e:
            error_msg = str(e)
            if "Video unavailable" in error_msg:
                raise Exception("Video is unavailable")
            if "Private video" in error_msg:
                raise Exception("Video is private")
            if "age-restricted" in error_msg.lower():
                raise Exception("Video is age-restricted")
            raise Exception(f"Download error: {error_msg}")
        except Exception as exc:
            logger.error(f"Error in create_source: {exc}")
            raise Exception(f"Could not process request: {exc}")
    
    @classmethod
    def _format_song_data(cls, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Format raw YouTube-DL data into our song format"""
        raw = dict(data)
        return {
            'title': raw.get('title', 'Unknown Title'),
            'url': raw.get('url'),
            'webpage_url': raw.get('webpage_url'),
            'duration': raw.get('duration', 0),
            'thumbnail': raw.get('thumbnail'),
            'uploader': raw.get('uploader', 'Unknown'),
            'view_count': raw.get('view_count', 0),
            'id': raw.get('id'),
            'extractor': raw.get('extractor'),
            'formats': raw.get('formats', []),
            'data': raw,
        }
    
    @classmethod
    async def regather_stream(
        cls,
        song_data: Dict[str, Any],
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        volume: float = 0.5,
    ):
        """Regather the stream URL for playback"""
        loop = loop or asyncio.get_event_loop()

        try:
            _ensure_js_runtime()

            url = song_data.get('url')
            if isinstance(url, str) and url:
                try:
                    return cls(
                        discord.FFmpegPCMAudio(
                            url,
                            before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                            options='-vn',
                        ),
                        data=song_data,
                        volume=volume,
                    )
                except Exception:
                    pass  # URL might be expired; fall back to regathering

            webpage_url = song_data.get('webpage_url')
            if not isinstance(webpage_url, str) or not webpage_url:
                raise Exception("No URL available for regathering")

            ytdl_opts: Dict[str, Any] = dict(YTDL_FORMAT_OPTIONS)
            regather_ytdl = yt_dlp.YoutubeDL(ytdl_opts)  # type: ignore[arg-type]
            result = await loop.run_in_executor(
                None, lambda: regather_ytdl.extract_info(webpage_url, download=False)
            )

            fresh_data = dict(result or {})
            new_url = fresh_data.get('url')
            if not isinstance(new_url, str) or not new_url:
                raise Exception("Could not regather stream URL")

            song_data.update({
                'url': new_url,
                'formats': fresh_data.get('formats', []),
            })

            return cls(
                discord.FFmpegPCMAudio(
                    new_url,
                    before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    options='-vn',
                ),
                data=song_data,
                volume=volume,
            )

        except MissingJSRuntimeError:
            raise
        except Exception as exc:
            logger.error(f"Error regathering stream: {exc}")
            raise Exception(f"Could not prepare audio stream: {exc}")
    
    @staticmethod
    def format_duration(seconds: int) -> str:
        """Format duration in seconds to MM:SS or HH:MM:SS"""
        if not seconds:
            return "Unknown"
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"
    
    @staticmethod
    def format_views(count: int) -> str:
        """Format view count to human readable format"""
        if not count:
            return "Unknown"
        
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        elif count >= 1_000:
            return f"{count / 1_000:.1f}K"
        else:
            return str(count)
    
    def get_embed(self) -> discord.Embed:
        """Create a rich embed for the song"""
        embed = discord.Embed(
            title=self.title,
            url=self.data.get('webpage_url'),
            color=0xFF0000
        )
        
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        
        if self.uploader:
            embed.add_field(name="Uploader", value=self.uploader, inline=True)
        
        if self.duration:
            embed.add_field(
                name="Duration", 
                value=self.format_duration(self.duration), 
                inline=True
            )
        
        if self.view_count:
            embed.add_field(
                name="Views", 
                value=self.format_views(self.view_count), 
                inline=True
            )
        
        return embed
