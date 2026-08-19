#!/usr/bin/env python3
"""
Discord Music Bot with Twitter/TikTok Video Support
A feature-rich Discord bot that can play music from YouTube and convert Twitter/TikTok videos to MP4.
"""

import discord
from discord.ext import commands
import os
import logging
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import config

# Set up logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = config.DISCORD_TOKEN
if not DISCORD_TOKEN:
    raise ValueError("No Discord token found. Please set DISCORD_TOKEN in .env file")

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=config.COMMAND_PREFIX,
            intents=intents,
            help_command=None  # We'll define our own
        )
    
    async def setup_hook(self):
        """Load cogs when bot starts"""
        logger.info("Loading cogs...")
        
        # Load music cog
        try:
            await self.load_extension('src.cogs.music')
            logger.info("Music cog loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load music cog: {e}")
        
        # Load media handler cog
        try:
            await self.load_extension('src.cogs.media_handler')
            logger.info("Media handler cog loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load media handler cog: {e}")
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guilds')
        
        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="!help for commands"
            )
        )
    
    async def on_voice_state_update(self, member, before, after):
        """Handle voice state changes"""
        # If the bot was disconnected from voice
        if member == self.user and before.channel is not None and after.channel is None:
            logger.warning("Bot was disconnected from voice channel")
            
            # Clean up music player state
            music_cog = self.get_cog('Music')
            if music_cog and hasattr(music_cog, 'player'):
                # Type cast to access the player attribute safely
                player = getattr(music_cog, 'player', None)
                if player:
                    # Force cleanup without attempting graceful disconnect
                    player.voice_client = None
                    player.is_playing = False
                    player.is_paused = False
                    # Don't clear current_song in case we want to retry
                    logger.info("Cleared player state after voice disconnect")
        
        # If bot was moved between channels
        elif (member == self.user and 
              before.channel is not None and 
              after.channel is not None and 
              before.channel != after.channel):
            logger.info(f"Bot moved from {before.channel.name} to {after.channel.name}")
            
            # Update player's voice client channel reference
            music_cog = self.get_cog('Music')
            if music_cog and hasattr(music_cog, 'player'):
                player = getattr(music_cog, 'player', None)
                if player and player.voice_client:
                    # The voice client should automatically handle the move
                    logger.info("Voice client channel updated after move")
    
    async def on_command_error(self, ctx, error):
        """Global error handler"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found errors
        
        if isinstance(error, commands.MissingRequiredArgument):
            cmd_name = ctx.command.name if ctx.command else "that command"
            friendly = {
                'play': "🎵 Please provide a song name or URL!\n**Usage:** `!play <song name or URL>`\n**Example:** `!play Never Gonna Give You Up`",
                'convert': "📺 Please provide a URL to convert!\n**Usage:** `!convert <Twitter/X or TikTok URL>`",
                'remove': "🗑️ Please provide a queue position to remove!\n**Usage:** `!remove <position>`\n**Example:** `!remove 3`",
            }
            await ctx.send(friendly.get(cmd_name, f"⚠️ Missing required argument for `!{cmd_name}`: `{error.param.name}`"))
            return
        
        if isinstance(error, commands.BadArgument):
            cmd_name = ctx.command.name if ctx.command else "that command"
            await ctx.send(f"⚠️ Invalid argument for `!{cmd_name}`. Check `!help` for usage.")
            return
        
        logger.error(f"Unhandled error in command {ctx.command}: {error}")
        await ctx.send("An unexpected error occurred. Please try again later.")

def main():
    """Main entry point"""
    bot = MusicBot()
    
    @bot.command(name='reload')
    @commands.is_owner()
    async def reload_cog(ctx, cog_name: str):
        """Reload a cog (owner only)"""
        try:
            await bot.reload_extension(f'src.cogs.{cog_name}')
            await ctx.send(f'Reloaded {cog_name} cog')
            logger.info(f'Reloaded {cog_name} cog')
        except Exception as e:
            await ctx.send(f'Failed to reload {cog_name}: {e}')
            logger.error(f'Failed to reload {cog_name}: {e}')
    
    @bot.command(name='shutdown')
    @commands.is_owner()
    async def shutdown(ctx):
        """Shutdown the bot (owner only)"""
        await ctx.send('Shutting down...')
        logger.info('Bot shutting down by owner command')
        await bot.close()
    
    @bot.command(name='help')
    async def help_command(ctx):
        """Show this help message"""
        embed = discord.Embed(
            title="🎵 Music Bot Commands",
            description="A feature-rich Discord music bot with optional Twitter/TikTok media conversion.",
            color=0x3498db
        )
        
        # Music commands
        music_cmds = (
            "**!play** `<query>` — Play a song or add it to the queue\n"
            "**!skip** — Skip the current song\n"
            "**!stop** — Stop playback and clear queue\n"
            "**!pause** / **!resume** — Pause or resume playback\n"
            "**!queue** — Show the current queue\n"
            "**!nowplaying** — Show what's currently playing\n"
            "**!volume** `<0-100>` — Adjust playback volume\n"
            "**!join** / **!leave** — Join or leave your voice channel\n"
            "**!loop** — Toggle repeat mode"
        )
        embed.add_field(name="🎶 Music", value=music_cmds, inline=False)
        
        # Media conversion commands
        media_cmds = (
            "**!convert** `<url>` — Manually convert a Twitter/X or TikTok link to MP4\n"
            "**!media-toggle** — *(Admin only)* Enable/disable media conversion for this server\n"
            "**!mediainfo** — Show media conversion status and limits"
        )
        embed.add_field(name="📺 Media Conversion", value=media_cmds, inline=False)
        
        # Admin commands
        admin_cmds = (
            "**!voice-debug** — Debug voice connection issues\n"
            "**!force-reconnect** — Force a voice channel reconnection\n"
            "**!media-cleanup** — Clean up temporary media files\n"
            "**!media-status** — Show disk usage and handler status"
        )
        embed.add_field(name="🔧 Admin", value=admin_cmds, inline=False)
        
        # Per-server note
        embed.set_footer(text="Media conversion is per-server: use !media-toggle to disable it on servers that only want music.")
        
        await ctx.send(embed=embed)
    
    try:
        assert DISCORD_TOKEN is not None, "DISCORD_TOKEN cannot be None"
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

if __name__ == "__main__":
    main()
