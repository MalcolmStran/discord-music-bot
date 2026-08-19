"""Configuration — everything comes from environment variables (or .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


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
    rapidapi_key: str | None = None        # optional TikTok fallback

    # paths
    download_dir: Path = Path("./downloads")
    ytdl_cookies_file: Path | None = None  # optional cookies.txt for age-gated / rate-limited sites

    log_level: str = "INFO"

    @property
    def data_dir(self) -> Path:
        # kept under download_dir so the existing docker volume keeps the old settings file
        return self.download_dir / "bot_settings"

    @property
    def media_tmp_dir(self) -> Path:
        return self.download_dir / "media_tmp"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "your_discord_token_here":
            raise SystemExit("DISCORD_TOKEN is not set (put it in .env)")
        owners = frozenset(int(x) for x in os.getenv("OWNER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit())
        cookies = os.getenv("YTDL_COOKIES_FILE", "").strip()
        return cls(
            token=token,
            prefix=os.getenv("COMMAND_PREFIX", "!"),
            owner_ids=owners,
            max_queue_size=_int("MAX_QUEUE_SIZE", 50),
            max_song_duration=_int("MAX_SONG_DURATION", 7200),
            default_volume=max(0.0, min(1.0, _float("DEFAULT_VOLUME", 0.5))),
            idle_disconnect_seconds=_int("VOICE_AUTO_DISCONNECT_TIMEOUT", 300),
            media_enabled_default=_bool("MEDIA_ENABLED_DEFAULT", True),
            max_download_mb=_int("MAX_DOWNLOAD_MB", 500),
            max_concurrent_encodes=max(1, _int("MAX_CONCURRENT_ENCODES", 2)),
            encode_timeout_seconds=_int("ENCODE_TIMEOUT_SECONDS", 600),
            rapidapi_key=(os.getenv("RAPIDAPI_KEY") or "").strip() or None,
            download_dir=Path(os.getenv("DOWNLOAD_DIR", "./downloads")),
            ytdl_cookies_file=Path(cookies) if cookies else None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
