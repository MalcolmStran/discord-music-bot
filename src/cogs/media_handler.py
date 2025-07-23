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
        Compress video to meet 7MB target using ffmpeg-python
        
        Uses adaptive compression settings based on the required compression ratio:
        - Light compression: CRF 26, 854px width, 96k audio
        - Medium compression: CRF 28, 640px width, 64k audio  
        - Heavy compression: CRF 30, 480px width, 48k audio
        - Very heavy compression: CRF 32, 320px width, 32k audio
        
        If first pass still exceeds target, attempts second pass with more aggressive settings.
        Properly handles videos with or without audio tracks.
        """
        try:
            compressed_path = str(self.temp_dir / f'compressed_{uuid.uuid4()}.mp4')
            
            # Get video information for better compression planning
            original_size = os.path.getsize(file_path)
            target_size = self.max_file_size  # 7MB target
            
            logger.info(f"Compressing video: {original_size / 1024 / 1024:.2f}MB -> target: {target_size / 1024 / 1024:.2f}MB")
            
            # Calculate compression ratio needed
            size_ratio = target_size / original_size
            
            # Determine compression settings based on size ratio
            if size_ratio > 0.8:
                # Minor compression needed
                crf = 26
                scale_width = 854  # 480p width
                audio_bitrate = '96k'
            elif size_ratio > 0.5:
                # Medium compression needed  
                crf = 28
                scale_width = 640  # Lower resolution
                audio_bitrate = '64k'
            elif size_ratio > 0.3:
                # Heavy compression needed
                crf = 30
                scale_width = 480
                audio_bitrate = '48k'
            else:
                # Very heavy compression needed
                crf = 32
                scale_width = 320
                audio_bitrate = '32k'
            
            # Use ffmpeg-python to build the compression pipeline
            input_stream = ffmpeg.input(file_path)
            
            # Check if video has audio stream
            try:
                probe = ffmpeg.probe(file_path)
                has_audio = any(stream['codec_type'] == 'audio' for stream in probe['streams'])
            except Exception:
                has_audio = False  # Assume no audio if probe fails
            
            # Build video filter chain
            video = input_stream.video.filter('scale', scale_width, -2)
            
            # Apply compression settings
            if has_audio:
                audio = input_stream.audio
                output = ffmpeg.output(
                    video, audio,
                    compressed_path,
                    vcodec='libx264',
                    crf=crf,
                    preset='fast',
                    acodec='aac',
                    audio_bitrate=audio_bitrate,
                    movflags='+faststart'
                ).overwrite_output()
            else:
                # Video only, no audio
                output = ffmpeg.output(
                    video,
                    compressed_path,
                    vcodec='libx264',
                    crf=crf,
                    preset='fast',
                    movflags='+faststart'
                ).overwrite_output()
            
            # Run the compression
            logger.info(f"Running compression with CRF={crf}, scale={scale_width}, audio={audio_bitrate}")
            process = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"FFmpeg compression failed: {stderr.decode()}")
                return file_path
            
            # Check result
            if os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                logger.info(f"First compression attempt: {compressed_size / 1024 / 1024:.2f}MB")
                
                # If still too large, try a second pass with more aggressive settings
                if compressed_size > target_size and compressed_size < original_size:
                    logger.info("First compression too large, attempting second pass")
                    second_pass_path = str(self.temp_dir / f'compressed2_{uuid.uuid4()}.mp4')
                    
                    # More aggressive settings
                    aggressive_crf = min(35, crf + 4)
                    smaller_scale = max(240, scale_width - 160)
                    lower_audio = '24k' if audio_bitrate != '32k' else '16k'
                    
                    input_stream2 = ffmpeg.input(compressed_path)
                    video2 = input_stream2.video.filter('scale', smaller_scale, -2)
                    
                    if has_audio:
                        audio2 = input_stream2.audio
                        output2 = ffmpeg.output(
                            video2, audio2,
                            second_pass_path,
                            vcodec='libx264',
                            crf=aggressive_crf,
                            preset='fast',
                            acodec='aac',
                            audio_bitrate=lower_audio,
                            movflags='+faststart'
                        ).overwrite_output()
                    else:
                        output2 = ffmpeg.output(
                            video2,
                            second_pass_path,
                            vcodec='libx264',
                            crf=aggressive_crf,
                            preset='fast',
                            movflags='+faststart'
                        ).overwrite_output()
                    
                    logger.info(f"Second pass: CRF={aggressive_crf}, scale={smaller_scale}, audio={lower_audio}")
                    process2 = await asyncio.create_subprocess_exec(
                        *ffmpeg.compile(output2),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await process2.communicate()
                    
                    if os.path.exists(second_pass_path):
                        second_size = os.path.getsize(second_pass_path)
                        logger.info(f"Second compression result: {second_size / 1024 / 1024:.2f}MB")
                        
                        # Use the better result
                        if second_size <= target_size or second_size < compressed_size:
                            os.remove(compressed_path)
                            compressed_path = second_pass_path
                            compressed_size = second_size
                        else:
                            os.remove(second_pass_path)
                
                # Check final result
                if compressed_size <= target_size:
                    # Success - remove original and return compressed
                    os.remove(file_path)
                    logger.info(f"Compression successful: {compressed_size / 1024 / 1024:.2f}MB (target: {target_size / 1024 / 1024:.2f}MB)")
                    return compressed_path
                else:
                    # Still too large but smaller than original - return compressed anyway
                    if compressed_size < original_size:
                        os.remove(file_path)
                        logger.warning(f"Compressed file still over target ({compressed_size / 1024 / 1024:.2f}MB) but smaller than original")
                        return compressed_path
                    else:
                        # Compression made it larger somehow - return original
                        os.remove(compressed_path)
                        logger.warning("Compression failed to reduce file size")
                        return file_path
            
            # No compressed file created
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
