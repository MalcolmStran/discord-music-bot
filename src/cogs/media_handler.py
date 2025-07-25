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
        
        # Download limits to prevent disk space issues
        self.max_download_size = 500 * 1024 * 1024  # 500MB max download
        self.min_free_space = 1024 * 1024 * 1024  # Require 1GB free space
        
        # TikTok API configuration
        self.tiktok_api_url = "https://tiktok-download-without-watermark.p.rapidapi.com/analysis"
        self.tiktok_headers = {
            "x-rapidapi-host": "tiktok-download-without-watermark.p.rapidapi.com",
            "x-rapidapi-key": self.rapidapi_key
        } if self.rapidapi_key else None
        
        # YouTube-dl options for Twitter with size limits
        self.ytdl_opts = {
            'format': f'best[filesize<{self.max_download_size}][ext=mp4]',
            'outtmpl': str(self.temp_dir / '%(extractor)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'max_filesize': self.max_download_size,
        }
        
        # Cleanup old files on startup
        asyncio.create_task(self._cleanup_old_files())
        
        # Start periodic cleanup task
        asyncio.create_task(self._periodic_cleanup())
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Auto-convert media links in messages with improved error handling"""
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
                
                # Send user-friendly error messages for specific cases
                if "too large" in str(e).lower():
                    await message.reply(
                        f"❌ Video is too large to process (max: {self.max_download_size // 1024 // 1024}MB)\n"
                        f"Original URL: {url}",
                        delete_after=30
                    )
                elif "disk space" in str(e).lower() or "no space left" in str(e).lower():
                    await message.reply(
                        f"⚠️ Insufficient storage space for video processing.\n"
                        f"Please try again later.",
                        delete_after=30
                    )
                elif "timeout" in str(e).lower():
                    await message.reply(
                        f"⏱️ Video download timed out. The video may be too large or the connection too slow.\n"
                        f"Original URL: {url}",
                        delete_after=30
                    )
                # For automatic link detection, don't send generic error messages
                # Only send specific known error types to avoid spam
    
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
    
    async def _cleanup_old_files(self):
        """Clean up old temporary files on startup"""
        try:
            import time
            current_time = time.time()
            cleaned_count = 0
            
            for file_path in self.temp_dir.glob('*'):
                if file_path.is_file():
                    # Remove files older than 1 hour
                    if current_time - file_path.stat().st_mtime > 3600:
                        try:
                            file_path.unlink()
                            cleaned_count += 1
                        except Exception as e:
                            logger.warning(f"Could not remove old file {file_path}: {e}")
            
            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} old temporary files")
                
        except Exception as e:
            logger.error(f"Error during startup cleanup: {e}")
    
    async def _periodic_cleanup(self):
        """Periodically clean up temporary files"""
        while True:
            try:
                await asyncio.sleep(1800)  # Clean up every 30 minutes
                await self._cleanup_old_files()
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
                await asyncio.sleep(1800)  # Wait before retrying
    
    def _check_disk_space(self) -> bool:
        """Check if there's enough disk space for downloads"""
        try:
            import shutil
            free_space = shutil.disk_usage(self.temp_dir).free
            
            if free_space < self.min_free_space:
                logger.warning(f"Low disk space: {free_space // 1024 // 1024}MB free, need {self.min_free_space // 1024 // 1024}MB")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking disk space: {e}")
            return True  # Assume OK if we can't check
    
    async def _safe_download_with_cleanup(self, download_func, *args, **kwargs):
        """Wrapper for downloads with automatic cleanup on failure"""
        temp_files = []
        
        try:
            # Check disk space before starting
            if not self._check_disk_space():
                raise Exception("Insufficient disk space for download")
            
            # Track files before download
            existing_files = set(self.temp_dir.glob('*'))
            
            # Perform download
            result = await download_func(*args, **kwargs)
            
            # Track new files created
            new_files = set(self.temp_dir.glob('*')) - existing_files
            temp_files.extend(new_files)
            
            return result
            
        except Exception as e:
            # Clean up any files created during failed download
            for file_path in temp_files:
                try:
                    if file_path.exists():
                        file_path.unlink()
                        logger.debug(f"Cleaned up failed download file: {file_path}")
                except Exception as cleanup_error:
                    logger.warning(f"Could not clean up file {file_path}: {cleanup_error}")
            
            # Re-raise the original exception
            raise e
    
    async def _download_tiktok_video(self, url: str) -> Optional[str]:
        """Download TikTok video using RapidAPI with improved error handling"""
        if not self.rapidapi_key:
            logger.warning("No RapidAPI key provided for TikTok downloads")
            return None

        return await self._safe_download_with_cleanup(self._download_tiktok_video_impl, url)
    
    async def _download_tiktok_video_impl(self, url: str) -> Optional[str]:
        """Implementation of TikTok video download"""
        try:
            # Make API request with timeout
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
            
            # Create unique filename
            unique_filename = self.temp_dir / f'tiktok_{uuid.uuid4()}.mp4'
            
            # Download with size limit and timeout
            video_response = requests.get(
                video_url, 
                timeout=120,  # 2 minute timeout
                stream=True   # Stream to check size during download
            )
            video_response.raise_for_status()
            
            # Check content length if available
            content_length = video_response.headers.get('content-length')
            if content_length and int(content_length) > self.max_download_size:
                raise Exception(f"Video too large: {int(content_length) // 1024 // 1024}MB (max: {self.max_download_size // 1024 // 1024}MB)")
            
            # Download with size checking
            downloaded_size = 0
            with open(unique_filename, 'wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    if chunk:
                        downloaded_size += len(chunk)
                        
                        # Check size limit during download
                        if downloaded_size > self.max_download_size:
                            raise Exception(f"Download exceeded size limit: {downloaded_size // 1024 // 1024}MB")
                        
                        f.write(chunk)
            
            logger.info(f"TikTok video downloaded: {downloaded_size // 1024 // 1024}MB")
            
            # Check and compress if needed
            return await self._process_video_file(str(unique_filename))
        
        except requests.exceptions.Timeout:
            raise Exception("Download timeout - video may be too large")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error during download: {e}")
        except Exception as e:
            logger.error(f"TikTok download error: {e}")
            raise e
    
    async def _download_twitter_video(self, url: str) -> Optional[str]:
        """Download Twitter video using yt-dlp with improved error handling"""
        return await self._safe_download_with_cleanup(self._download_twitter_video_impl, url)
    
    async def _download_twitter_video_impl(self, url: str) -> Optional[str]:
        """Implementation of Twitter video download"""
        try:
            # Convert x.com to twitter.com for better compatibility
            if 'x.com' in url:
                url = url.replace('x.com', 'twitter.com')
            
            # Create unique filename
            unique_filename = self.temp_dir / f'twitter_{uuid.uuid4()}.mp4'
            
            # Configure yt-dlp options with size limits
            ytdl_opts = self.ytdl_opts.copy()
            ytdl_opts['outtmpl'] = str(unique_filename)
            
            # Download using yt-dlp with timeout
            with yt_dlp.YoutubeDL(ytdl_opts) as ydl:
                # Extract info first to check if video exists and size
                try:
                    info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: ydl.extract_info(url, download=False)
                        ),
                        timeout=30  # 30 second timeout for info extraction
                    )
                except asyncio.TimeoutError:
                    raise Exception("Timeout while checking video information")
                
                if not info:
                    return None
                
                # Check filesize if available
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > self.max_download_size:
                    raise Exception(f"Video too large: {filesize // 1024 // 1024}MB (max: {self.max_download_size // 1024 // 1024}MB)")
                
                # Download the video with timeout
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: ydl.download([url])
                        ),
                        timeout=300  # 5 minute timeout for download
                    )
                except asyncio.TimeoutError:
                    raise Exception("Download timeout - video may be too large")
            
            # Check if file was created
            if not unique_filename.exists():
                return None
            
            # Verify downloaded file size
            actual_size = unique_filename.stat().st_size
            logger.info(f"Twitter video downloaded: {actual_size // 1024 // 1024}MB")
            
            # Process the video file
            return await self._process_video_file(str(unique_filename))
        
        except yt_dlp.utils.DownloadError as e:
            if "Unsupported URL" in str(e) or "No video" in str(e):
                logger.info(f"No video found in Twitter URL: {url}")
                return None
            elif "File is larger than max-filesize" in str(e):
                raise Exception("Video exceeds maximum download size limit")
            logger.error(f"Twitter download error: {e}")
            raise e
        
        except Exception as e:
            logger.error(f"Twitter download error: {e}")
            raise e
    
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
        file_size = 0  # Initialize to prevent unbound variable error
        
        try:
            file_size = os.path.getsize(video_path)
            
            # If file is still too large, try one final aggressive compression
            if file_size > self.max_file_size:
                logger.info(f"Video still too large ({file_size} bytes), attempting final compression")
                final_compressed_path = await self._final_aggressive_compression(video_path)
                
                if final_compressed_path and final_compressed_path != video_path:
                    # Use the newly compressed file
                    if os.path.exists(video_path):
                        os.remove(video_path)  # Clean up original
                    video_path = final_compressed_path
                    file_size = os.path.getsize(video_path)
                
                # If still too large after final compression, we'll try to upload anyway
                # Discord might still accept it, or the user has Nitro
                if file_size > self.max_file_size:
                    logger.warning(f"Video still large after final compression: {file_size} bytes")
            
            # Create Discord file
            discord_file = discord.File(video_path)
            
            # Send the file
            await message.reply(file=discord_file)
            
            logger.info(f"Successfully sent video file: {video_path} ({file_size} bytes)")
        
        except discord.HTTPException as e:
            logger.error(f"Discord upload error: {e}")
            # If upload fails due to size, provide helpful message
            if "Request entity too large" in str(e) or "Payload Too Large" in str(e):
                await message.reply(
                    f"❌ Video is too large to upload even after compression ({file_size // 1024 // 1024}MB).\n"
                    f"Discord's limit is 8MB.\n",
                    delete_after=30
                )
            else:
                await message.reply(
                    f"Failed to upload video: {str(e)}\nOriginal URL: {original_url}",
                    delete_after=30
                )
        
        except Exception as e:
            logger.error(f"Error sending video file: {e}")
            await message.reply(
                f"❌ Error processing video: {str(e)}\nOriginal URL: {original_url}",
                delete_after=30
            )
        
        finally:
            # Clean up the file
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
            except Exception as e:
                logger.error(f"Error cleaning up file {video_path}: {e}")
    
    async def _final_aggressive_compression(self, file_path: str) -> Optional[str]:
        """Final aggressive compression attempt for oversized videos"""
        try:
            compressed_path = str(self.temp_dir / f'final_compressed_{uuid.uuid4()}.mp4')
            
            # Get video information
            try:
                probe = ffmpeg.probe(file_path)
                video_info = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
                if not video_info:
                    logger.error("No video stream found for final compression")
                    return file_path
                
                duration = float(probe['format'].get('duration', 0))
                if duration <= 0:
                    logger.error("Invalid video duration for final compression")
                    return file_path
                
            except ffmpeg.Error as e:
                logger.error(f"Error probing video for final compression: {e}")
                return file_path
            
            # Very aggressive settings to ensure we get under Discord's limit
            # Target 6MB to provide safety margin
            target_size_mb = 6
            target_size_bits = target_size_mb * 1024 * 1024 * 8
            target_bitrate_bps = int(target_size_bits / duration)
            
            # Reserve minimal audio bitrate and apply overhead
            audio_bitrate_bps = 32 * 1000  # Very low audio bitrate
            overhead_factor = 0.85  # More aggressive overhead buffer
            target_video_bitrate = int((target_bitrate_bps - audio_bitrate_bps) * overhead_factor)
            
            # Absolute minimum video bitrate
            target_video_bitrate = max(target_video_bitrate, 100 * 1000)  # 100kbps minimum
            
            logger.info(f"Final aggressive compression: target={target_size_mb}MB, video_bitrate={target_video_bitrate//1000}kbps")
            
            # Very aggressive settings
            stream = ffmpeg.input(file_path)
            # Scale to maximum 360p and apply additional filters for size reduction
            video_stream = stream.video.filter('scale', width=-2, height='min(360,ih)').filter('fps', fps=15)
            audio_stream = stream.audio
            
            output = ffmpeg.output(
                video_stream,
                audio_stream,
                compressed_path,
                vcodec='libx264',
                acodec='aac',
                video_bitrate=target_video_bitrate,
                audio_bitrate='32k',
                preset='veryslow',  # Better compression
                crf=28,  # Higher CRF for more compression
                y=None
            )
            
            # Run compression
            process = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"Final compression failed: {stderr.decode()}")
                return file_path
            
            if os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                logger.info(f"Final compression result: {compressed_size} bytes")
                return compressed_path
            
            return file_path
        
        except Exception as e:
            logger.error(f"Final compression error: {e}")
            return file_path
    
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
                
                # Provide specific error messages for manual commands
                if "too large" in str(e).lower():
                    await ctx.send(f"❌ Video is too large to process (max: {self.max_download_size // 1024 // 1024}MB)")
                elif "disk space" in str(e).lower() or "no space left" in str(e).lower():
                    await ctx.send("⚠️ Insufficient storage space. Please try again later.")
                elif "timeout" in str(e).lower():
                    await ctx.send("⏱️ Download timed out. The video may be too large or slow to download.")
                elif "network" in str(e).lower():
                    await ctx.send("🌐 Network error. Please check the URL and try again.")
                else:
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
    
    @commands.command(name='media-cleanup')
    @commands.has_permissions(administrator=True)
    async def manual_cleanup(self, ctx):
        """Manually clean up temporary media files (Admin only)"""
        try:
            # Get current file count and sizes
            files_before = list(self.temp_dir.glob('*'))
            total_size_before = sum(f.stat().st_size for f in files_before if f.is_file())
            
            await self._cleanup_old_files()
            
            # Get file count after cleanup
            files_after = list(self.temp_dir.glob('*'))
            total_size_after = sum(f.stat().st_size for f in files_after if f.is_file())
            
            files_removed = len(files_before) - len(files_after)
            space_freed = total_size_before - total_size_after
            
            embed = discord.Embed(
                title="🧹 Media Cleanup Complete",
                color=0x00FF00
            )
            embed.add_field(name="Files Removed", value=str(files_removed), inline=True)
            embed.add_field(name="Space Freed", value=f"{space_freed // 1024 // 1024}MB", inline=True)
            embed.add_field(name="Remaining Files", value=str(len(files_after)), inline=True)
            
            # Check available disk space
            import shutil
            free_space = shutil.disk_usage(self.temp_dir).free
            embed.add_field(name="Available Space", value=f"{free_space // 1024 // 1024 // 1024}GB", inline=True)
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Manual cleanup error: {e}")
            await ctx.send(f"❌ Cleanup failed: {e}")
    
    @commands.command(name='media-status')
    @commands.has_permissions(administrator=True)
    async def media_status(self, ctx):
        """Show media handler status and disk usage (Admin only)"""
        try:
            # Get temp directory info
            temp_files = list(self.temp_dir.glob('*'))
            temp_file_count = len([f for f in temp_files if f.is_file()])
            temp_total_size = sum(f.stat().st_size for f in temp_files if f.is_file())
            
            # Get disk space info
            import shutil
            disk_usage = shutil.disk_usage(self.temp_dir)
            
            embed = discord.Embed(
                title="📊 Media Handler Status",
                color=0x3498db
            )
            
            embed.add_field(
                name="Temporary Files", 
                value=f"{temp_file_count} files\n{temp_total_size // 1024 // 1024}MB total", 
                inline=True
            )
            
            embed.add_field(
                name="Disk Usage", 
                value=f"Free: {disk_usage.free // 1024 // 1024 // 1024}GB\n"
                      f"Used: {disk_usage.used // 1024 // 1024 // 1024}GB\n"
                      f"Total: {disk_usage.total // 1024 // 1024 // 1024}GB", 
                inline=True
            )
            
            embed.add_field(
                name="Settings",
                value=f"Max Download: {self.max_download_size // 1024 // 1024}MB\n"
                      f"Target Size: {self.target_file_size // 1024 // 1024}MB\n"
                      f"Min Free Space: {self.min_free_space // 1024 // 1024 // 1024}GB",
                inline=True
            )
            
            # Add warning if disk space is low
            if disk_usage.free < self.min_free_space:
                embed.add_field(
                    name="⚠️ Warning",
                    value="Disk space is below minimum threshold!",
                    inline=False
                )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Media status error: {e}")
            await ctx.send(f"❌ Status check failed: {e}")
    
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
