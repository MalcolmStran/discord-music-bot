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
        """Connect to a voice channel with enhanced retry logic"""
        max_retries = config.VOICE_RECONNECT_ATTEMPTS
        retry_delay = config.VOICE_RETRY_DELAY
        timeout = config.VOICE_CONNECTION_TIMEOUT
        
        for attempt in range(max_retries):
            try:
                # Enhanced cleanup
                if self.voice_client:
                    try:
                        if self.voice_client.is_connected():
                            await self.voice_client.disconnect(force=True)
                        await asyncio.sleep(3)  # Cleanup delay
                    except Exception as cleanup_error:
                        logger.warning(f"Cleanup error: {cleanup_error}")
                    finally:
                        self.voice_client = None
                
                # Progressive delay with improved backoff
                if attempt > 0:
                    delay = retry_delay * (1.5 ** (attempt - 1))  # Gradual backoff
                    delay = min(delay, 20)  # Cap at 20 seconds
                    logger.info(f"Waiting {delay:.1f} seconds before retry...")
                    await asyncio.sleep(delay)
                
                logger.info(f"Attempting to connect to {channel.name} (attempt {attempt + 1}/{max_retries})")
                
                # Connect with optimized timeout
                self.voice_client = await asyncio.wait_for(
                    channel.connect(
                        timeout=timeout,
                        reconnect=False,  # Handle reconnection manually
                        self_deaf=True
                    ),
                    timeout=timeout + 5
                )
                
                # Post-connection validation
                if self.voice_client and self.voice_client.is_connected():
                    await asyncio.sleep(1)  # Let connection stabilize
                    if not self.voice_client.is_connected():
                        logger.warning("Connection became unstable immediately after connect")
                        continue
                    
                    logger.info(f"Successfully connected to voice channel: {channel.name}")
                    self._cancel_disconnect_timer()
                    return True
                else:
                    logger.warning("Connection established but not properly connected")
                    continue
                
            except asyncio.TimeoutError:
                logger.warning(f"Connection timeout on attempt {attempt + 1}")
                
            except discord.errors.ConnectionClosed as e:
                logger.warning(f"Connection closed during connect (code {e.code})")
                if e.code == 4006:  # Session invalid
                    session_delay = 12
                    logger.warning(f"Session error - waiting {session_delay}s before retry")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(session_delay)
                
            except discord.errors.ClientException as e:
                if "already connected" in str(e).lower():
                    logger.warning("Already connected to voice - enhanced cleanup...")
                    if self.voice_client:
                        try:
                            await self.voice_client.disconnect(force=True)
                            await asyncio.sleep(3)
                        except:
                            pass
                        self.voice_client = None
                elif "opus" in str(e).lower():
                    logger.error("Opus library not loaded - voice will not work properly")
                    return False
                else:
                    logger.warning(f"Discord client error on attempt {attempt + 1}: {e}")
                        
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt + 1}: {e}")
                # Enhanced session error handling
                if "4006" in str(e) or "session" in str(e).lower():
                    session_delay = 12
                    logger.warning(f"Session error detected - waiting {session_delay}s before retry")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(session_delay)
        
        logger.error(f"Failed to connect after {max_retries} attempts")
        return False
    
    async def disconnect(self, force_cleanup=False):
        """Disconnect from voice channel with improved cleanup"""
        if self.voice_client:
            self.stop()
            try:
                # Stop any ongoing audio first
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                
                # Disconnect with force flag
                await self.voice_client.disconnect(force=True)
                logger.info("Disconnected from voice channel")
                
                # Additional cleanup for persistent connections
                if force_cleanup:
                    await asyncio.sleep(2)  # Give time for cleanup
                    
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.voice_client = None
        
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        self._cancel_disconnect_timer()
    
    async def ensure_connection(self, channel: discord.VoiceChannel) -> bool:
        """Ensure we have a valid voice connection with session validation"""
        if not self.is_connected:
            return await self.connect(channel)
        
        # Check if connection is still valid
        try:
            # Test the connection by checking the channel
            if self.voice_client and self.voice_client.channel != channel:
                logger.info(f"Moving from {self.voice_client.channel} to {channel.name}")
                await self.voice_client.move_to(channel)
                logger.info(f"Moved to voice channel: {channel.name}")
            
            # Additional validation - check if we can actually use the connection
            if self.voice_client:
                # Check if the voice client is still properly connected
                if not self.voice_client.is_connected():
                    logger.warning("Voice client reports not connected - reconnecting")
                    return await self.connect(channel)
                    
        except discord.errors.ConnectionClosed as e:
            logger.warning(f"Connection closed error (code {e.code}): {e}")
            if e.code == 4006:  # Session no longer valid
                logger.warning("Session invalidated - forcing reconnection")
                await self.disconnect(force_cleanup=True)
                await asyncio.sleep(3)  # Wait before reconnecting
                return await self.connect(channel)
            return await self.connect(channel)
            
        except Exception as e:
            logger.warning(f"Connection validation failed: {e}")
            # For any other error, try to reconnect
            await self.disconnect(force_cleanup=True)
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
        """Check if connected to a voice channel with additional validation"""
        if not self.voice_client:
            return False
        
        try:
            return self.voice_client.is_connected()
        except:
            # If checking connection throws an error, we're not connected
            return False
    
    async def handle_voice_error(self, error):
        """Handle voice-related errors and attempt recovery"""
        if "4006" in str(error):
            logger.warning("Session invalidated (4006) - clearing connection state")
            await self.disconnect(force_cleanup=True)
        elif "ConnectionClosed" in str(error):
            logger.warning("Connection closed - clearing connection state")
            await self.disconnect(force_cleanup=True)
        else:
            logger.error(f"Unhandled voice error: {error}")
    
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
