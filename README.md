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
- Smart video compression for Discord's 8MB limit
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
DISCORD_TOKEN=your_discord_bot_token_here

# Optional - for TikTok support
RAPIDAPI_KEY=your_rapidapi_key_here

# Optional settings
MAX_QUEUE_SIZE=20
MAX_SONG_DURATION=7200
DOWNLOAD_DIR=./downloads
```

### 4. Run the Bot
```bash
python main.py
```

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
- `!volume <0-100>` - Set volume
- `!queue [page]` - Show current queue
- `!nowplaying` - Show current song info
- `!clear` - Clear the queue
- `!shuffle` - Randomize queue order
- `!remove <position>` - Remove song from queue
- `!disconnect` - Disconnect from voice channel

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
- `MAX_QUEUE_SIZE` - Maximum songs in queue (default: 20)
- `MAX_SONG_DURATION` - Maximum song length in seconds (default: 7200)
- `DOWNLOAD_DIR` - Directory for temporary downloads (default: ./downloads)

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

**Media conversion doesn't work:**
- Check internet connection
- For TikTok: Verify RapidAPI key is correct
- Check bot logs for specific error messages

**File size errors:**
- Videos are automatically compressed to fit Discord's 8MB limit
- Very large videos may still fail to upload

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

- Built with [discord.py](https://discordpy.readthedocs.io/)
- Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) for video downloading
- TikTok support via RapidAPI
- Audio processing with FFmpeg
