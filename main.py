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

# Set up logging
logging.basicConfig(
    level=logging.INFO,
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
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
if not DISCORD_TOKEN:
    raise ValueError("No Discord token found. Please set DISCORD_TOKEN in .env file")

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=commands.DefaultHelpCommand()
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
    
    async def on_command_error(self, ctx, error):
        """Global error handler"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found errors
        
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing required argument: {error.param}")
            return
        
        if isinstance(error, commands.BadArgument):
            await ctx.send(f"Bad argument: {error}")
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
    
    try:
        assert DISCORD_TOKEN is not None, "DISCORD_TOKEN cannot be None"
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

if __name__ == "__main__":
    main()
