"""
Player management for the music bot
"""

import discord
import logging
import asyncio
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class Player:
    def __init__(self):
        self.voice_client: Optional[discord.VoiceClient] = None
        self.is_playing = False
        self.is_paused = False
        self.volume = 0.5
        self.current_song: Optional[Dict[str, Any]] = None
        self._disconnect_timer = None
        
    def play(self, source: discord.AudioSource, after=None):
        """Play an audio source"""
        if self.voice_client and not self.voice_client.is_playing():
            self.voice_client.play(source, after=after)
            self.is_playing = True
            self.is_paused = False
            logger.info(f"Started playing: {getattr(source, 'title', 'Unknown')}")
    
    def stop(self):
        """Stop current playback"""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        logger.info("Playback stopped")
    
    def pause(self):
        """Pause current playback"""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.is_paused = True
            logger.info("Playback paused")
    
    def resume(self):
        """Resume paused playback"""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.is_paused = False
            logger.info("Playback resumed")
    
    def set_volume(self, volume: float):
        """Set playback volume (0.0 to 1.0)"""
        self.volume = max(0.0, min(1.0, volume))
        # Volume will be applied when creating the next audio source
        logger.info(f"Volume set to {self.volume}")
    
    async def connect(self, channel: discord.VoiceChannel) -> bool:
        """Connect to a voice channel"""
        try:
            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.move_to(channel)
            else:
                self.voice_client = await channel.connect()
            
            logger.info(f"Connected to voice channel: {channel.name}")
            self._cancel_disconnect_timer()
            return True
        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from voice channel"""
        if self.voice_client:
            self.stop()
            await self.voice_client.disconnect()
            self.voice_client = None
            logger.info("Disconnected from voice channel")
        
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        self._cancel_disconnect_timer()
    
    def start_disconnect_timer(self, timeout: int = 300):
        """Start a timer to disconnect after inactivity"""
        self._cancel_disconnect_timer()
        self._disconnect_timer = asyncio.create_task(self._disconnect_after_timeout(timeout))
    
    def _cancel_disconnect_timer(self):
        """Cancel the disconnect timer"""
        if self._disconnect_timer and not self._disconnect_timer.done():
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
    
    async def _disconnect_after_timeout(self, timeout: int):
        """Disconnect after a timeout period"""
        try:
            await asyncio.sleep(timeout)
            if not self.is_playing and self.voice_client:
                await self.disconnect()
                logger.info(f"Auto-disconnected after {timeout} seconds of inactivity")
        except asyncio.CancelledError:
            pass
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to a voice channel"""
        return self.voice_client is not None and self.voice_client.is_connected()
    
    @property
    def current_channel(self):
        """Get the current voice channel"""
        return self.voice_client.channel if self.voice_client else None
    
    def get_status(self) -> dict:
        """Get current player status"""
        return {
            'is_connected': self.is_connected,
            'is_playing': self.is_playing,
            'is_paused': self.is_paused,
            'volume': self.volume,
            'current_song': self.current_song,
            'channel': self.current_channel.name if self.current_channel else None
        }
