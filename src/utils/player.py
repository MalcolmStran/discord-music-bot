"""
Player management for the music bot
"""

import discord
import logging
import asyncio
from typing import Optional, Dict, Any
import sys
import os

# Add parent directory to path to import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import config

logger = logging.getLogger(__name__)

class Player:
    def __init__(self):
        self.voice_client: Optional[discord.VoiceClient] = None
        self.is_playing = False
        self.is_paused = False
        self.volume = config.DEFAULT_VOLUME
        self.current_song: Optional[Dict[str, Any]] = None
        self._disconnect_timer = None
        self.repeat_mode = False  # Add repeat mode flag
        
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
    
    def toggle_repeat(self) -> bool:
        """Toggle repeat mode and return the new state"""
        self.repeat_mode = not self.repeat_mode
        logger.info(f"Repeat mode {'enabled' if self.repeat_mode else 'disabled'}")
        return self.repeat_mode
    
    async def connect(self, channel: discord.VoiceChannel) -> bool:
        """Connect to a voice channel with retry logic"""
        max_retries = config.VOICE_RECONNECT_ATTEMPTS
        retry_delay = config.VOICE_RETRY_DELAY
        timeout = config.VOICE_CONNECTION_TIMEOUT
        
        for attempt in range(max_retries):
            try:
                # Clean up any existing connection first
                if self.voice_client:
                    try:
                        await self.voice_client.disconnect(force=True)
                        await asyncio.sleep(1)  # Give time for cleanup
                    except:
                        pass
                    self.voice_client = None
                
                # Connect with timeout
                self.voice_client = await asyncio.wait_for(
                    channel.connect(timeout=timeout, reconnect=True, self_deaf=True),
                    timeout=timeout + 5
                )
                
                logger.info(f"Connected to voice channel: {channel.name} (attempt {attempt + 1})")
                self._cancel_disconnect_timer()
                return True
                
            except asyncio.TimeoutError:
                logger.warning(f"Connection timeout on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    
            except discord.ClientException as e:
                logger.warning(f"Discord client error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    
            except Exception as e:
                logger.error(f"Failed to connect to voice channel on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
        
        logger.error(f"Failed to connect after {max_retries} attempts")
        return False
    
    async def disconnect(self):
        """Disconnect from voice channel"""
        if self.voice_client:
            self.stop()
            try:
                await self.voice_client.disconnect(force=True)
                logger.info("Disconnected from voice channel")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.voice_client = None
        
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        self._cancel_disconnect_timer()
    
    async def ensure_connection(self, channel: discord.VoiceChannel) -> bool:
        """Ensure we have a valid voice connection"""
        if not self.is_connected:
            return await self.connect(channel)
        
        # Check if connection is still valid
        try:
            if self.voice_client and self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
                logger.info(f"Moved to voice channel: {channel.name}")
        except Exception as e:
            logger.warning(f"Connection validation failed: {e}")
            # Try to reconnect
            return await self.connect(channel)
        
        return True
    
    def start_disconnect_timer(self, timeout: Optional[int] = None):
        """Start a timer to disconnect after inactivity"""
        if timeout is None:
            timeout = config.VOICE_AUTO_DISCONNECT_TIMEOUT
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
            'repeat_mode': self.repeat_mode,
            'current_song': self.current_song,
            'channel': self.current_channel.name if self.current_channel else None
        }
