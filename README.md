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
| `/play <query\|url\|playlist>` (`!p`) | search term, video URL, playlist URL, or **Spotify** track/album/playlist link |
| `/skip` `/stop` `/pause` `/resume` | |
| `/queue [page]` (`!q`) · `/nowplaying` (`!np`) | |
| `/volume [0-150]` | live, no restart of the track |
| `/loop [off\|one\|all]` · `/shuffle` · `/remove n` · `/move a b` · `/clear` | |
| `/join` · `/leave` · `/status` | |

| Media | |
|---|---|
| *(paste a Twitter/X or TikTok link)* | auto-converted; original embed suppressed when possible |
| *(fxtwitter / vxtwitter / fixupx / fixvx / twittpr / vxtiktok / tnktok link)* | **left alone** — it already embeds its own video, so converting would post the clip twice |
| `/autoconvert [on\|off]` | anyone can opt their **own** posts out of auto-conversion (applies in every server the bot is in); omit the argument to see your current setting |
| *(silent clip)* | sent as a looping GIF instead of an MP4, sized to fit the server's upload cap (`MAX_GIF_SECONDS=0` to disable) |
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
(`YTDLP_AUTO_UPDATE=false` in `.env` to disable), and keeps guild settings in the
`bot-downloads` volume (`bot_settings/guild_settings.json`, v1 format migrated
automatically). A `HEALTHCHECK` watches a heartbeat file the bot touches only while its
gateway connection is live, so a wedged-but-running bot is restarted too.

Bare metal: Python 3.11+, ffmpeg, a JS runtime for yt-dlp (node/deno), then
`pip install -r requirements.txt && python -m bot`.

Every setting is validated at start-up: out-of-range numbers are clamped (with a warning),
an unknown `LOG_LEVEL` falls back to `INFO`, and an empty `COMMAND_PREFIX` is rejected
(it would otherwise match every message).

## Develop
```bash
./check.sh --install      # deps, then lint + the whole offline suite
./check.sh                # lint + tests (160+, no Discord and no network)
./check.sh --docker       # also build the image and run the suite inside it
```
Or run the pieces directly: `ruff check bot tests` and `python -m pytest`.

There is no GitHub Actions workflow — hosted runners bill against the repository owner's
account and Actions is not available here — so `check.sh` is the thing to run before
pushing. The suite is pure-offline and takes under a second.

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
  core/spotify.py  Spotify links → metadata (embed page, or Web API when creds are set)
  core/settings.py per-guild JSON settings
tests/           offline unit tests — queue/settings/links, player loop modes, encoder
                 planning, config parsing, ytdl helpers, Spotify parsing
```

## Design notes
* **Playback loop per guild** (`GuildPlayer._player_loop`): wait for track → fetch stream → play → await finish. Skip/stop just stop the voice client. No recursion, no `run_coroutine_threadsafe` chains.
* **Voice**: minimal `channel.connect(reconnect=True)`; discord.py handles resumes. Retry loops around it caused the 4006/4017 errors in the past — don't add them back.
* **Playlists** resolve flat (one yt-dlp call, ~1 s for 100 items); stream URLs are fetched right before each track plays.
* **Silent clips become GIFs** (`core/video.py`): a clip with no audio track and a duration within `MAX_GIF_SECONDS` is rendered as a looping GIF — Discord autoplays those inline, where a muted MP4 gets a click-to-play card. Two ffmpeg passes (`palettegen` then `paletteuse`, never one `split` filter: generating the palette inline buffers every decoded frame and blows the container's memory cap). Its own ladder drops fps, longest edge and palette size until it fits; the cap applies to the **longest** edge so portrait TikToks do not come out 480x853. If no rung fits, it silently falls back to the normal MP4 path rather than failing.
* **Compression ladder** (`core/video.py`): x264 veryfast source-res → x264 480p → x265 ultrafast 480p, all with AAC audio. Two-pass, target = 97 % of `guild.filesize_limit`. Measured on rock5: a 3.4-min 720p clip 17.5 MB → 7.7 MB in ~75 s. Encodes are bounded by `MAX_CONCURRENT_ENCODES` (default 2) — never unbounded. Each rung is size-checked *before* it runs (`plan_step`): a clip that provably cannot fit is rejected in a second with the longest duration that would, instead of burning six ffmpeg passes to find out.
* **Embed-fixer links are skipped** (`cogs/media.py`): `is_embed_fixer()` matches the known
  front-ends whose only job is to render a playable embed. Posting one already solves the
  problem the bot exists for, so the listener ignores it — but they stay in `SUPPORTED` and
  `normalise()` still rewrites them, so an explicit `/convert` on one works. TikTok's own
  `vm.`/`vt.` shorteners are deliberately **not** fixers: they redirect to an ordinary post.
* **Per-user opt-out** (`/autoconvert off`): stored globally under the reserved guild id 0,
  not per guild — it is a statement about your own messages, so opting out once covers every
  server. `set_media_optout()` does its read-modify-write under the settings lock, because
  doing it as `get()` then `set()` would drop one of two concurrent opt-outs.
* **Link allowlisting** (`cogs/media.py`): `classify()` parses the host with `urlsplit` and matches it against an exact domain/subdomain list. It must never be reimplemented with string splitting — a `#` or `?` can smuggle an allowlisted suffix past that and turn the auto-converter into an SSRF primitive.
* **Playback failures**: `GuildPlayer.current` is `None` whenever a track did not actually play. That is what keeps a broken track out of the loop-all rotation; assigning `current` only on success made a failed track re-queue its *predecessor* and evict its successor. Five consecutive failures stop the player rather than spamming the channel.
* **Spotify links** (`core/spotify.py`): Spotify audio is DRM'd, so links are resolved to metadata (keyless, via the public embed page — tracks, albums, playlists up to ~50–100 items; set `SPOTIFY_CLIENT_ID/SECRET` for full lists via the Web API) and each track is matched on YouTube *when it's about to play*, so a 50-track playlist queues instantly.
* **yt-dlp needs a JS runtime** for YouTube; the image ships Node. If YouTube breaks, the start-up self-update usually fixes it; otherwise set `YTDL_COOKIES_FILE`.
