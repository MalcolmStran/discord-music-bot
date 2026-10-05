"""Environment parsing: degenerate values used to be accepted verbatim and brick the bot."""
import logging
import os
from pathlib import Path

import pytest

from bot.config import Config, _bool, _float, _int, _log_level, _prefix


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DISCORD_TOKEN", "COMMAND_PREFIX", "OWNER_IDS", "MAX_QUEUE_SIZE",
                 "MAX_SONG_DURATION", "DEFAULT_VOLUME", "VOICE_AUTO_DISCONNECT_TIMEOUT", "VOICE_RECONNECT_GRACE",
                 "MAX_CONCURRENT_ENCODES", "MAX_DOWNLOAD_MB", "ENCODE_TIMEOUT_SECONDS",
                 "LOG_LEVEL", "LOG_DIR", "DOWNLOAD_DIR", "MEDIA_ENABLED_DEFAULT",
                 "RAPIDAPI_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                 "YTDL_COOKIES_FILE", "FORCE_COMMAND_SYNC"):
        monkeypatch.delenv(name, raising=False)


def test_int_parsing_and_clamping(monkeypatch):
    monkeypatch.setenv("N", "7")
    assert _int("N", 1) == 7
    monkeypatch.setenv("N", "not a number")
    assert _int("N", 1) == 1
    monkeypatch.setenv("N", "")
    assert _int("N", 1) == 1
    monkeypatch.setenv("N", "0")
    assert _int("N", 5, minimum=1) == 1
    monkeypatch.setenv("N", "999")
    assert _int("N", 5, maximum=16) == 16
    monkeypatch.setenv("N", " 12 ")
    assert _int("N", 5) == 12


def test_float_parsing_and_clamping(monkeypatch):
    monkeypatch.setenv("F", "0.25")
    assert _float("F", 0.5) == 0.25
    monkeypatch.setenv("F", "nope")
    assert _float("F", 0.5) == 0.5
    monkeypatch.setenv("F", "9")
    assert _float("F", 0.5, minimum=0.0, maximum=1.0) == 1.0


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("anything", False),
])
def test_bool_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("B", raw)
    assert _bool("B", not expected) is expected


def test_bool_default_when_unset():
    assert _bool("DEFINITELY_UNSET_XYZ", True) is True


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_empty_prefix_falls_back(monkeypatch, raw):
    """`when_mentioned_or("")` yields "" as a prefix, which matches EVERY message: the bot
    would try to parse all chatter as commands and never auto-convert a link."""
    monkeypatch.setenv("COMMAND_PREFIX", raw)
    assert _prefix() == "!"


def test_prefix_is_stripped(monkeypatch):
    monkeypatch.setenv("COMMAND_PREFIX", "  ?  ")
    assert _prefix() == "?"


def test_log_level_validated(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert _log_level() == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    assert _log_level() == "INFO"       # would otherwise raise inside setup_logging


def test_missing_token_is_fatal():
    with pytest.raises(SystemExit):
        Config.from_env()


def test_placeholder_token_is_fatal(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "your_discord_token_here")
    with pytest.raises(SystemExit):
        Config.from_env()


def test_degenerate_values_are_clamped_not_accepted(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("MAX_QUEUE_SIZE", "0")
    monkeypatch.setenv("MAX_SONG_DURATION", "-5")
    monkeypatch.setenv("VOICE_AUTO_DISCONNECT_TIMEOUT", "-1")
    monkeypatch.setenv("DEFAULT_VOLUME", "9")
    monkeypatch.setenv("MAX_CONCURRENT_ENCODES", "0")
    cfg = Config.from_env()
    assert cfg.max_queue_size >= 1
    assert cfg.max_song_duration >= 1
    assert cfg.idle_disconnect_seconds >= 10
    assert 0.0 <= cfg.default_volume <= 1.0
    assert cfg.max_concurrent_encodes >= 1


def test_owner_ids_accept_both_separators(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("OWNER_IDS", "1, 2;3 , junk,")
    assert Config.from_env().owner_ids == frozenset({1, 2, 3})


def test_derived_paths_live_under_download_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.data_dir.parent == tmp_path and cfg.media_tmp_dir.parent == tmp_path


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_download_dir_falls_back_to_the_default(monkeypatch, raw):
    """Path("") is ".", which put guild settings in ./bot_settings, outside the persisted
    volume, so they were lost on every container rebuild."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DOWNLOAD_DIR", raw)
    cfg = Config.from_env()
    assert cfg.download_dir == Path("./downloads")
    assert cfg.data_dir == Path("./downloads/bot_settings")


def test_missing_cookies_file_is_warned_about(monkeypatch, tmp_path, caplog):
    """yt-dlp's callers skip a missing cookies file silently; under Docker a host path never
    exists inside the container, so cookies looked configured but were never sent."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    missing = tmp_path / "nope" / "cookies.txt"
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(missing))
    with caplog.at_level(logging.WARNING, logger="bot.config"):
        cfg = Config.from_env()
    assert cfg.ytdl_cookies_file is None
    [rec] = [r for r in caplog.records if "YTDL_COOKIES_FILE" in r.getMessage()]
    assert rec.levelno == logging.WARNING
    assert str(missing) in rec.getMessage() and "inside the container" in rec.getMessage()


def test_a_cookies_directory_is_warned_about_and_not_used(monkeypatch, tmp_path, caplog):
    """Docker creates a directory at the mount point when the cookies file it mounts is
    missing. exists() let it through without a word, and yt-dlp then failed every /play and
    every conversion with "Is a directory"."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    made_by_docker = tmp_path / "cookies.txt"
    made_by_docker.mkdir()
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(made_by_docker))
    with caplog.at_level(logging.WARNING, logger="bot.config"):
        cfg = Config.from_env()
    assert cfg.ytdl_cookies_file is None
    [rec] = [r for r in caplog.records if "YTDL_COOKIES_FILE" in r.getMessage()]
    assert rec.levelno == logging.WARNING
    assert "is a directory" in rec.getMessage() and "rmdir cookies.txt" in rec.getMessage()


def test_an_unreadable_cookies_file_is_warned_about_and_not_used(monkeypatch, tmp_path, caplog):
    """yt-dlp skips a cookies file it can't read without a word, so cookies looked configured
    but were never sent."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    jar.chmod(0)
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(jar))
    # root reads a mode-000 file anyway (and the suite runs as root in the container), so
    # answer the permission check the way it is answered for the bot's unprivileged user
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: False if Path(p) == jar else real_access(p, mode))
    with caplog.at_level(logging.WARNING, logger="bot.config"):
        cfg = Config.from_env()
    assert cfg.ytdl_cookies_file is None
    [rec] = [r for r in caplog.records if "YTDL_COOKIES_FILE" in r.getMessage()]
    assert rec.levelno == logging.WARNING
    assert str(jar) in rec.getMessage() and "permissions" in rec.getMessage()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_cookies_path_that_is_not_a_regular_file_is_not_used(monkeypatch, tmp_path, caplog):
    """A FIFO (or socket, or device) passes the read-permission check but is no cookies file;
    YTDL and video.download skip anything but a regular file without a word, so this warning
    is the only sign that cookies are not being sent."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    fifo = tmp_path / "cookies.txt"
    os.mkfifo(fifo)
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(fifo))
    with caplog.at_level(logging.WARNING, logger="bot.config"):
        cfg = Config.from_env()
    assert cfg.ytdl_cookies_file is None
    [rec] = [r for r in caplog.records if "YTDL_COOKIES_FILE" in r.getMessage()]
    assert str(fifo) in rec.getMessage() and "not a file" in rec.getMessage()


def test_existing_cookies_file_is_used_and_not_warned_about(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(jar))
    with caplog.at_level(logging.WARNING, logger="bot.config"):
        cfg = Config.from_env()
    assert cfg.ytdl_cookies_file == jar
    assert not [r for r in caplog.records if "YTDL_COOKIES_FILE" in r.getMessage()]


def test_blank_bool_falls_back_to_the_default(monkeypatch):
    """A blank value means "unset", matching _int/_float. Treating it as False made
    `MEDIA_ENABLED_DEFAULT=` silently disable the feature."""
    monkeypatch.setenv("B", "")
    assert _bool("B", True) is True
    assert _bool("B", False) is False
    monkeypatch.setenv("B", "   ")
    assert _bool("B", True) is True


def test_max_gif_seconds_defaults_and_clamps(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    assert Config.from_env().max_gif_seconds == 30
    monkeypatch.setenv("MAX_GIF_SECONDS", "0")          # 0 turns the feature off
    assert Config.from_env().max_gif_seconds == 0
    monkeypatch.setenv("MAX_GIF_SECONDS", "-5")
    assert Config.from_env().max_gif_seconds == 0       # never negative
    monkeypatch.setenv("MAX_GIF_SECONDS", "99999")
    assert Config.from_env().max_gif_seconds == 600     # a 27-hour GIF is not a thing


def test_voice_reconnect_grace_default_and_bounds(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    assert Config.from_env().voice_reconnect_grace == 45
    monkeypatch.setenv("VOICE_RECONNECT_GRACE", "-5")
    assert Config.from_env().voice_reconnect_grace == 0
    monkeypatch.setenv("VOICE_RECONNECT_GRACE", "9999")
    assert Config.from_env().voice_reconnect_grace == 300
