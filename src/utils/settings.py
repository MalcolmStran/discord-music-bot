"""
Per-guild settings persistence for the Discord bot.
Settings are stored as JSON so they survive container restarts.
"""

import json
import os
from pathlib import Path
from typing import Dict, Set

# Store settings INSIDE the downloads directory so they persist across container restarts
SETTINGS_DIR = Path(os.getenv('DOWNLOAD_DIR', './downloads')) / 'bot_settings'
SETTINGS_FILE = SETTINGS_DIR / 'guild_settings.json'

# In-memory cache of per-guild media conversion enabled state
# Key: guild_id (int), Value: bool (True = enabled, False = disabled)
_media_conversion_enabled: Dict[int, bool] = {}
_loaded = False


def _ensure_loaded():
    """Lazy-load settings from disk."""
    global _loaded
    if _loaded:
        return
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                for k, v in raw.items():
                    _media_conversion_enabled[int(k)] = bool(v)
        except Exception:
            pass
    _loaded = True


def _save():
    """Persist settings to disk."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump({str(k): v for k, v in _media_conversion_enabled.items()}, f, indent=2)


def is_media_conversion_enabled(guild_id: int) -> bool:
    """
    Returns True if media conversion (Twitter/TikTok) is enabled for the given guild.
    Defaults to True so existing behavior is unchanged unless explicitly disabled.
    """
    _ensure_loaded()
    return _media_conversion_enabled.get(guild_id, True)


def set_media_conversion_enabled(guild_id: int, enabled: bool):
    """Enable or disable media conversion for a guild."""
    _ensure_loaded()
    _media_conversion_enabled[guild_id] = enabled
    _save()


def get_all_settings() -> Dict[int, bool]:
    """Return a copy of all guild settings."""
    _ensure_loaded()
    return dict(_media_conversion_enabled)
