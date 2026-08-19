# Discord Music Bot (v2)

Plays music from YouTube/SoundCloud/anything yt-dlp streams, and automatically turns
Twitter/X and TikTok links into uploaded MP4s (compressed to fit the server's upload limit).

v2 (2026-08-19) is a full rewrite of the v1 code (tag `v1-legacy`): slash commands, a
per-guild playback loop instead of recursive callbacks, instant playlist queueing, and
one parameterised encoder instead of 1 400 lines of copy-pasted ffmpeg calls.

## Commands
Everything works both as `!cmd` and `/cmd`.

| Music | |
|---|---|
| `/play <query\|url\|playlist>` (`!p`) | search term, video URL or playlist URL |
| `/skip` `/stop` `/pause` `/resume` | |
| `/queue [page]` (`!q`) · `/nowplaying` (`!np`) | |
| `/volume [0-150]` | live, no restart of the track |
| `/loop [off\|one\|all]` · `/shuffle` · `/remove n` · `/move a b` · `/clear` | |
| `/join` · `/leave` · `/status` | |

| Media | |
|---|---|
| *(paste a Twitter/X or TikTok link)* | auto-converted; original embed suppressed when possible |
| `/convert <url>` | manual |
| `/mediainfo` | status, limits, this server's upload cap |
| `/media-toggle` *(Manage Server)* | per-server on/off, persisted |
| `/media-cleanup` *(Manage Server)* | wipe temp files |

Owner-only (`OWNER_IDS`): `!sync`, `!reload <music|media>`.

## Run
```bash
cp .env.example .env   # set DISCORD_TOKEN (and OWNER_IDS)
docker compose up -d --build
docker logs -f discord-music-bot
```
The container runs as an unprivileged user, refreshes `yt-dlp` on every start
(`YTDLP_AUTO_UPDATE=false` to disable), and keeps guild settings in the `bot-downloads`
volume (`bot_settings/guild_settings.json`, v1 format migrated automatically).

Bare metal: Python 3.11+, ffmpeg, a JS runtime for yt-dlp (node/deno), then
`pip install -r requirements.txt && python -m bot`.

## Layout
```
bot/
  __main__.py      bot class, logging, help, slash sync, graceful shutdown
  config.py        env → Config dataclass
  cogs/music.py    hybrid music commands, auto-leave when alone
  cogs/media.py    link detection, /convert, /media-*
  core/player.py   GuildPlayer: voice, queue, playback loop, loop modes, idle disconnect
  core/queue.py    TrackQueue
  core/ytdl.py     resolve (flat playlists), lazy stream URLs, audio source
  core/video.py    download (yt-dlp + optional RapidAPI TikTok fallback), probe, fit_under (2-pass ladder)
  core/settings.py per-guild JSON settings
tests/test_core.py offline unit tests (run inside the image: pip install pytest; python -m pytest -q)
```

## Design notes
* **Playback loop per guild** (`GuildPlayer._player_loop`): wait for track → fetch stream → play → await finish. Skip/stop just stop the voice client. No recursion, no `run_coroutine_threadsafe` chains.
* **Voice**: minimal `channel.connect(reconnect=True)`; discord.py handles resumes. Retry loops around it caused the 4006/4017 errors in the past — don't add them back.
* **Playlists** resolve flat (one yt-dlp call, ~1 s for 100 items); stream URLs are fetched right before each track plays.
* **Compression ladder** (`core/video.py`): x264 veryfast source-res → x264 480p → x265 ultrafast 480p. Two-pass, target = 97 % of `guild.filesize_limit`. Measured on rock5: a 3.4-min 720p clip 17.5 MB → 7.7 MB in ~75 s. Encodes are bounded by `MAX_CONCURRENT_ENCODES` (default 2) — never unbounded.
* **yt-dlp needs a JS runtime** for YouTube; the image ships Node. If YouTube breaks, the start-up self-update usually fixes it; otherwise set `YTDL_COOKIES_FILE`.
