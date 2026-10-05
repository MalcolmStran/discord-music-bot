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

Running without Docker needs Python 3.11+ and `ffmpeg` (with `ffprobe`). yt-dlp also needs
Deno to solve YouTube's challenges; `requirements.txt` installs it on x86_64 and aarch64
(glibc 2.27+), macOS and 64-bit Windows. Elsewhere (32-bit ARM, musl/Alpine) it is skipped and
YouTube runs without the challenge solver unless you install Deno yourself:

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
*Privileged Gateway Intents*, turn on **Message Content**. This is not optional: the bot
asks for it on every connect, and without it Discord refuses the connection. The bot never
comes online, it exits with `Message Content intent is not enabled for this bot`, and under
Docker the container restarts in a loop. It is the single most common setup mistake.

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
Without *Attach Files* or *Read Message History* in a channel, the bot skips auto-conversion
there rather than downloading a video it can't post.

Two more are worth granting only if you need them, because they let the bot act on other
members too:

| Permission | Needed for |
|---|---|
| Move Members | joining a voice channel that is at its user limit (otherwise the bot says it's full) |
| Mute Members | speaking in a **Stage** channel; without it the bot requests to speak and a Stage moderator has to invite it |

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
| `/join` | `summon` | the bot leaves again after `VOICE_AUTO_DISCONNECT_TIMEOUT` if nothing is played |
| `/leave` | `dc`, `disconnect` | refused for someone outside the bot's channel while people are listening, unless they have *Move Members*; also happens automatically when the bot is left alone |
| `/status` | `voice-debug`, `vdebug` | voice and player diagnostics |

### Media

| Command | Aliases | Notes |
|---|---|---|
| *(paste a Twitter/X or TikTok link)* | | converted automatically; the original embed is hidden only when every link in the message was converted. Links inside `\|\|spoiler\|\|` tags are uploaded as spoilers. Profile, hashtag, live and Space links are left alone (no ⏳, and they don't use up one of the two links converted per message) |
| *(paste an fxtwitter / vxtwitter / fixupx / fixvx / twittpr / vxtiktok / tnktok link)* | | **left alone** — it already embeds its own video, so converting would post the clip twice |
| *(a clip with no audio track)* | | sent as a looping GIF sized to fit the upload cap; set `MAX_GIF_SECONDS=0` to disable |
| `/convert <url>` | | manual conversion, works on fixer links too |
| `/autoconvert [on \| off]` | | opt your **own** posts out of auto-conversion. Applies in every server the bot is in, and works in DMs. Omit the argument to see your current setting. |
| `/mediainfo` | `media-status` | status, limits, this server's upload cap |
| `/media-toggle` | | *Manage Server* — per-server on/off, persisted |
| `/media-cleanup` | | *Manage Server* — wipe temp files (files a running conversion is using are kept) |

Owner-only (`OWNER_IDS`, prefix-only): `!sync`, `!reload <music \| media>`.

---

## Configuration

Every setting is an environment variable, read from `.env` (see `.env.example`).
`DISCORD_TOKEN` is the only one you must set.

> **Comments go on their own line, and values are not quoted.** `docker run --env-file`
> (used by `run.sh`) takes values verbatim: `COMMAND_PREFIX=!  # note` sets the prefix to the
> literal `!  # note`, and `DISCORD_TOKEN="..."` keeps the quotes and fails to log in.

| Variable | Default | Range | Notes |
|---|---|---|---|
| `DISCORD_TOKEN` | — | | **required** |
| `COMMAND_PREFIX` | `!` | non-empty | an empty prefix would match every message and is rejected |
| `OWNER_IDS` | *(none)* | | comma- or semicolon-separated user ids for `!sync` / `!reload` |
| `LOG_LEVEL` | `INFO` | CRITICAL … DEBUG | anything else falls back to `INFO` |
| `LOG_DIR` | `./logs` | | |
| `DOWNLOAD_DIR` | `./downloads` | | settings live in `<DOWNLOAD_DIR>/bot_settings/`; blank means the default |
| `FORCE_COMMAND_SYNC` | `false` | | re-publish slash commands even if unchanged |
| **Music** | | | |
| `MAX_QUEUE_SIZE` | `50` | 1–10000 | also caps how many entries a playlist, channel or artist link queues |
| `MAX_SONG_DURATION` | `7200` | ≥ 1 | seconds; also checked when a track starts, for links whose length isn't known up front (Spotify matches, SoundCloud sets) |
| `DEFAULT_VOLUME` | `0.5` | 0.0–1.0 | |
| `VOICE_AUTO_DISCONNECT_TIMEOUT` | `300` | ≥ 10 | seconds idle in voice before leaving |
| `VOICE_RECONNECT_GRACE` | `45` | 0–300 | seconds to wait for a dropped voice connection to recover before resetting the player (0 = reset immediately) |
| **Media** | | | |
| `MEDIA_ENABLED_DEFAULT` | `true` | | starting state for servers that haven't used `/media-toggle` |
| `MAX_DOWNLOAD_MB` | `500` | ≥ 1 | a download is stopped as soon as it passes this size; each download also has a 30-minute limit |
| `MAX_CONCURRENT_ENCODES` | `2` | 1–16 | simultaneous ffmpeg jobs |
| `ENCODE_TIMEOUT_SECONDS` | `600` | ≥ 30 | per encode |
| `MAX_GIF_SECONDS` | `30` | 0–600 | silent clips up to this long become GIFs; `0` disables |
| **Optional integrations** | | | |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | *(none)* | | full playlists via the Web API; without them the keyless embed route caps out around 50–100 tracks |
| `RAPIDAPI_KEY` | *(none)* | | TikTok download fallback; yt-dlp handles TikTok natively, so this is rarely needed |
| `YTDL_COOKIES_FILE` | *(none)* | | path to a `cookies.txt` for age-gated or rate-limited content; read only, never written back. Under Docker see [below](#cookies-under-docker) |
| `YTDLP_AUTO_UPDATE` | `true` | | refresh yt-dlp on container start (Docker only). `1`/`true`/`yes`/`on` in any case; anything else turns it off and logs `yt-dlp: self-update disabled` |

Numbers outside their range are clamped and logged as a warning rather than taken at face
value, so a typo degrades the bot instead of breaking it: `MAX_QUEUE_SIZE=0` used to make
every `/play` report a full queue with no hint as to why.

### Cookies under Docker

A host path in `YTDL_COOKIES_FILE` doesn't exist inside the container. Put the file next to
`docker-compose.yml`, uncomment the `./cookies.txt:/app/cookies.txt:ro` line there, and set
`YTDL_COOKIES_FILE=/app/cookies.txt`. Create the file first, or Docker creates a directory
in its place. With `run.sh`, add `-v "$PWD/cookies.txt:/app/cookies.txt:ro"` to its
`docker run` line.

If the path is missing, is a directory, or can't be read, the bot logs a warning at start-up
and runs without cookies rather than failing every download. If Docker already created a
`cookies.txt` directory, remove it on the host (`rmdir cookies.txt`), create the file, and
recreate the container.

---

## Troubleshooting

**The bot never comes online, and the log says `Message Content intent is not enabled`.**
Turn it on as in [Discord setup](#discord-setup) step 2, then restart. Slash commands may
still appear in Discord, because they are published before the bot connects, but nothing
answers them.

**Slash commands don't show up.**
Global commands can take up to an hour to propagate. Check the invite used the
`applications.commands` scope, then run `!sync` as an owner.

**`DISCORD_TOKEN is not set (put it in .env)` on start-up.**
Either `.env` is missing, or the token is still the `your_discord_token_here` placeholder.

**YouTube playback suddenly fails everywhere.**
YouTube changed something and yt-dlp needs updating. The container self-updates on start,
so `docker compose restart` usually fixes it; if the log shows `yt-dlp: self-update
disabled`, `YTDLP_AUTO_UPDATE` is off. "The site refused the request" means YouTube (or the
source site) turned the bot away, which is usually rate-limiting of the bot's IP. If it persists, the video may be age-gated or
rate-limited — export a `cookies.txt` and set it up as in
[Cookies under Docker](#cookies-under-docker).

**"That video is X long — too long to fit in N MB at watchable quality."**
The clip provably can't be compressed to fit, and the bot says so in about a second rather
than burning six ffmpeg passes to find out; the message includes the longest duration that
would have fit. `"Couldn't compress the video enough to upload it"` is the same outcome
found the slow way, after the ladder ran. Boosted servers have higher upload caps and the
bot reads the current one per server.

**The bot goes quiet for a few seconds mid-song, then carries on.**
That's a voice connection drop being recovered (common on satellite or mobile uplinks); the
log shows `bot left voice (external); waiting up to 45s for a reconnect` followed by
`voice connection recovered`. If drops end the song instead, raise `VOICE_RECONNECT_GRACE`.

**The container restarts in a loop.**
The process keeps exiting, and `docker logs discord-music-bot` says why. The usual causes are
a wrong token, the Message Content intent being off (see above), or the bot's watchdog giving
up on a gateway connection that stayed dead for 10 minutes (`gateway dead for …s`).

---

## Development

```bash
./check.sh --install      # install dev deps, then lint + the whole suite
./check.sh                # lint + tests (581, no Discord and no network)
./check.sh --docker       # also build the image and run the suite inside it
```

Or run the pieces directly: `ruff check bot tests` and `python -m pytest`.

There is no CI workflow — hosted runners bill against the repository owner's account — so
`check.sh` is the thing to run before pushing. The suite is entirely offline and takes
about ten seconds.

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
tests/             offline unit tests: queue/settings/links, player loop modes, voice-drop
                   recovery, encoder planning, config parsing, ytdl helpers, Spotify parsing,
                   media cog
check.sh           lint + tests (+ optional Docker build)
Dockerfile         python:3.13-slim + ffmpeg (+ Deno via pip), runs as an unprivileged user
entrypoint.sh      optional yt-dlp self-update, then starts the bot
docker-compose.yml the supported way to run it
run.sh             plain `docker run` alternative; mounts the same settings volume as compose
```

### Deployment notes

The container runs as an unprivileged user and keeps guild settings in the `bot-downloads`
volume (`bot_settings/guild_settings.json`; the v1 format is migrated automatically).

A wedged-but-running bot is restarted by the bot itself, not by the `HEALTHCHECK`. The
healthcheck only *reports*: it marks the container `(unhealthy)` in `docker ps` when the bot
stops touching its heartbeat file, and plain Docker and compose never restart a container
for that. What does restart it is an in-process watchdog: under Docker, if the gateway has
been dead for 10 minutes, the bot logs `gateway dead for …s` and exits, and
`restart: unless-stopped` brings it back. A bare `python -m bot` run has no supervisor, so the
watchdog is off there and discord.py keeps trying to reconnect instead.

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

**A dropped voice connection is ridden out, not treated as a kick.** On a flaky uplink
Discord closes the voice websocket now and then; discord.py waits up to 30 s for a new voice
server, reconnects, and the audio player just pauses. Resetting the player on the first "bot
left voice" event cancelled that reconnect and ended the song, so `wait_for_reconnect()` now
waits up to `VOICE_RECONNECT_GRACE` (default 45 s, deliberately longer than discord.py's
window) and only resets if the connection didn't come back. If discord.py has already dropped
its voice client — a real kick or a deleted channel — it resets at once.

**Joining and leaving are serialised.** `connect()` and `disconnect()` share a lock, and a
reconnect waiter treats a join in progress as "not given up yet", so a waiter whose grace
runs out can't tear down the connection `/play` is building. The bot's own leaves are
counted, so the "bot left voice" event Discord echoes back for them isn't mistaken for a kick.
After a drop, the bot also clears the dead audio player discord.py leaves behind, which
otherwise made every following track fail with "Already playing audio".

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
primitive. The host check alone isn't enough, though: a tweet with no video makes yt-dlp
hand off to whatever link the tweet contains. So downloads are also restricted to yt-dlp's
`twitter`, `tiktok` and `vm.tiktok` extractors, and the generic one can never run.

**yt-dlp needs a JS runtime** for YouTube, and by default it only uses Deno. The image ships
Deno as the pip `deno` wheel. Debian's Node is below yt-dlp's minimum version, and
yt-dlp ignores Node unless it's told to use it.

---

## History

v2 (2026-08-19) is a full rewrite of the original bot, preserved at the `v1-legacy` tag: it
introduced slash commands, a per-guild playback loop in place of recursive callbacks,
instant playlist queueing, and one parameterised encoder instead of 1,400 lines of
copy-pasted ffmpeg calls.
