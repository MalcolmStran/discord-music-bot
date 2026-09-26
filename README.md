# Discord Music Bot

A self-hosted Discord bot that does two things:

* **Music** — streams from YouTube, SoundCloud, or anything else yt-dlp supports, with a
  real queue, loop modes, live volume and instant playlist queueing. Spotify links are
  resolved to metadata and matched on YouTube.
* **Media** — watches for Twitter/X and TikTok links and re-uploads the video directly into
  the channel, compressed to fit your server's upload limit. Silent clips are sent as
  looping GIFs so they autoplay instead of showing a click-to-play card.

Everything works as both a slash command (`/play`) and a prefix command (`!play`).

---

## Quick start

```bash
git clone https://github.com/MalcolmStran/discord-music-bot
cd discord-music-bot
cp .env.example .env      # then edit .env: DISCORD_TOKEN is the only required value
docker compose up -d --build
docker logs -f discord-music-bot
```

You should see `online as <bot name> (<id>) in N guilds` within a few seconds. If you
don't, jump to [Troubleshooting](#troubleshooting).

Running without Docker needs Python 3.11+, `ffmpeg` (with `ffprobe`) and a JS runtime for
yt-dlp (Node or Deno):

```bash
pip install -r requirements.txt
python -m bot
```

---

## Discord setup

The bot needs a little configuration on Discord's side before the token works.

**1. Create the application.** At the [Developer Portal](https://discord.com/developers/applications):
*New Application* → *Bot* → *Reset Token*. That token goes in `DISCORD_TOKEN`.

**2. Enable the Message Content intent.** On the same *Bot* page, under
*Privileged Gateway Intents*, turn on **Message Content**. This is not optional — without
it the bot starts, connects, and then silently ignores every `!command` and every pasted
link, because Discord delivers empty message bodies. It is the single most common reason
for "the bot is online but does nothing".

**3. Invite it.** Replace `YOUR_APP_ID` with the Application ID from the *General
Information* page:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=3271744&scope=bot%20applications.commands
```

The `applications.commands` scope is what makes slash commands appear; `bot` alone gives
you prefix commands only. The permissions integer covers:

| Permission | Used for |
|---|---|
| View Channels, Send Messages, Embed Links | everything |
| Attach Files | uploading converted videos and GIFs |
| Add Reactions | the ⏳ progress marker on a link being converted |
| Manage Messages | suppressing the original link's embed so the video isn't shown twice |
| Read Message History | replying to the message that was converted |
| Connect, Speak | voice playback |

*Add Reactions* and *Manage Messages* are the only optional ones — the bot still converts
links without them, it just loses the progress marker and can't hide the redundant preview.

Slash commands are published globally on first start and can take up to an hour to appear.
Use `!sync` (owner only) to force a refresh.

---

## Commands

### Music

| Command | Aliases | Notes |
|---|---|---|
| `/play <query \| url \| playlist>` | `p` | search term, video URL, playlist URL, or a Spotify track/album/playlist link |
| `/skip` | `s`, `next` | |
| `/stop` | | stops and clears the queue |
| `/pause` · `/resume` | `unpause` | |
| `/queue [page]` | `q` | paginated |
| `/nowplaying` | `np` | |
| `/volume [0-150]` | `vol` | applied live, the track doesn't restart |
| `/loop [off \| one \| all]` | `repeat` | |
| `/shuffle` · `/clear` | | `clear` keeps the current track |
| `/remove <n>` | `rm` | by queue position |
| `/move <from> <to>` | | |
| `/join` | `summon` | |
| `/leave` | `dc`, `disconnect` | also happens automatically when left alone |
| `/status` | `voice-debug`, `vdebug` | voice and player diagnostics |

### Media

| Command | Aliases | Notes |
|---|---|---|
| *(paste a Twitter/X or TikTok link)* | | converted automatically |
| *(paste an fxtwitter / vxtwitter / fixupx / fixvx / twittpr / vxtiktok / tnktok link)* | | **left alone** — it already embeds its own video, so converting would post the clip twice |
| *(a clip with no audio track)* | | sent as a looping GIF sized to fit the upload cap; set `MAX_GIF_SECONDS=0` to disable |
| `/convert <url>` | | manual conversion, works on fixer links too |
| `/autoconvert [on \| off]` | | opt your **own** posts out of auto-conversion. Applies in every server the bot is in, and works in DMs. Omit the argument to see your current setting. |
| `/mediainfo` | `media-status` | status, limits, this server's upload cap |
| `/media-toggle` | | *Manage Server* — per-server on/off, persisted |
| `/media-cleanup` | | *Manage Server* — wipe temp files |

Owner-only (`OWNER_IDS`, prefix-only): `!sync`, `!reload <music \| media>`.

---

## Configuration

Every setting is an environment variable, read from `.env` (see `.env.example`).
`DISCORD_TOKEN` is the only one you must set.

> **Comments must be on their own line.** `docker run --env-file` does not strip trailing
> comments, so `COMMAND_PREFIX=!  # note` sets the prefix to the literal `!  # note`.

| Variable | Default | Range | Notes |
|---|---|---|---|
| `DISCORD_TOKEN` | — | | **required** |
| `COMMAND_PREFIX` | `!` | non-empty | an empty prefix would match every message and is rejected |
| `OWNER_IDS` | *(none)* | | comma- or semicolon-separated user ids for `!sync` / `!reload` |
| `LOG_LEVEL` | `INFO` | CRITICAL … DEBUG | anything else falls back to `INFO` |
| `LOG_DIR` | `./logs` | | |
| `DOWNLOAD_DIR` | `./downloads` | | settings live in `<DOWNLOAD_DIR>/bot_settings/` |
| `FORCE_COMMAND_SYNC` | `false` | | re-publish slash commands even if unchanged |
| **Music** | | | |
| `MAX_QUEUE_SIZE` | `50` | 1–10000 | also caps how many playlist entries are resolved |
| `MAX_SONG_DURATION` | `7200` | ≥ 1 | seconds |
| `DEFAULT_VOLUME` | `0.5` | 0.0–1.0 | |
| `VOICE_AUTO_DISCONNECT_TIMEOUT` | `300` | ≥ 10 | seconds idle in voice before leaving |
| **Media** | | | |
| `MEDIA_ENABLED_DEFAULT` | `true` | | starting state for servers that haven't used `/media-toggle` |
| `MAX_DOWNLOAD_MB` | `500` | ≥ 1 | refuse to download anything larger |
| `MAX_CONCURRENT_ENCODES` | `2` | 1–16 | simultaneous ffmpeg jobs |
| `ENCODE_TIMEOUT_SECONDS` | `600` | ≥ 30 | per encode |
| `MAX_GIF_SECONDS` | `30` | 0–600 | silent clips up to this long become GIFs; `0` disables |
| **Optional integrations** | | | |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | *(none)* | | full playlists via the Web API; without them the keyless embed route caps out around 50–100 tracks |
| `RAPIDAPI_KEY` | *(none)* | | TikTok download fallback; yt-dlp handles TikTok natively, so this is rarely needed |
| `YTDL_COOKIES_FILE` | *(none)* | | path to a `cookies.txt` for age-gated or rate-limited content |
| `YTDLP_AUTO_UPDATE` | `true` | | refresh yt-dlp on container start (Docker only) |

Numbers outside their range are clamped and logged as a warning rather than taken at face
value, so a typo degrades the bot instead of breaking it: `MAX_QUEUE_SIZE=0` used to make
every `/play` report a full queue with no hint as to why.

---

## Troubleshooting

**The bot is online but ignores `!commands` and pasted links.**
The Message Content intent is off. See [Discord setup](#discord-setup) step 2. Slash
commands keep working, which is what makes this confusing.

**Slash commands don't show up.**
Global commands can take up to an hour to propagate. Check the invite used the
`applications.commands` scope, then run `!sync` as an owner.

**`DISCORD_TOKEN is not set (put it in .env)` on start-up.**
Either `.env` is missing, or the token is still the `your_discord_token_here` placeholder.

**YouTube playback suddenly fails everywhere.**
YouTube changed something and yt-dlp needs updating. The container self-updates on start,
so `docker compose restart` usually fixes it. If it persists, the video may be age-gated or
rate-limited — point `YTDL_COOKIES_FILE` at an exported `cookies.txt`.

**"That video is X long — too long to fit in N MB at watchable quality."**
The clip provably can't be compressed to fit, and the bot says so in about a second rather
than burning six ffmpeg passes to find out; the message includes the longest duration that
would have fit. `"Couldn't compress the video enough to upload it"` is the same outcome
found the slow way, after the ladder ran. Boosted servers have higher upload caps and the
bot reads the current one per server.

**The container restarts in a loop.**
The `HEALTHCHECK` watches a heartbeat file the bot touches only while its gateway
connection is live, so this usually means the bot can't stay connected — check the token,
then `docker logs discord-music-bot`.

---

## Development

```bash
./check.sh --install      # install dev deps, then lint + the whole suite
./check.sh                # lint + tests (306, no Discord and no network)
./check.sh --docker       # also build the image and run the suite inside it
```

Or run the pieces directly: `ruff check bot tests` and `python -m pytest`.

There is no CI workflow — hosted runners bill against the repository owner's account — so
`check.sh` is the thing to run before pushing. The suite is entirely offline and takes
about a second.

### Layout

```
bot/
  __main__.py      bot class, logging, help, slash sync, graceful shutdown
  config.py        env -> Config dataclass
  cogs/music.py    hybrid music commands, auto-leave when alone
  cogs/media.py    link detection, /convert, /autoconvert, /media-*
  core/player.py   GuildPlayer: voice, queue, playback loop, loop modes, idle disconnect
  core/queue.py    TrackQueue
  core/ytdl.py     resolve (flat playlists), lazy stream URLs, audio source
  core/video.py    download, probe, fit_under (2-pass ladder), to_gif
  core/spotify.py  Spotify links -> metadata (embed page, or Web API when creds are set)
  core/settings.py per-guild JSON settings + the global opt-out list
tests/             offline unit tests: queue/settings/links, player loop modes, encoder
                   planning, config parsing, ytdl helpers, Spotify parsing, media cog
check.sh           lint + tests (+ optional Docker build)
Dockerfile         python:3.13-slim + ffmpeg + node, runs as an unprivileged user
entrypoint.sh      optional yt-dlp self-update, then starts the bot
docker-compose.yml the supported way to run it
run.sh             plain `docker run` alternative
```

### Deployment notes

The container runs as an unprivileged user and keeps guild settings in the `bot-downloads`
volume (`bot_settings/guild_settings.json`; the v1 format is migrated automatically). A
`HEALTHCHECK` watches a heartbeat file the bot touches only while its gateway connection is
live, so a wedged-but-running bot gets restarted too.

---

## How it works

Notes on the decisions that aren't obvious from the code, several of which are scar tissue
from things that went wrong.

**Playback is a loop, not a callback chain.** `GuildPlayer._player_loop` waits for a track,
fetches the stream URL, plays, awaits the finish. Skip and stop simply stop the voice
client. No recursion and no `run_coroutine_threadsafe` chains.

**Voice connection is deliberately minimal** — a plain `channel.connect(reconnect=True)`,
letting discord.py handle resumes. Retry loops around it are what caused the 4006/4017
errors in the past; don't add them back.

**Playlists resolve flat** (one yt-dlp call, about a second for 100 items). Individual
stream URLs are fetched immediately before each track plays, so a long playlist queues
instantly and doesn't go stale.

**Spotify audio is DRM'd**, so links are resolved to *metadata* and each track is matched on
YouTube when it's about to play. Keyless resolution uses the public embed page; setting
`SPOTIFY_CLIENT_ID`/`SECRET` switches to the Web API for complete lists.

**A failed track never becomes `GuildPlayer.current`.** That is what keeps a broken track
out of the loop-all rotation — assigning `current` before the track actually started made a
failure re-queue its *predecessor* and evict its successor. Five consecutive failures stop
the player rather than spamming the channel.

**Compression is a ladder with a pre-check** (`core/video.py`): x264 veryfast at source
resolution → x264 480p → x265 ultrafast 480p, all two-pass, targeting 97% of
`guild.filesize_limit`. `plan_step` size-checks each rung *before* running it, so a clip
that provably cannot fit is rejected in about a second. Encodes are bounded by
`MAX_CONCURRENT_ENCODES` — never unbounded. Measured on a Rock 5: a 3.4-minute 720p clip,
17.5 MB → 7.7 MB in roughly 75 seconds.

**Silent clips become GIFs** because Discord autoplays a GIF inline where a muted MP4 gets a
click-to-play card. Two ffmpeg passes, `palettegen` then `paletteuse` — never one `split`
filter, which buffers every decoded frame and blows the container's memory cap. A second
ladder drops fps, longest edge and palette size until it fits; the size cap applies to the
**longest** edge, so portrait TikToks don't come out 480x853. If no rung fits, it falls back
to the normal MP4 path rather than failing.

**Embed-fixer links are skipped.** `is_embed_fixer()` matches the front-ends whose only job
is to render a playable embed; posting one already solves the problem this bot exists for.
They stay in `SUPPORTED` and `normalise()` still rewrites them, so an explicit `/convert`
works. TikTok's own `vm.`/`vt.` shorteners are deliberately *not* treated as fixers — they
redirect to an ordinary post.

**The per-user opt-out is global, not per-guild.** `/autoconvert off` is a statement about
your own messages, so opting out once covers every server; it's stored under the reserved
guild id `0`. `set_media_optout()` does its read-modify-write while holding the settings
lock, because a `get()` then `set()` would drop one of two concurrent opt-outs.

**Link allowlisting parses the host with `urlsplit`** and matches it against an exact
domain/subdomain list. This must never be reimplemented with string splitting: a `#` or `?`
can smuggle an allowlisted suffix past that check and turn the auto-converter into an SSRF
primitive.

**yt-dlp needs a JS runtime** for YouTube; the image ships Node.

---

## History

v2 (2026-08-19) is a full rewrite of the original bot, preserved at the `v1-legacy` tag: it
introduced slash commands, a per-guild playback loop in place of recursive callbacks,
instant playlist queueing, and one parameterised encoder instead of 1,400 lines of
copy-pasted ffmpeg calls.
