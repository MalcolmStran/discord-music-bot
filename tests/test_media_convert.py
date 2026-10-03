"""convert_and_send end to end: the real cog code, with Discord and the network faked.

video.download / probe / fit_under / to_gif are replaced by stand-ins that write real files
into the real work dir, so what the cog uploads, counts and leaves on disk is the real thing.
"""
from pathlib import Path

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

    async def reply(self, content=None, *, file=None, **kw):
        if file is not None:
            self.uploads.append((file.filename, file.spoiler))
            file.close()
        else:
            self.replies.append(content)
        return _Status()

    async def add_reaction(self, emoji):
        pass

    async def remove_reaction(self, emoji, member):
        pass

    async def edit(self, **kw):
        self.edits.append(kw)


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
