"""
Media handler cog for Twitter/TikTok video conversion
Automatically converts Twitter and TikTok links to MP4 files

Features:
- Uses ffmpeg-python for robust video processing
- Targets 7MB file size (safe margin under Discord's 8MB limit)
- Adaptive compression settings based on original file size
- Two-pass compression for challenging cases
- Handles videos with or without audio tracks
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
        
        # Discord file size limit - target 7MB to be safe (8MB actual limit)
        self.max_file_size = 7 * 1024 * 1024  # 7MB in bytes
        
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
        
        if file_size <= self.max_file_size:
            return file_path
        
        # File is too large, try to compress
        logger.info(f"File too large ({file_size} bytes), attempting compression")
        return await self._compress_video(file_path)
    
    async def _compress_video(self, file_path: str) -> Optional[str]:
        """
        Compress video to meet 7MB target using ffmpeg-python with two-pass, target-bitrate encoding.
        This approach calculates the required bitrate to fit the file under the size limit, and uses ffmpeg's two-pass encoding for best results.
        """
        try:
            import math
            compressed_path = str(self.temp_dir / f'compressed_{uuid.uuid4()}.mp4')
            original_size = os.path.getsize(file_path)
            target_size = self.max_file_size  # 7MB target

            logger.info(f"Compressing video: {original_size / 1024 / 1024:.2f}MB -> target: {target_size / 1024 / 1024:.2f}MB")

            # Probe for duration and audio
            try:
                probe = ffmpeg.probe(file_path)
                streams = probe['streams']
                video_stream = next(s for s in streams if s['codec_type'] == 'video')
                duration = float(video_stream.get('duration') or probe['format']['duration'])
                has_audio = any(s['codec_type'] == 'audio' for s in streams)
            except Exception as e:
                logger.error(f"ffmpeg.probe failed: {e}")
                return file_path

            # Calculate total bitrate (bits/sec) to fit target size
            # Reserve a minimum for audio, rest for video
            min_audio_bitrate = 48000 if has_audio else 0  # 48k for audio
            # Subtract a little for container overhead (2%)
            overhead = 0.98
            total_bitrate = int((target_size * 8 * overhead) / duration)  # bits/sec
            if has_audio:
                video_bitrate = max(32000, total_bitrate - min_audio_bitrate)  # at least 32k for video
                audio_bitrate = min_audio_bitrate
            else:
                video_bitrate = total_bitrate
                audio_bitrate = None

            logger.info(f"Target bitrates: video={video_bitrate//1000}k, audio={audio_bitrate//1000 if audio_bitrate else 0}k, duration={duration:.2f}s")

            # Build ffmpeg-python command for two-pass encoding
            # First pass
            passlog = str(self.temp_dir / f'ffmpeg2pass_{uuid.uuid4()}')
            input_kwargs = {}
            output_kwargs = {
                'vcodec': 'libx264',
                'b:v': str(video_bitrate),
                'preset': 'fast',
                'pass': 1,
                'f': 'mp4',
                'movflags': '+faststart',
                'y': None,
                'loglevel': 'error',
            }
            if has_audio:
                output_kwargs['an'] = None  # no audio in first pass
            
            # First pass (no output file, just stats)
            output1 = ffmpeg.output(
                ffmpeg.input(file_path, **input_kwargs),
                'NUL' if os.name == 'nt' else '/dev/null',
                **output_kwargs,
                passlogfile=passlog
            )
            # Second pass
            output_kwargs2 = output_kwargs.copy()
            output_kwargs2['pass'] = 2
            if has_audio:
                output_kwargs2.pop('an', None)
                output_kwargs2['acodec'] = 'aac'
                output_kwargs2['b:a'] = str(audio_bitrate)
            output2 = ffmpeg.output(
                ffmpeg.input(file_path, **input_kwargs),
                compressed_path,
                **output_kwargs2,
                passlogfile=passlog
            )

            # Run first pass
            logger.info(f"Running ffmpeg two-pass first pass...")
            process1 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output1),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process1.communicate()

            # Run second pass
            logger.info(f"Running ffmpeg two-pass second pass...")
            process2 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output2),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process2.communicate()

            # Clean up passlog files
            for ext in ('-0.log', '-0.log.mbtree', '-0.log.temp'):  # x264 two-pass log files
                try:
                    os.remove(passlog + ext)
                except Exception:
                    pass

            # Check result
            if os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                logger.info(f"Compressed result: {compressed_size / 1024 / 1024:.2f}MB")
                if compressed_size <= target_size:
                    os.remove(file_path)
                    logger.info(f"Compression successful: {compressed_size / 1024 / 1024:.2f}MB (target: {target_size / 1024 / 1024:.2f}MB)")
                    return compressed_path
                else:
                    # Try fallback: lower bitrate and/or scale down
                    logger.warning(f"Compressed file still over target ({compressed_size / 1024 / 1024:.2f}MB), trying fallback")
                    fallback_bitrate = int(video_bitrate * 0.7)
                    fallback_audio = int(audio_bitrate * 0.7) if audio_bitrate else None
                    fallback_path = str(self.temp_dir / f'compressed_fallback_{uuid.uuid4()}.mp4')
                    output_kwargs2['b:v'] = str(fallback_bitrate)
                    if fallback_audio:
                        output_kwargs2['b:a'] = str(fallback_audio)
                    output3 = ffmpeg.output(
                        ffmpeg.input(file_path, **input_kwargs),
                        fallback_path,
                        **output_kwargs2,
                        passlogfile=passlog
                    )
                    process3 = await asyncio.create_subprocess_exec(
                        *ffmpeg.compile(output3),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await process3.communicate()
                    if os.path.exists(fallback_path):
                        fallback_size = os.path.getsize(fallback_path)
                        if fallback_size < compressed_size:
                            os.remove(compressed_path)
                            compressed_path = fallback_path
                            compressed_size = fallback_size
                        else:
                            os.remove(fallback_path)
                    if compressed_size <= target_size:
                        os.remove(file_path)
                        logger.info(f"Fallback compression successful: {compressed_size / 1024 / 1024:.2f}MB")
                        return compressed_path
                    else:
                        if compressed_size < original_size:
                            os.remove(file_path)
                            logger.warning(f"Compressed file still over target ({compressed_size / 1024 / 1024:.2f}MB) but smaller than original")
                            return compressed_path
                        else:
                            os.remove(compressed_path)
                            logger.warning("Compression failed to reduce file size")
                            return file_path
            logger.error("No compressed file was created")
            return file_path
        except Exception as e:
            logger.error(f"Video compression error: {e}")
            return file_path  # Return original if compression fails
    
    async def _send_video_file(self, message: discord.Message, video_path: str, original_url: str):
        """Send the video file to Discord"""
        try:
            file_size = os.path.getsize(video_path)
            
            if file_size > self.max_file_size:
                await message.reply(
                    f"Video is too large to upload ({file_size // 1024 // 1024}MB > 7MB limit).\n"
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

async def setup(bot):
    await bot.add_cog(MediaHandler(bot))
