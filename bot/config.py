"""Configuration — everything comes from environment variables (or .env)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _int(name: str, default: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    """Parse an int env var, clamped into [minimum, maximum].

    Values outside the range used to be taken at face value: MAX_QUEUE_SIZE=0 made every
    /play report a full queue, and a negative idle timeout made the player disconnect
    immediately, both with no hint as to why.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            log.warning("%s=%r is not a whole number; using %s", name, raw, default)
            value = default
    return _clamp(name, value, minimum, maximum, default)


def _float(name: str, default: float, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError:
            log.warning("%s=%r is not a number; using %s", name, raw, default)
            value = default
    return _clamp(name, value, minimum, maximum, default)


def _clamp(name, value, minimum, maximum, default):
    if minimum is not None and value < minimum:
        log.warning("%s=%s is below the minimum %s; using %s", name, value, minimum, minimum)
        return minimum
    if maximum is not None and value > maximum:
        log.warning("%s=%s is above the maximum %s; using %s", name, value, maximum, maximum)
        return maximum
    return value


def _bool(name: str, default: bool) -> bool:
    """A blank value means "unset" and falls back to `default`, matching _int/_float.

    Treating it as False made `MEDIA_ENABLED_DEFAULT=` silently disable the feature rather
    than use the documented default — and .env.example ships several keys with empty values.
    """
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _prefix(default: str = "!") -> str:
    """A command prefix that is safe to hand to `when_mentioned_or`.

    An empty prefix matches *every* message, so the bot would try to parse all chatter as
    commands and the media cog would never auto-convert anything.
    """
    raw = os.getenv("COMMAND_PREFIX", default)
    prefix = raw.strip()
    if not prefix:
        if raw:
            log.warning("COMMAND_PREFIX is only whitespace; using %r", default)
        return default
    return prefix


def _log_level(default: str = "INFO") -> str:
    level = (os.getenv("LOG_LEVEL") or default).strip().upper()
    if level not in VALID_LOG_LEVELS:
        log.warning("LOG_LEVEL=%r is not one of %s; using %s", level, ", ".join(VALID_LOG_LEVELS), default)
        return default
    return level


@dataclass(frozen=True)
class Config:
    token: str
    prefix: str = "!"
    owner_ids: frozenset[int] = field(default_factory=frozenset)

    # music
    max_queue_size: int = 50
    max_song_duration: int = 7200          # seconds
    default_volume: float = 0.5            # 0..1
    idle_disconnect_seconds: int = 300     # leave voice after this long with nothing to play

    # media conversion
    media_enabled_default: bool = True
    max_download_mb: int = 500
    max_concurrent_encodes: int = 2        # ffmpeg jobs at once (lesson: never unbounded)
    encode_timeout_seconds: int = 600
    rapidapi_key: Optional[str] = None     # optional TikTok fallback
    spotify_client_id: Optional[str] = None   # optional: full playlists via Web API (else embed page, ~50-100 tracks)
    spotify_client_secret: Optional[str] = None

    # paths
    download_dir: Path = Path("./downloads")
    log_dir: Path = Path("./logs")
    ytdl_cookies_file: Optional[Path] = None  # optional cookies.txt for age-gated / rate-limited sites

    log_level: str = "INFO"
    force_sync: bool = False               # re-publish slash commands even if unchanged

    @property
    def data_dir(self) -> Path:
        # kept under download_dir so the existing docker volume keeps the old settings file
        return self.download_dir / "bot_settings"

    @property
    def media_tmp_dir(self) -> Path:
        return self.download_dir / "media_tmp"

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "your_discord_token_here":
            raise SystemExit("DISCORD_TOKEN is not set (put it in .env)")
        owners = frozenset(int(x) for x in os.getenv("OWNER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit())
        cookies = os.getenv("YTDL_COOKIES_FILE", "").strip()
        logs = os.getenv("LOG_DIR", "").strip()
        return cls(
            token=token,
            prefix=_prefix(),
            owner_ids=owners,
            max_queue_size=_int("MAX_QUEUE_SIZE", 50, minimum=1, maximum=10_000),
            max_song_duration=_int("MAX_SONG_DURATION", 7200, minimum=1),
            default_volume=_float("DEFAULT_VOLUME", 0.5, minimum=0.0, maximum=1.0),
            idle_disconnect_seconds=_int("VOICE_AUTO_DISCONNECT_TIMEOUT", 300, minimum=10),
            media_enabled_default=_bool("MEDIA_ENABLED_DEFAULT", True),
            max_download_mb=_int("MAX_DOWNLOAD_MB", 500, minimum=1),
            max_concurrent_encodes=_int("MAX_CONCURRENT_ENCODES", 2, minimum=1, maximum=16),
            encode_timeout_seconds=_int("ENCODE_TIMEOUT_SECONDS", 600, minimum=30),
            rapidapi_key=(os.getenv("RAPIDAPI_KEY") or "").strip() or None,
            spotify_client_id=(os.getenv("SPOTIFY_CLIENT_ID") or "").strip() or None,
            spotify_client_secret=(os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip() or None,
            download_dir=Path(os.getenv("DOWNLOAD_DIR", "./downloads")),
            log_dir=Path(logs) if logs else Path("./logs"),
            ytdl_cookies_file=Path(cookies) if cookies else None,
            log_level=_log_level(),
            force_sync=_bool("FORCE_COMMAND_SYNC", False),
        )
