"""convert_and_send end to end: the real cog code, with Discord and the network faked.

video.download / probe / fit_under / to_gif are replaced by stand-ins that write real files
into the real work dir, so what the cog uploads, counts and leaves on disk is the real thing.
"""
import os
import time
from pathlib import Path

import discord
import pytest

from bot.cogs.media import Media
from bot.core import video

MB = 1024 * 1024


class _Guild:
    id = 7
    filesize_limit = 10 * MB
    me = object()


class _Message:
    def __init__(self):
        self.guild = _Guild()
        self.uploads = []           # (filename, spoiler)
        self.replies = []
        self.edits = []

    reject_uploads = False

    async def reply(self, content=None, *, file=None, **kw):
        if file is not None:
            file.close()
            if self.reject_uploads:
                raise discord.HTTPException(_Response(), "Request entity too large")
            self.uploads.append((file.filename, file.spoiler))
        else:
            self.replies.append(content)
        return _Status()

    async def add_reaction(self, emoji):
        pass

    async def remove_reaction(self, emoji, member):
        pass

    async def edit(self, **kw):
        self.edits.append(kw)


class _Response:
    status = 413
    reason = "Payload Too Large"


class _Status:
    async def edit(self, **kw):
        pass

    async def delete(self):
        pass


class _Cfg:
    ytdl_cookies_file = None
    rapidapi_key = None
    max_gif_seconds = 0
    encode_timeout_seconds = 600


@pytest.fixture
def cog(tmp_path: Path):
    c = Media.__new__(Media)
    c.cfg = _Cfg()
    c.workdir = tmp_path
    c.max_bytes = 500 * MB
    c._busy = {}
    c.stats = {"ok": 0, "failed": 0, "compressed": 0, "gif": 0, "skipped": 0}
    return c


def _fake_download(size):
    async def download(url, workdir, max_bytes, **kw):
        p = workdir / "dl_test.mp4"
        p.write_bytes(b"\0" * size)
        return p
    return download


async def _probe(path, **kw):
    return video.Probe(duration=10.0, width=1280, height=720, has_audio=True)


@pytest.fixture
def small_clip(monkeypatch):
    monkeypatch.setattr(video, "download", _fake_download(1024))
    monkeypatch.setattr(video, "probe", _probe)


async def test_a_spoiler_is_uploaded_as_a_spoiler(cog, small_clip):
    msg = _Message()
    assert await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter",
                                      reply_errors=False, spoiler=True)
    assert msg.uploads == [("SPOILER_twitter.mp4", True)]


async def test_an_ordinary_link_is_not(cog, small_clip):
    msg = _Message()
    assert await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert msg.uploads == [("twitter.mp4", False)]
    assert msg.edits == [{"suppress": True}], "/convert still suppresses straight away"


# ------------------------------------------------- cleanup vs. running conversions
class _Ctx:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.mark.parametrize("run_cleanup", [
    lambda cog: Media.media_cleanup.callback(cog, _Ctx()),     # /media-cleanup, 60 s floor
    lambda cog: Media.cleanup_loop.coro(cog),                   # the periodic loop, 1 h floor
], ids=["media-cleanup", "cleanup-loop"])
async def test_cleanup_spares_the_source_of_a_conversion_still_encoding(cog, big_clip, monkeypatch, run_cleanup):
    """The download is written once, then only read by each ffmpeg pass of each rung, so
    by mtime it looked stale mid-ladder (or while queued for an encode slot) and was
    deleted; every later pass then failed and the user was told to try a shorter clip."""
    stale = cog.workdir / "dl_abandoned.mp4"
    stale.write_bytes(b"old")
    _age(stale, 2 * 3600)
    seen = {}

    async def fit_under(src, target, workdir, **kw):
        _age(src, 2 * 3600)                 # rung 1 has been running a long time
        await run_cleanup(cog)
        seen["src survived"] = src.exists()
        out = workdir / "enc_test.mp4"
        out.write_bytes(b"\0" * 1024)
        return out

    monkeypatch.setattr(video, "fit_under", fit_under)
    msg = _Message()
    assert await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert seen == {"src survived": True}
    assert msg.uploads == [("twitter.mp4", False)]
    assert not stale.exists(), "genuinely stale files are still reclaimed"
    assert cog._busy == {}, "a finished job releases its files"
    assert list(cog.workdir.iterdir()) == []


async def test_a_failed_job_releases_its_files_too(cog, big_clip, monkeypatch):
    async def boom(*a, **kw):
        raise video.VideoError("nope")

    monkeypatch.setattr(video, "fit_under", boom)
    assert not await cog.convert_and_send(_Message(), "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert cog._busy == {}


# ------------------------------------------------------------- /mediainfo counters
@pytest.fixture
def big_clip(monkeypatch):
    """A clip over the 10 MB limit, so it has to be compressed."""
    monkeypatch.setattr(video, "download", _fake_download(11 * MB))
    monkeypatch.setattr(video, "probe", _probe)


def _fit_under_ok():
    async def fit_under(src, target, workdir, **kw):
        out = workdir / "enc_test.mp4"
        out.write_bytes(b"\0" * 1024)
        return out
    return fit_under


async def test_a_successful_compression_is_counted(cog, big_clip, monkeypatch):
    monkeypatch.setattr(video, "fit_under", _fit_under_ok())
    assert await cog.convert_and_send(_Message(), "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert (cog.stats["ok"], cog.stats["compressed"], cog.stats["failed"]) == (1, 1, 0)


async def test_a_compression_that_fails_is_not_counted_as_compressed(cog, big_clip, monkeypatch):
    """The footer read "1 failed · 1 compressed" for a clip that was never compressed."""
    async def too_long(*a, **kw):
        raise video.VideoError("That video is 20:00 long — too long to fit in 10 MB")

    monkeypatch.setattr(video, "fit_under", too_long)
    assert not await cog.convert_and_send(_Message(), "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert (cog.stats["compressed"], cog.stats["failed"]) == (0, 1)


async def test_a_rejected_upload_is_not_counted_as_compressed(cog, big_clip, monkeypatch):
    monkeypatch.setattr(video, "fit_under", _fit_under_ok())
    msg = _Message()
    msg.reject_uploads = True
    assert not await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert (cog.stats["compressed"], cog.stats["failed"]) == (0, 1)


async def test_a_rejected_gif_is_not_counted_as_a_gif(cog, small_clip, monkeypatch):
    async def to_gif(src, target, workdir, **kw):
        out = workdir / "gif_test.gif"
        out.write_bytes(b"GIF89a")
        return out

    cog.cfg = type("Cfg", (_Cfg,), {"max_gif_seconds": 30})()
    monkeypatch.setattr(video, "should_gif", lambda info, cap: True)
    monkeypatch.setattr(video, "to_gif", to_gif)
    msg = _Message()
    msg.reject_uploads = True
    assert not await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert (cog.stats["gif"], cog.stats["failed"]) == (0, 1)

    msg.reject_uploads = False
    assert await cog.convert_and_send(msg, "https://x.com/a/status/1", "twitter", reply_errors=False)
    assert msg.uploads == [("twitter.gif", False)]
    assert (cog.stats["gif"], cog.stats["ok"]) == (1, 1)
