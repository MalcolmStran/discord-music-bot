"""
YouTube-DL source handling for the music bot
"""

import discord
import yt_dlp
import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Union, Optional, Any

logger = logging.getLogger(__name__)

# YouTube-DL options
YTDL_FORMAT_OPTIONS = {
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
    async def create_source(cls, ctx, search: str, *, loop=None, playlist_items=None):
        """Create a source from a search query or URL"""
        loop = loop or asyncio.get_event_loop()
        
        # Prepare YTDL options
        ytdl_opts = YTDL_FORMAT_OPTIONS.copy()
        if playlist_items:
            ytdl_opts['playliststart'] = 1
            ytdl_opts['playlistend'] = int(playlist_items)
        
        try:
            # Check if it's a playlist
            if any(keyword in search.lower() for keyword in ['playlist', 'list=']):
                ytdl_opts['extract_flat'] = True
                
                # Extract playlist info first
                ytdl = yt_dlp.YoutubeDL(ytdl_opts)
                partial_data = await loop.run_in_executor(
                    None, lambda: ytdl.extract_info(search, download=False)
                )
                
                if not partial_data:
                    raise Exception("Could not extract playlist information")
                
                # Process playlist entries
                entries = partial_data.get('entries', [])
                if not entries:
                    raise Exception("Playlist is empty or unavailable")
                
                # Limit entries to prevent overwhelming
                if len(entries) > 50:
                    entries = entries[:50]
                
                # Extract full info for each entry
                ytdl_opts['extract_flat'] = False
                processed_entries = []
                
                for entry in entries:
                    if not entry:
                        continue
                    
                    try:
                        entry_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                        entry_data = await loop.run_in_executor(
                            None, lambda: ytdl.extract_info(entry_url, download=False)
                        )
                        
                        if entry_data:
                            processed_entries.append(cls._format_song_data(entry_data))
                    
                    except Exception as e:
                        logger.error(f"Error processing playlist entry: {e}")
                        continue
                
                return processed_entries
            
            else:
                # Single video or search
                ytdl = yt_dlp.YoutubeDL(ytdl_opts)
                data = await loop.run_in_executor(
                    None, lambda: ytdl.extract_info(search, download=False)
                )
                
                if not data:
                    raise Exception("Could not extract video information")
                
                # Handle search results
                if 'entries' in data:
                    # Search results
                    entries = [entry for entry in data['entries'] if entry]
                    if not entries:
                        raise Exception("No results found")
                    
                    # Return first result for search
                    return cls._format_song_data(entries[0])
                else:
                    # Direct video
                    return cls._format_song_data(data)
        
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            if "Video unavailable" in error_msg:
                raise Exception("Video is unavailable")
            elif "Private video" in error_msg:
                raise Exception("Video is private")
            elif "age-restricted" in error_msg.lower():
                raise Exception("Video is age-restricted")
            else:
                raise Exception(f"Download error: {error_msg}")
        
        except Exception as e:
            logger.error(f"Error in create_source: {e}")
            raise Exception(f"Could not process request: {str(e)}")
    
    @classmethod
    def _format_song_data(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Format raw YouTube-DL data into our song format"""
        return {
            'title': data.get('title', 'Unknown Title'),
            'url': data.get('url'),
            'webpage_url': data.get('webpage_url'),
            'duration': data.get('duration', 0),
            'thumbnail': data.get('thumbnail'),
            'uploader': data.get('uploader', 'Unknown'),
            'view_count': data.get('view_count', 0),
            'id': data.get('id'),
            'extractor': data.get('extractor'),
            'formats': data.get('formats', []),
            'data': data  # Keep original data for regathering
        }
    
    @classmethod
    async def regather_stream(cls, song_data: Dict[str, Any], *, loop=None, volume=0.5):
        """Regather the stream URL for playback"""
        loop = loop or asyncio.get_event_loop()
        
        try:
            # Check if we have a direct URL that's still valid
            if song_data.get('url'):
                try:
                    return cls(
                        discord.FFmpegPCMAudio(
                            song_data['url'], 
                            before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                            options='-vn'
                        ),
                        data=song_data,
                        volume=volume
                    )
                except Exception:
                    pass  # URL might be expired, continue to regather
            
            # Regather stream from webpage URL
            webpage_url = song_data.get('webpage_url')
            if not webpage_url:
                raise Exception("No URL available for regathering")
            
            ytdl_opts = YTDL_FORMAT_OPTIONS.copy()
            ytdl = yt_dlp.YoutubeDL(ytdl_opts)
            
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(webpage_url, download=False)
            )
            
            if not data or not data.get('url'):
                raise Exception("Could not regather stream URL")
            
            # Update song data with fresh URL
            song_data.update({
                'url': data.get('url'),
                'formats': data.get('formats', [])
            })
            
            return cls(
                discord.FFmpegPCMAudio(
                    data['url'], 
                    before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    options='-vn'
                ),
                data=song_data,
                volume=volume
            )
        
        except Exception as e:
            logger.error(f"Error regathering stream: {e}")
            raise Exception(f"Could not prepare audio stream: {str(e)}")
    
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
