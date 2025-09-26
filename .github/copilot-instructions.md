# Copilot Instructions for discord-music-bot

## Architecture essentials
- `main.MusicBot` (discord.py 2.x) loads `src.cogs.music` and `src.cogs.media_handler` inside `setup_hook`; new features generally join as cogs loaded here.
- Runtime config lives in `config.py`, sourced from `.env`; respect per-guild limits like `MAX_QUEUE_SIZE`, voice retry/backoff constants, and `DOWNLOAD_DIR`.
- Logging is configured once in `main.py` to write both to stdout and `bot.log`; avoid reconfiguring loggers in cogs, just reuse `logging.getLogger(__name__)`.

## Music playback flow
- `MusicCog.play` resolves queries via `YTDLSource.create_source` (async, runs yt-dlp in executor); playlist detection relies on keywords/`list=` and kicks off `_process_full_playlist` background tasks.
- Each guild keeps its own `Player` and `Queue` (see `self.players`/`self.queues` dicts). When extending commands, always call `_get_player/_get_queue` to stay per-guild.
- `Player` controls the shared `discord.VoiceClient` with aggressive reconnect logic (`connect`, `ensure_connection`, `disconnect(force_cleanup)`); reuse these helpers instead of reimplementing voice handling.
- Queue mutations go through `Queue` methods (`add`, `remove`, `shuffle`, `get_queue_info`); they emit logs and enforce `max_size`.

## Media conversion flow
- `MediaHandler.on_message` scans guild messages for TikTok/Twitter links and routes to `_download_tiktok_video` or `_download_twitter_video`; manual entry point is `!convert`.
- Downloads land in `tempfile.gettempdir()/discord_bot_media`; `_safe_download_with_cleanup` ensures partial files are removed on errors.
- Compression depends on ffmpeg-python, targeting ≤8 MB using H.265→H.264 fallbacks and async subprocesses. Respect `self.target_file_size` when altering pipelines.
- TikTok support requires `RAPIDAPI_KEY`; guard new features behind the same `self.rapidapi_key` checks so bot degrades gracefully.

## External requirements
- System FFmpeg must be on PATH (checked by `setup.py`); Python deps are pinned in `requirements.txt` (`discord.py[voice]`, `yt-dlp`, `ffmpeg-python`, etc.).
- `.env` must at minimum define `DISCORD_TOKEN`; optional keys drive voice behaviour and media limits. Use `python-dotenv`-style lookups via `config.py` so unit changes stay centralized.

## Local workflows
- Quick start: run `start.bat`/`start.sh` to create a venv, install deps, scaffold `.env`, then execute `python main.py`. Direct runs assume FFmpeg + `.env` already exist.
- No automated tests ship with the repo; manual smoke tests mean inviting the bot, running `!play`, and dropping sample TikTok/Twitter links.
- Temporary media lives under `downloads/` (music) and the OS temp dir (videos); clean-up commands (`!media-cleanup`, embedded timers) expect these paths—avoid relocating without updating both cogs.

## Extending safely
- New commands belong in cogs; remember `commands.command` decorators and async context. For guild-only commands mimic `MusicCog.cog_check` behaviour.
- When adding scheduled jobs or background tasks, prefer `self.bot.loop.create_task` and follow the cleanup patterns in `MediaHandler.cog_unload`.
- Keep embeds consistent with existing style (title emoji + concise fields) and reuse helpers like `YTDLSource.format_duration`.
- For voice features, disable repeat or disconnect timers via `Player` methods (`toggle_repeat`, `start_disconnect_timer`) instead of manual flags.

Let me know if any section needs more depth or examples and I can iterate further.
