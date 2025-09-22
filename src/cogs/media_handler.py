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
from yt_dlp.utils import DownloadError
import uuid
import tempfile
import subprocess
import logging
import ffmpeg
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class MediaHandler(commands.Cog, name="Media"):
    """Handles media conversion from Twitter and TikTok links"""
    _status_messages: Dict[int, List[discord.Message]]
    
    def __init__(self, bot):
        self.bot = bot
        self.rapidapi_key = os.getenv('RAPIDAPI_KEY')

        # Create temp directory for media files
        self.temp_dir = Path(tempfile.gettempdir()) / 'discord_bot_media'
        self.temp_dir.mkdir(exist_ok=True)

        # Discord file size limit (10MB for standard uploads; higher for Nitro)
        # Target 8MB to provide a safety buffer and ensure reliable uploads
        self.max_file_size = 10 * 1024 * 1024  # 10MB in bytes
        self.target_file_size = 8 * 1024 * 1024  # 8MB in bytes (compression target)

        # Download limits to prevent issues
        self.max_download_size = 500 * 1024 * 1024  # 500MB max download

        # TikTok API configuration
        self.tiktok_api_url = "https://tiktok-download-without-watermark.p.rapidapi.com/analysis"
        self.tiktok_headers = ({
            "x-rapidapi-host": "tiktok-download-without-watermark.p.rapidapi.com",
            "x-rapidapi-key": self.rapidapi_key
        } if self.rapidapi_key else None)

        # yt-dlp options with size limits
        self.ytdl_opts = {
            'format': 'best[ext=mp4]/best[height<=720]/best',  # More flexible format selection
            'outtmpl': str(self.temp_dir / '%(extractor)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'max_filesize': self.max_download_size,
        }

        # Cache for status messages to clean up after posting
        self._status_messages = {}

        # Startup cleanup tasks
        asyncio.create_task(self._cleanup_old_files())
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
                    video_path = await self._download_tiktok_video(url, status_target=message)
                
                # Check for Twitter/X URLs
                elif self._is_twitter_url(url):
                    video_path = await self._download_twitter_video(url, status_target=message)
                
                # Send the video if successfully downloaded
                if video_path and os.path.exists(video_path):
                    await self._send_video_file(message, video_path, url)
            
            except Exception as e:
                logger.error(f"Error processing URL {url}: {e}")
                
                # Send user-friendly error messages for specific cases
                if "too large" in str(e).lower():
                    await message.reply(
                        f"❌ Video is too large to process (max: {self.max_download_size // 1024 // 1024}MB)",
                        delete_after=5
                    )
                elif "timeout" in str(e).lower():
                    await message.reply(
                        f"⏱️ Video download timed out. The video may be too large or the connection too slow.",
                        delete_after=5
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
    
    async def _safe_download_with_cleanup(self, download_func, *args, **kwargs):
        """Wrapper for downloads with automatic cleanup on failure"""
        temp_files = []
        
        try:
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
    
    async def _download_tiktok_video(self, url: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Download TikTok video using RapidAPI with improved error handling"""
        if not self.rapidapi_key:
            logger.warning("No RapidAPI key provided for TikTok downloads")
            return None

        return await self._safe_download_with_cleanup(self._download_tiktok_video_impl, url, status_target=status_target)
    
    async def _download_tiktok_video_impl(self, url: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
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
            return await self._process_video_file(str(unique_filename), status_target=status_target)
        
        except requests.exceptions.Timeout:
            raise Exception("Download timeout - video may be too large")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error during download: {e}")
        except Exception as e:
            logger.error(f"TikTok download error: {e}")
            raise e
    
    async def _download_twitter_video(self, url: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Download Twitter video using yt-dlp with improved error handling"""
        return await self._safe_download_with_cleanup(self._download_twitter_video_impl, url, status_target=status_target)
    
    async def _download_twitter_video_impl(self, url: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Implementation of Twitter video download"""
        try:
            # Convert x.com to twitter.com for better compatibility
            if 'x.com' in url:
                url = url.replace('x.com', 'twitter.com')
            
            # Create unique filename - use simpler template
            filename_base = f'twitter_{uuid.uuid4().hex[:8]}'
            unique_filename = self.temp_dir / f'{filename_base}.%(ext)s'
            
            # Configure yt-dlp options with more flexible format selection
            ytdl_opts: Dict[str, Any] = {
                'format': 'best[ext=mp4]/best[height<=720]/best',  # Flexible format selection
                'outtmpl': str(unique_filename),
                'quiet': True,
                'no_warnings': True,
                'max_filesize': self.max_download_size,
                'extract_flat': False,
                'writeinfojson': False,
                'writesubtitles': False,
                'writeautomaticsub': False,
            }
            
            # Download using yt-dlp with timeout
            with yt_dlp.YoutubeDL(ytdl_opts) as ydl:  # type: ignore[arg-type]
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
                
                # Check if there are any formats available
                formats = info.get('formats', [])
                if not formats:
                    logger.warning(f"No video formats found for URL: {url}")
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
            
            # Find the downloaded file (yt-dlp might change the extension)
            # Use a more flexible pattern to find any files created during download
            base_pattern = filename_base  # Use the simplified base name
            downloaded_files = list(self.temp_dir.glob(f'{base_pattern}*'))
            
            # If no files found with our pattern, try broader search for recent files
            if not downloaded_files:
                logger.debug(f"No files found with pattern '{base_pattern}*', checking all recent files")
                import time
                current_time = time.time()
                # Look for any files created in the last 30 seconds
                recent_files = [
                    f for f in self.temp_dir.glob('*') 
                    if f.is_file() and (current_time - f.stat().st_mtime) < 30
                ]
                logger.debug(f"Recent files in temp dir: {[f.name for f in recent_files]}")
                
                # Filter for video files
                video_extensions = ['.mp4', '.webm', '.mkv', '.avi', '.mov']
                downloaded_files = [
                    f for f in recent_files 
                    if any(f.name.lower().endswith(ext) for ext in video_extensions)
                ]
            
            if not downloaded_files:
                logger.warning(f"No file found after download for: {url}")
                return None
            
            # Use the first (and should be only) downloaded file
            actual_filename = downloaded_files[0]
            logger.info(f"Found downloaded file: {actual_filename.name}")
            
            # Verify downloaded file size
            actual_size = actual_filename.stat().st_size
            logger.info(f"Twitter video downloaded: {actual_size // 1024 // 1024}MB")
            
            # Process the video file
            return await self._process_video_file(str(actual_filename), status_target=status_target)

        except DownloadError as e:
            if "Unsupported URL" in str(e) or "No video" in str(e):
                logger.info(f"No video found in Twitter URL: {url}")
                return None
            elif "File is larger than max-filesize" in str(e):
                raise Exception("Video exceeds maximum download size limit")
            elif "Requested format is not available" in str(e):
                logger.warning(f"Format not available for Twitter URL: {url}")
                # Try with even more permissive format
                return await self._download_twitter_video_fallback(url, status_target=status_target)
            logger.error(f"Twitter download error: {e}")
            raise e

        except Exception as e:
            logger.error(f"Twitter download error: {e}")
            raise e
    
    async def _download_twitter_video_fallback(self, url: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Fallback Twitter download with most permissive settings"""
        try:
            logger.info(f"Attempting fallback download for Twitter URL: {url}")
            
            # Create unique filename - use simpler template
            filename_base = f'twitter_fallback_{uuid.uuid4().hex[:8]}'
            unique_filename = self.temp_dir / f'{filename_base}.%(ext)s'
            
            # Most permissive yt-dlp options
            ytdl_opts: Dict[str, Any] = {
                'format': 'worst/best',  # Accept any available format
                'outtmpl': str(unique_filename),
                'quiet': False,  # Enable output for debugging
                'no_warnings': False,
                'max_filesize': self.max_download_size,
                'extract_flat': False,
                'writeinfojson': False,
                'writesubtitles': False,
                'writeautomaticsub': False,
                'ignoreerrors': False,
            }
            
            # Download using yt-dlp with timeout
            with yt_dlp.YoutubeDL(ytdl_opts) as ydl:  # type: ignore[arg-type]
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: ydl.download([url])
                        ),
                        timeout=300  # 5 minute timeout for download
                    )
                except asyncio.TimeoutError:
                    raise Exception("Fallback download timeout")
            
            # Find the downloaded file with improved detection
            base_pattern = filename_base  # Use the simplified base name
            downloaded_files = list(self.temp_dir.glob(f'{base_pattern}*'))
            
            # If no files found with our pattern, try broader search for recent files
            if not downloaded_files:
                logger.debug(f"Fallback: No files found with pattern '{base_pattern}*', checking all recent files")
                import time
                current_time = time.time()
                # Look for any files created in the last 30 seconds
                recent_files = [
                    f for f in self.temp_dir.glob('*') 
                    if f.is_file() and (current_time - f.stat().st_mtime) < 30
                ]
                logger.debug(f"Fallback recent files: {[f.name for f in recent_files]}")
                
                # Filter for video files
                video_extensions = ['.mp4', '.webm', '.mkv', '.avi', '.mov']
                downloaded_files = [
                    f for f in recent_files 
                    if any(f.name.lower().endswith(ext) for ext in video_extensions)
                ]
            
            if not downloaded_files:
                logger.warning(f"No file found after fallback download for: {url}")
                return None
            
            # Use the first downloaded file
            actual_filename = downloaded_files[0]
            logger.info(f"Fallback found downloaded file: {actual_filename.name}")
            
            # Verify file exists and has content
            if not actual_filename.exists() or actual_filename.stat().st_size == 0:
                logger.warning(f"Downloaded file is empty or doesn't exist: {actual_filename}")
                return None
            
            actual_size = actual_filename.stat().st_size
            logger.info(f"Twitter fallback video downloaded: {actual_size // 1024 // 1024}MB")
            
            # Process the video file
            return await self._process_video_file(str(actual_filename), status_target=status_target)
            
        except Exception as e:
            logger.error(f"Twitter fallback download error: {e}")
            return None
    
    async def _process_video_file(self, file_path: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Process video file - compress if too large"""
        if not os.path.exists(file_path):
            return None
        
        # Check file size
        file_size = os.path.getsize(file_path)
        
        if file_size <= self.target_file_size:
            return file_path
        
        # File is too large, try to compress
        logger.info(f"File too large ({file_size} bytes), attempting compression")
        # Inform the user we're compressing if we have a message context
        if status_target:
            try:
                notice = await status_target.reply("🗜️ Compressing video to fit under the upload limit… this may take a minute.")
                self._status_messages.setdefault(status_target.id, []).append(notice)
            except Exception as notify_err:
                logger.debug(f"Could not send compression notice: {notify_err}")
        return await self._compress_video(file_path)
    
    async def _compress_video(self, file_path: str) -> Optional[str]:
        """Compress video to meet Discord's file size limit using H.265 and Opus"""
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
            
            # Reserve some bitrate for audio - Opus is more efficient than AAC
            audio_bitrate_bps = 48 * 1000  # 48kbps Opus (equivalent to 64kbps AAC)
            overhead_factor = 0.9  # 10% overhead buffer
            target_video_bitrate = int((target_bitrate_bps - audio_bitrate_bps) * overhead_factor)
            
            # Ensure minimum quality - H.265 can go lower than H.264
            target_video_bitrate = max(target_video_bitrate, 150 * 1000)  # 150kbps minimum for H.265
            
            logger.info(f"Compressing video with H.265: duration={duration:.2f}s, target_video_bitrate={target_video_bitrate//1000}kbps")
            
            # Try H.265 + Opus first (best compression)
            success = await self._try_compression(
                file_path, compressed_path, target_video_bitrate, 
                vcodec='libx265', acodec='libopus', preset='ultrafast'
            )
            
            if not success:
                # Fallback to H.264 + Opus if H.265 fails
                logger.info("H.265 failed, trying H.264 + Opus")
                success = await self._try_compression(
                    file_path, compressed_path, target_video_bitrate,
                    vcodec='libx264', acodec='libopus', preset='ultrafast'
                )
            
            if not success:
                # Final fallback to H.264 + AAC
                logger.info("H.264 + Opus failed, trying H.264 + AAC")
                success = await self._try_compression(
                    file_path, compressed_path, target_video_bitrate,
                    vcodec='libx264', acodec='aac', preset='ultrafast'
                )
            
            if success and os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                
                # If still too large, try more aggressive compression
                if compressed_size > self.target_file_size:
                    logger.info(f"First compression attempt: {compressed_size} bytes, trying more aggressive compression")
                    return await self._try_aggressive_compression(file_path, target_video_bitrate)
                
                if compressed_size <= self.target_file_size:
                    # Remove original and return compressed
                    os.remove(file_path)
                    logger.info(f"Compression successful: {compressed_size} bytes (target: {self.target_file_size} bytes)")
                    return compressed_path
            
            return file_path  # Return original if compression didn't help
        
        except Exception as e:
            logger.error(f"Video compression error: {e}")
            return file_path  # Return original if compression fails
    
    async def _try_compression(self, input_path: str, output_path: str, video_bitrate: int, 
                             vcodec: str, acodec: str, preset: str) -> bool:
        """Try compression with two-pass encoding for precise file size control"""
        try:
            # Remove existing output file if present
            if os.path.exists(output_path):
                os.remove(output_path)
            
            # Get video duration for bitrate calculation
            try:
                probe = ffmpeg.probe(input_path)
                duration = float(probe['format'].get('duration', 0))
                if duration <= 0:
                    logger.error("Invalid video duration for compression")
                    return False
            except ffmpeg.Error as e:
                logger.error(f"Error getting video duration: {e}")
                return False
            
            # Calculate precise bitrates for target file size
            target_size_bits = self.target_file_size * 8  # Convert to bits
            audio_bitrate_kbps = 48  # 48 kbps for Opus/AAC
            audio_bitrate_bps = audio_bitrate_kbps * 1000
            
            # Reserve 5% overhead for container and metadata
            overhead_factor = 0.95
            available_bitrate = int((target_size_bits / duration) * overhead_factor)
            target_video_bitrate_bps = available_bitrate - audio_bitrate_bps
            
            # Ensure minimum video bitrate
            min_video_bitrate = 80 * 1000 if vcodec == 'libx265' else 150 * 1000
            target_video_bitrate_bps = max(target_video_bitrate_bps, min_video_bitrate)
            
            logger.info(f"Two-pass encoding: duration={duration:.2f}s, target_video_bitrate={target_video_bitrate_bps//1000}kbps")
            
            # Use two-pass encoding for precise file size control
            if vcodec == 'libx265':
                success = await self._two_pass_h265_encode(
                    input_path, output_path, target_video_bitrate_bps, audio_bitrate_kbps, acodec, preset
                )
            else:  # libx264
                success = await self._two_pass_h264_encode(
                    input_path, output_path, target_video_bitrate_bps, audio_bitrate_kbps, acodec, preset
                )
            
            if success:
                # Verify the output file size
                actual_size = os.path.getsize(output_path)
                logger.info(f"Two-pass encoding result: {actual_size} bytes (target: {self.target_file_size} bytes)")
                
                if actual_size <= self.target_file_size * 1.05:  # Allow 5% tolerance
                    logger.info(f"Two-pass compression successful with {vcodec} + {acodec}")
                    return True
                else:
                    logger.warning(f"Two-pass result exceeded target: {actual_size} vs {self.target_file_size}")
                    return False
            
            return False
                
        except Exception as e:
            logger.error(f"Error in two-pass compression with {vcodec} + {acodec}: {e}")
            return False
    
    async def _two_pass_h265_encode(self, input_path: str, output_path: str, 
                                  video_bitrate_bps: int, audio_bitrate_kbps: int, 
                                  acodec: str, preset: str) -> bool:
        """Two-pass H.265 encoding for precise file size control"""
        passlog_file = str(self.temp_dir / f'passlog_{uuid.uuid4()}')
        
        try:
            # PASS 1: Analysis pass
            logger.info("Starting H.265 pass 1 (analysis)")
            
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height='min(720,ih)')
            
            pass1_args = {
                'vcodec': 'libx265',
                'b:v': f'{video_bitrate_bps}',
                'pass': 1,
                'passlogfile': passlog_file,
                'preset': preset,
                'x265-params': f'log-level=error:pass=1',
                'f': 'null',
                'y': None
            }
            
            output1 = ffmpeg.output(video_stream, 'NUL' if os.name == 'nt' else '/dev/null', **pass1_args)
            
            process1 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output1),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout1, stderr1 = await process1.communicate()
            
            if process1.returncode != 0:
                logger.error(f"H.265 pass 1 failed: {stderr1.decode()}")
                return False
            
            logger.info("H.265 pass 1 completed, starting pass 2")
            
            # PASS 2: Final encoding
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height='min(720,ih)')
            audio_stream = stream.audio
            
            pass2_args = {
                'vcodec': 'libx265',
                'acodec': acodec,
                'b:v': f'{video_bitrate_bps}',
                'b:a': f'{audio_bitrate_kbps}k',
                'pass': 2,
                'passlogfile': passlog_file,
                'preset': preset,
                'x265-params': f'log-level=error:pass=2',
                'tag:v': 'hvc1',
                'y': None
            }
            
            output2 = ffmpeg.output(video_stream, audio_stream, output_path, **pass2_args)
            
            process2 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output2),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout2, stderr2 = await process2.communicate()
            
            if process2.returncode == 0:
                logger.info("H.265 two-pass encoding completed successfully")
                return True
            else:
                logger.error(f"H.265 pass 2 failed: {stderr2.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in H.265 two-pass encoding: {e}")
            return False
        finally:
            # Always clean up pass log files
            self._cleanup_passlog_files(passlog_file)
    
    async def _two_pass_h264_encode(self, input_path: str, output_path: str, 
                                  video_bitrate_bps: int, audio_bitrate_kbps: int, 
                                  acodec: str, preset: str) -> bool:
        """Two-pass H.264 encoding for precise file size control"""
        passlog_file = str(self.temp_dir / f'passlog_{uuid.uuid4()}')
        
        try:
            # PASS 1: Analysis pass
            logger.info("Starting H.264 pass 1 (analysis)")
            
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height='min(720,ih)')
            
            pass1_args = {
                'vcodec': 'libx264',
                'b:v': f'{video_bitrate_bps}',
                'pass': 1,
                'passlogfile': passlog_file,
                'preset': preset,
                'f': 'null',
                'y': None
            }
            
            output1 = ffmpeg.output(video_stream, 'NUL' if os.name == 'nt' else '/dev/null', **pass1_args)
            
            process1 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output1),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout1, stderr1 = await process1.communicate()
            
            if process1.returncode != 0:
                logger.error(f"H.264 pass 1 failed: {stderr1.decode()}")
                return False
            
            logger.info("H.264 pass 1 completed, starting pass 2")
            
            # PASS 2: Final encoding
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height='min(720,ih)')
            audio_stream = stream.audio
            
            pass2_args = {
                'vcodec': 'libx264',
                'acodec': acodec,
                'b:v': f'{video_bitrate_bps}',
                'b:a': f'{audio_bitrate_kbps}k',
                'pass': 2,
                'passlogfile': passlog_file,
                'preset': preset,
                'y': None
            }
            
            output2 = ffmpeg.output(video_stream, audio_stream, output_path, **pass2_args)
            
            process2 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output2),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout2, stderr2 = await process2.communicate()
            
            if process2.returncode == 0:
                logger.info("H.264 two-pass encoding completed successfully")
                return True
            else:
                logger.error(f"H.264 pass 2 failed: {stderr2.decode()}")
                return False
                
            return False
        finally:
            # Always clean up pass log files
            self._cleanup_passlog_files(passlog_file)
    
    async def _two_pass_h265_encode_scaled(self, input_path: str, output_path: str, 
                                         video_bitrate_bps: int, audio_bitrate_kbps: int, 
                                         acodec: str, preset: str, max_height: int) -> bool:
        """Two-pass H.265 encoding with scaling for aggressive compression"""
        passlog_file = str(self.temp_dir / f'passlog_{uuid.uuid4()}')
        
        try:
            # PASS 1: Analysis pass
            logger.info(f"Starting H.265 pass 1 (analysis) with {max_height}p scaling")
            
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height=f'min({max_height},ih)')
            
            pass1_args = {
                'vcodec': 'libx265',
                'b:v': f'{video_bitrate_bps}',
                'pass': 1,
                'passlogfile': passlog_file,
                'preset': preset,
                'x265-params': f'log-level=error:pass=1',
                'f': 'null',
                'y': None
            }
            
            output1 = ffmpeg.output(video_stream, 'NUL' if os.name == 'nt' else '/dev/null', **pass1_args)
            
            process1 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output1),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout1, stderr1 = await process1.communicate()
            
            if process1.returncode != 0:
                logger.error(f"H.265 scaled pass 1 failed: {stderr1.decode()}")
                return False
            
            logger.info("H.265 scaled pass 1 completed, starting pass 2")
            
            # PASS 2: Final encoding
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height=f'min({max_height},ih)')
            audio_stream = stream.audio
            
            pass2_args = {
                'vcodec': 'libx265',
                'acodec': acodec,
                'b:v': f'{video_bitrate_bps}',
                'b:a': f'{audio_bitrate_kbps}k',
                'pass': 2,
                'passlogfile': passlog_file,
                'preset': preset,
                'x265-params': f'log-level=error:pass=2',
                'tag:v': 'hvc1',
                'y': None
            }
            
            output2 = ffmpeg.output(video_stream, audio_stream, output_path, **pass2_args)
            
            process2 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output2),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout2, stderr2 = await process2.communicate()
            
            if process2.returncode == 0:
                logger.info(f"H.265 scaled two-pass encoding completed successfully")
                return True
            else:
                logger.error(f"H.265 scaled pass 2 failed: {stderr2.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in H.265 scaled two-pass encoding: {e}")
            return False
        finally:
            # Always clean up pass log files
            self._cleanup_passlog_files(passlog_file)
    
    async def _two_pass_h264_encode_scaled(self, input_path: str, output_path: str, 
                                         video_bitrate_bps: int, audio_bitrate_kbps: int, 
                                         acodec: str, preset: str, max_height: int) -> bool:
        """Two-pass H.264 encoding with scaling for aggressive compression"""
        passlog_file = str(self.temp_dir / f'passlog_{uuid.uuid4()}')
        
        try:
            # PASS 1: Analysis pass
            logger.info(f"Starting H.264 pass 1 (analysis) with {max_height}p scaling")
            
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height=f'min({max_height},ih)')
            
            pass1_args = {
                'vcodec': 'libx264',
                'b:v': f'{video_bitrate_bps}',
                'pass': 1,
                'passlogfile': passlog_file,
                'preset': preset,
                'f': 'null',
                'y': None
            }
            
            output1 = ffmpeg.output(video_stream, 'NUL' if os.name == 'nt' else '/dev/null', **pass1_args)
            
            process1 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output1),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout1, stderr1 = await process1.communicate()
            
            if process1.returncode != 0:
                logger.error(f"H.264 scaled pass 1 failed: {stderr1.decode()}")
                return False
            
            logger.info("H.264 scaled pass 1 completed, starting pass 2")
            
            # PASS 2: Final encoding
            stream = ffmpeg.input(input_path)
            video_stream = stream.video.filter('scale', width=-2, height=f'min({max_height},ih)')
            audio_stream = stream.audio
            
            pass2_args = {
                'vcodec': 'libx264',
                'acodec': acodec,
                'b:v': f'{video_bitrate_bps}',
                'b:a': f'{audio_bitrate_kbps}k',
                'pass': 2,
                'passlogfile': passlog_file,
                'preset': preset,
                'y': None
            }
            
            output2 = ffmpeg.output(video_stream, audio_stream, output_path, **pass2_args)
            
            process2 = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output2),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout2, stderr2 = await process2.communicate()
            
            if process2.returncode == 0:
                logger.info(f"H.264 scaled two-pass encoding completed successfully")
                return True
            else:
                logger.error(f"H.264 scaled pass 2 failed: {stderr2.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in H.264 scaled two-pass encoding: {e}")
            return False
        finally:
            # Always clean up pass log files
            self._cleanup_passlog_files(passlog_file)
    
    def _cleanup_passlog_files(self, passlog_file: str):
        """Clean up pass log files created during two-pass encoding"""
        try:
            # Common pass log file extensions
            extensions = ['.log', '.log.mbtree', '-0.log', '-0.log.mbtree']
            
            for ext in extensions:
                log_file = f"{passlog_file}{ext}"
                if os.path.exists(log_file):
                    os.remove(log_file)
                    logger.debug(f"Cleaned up passlog file: {log_file}")
                    
        except Exception as e:
            logger.warning(f"Error cleaning up passlog files: {e}")
    
    async def _try_aggressive_compression(self, file_path: str, base_bitrate: int) -> Optional[str]:
        """Try more aggressive compression settings with two-pass encoding"""
        try:
            compressed_path = str(self.temp_dir / f'aggressive_{uuid.uuid4()}.mp4')
            
            # Get video duration for bitrate calculation
            try:
                probe = ffmpeg.probe(file_path)
                duration = float(probe['format'].get('duration', 0))
                if duration <= 0:
                    logger.error("Invalid video duration for aggressive compression")
                    return file_path
            except ffmpeg.Error as e:
                logger.error(f"Error getting video duration for aggressive compression: {e}")
                return file_path
            
            # Calculate more aggressive bitrate targeting 5MB
            target_size_mb = 5
            target_size_bits = target_size_mb * 1024 * 1024 * 8
            audio_bitrate_kbps = 32  # Lower audio bitrate for aggressive compression
            audio_bitrate_bps = audio_bitrate_kbps * 1000
            
            # More aggressive overhead factor
            overhead_factor = 0.90
            available_bitrate = int((target_size_bits / duration) * overhead_factor)
            target_video_bitrate_bps = available_bitrate - audio_bitrate_bps
            
            # Lower minimum bitrates for aggressive compression
            min_video_bitrate = 60 * 1000 if base_bitrate < 200000 else 100 * 1000
            target_video_bitrate_bps = max(target_video_bitrate_bps, min_video_bitrate)
            
            logger.info(f"Aggressive two-pass compression: target={target_size_mb}MB, video_bitrate={target_video_bitrate_bps//1000}kbps")
            
            # Try H.265 first for aggressive compression (scale to 480p max)
            success = await self._two_pass_h265_encode_scaled(
                file_path, compressed_path, target_video_bitrate_bps, audio_bitrate_kbps, 'libopus', 'ultrafast', 480
            )
            
            if not success:
                # Fallback to H.264
                success = await self._two_pass_h264_encode_scaled(
                    file_path, compressed_path, target_video_bitrate_bps, audio_bitrate_kbps, 'libopus', 'ultrafast', 480
                )
            
            if success and os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                if compressed_size <= self.target_file_size:
                    os.remove(file_path)
                    logger.info(f"Aggressive two-pass compression successful: {compressed_size} bytes")
                    return compressed_path
                else:
                    os.remove(compressed_path)
                    logger.warning(f"Aggressive compression still too large: {compressed_size} bytes")
            
            return file_path
            
        except Exception as e:
            logger.error(f"Aggressive compression error: {e}")
            return file_path
    
    async def _send_video_file(self, message: discord.Message, video_path: str, original_url: str):
        """Send the video file to Discord"""
        file_size = 0  # Initialize to prevent unbound variable error
        
        try:
            file_size = os.path.getsize(video_path)
            
            # If file is still too large, try one final aggressive compression
            if file_size > self.max_file_size:
                logger.info(f"Video still too large ({file_size} bytes), attempting final compression")
                final_compressed_path = await self._final_aggressive_compression(video_path, status_target=message)
                
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
            sent_message = await message.reply(file=discord_file)
            
            # Clean up any compression status messages linked to this message
            try:
                pending = self._status_messages.pop(message.id, [])
                for m in pending:
                    try:
                        await m.delete()
                    except Exception as del_err:
                        logger.debug(f"Could not delete status message: {del_err}")
            except Exception as cleanup_err:
                logger.debug(f"Status message cleanup failed: {cleanup_err}")
            
            logger.info(f"Successfully sent video file: {video_path} ({file_size} bytes)")
        
        except discord.HTTPException as e:
            logger.error(f"Discord upload error: {e}")
            # If upload fails due to size, provide helpful message
            if "Request entity too large" in str(e) or "Payload Too Large" in str(e):
                limit_mb = self.max_file_size // 1024 // 1024
                await message.reply(
                    f"❌ Video is too large to upload even after compression ({file_size // 1024 // 1024}MB).\n"
                    f"Limit is {limit_mb}MB (try a shorter clip).\n",
                    delete_after=5
                )
            else:
                await message.reply(
                    f"Failed to upload video: {str(e)}",
                    delete_after=5
                )
        
        except Exception as e:
            logger.error(f"Error sending video file: {e}")
            await message.reply(
                f"❌ Error processing video: {str(e)}",
                delete_after=5
            )
        
        finally:
            # Clean up the file
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
            except Exception as e:
                logger.error(f"Error cleaning up file {video_path}: {e}")
    
    async def _final_aggressive_compression(self, file_path: str, status_target: Optional[discord.Message] = None) -> Optional[str]:
        """Final aggressive compression attempt using H.265 with maximum settings"""
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
            
            # Reserve minimal audio bitrate and apply overhead - Opus is very efficient
            audio_bitrate_bps = 24 * 1000  # Very low Opus bitrate (equivalent to 32kbps AAC)
            overhead_factor = 0.85  # More aggressive overhead buffer
            target_video_bitrate = int((target_bitrate_bps - audio_bitrate_bps) * overhead_factor)
            
            # Absolute minimum video bitrate for H.265
            target_video_bitrate = max(target_video_bitrate, 80 * 1000)  # 80kbps minimum for H.265
            
            logger.info(f"Final aggressive H.265 compression: target={target_size_mb}MB, video_bitrate={target_video_bitrate//1000}kbps")
            if status_target:
                try:
                    final_notice = await status_target.reply("🔧 Performing a final pass to shrink the video further…")
                    self._status_messages.setdefault(status_target.id, []).append(final_notice)
                except Exception as notify_err:
                    logger.debug(f"Could not send final compression notice: {notify_err}")
            
            # Very aggressive H.265 settings for maximum compression
            stream = ffmpeg.input(file_path)
            # Scale to maximum 360p and apply additional filters for size reduction
            video_stream = stream.video.filter('scale', width=-2, height='min(360,ih)').filter('fps', fps=15)
            audio_stream = stream.audio
            
            # Try H.265 + Opus first (best compression)
            success = await self._try_final_compression(
                stream, compressed_path, target_video_bitrate,
                vcodec='libx265', acodec='libopus'
            )
            
            if not success:
                # Fallback to H.264 + Opus
                logger.info("Final H.265 failed, trying H.264 + Opus")
                success = await self._try_final_compression(
                    stream, compressed_path, target_video_bitrate,
                    vcodec='libx264', acodec='libopus'
                )
            
            if not success:
                # Final fallback to H.264 + AAC
                logger.info("Final H.264 + Opus failed, trying H.264 + AAC")
                success = await self._try_final_compression(
                    stream, compressed_path, target_video_bitrate,
                    vcodec='libx264', acodec='aac'
                )
            
            if success and os.path.exists(compressed_path):
                compressed_size = os.path.getsize(compressed_path)
                logger.info(f"Final compression result: {compressed_size} bytes")
                return compressed_path
            
            return file_path
        
        except Exception as e:
            logger.error(f"Final compression error: {e}")
            return file_path
    
    async def _try_final_compression(self, input_stream, output_path: str, video_bitrate: int,
                                   vcodec: str, acodec: str) -> bool:
        """Try final compression with specific codec settings"""
        try:
            # Remove existing output file if present
            if os.path.exists(output_path):
                os.remove(output_path)
            
            video_stream = input_stream.video.filter('scale', width=-2, height='min(360,ih)').filter('fps', fps=15)
            audio_stream = input_stream.audio
            
            # Build output with specific codecs and aggressive settings
            output_args = {
                'vcodec': vcodec,
                'acodec': acodec,
                'video_bitrate': video_bitrate,
                'audio_bitrate': '24k',
                'y': None
            }
            
            # Add codec-specific parameters for maximum compression
            if vcodec == 'libx265':
                output_args['preset'] = 'ultrafast'  # Speed over quality
                output_args['crf'] = 32  # Higher CRF for more compression
                output_args['x265_params'] = 'log-level=error:no-scenecut:keyint=30'
                output_args['tag'] = 'hvc1'
            else:  # libx264
                output_args['preset'] = 'ultrafast'
                output_args['crf'] = 30  # High CRF for H.264
            
            output = ffmpeg.output(video_stream, audio_stream, output_path, **output_args)
            
            # Run compression
            process = await asyncio.create_subprocess_exec(
                *ffmpeg.compile(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                logger.info(f"Final compression successful with {vcodec} + {acodec}")
                return True
            else:
                logger.warning(f"Final compression failed with {vcodec} + {acodec}: {stderr.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in final compression with {vcodec} + {acodec}: {e}")
            return False
    
    @commands.command(name='convert')
    async def manual_convert(self, ctx, url: str):
        """Manually convert a Twitter or TikTok URL to MP4
        
        Usage: !convert <URL>
        """
        async with ctx.typing():
            try:
                video_path = None
                
                if self._is_tiktok_url(url):
                    video_path = await self._download_tiktok_video(url, status_target=ctx.message)
                elif self._is_twitter_url(url):
                    video_path = await self._download_twitter_video(url, status_target=ctx.message)
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
                      f"Target Size: {self.target_file_size // 1024 // 1024}MB",
                inline=True
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
