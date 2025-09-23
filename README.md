# Discord Music Bot with Media Conversion

A feature-rich Discord bot that can play music from YouTube and automatically convert Twitter/TikTok videos to MP4 files.

## Features

### 🎵 Music Features
- Play music from YouTube, playlists, and search queries
- Queue management with up to 20 songs
- Skip, pause, resume, stop functionality
- Volume control (0-100%)
- Shuffle and queue manipulation
- Rich embeds with song information
- Auto-disconnect after inactivity
- Playlist processing with progress tracking

### 📱 Media Conversion Features
- Automatic Twitter/X video conversion to MP4
- TikTok video download and conversion
- Smart video compression for Discord's 10MB limit
- Manual conversion command
- Support for mobile-friendly formats

### 🛠️ Technical Features
- Modern Discord.py implementation
- Robust error handling and logging
- Cog-based architecture for easy maintenance
- Environment variable configuration
- Automatic cleanup of temporary files

## Prerequisites

### Required Software
1. **Python 3.8+**
2. **FFmpeg** - Required for audio processing
   - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html)
   - Add FFmpeg to your system PATH
3. **Git** (optional, for cloning)

### Python Dependencies
All Python dependencies are listed in `requirements.txt` and will be installed automatically.

## Installation

### 1. Clone or Download
```bash
git clone https://github.com/MalcolmStran/discord-music-bot.git
cd discordbot
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Setup Environment Variables
Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:
```env
# Required
DISCORD_TOKEN=your_discord_token_here

# Optional - for TikTok support
RAPIDAPI_KEY=your_rapidapi_key_here

# Voice Connection Settings
VOICE_CONNECTION_TIMEOUT=35
VOICE_RECONNECT_ATTEMPTS=3
VOICE_RETRY_DELAY=2
VOICE_AUTO_DISCONNECT_TIMEOUT=300

# Music Settings
MAX_QUEUE_SIZE=20
MAX_SONG_DURATION=7200
DEFAULT_VOLUME=0.5

# Bot Settings
COMMAND_PREFIX=!
DOWNLOAD_DIR=./downloads

# Logging
LOG_LEVEL=INFO
```

### 4. Run the Bot

#### Standard Installation
```bash
python main.py
```

#### Docker Installation (Recommended for Servers)
1. **Using Docker Compose (easiest):**
```bash
# Copy your .env file first
cp .env.example .env
# Edit .env with your settings

# Run with docker-compose
docker-compose up -d
```

2. **Using Docker directly:**
```bash
# Build the image
docker build -t discord-music-bot .

# Run the container
docker run -d --name discord-music-bot \
  --env-file .env \
  -e DOCKER_CONTAINER=true \
  discord-music-bot
```

**Docker Benefits:**
- Consistent environment across different systems
- Automatic container-optimized voice connection settings
- Easier deployment and management
- Built-in FFmpeg and dependencies

## Setup Instructions

### Discord Bot Setup
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to "Bot" section and create a bot
4. Copy the bot token and add it to your `.env` file
5. Enable the following bot permissions:
   - Send Messages
   - Connect
   - Speak
   - Use Voice Activity
   - Attach Files
   - Embed Links
   - Read Message History

### TikTok Support Setup (Optional)
1. Sign up at [RapidAPI](https://rapidapi.com/)
2. Subscribe to the "TikTok Download Without Watermark" API
3. Copy your RapidAPI key to the `.env` file

**Note:** Twitter/X video support works without additional setup using yt-dlp.

## Commands

### Music Commands
- `!play <song/URL>` - Play a song or add to queue
- `!skip` - Skip current song
- `!stop` - Stop playback and clear queue
- `!pause` - Pause current song
- `!resume` - Resume paused song
- `!repeat` - Toggle repeat mode for current song
- `!volume <0-100>` - Set volume
- `!queue [page]` - Show current queue
- `!nowplaying` - Show current song info
- `!clear` - Clear the queue
- `!shuffle` - Randomize queue order
- `!remove <position>` - Remove song from queue
- `!disconnect` - Disconnect from voice channel
- `!reconnect` - Manually reconnect to voice channel
- `!status` - Check voice connection status

### Media Commands
- `!convert <URL>` - Manually convert Twitter/TikTok URL
- `!mediainfo` - Show media handler information

### Admin Commands (Bot Owner Only)
- `!reload <cog>` - Reload a cog
- `!shutdown` - Shutdown the bot

## Usage Examples

### Playing Music
```
!play Never Gonna Give You Up
!play https://www.youtube.com/watch?v=dQw4w9WgXcQ
!play https://www.youtube.com/playlist?list=PLuHdbqhRgWHKUuRs-2-kxmfneCpxJEK66
```

### Media Conversion
Simply paste Twitter or TikTok links in chat:
```
https://twitter.com/user/status/1234567890
https://www.tiktok.com/@user/video/1234567890
```

Or use manual conversion:
```
!convert https://twitter.com/user/status/1234567890
```

## File Structure
```
discordbot/
├── main.py                 # Main bot file
├── requirements.txt        # Python dependencies
├── .env.example           # Environment variables template
├── .gitignore             # Git ignore file
├── README.md              # This file
└── src/
    ├── __init__.py
    ├── cogs/
    │   ├── __init__.py
    │   ├── music.py           # Music functionality
    │   └── media_handler.py   # Media conversion
    └── utils/
        ├── __init__.py
        ├── queue.py           # Queue management
        ├── player.py          # Audio player
        └── ytdl.py           # YouTube-DL wrapper
```

## Configuration

### Environment Variables
- `DISCORD_TOKEN` - Your Discord bot token (required)
- `RAPIDAPI_KEY` - RapidAPI key for TikTok support (optional)
- `VOICE_CONNECTION_TIMEOUT` - How long to wait for voice connection in seconds (default: 35)
- `VOICE_RECONNECT_ATTEMPTS` - Number of retry attempts for voice connections (default: 3)
- `VOICE_RETRY_DELAY` - Initial delay between retries in seconds (default: 2)
- `VOICE_AUTO_DISCONNECT_TIMEOUT` - Auto-disconnect timeout in seconds (default: 300)
- `MAX_QUEUE_SIZE` - Maximum songs in queue (default: 20)
- `MAX_SONG_DURATION` - Maximum song length in seconds (default: 7200)
- `DEFAULT_VOLUME` - Default volume level 0.0-1.0 (default: 0.5)
- `COMMAND_PREFIX` - Bot command prefix (default: !)
- `DOWNLOAD_DIR` - Directory for temporary downloads (default: ./downloads)
- `LOG_LEVEL` - Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO)

### FFmpeg Requirements
The bot requires FFmpeg for audio processing. Make sure it's installed and available in your system PATH.

## Troubleshooting

### Common Issues

**Bot doesn't respond to commands:**
- Check if the bot has proper permissions in your server
- Verify the bot token in `.env` is correct
- Check if the bot is online in Discord

**Music commands fail:**
- Ensure FFmpeg is installed and in PATH
- Check if the bot has voice permissions
- Verify you're in a voice channel

**Voice connection issues (Error 4006):**
- Use `!reconnect` command to manually reconnect
- Check `!status` for current voice connection state
- The bot now has improved retry logic for unstable connections
- If issues persist, try `!disconnect` and `!play` again
- Ensure the bot has "Connect" and "Speak" permissions in the voice channel

**Docker/Container specific issues:**
- The bot automatically detects container environments and adjusts settings
- Use `!force-reconnect` for enhanced container reconnection logic
- Error 4006 is more common in containers due to networking constraints
- Consider restarting the container if voice issues persist
- Ensure `DOCKER_CONTAINER=true` is set in your environment variables

**Media conversion doesn't work:**
- Check internet connection
- For TikTok: Verify RapidAPI key is correct
- Check bot logs for specific error messages

**File size errors:**
- Videos are automatically compressed to fit Discord's 10MB limit
- Very large videos may still fail to upload

### New Voice Connection Features
- **Automatic retry logic**: The bot will attempt to reconnect multiple times with exponential backoff
- **Connection validation**: The bot checks connection health before playing songs
- **Manual reconnection**: Use `!reconnect` to force a new connection
- **Status monitoring**: Use `!status` to check voice connection health
- **Better error handling**: More informative error messages for connection issues

### Configuration Options
All voice connection and bot behavior can be customized in your `.env` file. The available options are:
- `VOICE_CONNECTION_TIMEOUT`: How long to wait for voice connection (default: 35 seconds)
- `VOICE_RECONNECT_ATTEMPTS`: Number of retry attempts (default: 3)
- `VOICE_RETRY_DELAY`: Initial delay between retries (default: 2 seconds)
- `VOICE_AUTO_DISCONNECT_TIMEOUT`: Auto-disconnect timeout (default: 300 seconds)
- `DEFAULT_VOLUME`: Default playback volume 0.0-1.0 (default: 0.5)
- `COMMAND_PREFIX`: Bot command prefix (default: !)
- `LOG_LEVEL`: Logging verbosity level (default: INFO)

### Logging
The bot logs to both console and `bot.log` file. Check the logs for detailed error information.

## Development

### Adding New Features
1. Create new cogs in `src/cogs/`
2. Add utility functions to `src/utils/`
3. Update `main.py` to load new cogs
4. Test thoroughly before deployment

### Code Structure
- **Cogs**: Feature modules (music, media handling)
- **Utils**: Shared utility classes and functions
- **Main**: Bot initialization and global error handling

## Support

For issues and questions:
1. Check the troubleshooting section
2. Review bot logs for error messages
3. Ensure all prerequisites are installed
4. Verify configuration in `.env` file

## License

This project is open source. Feel free to modify and distribute according to your needs.

## Credits

### Core Technologies
- **[discord.py](https://discordpy.readthedocs.io/)** - Python library for Discord API integration
- **[Python](https://www.python.org/)** - Programming language and runtime environment
- **[FFmpeg](https://ffmpeg.org/)** - Multimedia framework for audio/video processing

### Media Processing Libraries
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** - YouTube and media downloader (fork of youtube-dl)
- **[PyNaCl](https://github.com/pyca/pynacl)** - Cryptographic library for Discord voice encryption
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** - Environment variable management

### External APIs
- **[RapidAPI](https://rapidapi.com/)** - TikTok Download Without Watermark API
- **[Discord API](https://discord.com/developers/docs)** - Bot integration and voice services

### Development Tools
- **[asyncio](https://docs.python.org/3/library/asyncio.html)** - Asynchronous programming support
- **[logging](https://docs.python.org/3/library/logging.html)** - Python's built-in logging framework
- **[pathlib](https://docs.python.org/3/library/pathlib.html)** - Object-oriented filesystem paths
- **[tempfile](https://docs.python.org/3/library/tempfile.html)** - Temporary file and directory creation

### Special Thanks
- **YouTube-DL project** - Original inspiration for media downloading capabilities
- **Discord.py community** - Documentation, examples, and community support
- **FFmpeg developers** - Robust multimedia processing foundation
