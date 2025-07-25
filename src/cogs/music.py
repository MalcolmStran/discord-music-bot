"""
Music cog for the Discord bot
Handles music playback, queue management, and YouTube integration
"""

import discord
from discord.ext import commands
import os
import asyncio
import logging
from pathlib import Path
from typing import Optional
import tempfile
import sys

# Add parent directory to path to import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import config

from ..utils.queue import Queue
from ..utils.player import Player
from ..utils.ytdl import YTDLSource

logger = logging.getLogger(__name__)

class MusicCog(commands.Cog, name="Music"):
    """Music commands for playing audio from YouTube and other sources"""
    
    def __init__(self, bot):
        self.bot = bot
        self.player = Player()
        self.queue = Queue(max_size=config.MAX_QUEUE_SIZE)
        self.max_duration = config.MAX_SONG_DURATION
        
        # Create downloads directory
        self.downloads_dir = Path(config.DOWNLOAD_DIR)
        self.downloads_dir.mkdir(exist_ok=True)
    
    def cog_check(self, ctx):
        """Check if command is used in a guild"""
        if not ctx.guild:
            raise commands.NoPrivateMessage('Music commands cannot be used in DMs')
        return True
    
    async def cog_command_error(self, ctx, error):
        """Handle cog-specific errors"""
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Music commands can only be used in servers!")
        else:
            logger.error(f"Error in music command {ctx.command}: {error}")
            await ctx.send(f"An error occurred: {str(error)}")
    
    @commands.command(name='play', aliases=['p'])
    async def play(self, ctx, *, query: str):
        """Play a song or add it to the queue
        
        Usage: !play <song name or URL>
        Example: !play Never Gonna Give You Up
        """
        # Check queue size
        if self.queue.is_full():
            return await ctx.send(f"Queue is full ({self.queue.max_size} songs max)!")
          # Ensure bot is connected to voice
        if not await self._ensure_voice_connection(ctx):
            return
        
        async with ctx.typing():
            try:
                # Handle playlists differently
                if any(keyword in query.lower() for keyword in ['playlist', 'list=']):
                    await self._handle_playlist(ctx, query)
                else:
                    await self._handle_single_song(ctx, query)
                
                # Start playing if not already playing
                if not self.player.is_playing and not self.queue.is_empty():
                    await self._play_next(ctx)
            except Exception as e:
                logger.error(f"Error in play command: {e}")
                await ctx.send(f"Error playing song: {str(e)}")
    
    async def _handle_single_song(self, ctx, query: str):
        """Handle single song requests"""
        result = await YTDLSource.create_source(ctx, query, loop=self.bot.loop)
        
        if not result:
            return await ctx.send("Could not find that song!")
        
        # Handle both single song and list results
        if isinstance(result, list):
            source = result[0] if result else None
        else:
            source = result
        
        if not source:
            return await ctx.send("Could not find that song!")
        
        # Check duration
        if source.get('duration', 0) > self.max_duration:
            return await ctx.send(f"Song is too long! (Max: {self.max_duration//60} minutes)")
        
        # Add to queue
        if self.queue.add(source):
            embed = discord.Embed(
                title="Added to Queue",
                description=f"**{source['title']}**",
                color=0x00FF00
            )
            embed.add_field(
                name="Duration", 
                value=YTDLSource.format_duration(source.get('duration', 0)), 
                inline=True
            )
            embed.add_field(
                name="Position", 
                value=str(self.queue.size()), 
                inline=True
            )
            
            if source.get('thumbnail'):
                embed.set_thumbnail(url=source['thumbnail'])
            
            await ctx.send(embed=embed)
        else:
            await ctx.send("Could not add song to queue!")
    
    async def _handle_playlist(self, ctx, query: str):
        """Handle playlist requests"""
        # Show initial message
        progress_msg = await ctx.send("🎵 Processing playlist...")
        
        try:
            # Get first song quickly
            first_source = await YTDLSource.create_source(
                ctx, query, loop=self.bot.loop, playlist_items="1"
            )
            
            if isinstance(first_source, list) and first_source:
                first_song = first_source[0]
                if first_song.get('duration', 0) <= self.max_duration:
                    self.queue.add(first_song)
                    await progress_msg.edit(content=f"✅ Added first song: **{first_song['title']}**\nProcessing remaining playlist...")
            
            # Process full playlist in background
            self.bot.loop.create_task(self._process_full_playlist(ctx, query, progress_msg))
        
        except Exception as e:
            await progress_msg.edit(content=f"❌ Error processing playlist: {str(e)}")
    
    async def _process_full_playlist(self, ctx, query: str, progress_msg):
        """Process full playlist in background"""
        try:
            # Get full playlist
            sources = await YTDLSource.create_source(ctx, query, loop=self.bot.loop)
            
            if not isinstance(sources, list):
                return await progress_msg.edit(content="❌ Invalid playlist")
            
            # Process remaining songs (skip first one already added)
            added_count = 1  # First song already added
            skipped_count = 0
            total_songs = len(sources)
            
            for i, source in enumerate(sources[1:], 2):  # Start from second song
                if self.queue.is_full():
                    break
                
                # Check duration
                if source.get('duration', 0) > self.max_duration:
                    skipped_count += 1
                    continue
                
                # Add to queue
                if self.queue.add(source):
                    added_count += 1
                
                # Update progress every 5 songs
                if i % 5 == 0:
                    await progress_msg.edit(
                        content=f"📝 Processing playlist... {i}/{total_songs} songs checked"
                    )
                
                # Small delay to prevent rate limiting
                await asyncio.sleep(0.1)
            
            # Final summary
            summary = f"✅ Playlist processed!\n"
            summary += f"📥 Added: **{added_count}** songs\n"
            if skipped_count > 0:
                summary += f"⏭️ Skipped: **{skipped_count}** songs (too long)\n"
            summary += f"📊 Queue: **{self.queue.size()}/{self.queue.max_size}**"
            
            await progress_msg.edit(content=summary)
        
        except Exception as e:
            logger.error(f"Error processing playlist: {e}")
            await progress_msg.edit(content=f"❌ Error processing playlist: {str(e)}")
    
    async def _ensure_voice_connection(self, ctx) -> bool:
        """Ensure bot is connected to a voice channel with better error handling"""
        if not ctx.author.voice:
            await ctx.send("You need to join a voice channel first!")
            return False
        
        target_channel = ctx.author.voice.channel
        
        if not self.player.is_connected:
            await ctx.send("🔗 Connecting to voice channel...")
            success = await self.player.connect(target_channel)
            if not success:
                await ctx.send("❌ Could not connect to voice channel! This might be due to:")
                await ctx.send("• Discord voice server issues\n• Bot permissions\n• Network connectivity")
                await ctx.send("Please try again in a few moments.")
                return False
            await ctx.send(f"✅ Connected to **{target_channel.name}**")
        else:
            # Ensure we're still properly connected
            success = await self.player.ensure_connection(target_channel)
            if not success:
                await ctx.send("❌ Lost voice connection! Attempting to reconnect...")
                # Clear the connection and try again
                await self.player.disconnect(force_cleanup=True)
                await asyncio.sleep(2)
                success = await self.player.connect(target_channel)
                if not success:
                    await ctx.send("❌ Failed to reconnect. Please try the command again.")
                    return False
                await ctx.send(f"✅ Reconnected to **{target_channel.name}**")
        
        return True
    
    async def _play_next(self, ctx):
        """Play the next song in the queue"""
        # Check if we should repeat the current song
        if self.player.repeat_mode and self.player.current_song:
            next_song = self.player.current_song
        else:
            if self.queue.is_empty():
                self.player.is_playing = False
                self.player.start_disconnect_timer(300)  # 5 minutes
                return
            
            next_song = self.queue.get_next()
            if not next_song:
                return
        
        # Check if still connected to voice
        if not self.player.is_connected:
            logger.error("Error playing next song: Not connected to voice.")
            # Try to reconnect if user is still in voice
            if ctx.author.voice:
                logger.info("Attempting to reconnect...")
                if not await self._ensure_voice_connection(ctx):
                    await ctx.send("❌ Lost voice connection and couldn't reconnect!")
                    return
            else:
                await ctx.send("❌ Lost voice connection - please rejoin a voice channel!")
                return
        
        try:
            # Create audio source with better error handling
            try:
                source = await YTDLSource.regather_stream(next_song, loop=self.bot.loop, volume=self.player.volume)
            except Exception as e:
                logger.error(f"Failed to create audio source: {e}")
                await ctx.send(f"❌ Failed to load: **{next_song['title']}** - Skipping...")
                # If in repeat mode and this song failed, turn off repeat mode
                if self.player.repeat_mode:
                    self.player.repeat_mode = False
                    await ctx.send("🔁 Repeat mode disabled due to playback error")
                await self._play_next(ctx)  # Try next song
                return
            
            # Play with callback for next song
            def after_song(error):
                if error:
                    logger.error(f'Player error: {error}')
                    # Don't continue playing if there was an error
                    self.player.is_playing = False
                    return
                
                # Schedule next song only if still connected
                if self.player.is_connected:
                    asyncio.run_coroutine_threadsafe(self._play_next(ctx), self.bot.loop)
            
            self.player.current_song = next_song
            self.player.play(source, after=after_song)
            
            # Send now playing message
            title = "🔁 Repeating" if self.player.repeat_mode else "🎵 Now Playing"
            embed = discord.Embed(
                title=title,
                description=f"**{next_song['title']}**",
                color=0x0099FF
            )
            
            if next_song.get('uploader'):
                embed.add_field(name="Uploader", value=next_song['uploader'], inline=True)
            
            embed.add_field(
                name="Duration", 
                value=YTDLSource.format_duration(next_song.get('duration', 0)), 
                inline=True
            )
            
            if self.queue.size() > 0:
                embed.add_field(name="Queue", value=f"{self.queue.size()} songs", inline=True)
            
            if self.player.repeat_mode:
                embed.add_field(name="Repeat", value="🔁 On", inline=True)
            
            if next_song.get('thumbnail'):
                embed.set_thumbnail(url=next_song['thumbnail'])
            
            await ctx.send(embed=embed)
        
        except Exception as e:
            logger.error(f"Error playing next song: {e}")
            await ctx.send(f"❌ Error playing song: {str(e)}")
            # Only try next song if we still have connection
            if self.player.is_connected:
                await self._play_next(ctx)  # Try next song
    
    @commands.command(name='skip', aliases=['s'])
    async def skip(self, ctx):
        """Skip the current song"""
        if not self.player.is_playing:
            return await ctx.send("Nothing is playing!")
        
        # Turn off repeat mode when skipping
        was_repeating = self.player.repeat_mode
        if was_repeating:
            self.player.repeat_mode = False
        
        self.player.stop()
        
        if was_repeating:
            await ctx.send("⏭️ Skipped and disabled repeat mode!")
        else:
            await ctx.send("⏭️ Skipped!")
    
    @commands.command(name='stop')
    async def stop(self, ctx):
        """Stop playback and clear the queue"""
        self.queue.clear()
        self.player.repeat_mode = False  # Turn off repeat mode
        self.player.stop()
        await ctx.send("⏹️ Stopped playback and cleared queue!")
    
    @commands.command(name='pause')
    async def pause(self, ctx):
        """Pause the current song"""
        if not self.player.is_playing:
            return await ctx.send("Nothing is playing!")
        
        if self.player.is_paused:
            return await ctx.send("Already paused!")
        
        self.player.pause()
        await ctx.send("⏸️ Paused!")
    
    @commands.command(name='resume')
    async def resume(self, ctx):
        """Resume paused playback"""
        if not self.player.is_paused:
            return await ctx.send("Not paused!")
        
        self.player.resume()
        await ctx.send("▶️ Resumed!")
    
    @commands.command(name='volume', aliases=['vol'])
    async def volume(self, ctx, volume: Optional[int] = None):
        """Set or show the volume (0-100)"""
        if volume is None:
            return await ctx.send(f"Current volume: {int(self.player.volume * 100)}%")
        
        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100!")
        
        self.player.set_volume(volume / 100)
        await ctx.send(f"🔊 Volume set to {volume}%")
    
    @commands.command(name='queue', aliases=['q'])
    async def show_queue(self, ctx, page: int = 1):
        """Show the current queue"""
        if self.queue.is_empty():
            return await ctx.send("Queue is empty!")
        
        songs_per_page = 10
        queue_list = self.queue.current_queue()
        total_pages = (len(queue_list) + songs_per_page - 1) // songs_per_page
        
        if page < 1 or page > total_pages:
            return await ctx.send(f"Invalid page! (1-{total_pages})")
        
        start_idx = (page - 1) * songs_per_page
        end_idx = start_idx + songs_per_page
        page_songs = queue_list[start_idx:end_idx]
        
        embed = discord.Embed(
            title=f"📋 Queue (Page {page}/{total_pages})",
            color=0x0099FF
        )
        
        queue_text = ""
        for i, song in enumerate(page_songs, start_idx + 1):
            duration = YTDLSource.format_duration(song.get('duration', 0))
            queue_text += f"`{i}.` **{song['title']}** `[{duration}]`\n"
        
        embed.description = queue_text
        
        # Add queue info
        queue_info = self.queue.get_queue_info()
        embed.add_field(
            name="Queue Info",
            value=f"Songs: {queue_info['size']}/{queue_info['max_size']}\n"
                  f"Total Duration: {YTDLSource.format_duration(queue_info['total_duration'])}",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='repeat', aliases=['loop'])
    async def repeat(self, ctx):
        """Toggle repeat mode for the current song"""
        if not self.player.current_song:
            return await ctx.send("No song is currently playing!")
        
        new_state = self.player.toggle_repeat()
        
        embed = discord.Embed(
            title="🔁 Repeat Mode",
            description=f"Repeat is now **{'ON' if new_state else 'OFF'}**",
            color=0x00FF00 if new_state else 0xFF0000
        )
        
        if new_state:
            embed.add_field(
                name="Current Song",
                value=f"**{self.player.current_song['title']}**",
                inline=False
            )
            embed.set_footer(text="The current song will repeat until you turn off repeat mode or skip/stop.")
        else:
            embed.set_footer(text="The current song will finish and continue to the next song in queue.")
        
        await ctx.send(embed=embed)
    
    @commands.command(name='nowplaying', aliases=['np'])
    async def now_playing(self, ctx):
        """Show information about the current song"""
        if not self.player.is_playing or not self.player.current_song:
            return await ctx.send("Nothing is playing!")
        
        song = self.player.current_song
        embed = discord.Embed(
            title="🎵 Currently Playing",
            description=f"**{song['title']}**",
            color=0x0099FF
        )
        
        if song.get('uploader'):
            embed.add_field(name="Uploader", value=song['uploader'], inline=True)
        
        embed.add_field(
            name="Duration", 
            value=YTDLSource.format_duration(song.get('duration', 0)), 
            inline=True
        )
        
        embed.add_field(name="Volume", value=f"{int(self.player.volume * 100)}%", inline=True)
        
        if self.queue.size() > 0:
            next_song = self.queue.peek_next()
            if next_song:
                embed.add_field(
                    name="Up Next", 
                    value=next_song['title'][:50] + "..." if len(next_song['title']) > 50 else next_song['title'],
                    inline=False
                )
        
        if song.get('thumbnail'):
            embed.set_thumbnail(url=song['thumbnail'])
        
        if song.get('webpage_url'):
            embed.url = song['webpage_url']
        
        await ctx.send(embed=embed)
    
    @commands.command(name='clear')
    async def clear_queue(self, ctx):
        """Clear the queue"""
        if self.queue.is_empty():
            return await ctx.send("Queue is already empty!")
        
        self.queue.clear()
        await ctx.send("🗑️ Queue cleared!")
    
    @commands.command(name='shuffle')
    async def shuffle_queue(self, ctx):
        """Shuffle the queue"""
        if self.queue.size() < 2:
            return await ctx.send("Need at least 2 songs in queue to shuffle!")
        
        self.queue.shuffle()
        await ctx.send("🔀 Queue shuffled!")
    
    @commands.command(name='remove', aliases=['rm'])
    async def remove_song(self, ctx, index: int):
        """Remove a song from the queue by its position"""
        if self.queue.is_empty():
            return await ctx.send("Queue is empty!")
        
        if index < 1 or index > self.queue.size():
            return await ctx.send(f"Invalid position! Use a number between 1 and {self.queue.size()}")
        
        removed_song = self.queue.remove(index - 1)  # Convert to 0-based index
        if removed_song:
            await ctx.send(f"🗑️ Removed: **{removed_song['title']}**")
        else:
            await ctx.send("Could not remove that song!")
    
    @commands.command(name='reconnect')
    async def reconnect(self, ctx):
        """Manually reconnect to voice channel"""
        if not ctx.author.voice:
            return await ctx.send("You need to be in a voice channel!")
        
        # Disconnect first
        if self.player.is_connected:
            await self.player.disconnect()
            await asyncio.sleep(1)
        
        # Reconnect
        success = await self.player.connect(ctx.author.voice.channel)
        if success:
            await ctx.send("🔄 Successfully reconnected to voice channel!")
        else:
            await ctx.send("❌ Failed to reconnect to voice channel!")
    
    @commands.command(name='status')
    async def voice_status(self, ctx):
        """Check voice connection status"""
        status = self.player.get_status()
        
        embed = discord.Embed(
            title="🔊 Voice Status",
            color=0x00FF00 if status['is_connected'] else 0xFF0000
        )
        
        embed.add_field(name="Connected", value="✅ Yes" if status['is_connected'] else "❌ No", inline=True)
        embed.add_field(name="Playing", value="✅ Yes" if status['is_playing'] else "❌ No", inline=True)
        embed.add_field(name="Paused", value="✅ Yes" if status['is_paused'] else "❌ No", inline=True)
        embed.add_field(name="Volume", value=f"{int(status['volume'] * 100)}%", inline=True)
        embed.add_field(name="Channel", value=status['channel'] or "None", inline=True)
        embed.add_field(name="Queue Size", value=str(self.queue.size()), inline=True)
        embed.add_field(name="Repeat Mode", value="🔁 On" if status['repeat_mode'] else "❌ Off", inline=True)
        
        if status['current_song']:
            embed.add_field(
                name="Current Song", 
                value=status['current_song']['title'][:50] + "..." if len(status['current_song']['title']) > 50 else status['current_song']['title'],
                inline=False
            )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='disconnect', aliases=['dc', 'leave'])
    async def disconnect(self, ctx):
        """Disconnect the bot from the voice channel"""
        if not self.player.is_connected:
            return await ctx.send("Not connected to a voice channel!")
        
        await self.player.disconnect()
        self.queue.clear()
        await ctx.send("👋 Disconnected!")
    
    @commands.command(name='voice-debug', aliases=['vdebug'])
    @commands.has_permissions(administrator=True)
    async def voice_debug(self, ctx):
        """Debug voice connection issues (Admin only)"""
        embed = discord.Embed(title="🔧 Voice Connection Debug", color=0x3498db)
        
        # Check user voice state
        if ctx.author.voice:
            embed.add_field(
                name="User Voice Channel", 
                value=f"✅ {ctx.author.voice.channel.name}", 
                inline=False
            )
        else:
            embed.add_field(
                name="User Voice Channel", 
                value="❌ Not in voice channel", 
                inline=False
            )
        
        # Check bot voice state
        bot_voice = ctx.guild.voice_client
        if bot_voice:
            embed.add_field(
                name="Bot Voice Client", 
                value=f"✅ Connected to {bot_voice.channel.name}", 
                inline=False
            )
            embed.add_field(
                name="Voice Client State", 
                value=f"Connected: {bot_voice.is_connected()}\nPlaying: {bot_voice.is_playing()}\nPaused: {bot_voice.is_paused()}", 
                inline=False
            )
        else:
            embed.add_field(
                name="Bot Voice Client", 
                value="❌ Not connected", 
                inline=False
            )
        
        # Check player state
        player_status = self.player.get_status()
        embed.add_field(
            name="Player State", 
            value=f"Connected: {player_status['is_connected']}\nPlaying: {player_status['is_playing']}\nChannel: {player_status['channel'] or 'None'}", 
            inline=False
        )
        
        # Check bot permissions
        if ctx.author.voice and ctx.author.voice.channel:
            perms = ctx.author.voice.channel.permissions_for(ctx.guild.me)
            embed.add_field(
                name="Bot Permissions", 
                value=f"Connect: {'✅' if perms.connect else '❌'}\nSpeak: {'✅' if perms.speak else '❌'}\nUse VAD: {'✅' if perms.use_voice_activation else '❌'}", 
                inline=False
            )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='force-reconnect', aliases=['freconnect'])
    @commands.has_permissions(administrator=True)
    async def force_reconnect(self, ctx):
        """Force a voice reconnection (Admin only)"""
        if not ctx.author.voice:
            return await ctx.send("❌ You need to be in a voice channel!")
        
        await ctx.send("🔄 Forcing voice reconnection...")
        
        # Force disconnect first
        await self.player.disconnect(force_cleanup=True)
        await asyncio.sleep(3)
        
        # Try to reconnect
        success = await self.player.connect(ctx.author.voice.channel)
        if success:
            await ctx.send("✅ Successfully reconnected!")
        else:
            await ctx.send("❌ Failed to reconnect. Check the logs for details.")

async def setup(bot):
    await bot.add_cog(MusicCog(bot))
