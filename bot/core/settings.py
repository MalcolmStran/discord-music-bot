"""Per-guild settings persisted as JSON (survives container restarts)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class GuildSettings:
    """Tiny key/value store: {guild_id: {key: value}}. Backwards compatible with the
    v1 file format {guild_id: bool} which meant "media conversion enabled".

    Guild id 0 is reserved for the bot's own bookkeeping.
    """

    def __init__(self, path: Path, media_default: bool = True):
        self.path = path
        self.media_default = media_default
        self._data: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._degraded = False       # writes are failing; complain once, not every time
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._quarantine(e)
            return
        if not isinstance(raw, dict):
            # valid JSON but the wrong shape: `raw.items()` used to raise here and take
            # the whole bot down at startup.
            self._quarantine(TypeError(f"expected an object, got {type(raw).__name__}"))
            return
        for k, v in raw.items():
            try:
                gid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, bool):            # v1 format
                self._data[gid] = {"media_enabled": v}
            elif isinstance(v, dict):
                self._data[gid] = v

    def _quarantine(self, err: Exception) -> None:
        """Move an unusable settings file aside and start fresh."""
        log.warning("settings file unreadable (%s); starting fresh", err)
        # A fixed ".corrupt.json" name meant the second failure destroyed the copy kept from
        # the first. A timestamp alone is not enough either: it only has second resolution,
        # so repeated failures within the same second still overwrote each other (rename()
        # replaces silently). Add a counter so every rescue copy survives.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_suffix(f".corrupt-{stamp}.json")
        n = 1
        while dest.exists() and n < 1000:
            dest = self.path.with_suffix(f".corrupt-{stamp}-{n}.json")
            n += 1
        try:
            self.path.rename(dest)
        except OSError as e:
            log.warning("could not preserve the corrupt settings file: %s", e)

    def _save(self) -> None:
        """Atomically replace the settings file. Never raises: a full or read-only volume
        must not take down a command, and in-memory state stays usable either way."""
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({str(k): v for k, v in self._data.items()}, indent=2)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())       # otherwise a hard reset loses the last writes
            tmp.replace(self.path)
            self._degraded = False
        except OSError as e:
            if not self._degraded:
                log.error("could not persist settings to %s (%s); changes are in memory only",
                          self.path, e)
                self._degraded = True
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        return self._data.get(guild_id, {}).get(key, default)

    def set(self, guild_id: int, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(guild_id, {})[key] = value
            self._save()

    async def set_async(self, guild_id: int, key: str, value: Any) -> None:
        """`set` off the event loop — the write is small but it is still disk I/O."""
        await asyncio.to_thread(self.set, guild_id, key, value)

    # convenience
    def media_enabled(self, guild_id: int) -> bool:
        return bool(self.get(guild_id, "media_enabled", self.media_default))

    def set_media_enabled(self, guild_id: int, enabled: bool) -> None:
        self.set(guild_id, "media_enabled", bool(enabled))

    def forget_guild(self, guild_id: int) -> None:
        """Drop a guild's settings (the bot was removed from it)."""
        with self._lock:
            if self._data.pop(guild_id, None) is not None:
                self._save()

    def all(self) -> dict[int, dict[str, Any]]:
        return {k: dict(v) for k, v in self._data.items()}
