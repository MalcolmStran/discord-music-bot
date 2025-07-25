"""
Configuration settings for the Discord Music Bot
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Voice connection settings
VOICE_CONNECTION_TIMEOUT = int(os.getenv('VOICE_CONNECTION_TIMEOUT', 20))  # Reduced timeout
VOICE_RECONNECT_ATTEMPTS = int(os.getenv('VOICE_RECONNECT_ATTEMPTS', 5))  # Increased attempts
VOICE_RETRY_DELAY = int(os.getenv('VOICE_RETRY_DELAY', 3))  # Increased delay
VOICE_AUTO_DISCONNECT_TIMEOUT = int(os.getenv('VOICE_AUTO_DISCONNECT_TIMEOUT', 300))
VOICE_SESSION_TIMEOUT = int(os.getenv('VOICE_SESSION_TIMEOUT', 45))  # New session timeout

# Music settings
MAX_QUEUE_SIZE = int(os.getenv('MAX_QUEUE_SIZE', 20))
MAX_SONG_DURATION = int(os.getenv('MAX_SONG_DURATION', 7200))  # 2 hours
DEFAULT_VOLUME = float(os.getenv('DEFAULT_VOLUME', 0.5))

# Bot settings
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_PREFIX = os.getenv('COMMAND_PREFIX', '!')
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', './downloads')

# Logging level
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
