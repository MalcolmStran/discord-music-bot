"""
Media handler cog for Twitter/TikTok video conversion
Automatically converts Twitter and TikTok links to MP4 files
"""

import discord
from discord.ext import commands
import os
import requests
import re
import yt_dlp
import uuid
import tempfile
import subprocess
import logging
import ffmpeg
import asyncio
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class MediaHandler(commands.Cog, name="Media"):
    """Handles media conversion from Twitter and TikTok links"""
    
    def __init__(self, bot):
        self.bot = bot
        self.rapidapi_key = os.getenv('RAPIDAPI_KEY')
        
        # Create temp directory for media files
        self.temp_dir = Path(tempfile.gettempdir()) / 'discord_bot_media'
        self.temp_dir.mkdir(exist_ok=True)
        
        # Discord file size limit (8MB for regular users, 50MB for Nitro)
        # Target 7MB to provide buffer below Discord's 8MB limit
        self.max_file_size = 8 * 1024 * 1024  # 8MB in bytes
        self.target_file_size = 7 * 1024 * 1024  # 7MB in bytes (target for compression)
        
        # TikTok API configuration
        self.tiktok_api_url = "https://tiktok-download-without-watermark.p.rapidapi.com/analysis"
        self.tiktok_headers = {
            "x-rapidapi-host": "tiktok-download-without-watermark.p.rapidapi.com",
            "x-rapidapi-key": self.rapidapi_key
        } if self.rapidapi_key else None
        
        # YouTube-dl options for Twitter
        self.ytdl_opts = {
            'format': 'best[ext=mp4]',
            'outtmpl': str(self.temp_dir / '%(extractor)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True
        }
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Auto-convert media links in messages"""
        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return
        
        # Extract URLs from message
        urls = re.findall(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', 
            message.content
        )
        
        if not urls:
            return
        
        for url in urls:
            try:
                video_path = None
                
                # Check for TikTok URLs
                if self._is_tiktok_url(url):
                    video_path = await self._download_tiktok_video(url)
                
                # Check for Twitter/X URLs
                elif self._is_twitter_url(url):
                    video_path = await self._download_twitter_video(url)
                
                # Send the video if successfully downloaded
                if video_path and os.path.exists(video_path):
                    await self._send_video_file(message, video_path, url)
            
            except Exception as e:
                logger.error(f"Error processing URL {url}: {e}")
                # Only send error message for explicit conversion commands
                continue
    
    def _is_tiktok_url(self, url: str) -> bool:
        """Check if URL is a TikTok link"""
        tiktok_patterns = [
            r'tiktok\.com',
            r'vm\.tiktok\.com',
            r'www\.tiktok\.com'
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in tiktok_patterns)
    
    def _is_twitter_url(self, url: str) -> bool:
        """Check if URL is a Twitter/X link"""
        twitter_patterns = [
            r'twitter\.com',
            r'x\.com',
            r'www\.twitter\.com',
            r'www\.x\.com'
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in twitter_patterns)
    
    async def _download_tiktok_video(self, url: str) -> Optional[str]:
        """Download TikTok video using RapidAPI"""
        if not self.rapidapi_key:
            logger.warning("No RapidAPI key provided for TikTok downloads")
            return None
        
        try:
            # Make API request
            querystring = {"url": url, "hd": "0"}
            response = requests.get(
                self.tiktok_api_url, 
                headers=self.tiktok_headers, 
                params=querystring,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"TikTok API error: {response.status_code}")
                return None
            
            # Parse response
            data = response.json().get('data', {})
            video_url = data.get('play')
            
            if not video_url:
                logger.error("No video URL in TikTok API response")
                return None
            
            # Download the video
            unique_filename = self.temp_dir / f'tiktok_{uuid.uuid4()}.mp4'
            video_response = requests.get(video_url, timeout=60)
            video_response.raise_for_status()
            
            # Save video file
            with open(unique_filename, 'wb') as f:
                f.write(video_response.content)
            
            # Check and compress if needed
            return await self._process_video_file(str(unique_filename))
        
        except Exception as e:
            logger.error(f"TikTok download error: {e}")
            return None
    
    async def _download_twitter_video(self, url: str) -> Optional[str]:
        """Download Twitter video using yt-dlp"""
        try:
            # Convert x.com to twitter.com for better compatibility
            if 'x.com' in url:
                url = url.replace('x.com', 'twitter.com')
            
            # Create unique filename
            unique_filename = self.temp_dir / f'twitter_{uuid.uuid4()}.mp4'
            
            # Configure yt-dlp options
            ytdl_opts = self.ytdl_opts.copy()
            ytdl_opts['outtmpl'] = str(unique_filename)
            
            # Download using yt-dlp
            with yt_dlp.YoutubeDL(ytdl_opts) as ydl:
                # Extract info first to check if video exists
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                
                # Download the video
                ydl.download([url])
            
            # Check if file was created
            if not unique_filename.exists():
                return None
            
            # Process the video file
            return await self._process_video_file(str(unique_filename))
        
        except yt_dlp.utils.DownloadError as e:
            if "Unsupported URL" in str(e) or "No video" in str(e):
                logger.info(f"No video found in Twitter URL: {url}")
                return None
            logger.error(f"Twitter download error: {e}")
            return None
        
        except Exception as e:
            logger.error(f"Twitter download error: {e}")
            return None
    
    async def _process_video_file(self, file_path: str) -> Optional[str]:
        """Process video file - compress if too large"""
        if not os.path.exists(file_path):
            return None
        
        # Check file size
        file_size = os.path.getsize(file_path)
        
        if file_size <= self.target_file_size:
            return file_path
        
        # File is too large, try to compress
        logger.info(f"File too large ({file_size} bytes), attempting compression")
        return await self._compress_video(file_path)
    
    async def _compress_video(self, file_path: str) -> Optional[str]:
        """Compress video to meet Discord's file size limit using ffmpeg-python"""
        try:
            compressed_path = str(self.temp_dir / f'compressed_{uuid.uuid4()}.mp4')
            
            # Get video information first
            try:
                probe = ffmpeg.probe(file_path)
                video_info = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
                if not video_info:
                    logger.error("No video stream found in file")
                    return file_path
                
                duration = float(probe['format'].get('duration', 0))
                if duration <= 0:
                    logger.error("Invalid video duration")
                    return file_path
                
            except ffmpeg.Error as e:
                logger.error(f"Error probing video: {e}")
                return file_path
            
            # Calculate target bitrate for 7MB file
            # Formula: target_size_bits = bitrate * duration
            target_size_bits = self.target_file_size * 8  # Convert bytes to bits
            target_bitrate_bps = int(target_size_bits / duration)
            
            # Reserve some bitrate for audio (64kbps) and overhead
            audio_bitrate_bps = 64 * 1000  # 64kbps
            overhead_factor = 0.9  # 10% overhead buffer
            target_video_bitrate = int((target_bitrate_bps - audio_bitrate_bps) * overhead_factor)
            
            # Ensure minimum quality - don't go below 200kbps
            target_video_bitrate = max(target_video_bitrate, 200 * 1000)
            
            logger.info(f"Compressing video: duration={duration:.2f}s, target_video_bitrate={target_video_bitrate//1000}kbps")
            
            # Build ffmpeg pipeline using ffmpeg-python
            stream = ffmpeg.input(file_path)
            
            # Video processing
            video_stream = stream.video.filter('scale', width=-2, height='min(720,ih)')  # Scale down if needed, max height 720p
            
            # Audio processing
            audio_stream = stream.audio
            
            # Output with compression settings (removing movflags for now to debug)
            output = ffmpeg.output(
                video_stream,
                audio_stream,
                compressed_path,
                vcodec='libx264',
                acodec='aac',
                video_bitrate=target_video_bitrate,
                audio_bitrate='64k',
                preset='fast',
                y=None  # Overwrite output file
            )
            
            # Debug: print the command that will be executed
            cmd = ffmpeg.compile(output)
            logger.info(f"FFmpeg command: {' '.join(cmd)}")
            
            # Run the ffmpeg command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"FFmpeg compression failed: {stderr.decode()}")
                return file_path  # Return original if compression fails
            
            # Check if compressed file exists and is smaller
            if os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                
                # If still too large, try more aggressive compression
                if compressed_size > self.target_file_size:
                    logger.info(f"First compression attempt: {compressed_size} bytes, trying more aggressive compression")
                    
                    # Remove the first attempt
                    os.remove(compressed_path)
                    
                    # Try with even lower bitrate and smaller resolution
                    more_aggressive_bitrate = max(target_video_bitrate // 2, 150 * 1000)  # Half bitrate, min 150kbps
                    
                    stream = ffmpeg.input(file_path)
                    video_stream = stream.video.filter('scale', width=-2, height='min(480,ih)')  # Scale to 480p max
                    audio_stream = stream.audio
                    
                    output = ffmpeg.output(
                        video_stream,
                        audio_stream,
                        compressed_path,
                        vcodec='libx264',
                        acodec='aac',
                        video_bitrate=more_aggressive_bitrate,
                        audio_bitrate='48k',  # Lower audio bitrate too
                        preset='fast',
                        y=None
                    )
                    
                    # Run second compression attempt
                    process = await asyncio.create_subprocess_exec(
                        *ffmpeg.compile(output),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    
                    stdout, stderr = await process.communicate()
                    
                    if process.returncode != 0:
                        logger.error(f"Second FFmpeg compression failed: {stderr.decode()}")
                        return file_path
                    
                    if os.path.exists(compressed_path):
                        compressed_size = os.path.getsize(compressed_path)
                
                if compressed_size <= self.target_file_size:
                    # Remove original and return compressed
                    os.remove(file_path)
                    logger.info(f"Compression successful: {compressed_size} bytes (target: {self.target_file_size} bytes)")
                    return compressed_path
                else:
                    # Still too large, remove compressed file
                    if os.path.exists(compressed_path):
                        os.remove(compressed_path)
                    logger.warning(f"Compressed file still too large: {compressed_size} bytes (target: {self.target_file_size} bytes)")
            
            return file_path  # Return original if compression didn't help
        
        except ffmpeg.Error as e:
            logger.error(f"FFmpeg error during video compression: {e}")
            return file_path  # Return original if compression fails
        
        except Exception as e:
            logger.error(f"Video compression error: {e}")
            return file_path  # Return original if compression fails
    
    async def _send_video_file(self, message: discord.Message, video_path: str, original_url: str):
        """Send the video file to Discord"""
        try:
            file_size = os.path.getsize(video_path)
            
            if file_size > self.max_file_size:
                await message.reply(
                    f"Video is too large to upload ({file_size // 1024 // 1024}MB > 8MB limit).\n"
                    f"Original URL: {original_url}",
                    delete_after=30
                )
                return
            
            # Create Discord file
            discord_file = discord.File(video_path)
            
            # Send the file
            await message.reply(file=discord_file)
            
            logger.info(f"Successfully sent video file: {video_path} ({file_size} bytes)")
        
        except discord.HTTPException as e:
            logger.error(f"Discord upload error: {e}")
            await message.reply(
                f"Failed to upload video: {str(e)}\nOriginal URL: {original_url}",
                delete_after=30
            )
        
        except Exception as e:
            logger.error(f"Error sending video file: {e}")
        
        finally:
            # Clean up the file
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
            except Exception as e:
                logger.error(f"Error cleaning up file {video_path}: {e}")
    
    @commands.command(name='convert')
    async def manual_convert(self, ctx, url: str):
        """Manually convert a Twitter or TikTok URL to MP4
        
        Usage: !convert <URL>
        """
        async with ctx.typing():
            try:
                video_path = None
                
                if self._is_tiktok_url(url):
                    video_path = await self._download_tiktok_video(url)
                elif self._is_twitter_url(url):
                    video_path = await self._download_twitter_video(url)
                else:
                    return await ctx.send("❌ Unsupported URL! Only TikTok and Twitter/X links are supported.")
                
                if video_path and os.path.exists(video_path):
                    await self._send_video_file(ctx.message, video_path, url)
                else:
                    await ctx.send("❌ Could not download video from that URL.")
            
            except Exception as e:
                logger.error(f"Manual conversion error: {e}")
                await ctx.send(f"❌ Error converting video: {str(e)}")
    
    @commands.command(name='mediainfo')
    async def media_info(self, ctx):
        """Show media handler information and statistics"""
        embed = discord.Embed(
            title="📺 Media Handler Info",
            color=0x00FF00
        )
        
        # Service status
        tiktok_status = "✅ Available" if self.rapidapi_key else "❌ No API key"
        twitter_status = "✅ Available"
        
        embed.add_field(name="TikTok Support", value=tiktok_status, inline=True)
        embed.add_field(name="Twitter/X Support", value=twitter_status, inline=True)
        embed.add_field(name="Max File Size", value=f"{self.max_file_size // 1024 // 1024}MB", inline=True)
        embed.add_field(name="Compression Target", value=f"{self.target_file_size // 1024 // 1024}MB", inline=True)
        
        # Features
        features = [
            "🔄 Automatic link detection",
            "🗜️ Video compression",
            "📱 Mobile-friendly formats",
            "🚀 Fast processing"
        ]
        
        embed.add_field(
            name="Features",
            value="\n".join(features),
            inline=False
        )
        
        # Usage
        embed.add_field(
            name="Usage",
            value="Just paste TikTok or Twitter links in chat!\nOr use `!convert <URL>` for manual conversion.",
            inline=False
        )
        
        if not self.rapidapi_key:
            embed.add_field(
                name="⚠️ Setup Required",
                value="Add your RapidAPI key to `.env` for TikTok support:\n`RAPIDAPI_KEY=your_key_here`",
                inline=False
            )
        
        await ctx.send(embed=embed)
    
    async def cog_unload(self):
        """Clean up when cog is unloaded"""
        # Clean up temp files
        try:
            import shutil
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up temp directory: {e}")

# Add asyncio import at the top

async def setup(bot):
    await bot.add_cog(MediaHandler(bot))
