"""Per-guild settings persisted as JSON (survives container restarts)."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class GuildSettings:
    """Tiny key/value store: {guild_id: {key: value}}. Backwards compatible with the
    v1 file format {guild_id: bool} which meant "media conversion enabled"."""

    def __init__(self, path: Path, media_default: bool = True):
        self.path = path
        self.media_default = media_default
        self._data: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # corrupt file → start fresh, keep a copy
            log.warning("settings file unreadable (%s); starting fresh", e)
            try:
                self.path.rename(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return
        for k, v in raw.items():
            try:
                gid = int(k)
            except ValueError:
                continue
            if isinstance(v, bool):            # v1 format
                self._data[gid] = {"media_enabled": v}
            elif isinstance(v, dict):
                self._data[gid] = v

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in self._data.items()}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        return self._data.get(guild_id, {}).get(key, default)

    def set(self, guild_id: int, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(guild_id, {})[key] = value
            self._save()

    # convenience
    def media_enabled(self, guild_id: int) -> bool:
        return bool(self.get(guild_id, "media_enabled", self.media_default))

    def set_media_enabled(self, guild_id: int, enabled: bool) -> None:
        self.set(guild_id, "media_enabled", bool(enabled))

    def all(self) -> dict[int, dict[str, Any]]:
        return {k: dict(v) for k, v in self._data.items()}
