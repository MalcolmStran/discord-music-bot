"""ytdl helpers (shell quoting, header handling, format picking, error mapping), and
resolve/fetch_stream driven through a real YoutubeDL with offline fake extractors."""
import asyncio
import shlex
import threading

import pytest
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor, SearchInfoExtractor
from yt_dlp.utils import ExtractorError

from bot.core.ytdl import (
    FFMPEG_BEFORE,
    YTDL,
    TooLong,
    Track,
    _audio_format,
    _clean_header,
    _friendly,
    _shq,
    fmt_duration,
    looks_like_playlist,
    looks_like_url,
)


@pytest.mark.parametrize("value", [
    "User-Agent: Mozilla/5.0 (X11)\r\n",
    "Cookie: a='b'; c=\"d\"\r\n",
    "X: back\\slash and 'quote'\r\n",
    "",
    "spaces   and\ttabs",
])
def test_shq_survives_shlex(value):
    """discord.py shlex-splits before_options, so a header blob has to come back byte for
    byte or ffmpeg gets a mangled (or extra) argument."""
    argv = shlex.split(f"{FFMPEG_BEFORE} -headers {_shq(value)}")
    assert argv[-1] == value
    assert argv[-2] == "-headers"


def test_shq_cannot_inject_an_extra_argument():
    argv = shlex.split("x " + _shq("a' -evil-flag '"))
    assert len(argv) == 2 and argv[1] == "a' -evil-flag '"


@pytest.mark.parametrize("raw,expected", [
    ("Mozilla/5.0", "Mozilla/5.0"),
    ("Mozilla\r\nX-Injected: 1", "MozillaX-Injected: 1"),
    ("  padded  ", "padded"),
])
def test_clean_header_strips_crlf(raw, expected):
    """Header values are concatenated into ffmpeg's -headers blob; a CR/LF inside one
    would start a new header line."""
    assert _clean_header(raw) == expected


def test_audio_format_prefers_the_audio_stream():
    """requested_formats[0] is the *video* half of a merged selection, so taking it
    blindly handed the audio player a video-only URL."""
    info = {"requested_formats": [
        {"acodec": "none", "vcodec": "avc1", "url": "VIDEO"},
        {"acodec": "opus", "vcodec": "none", "url": "AUDIO"},
    ]}
    assert _audio_format(info)["url"] == "AUDIO"


def test_audio_format_falls_back_to_the_first_entry():
    info = {"requested_formats": [{"vcodec": "avc1", "url": "ONLY"}]}
    assert _audio_format(info)["url"] == "ONLY"


def test_audio_format_handles_a_single_format():
    assert _audio_format({"url": "direct"}) is None


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: Private video. Sign in if you've been granted access", "That video is private."),
    ("ERROR: Video unavailable", "That video is unavailable."),
    ("ERROR: Sign in to confirm your age", "That video is age-restricted (cookies needed)."),
    ("ERROR: Unsupported URL: https://example.com/x", "Unsupported URL."),
    ("ERROR: [youtube] x: Sign in to confirm your age", "That video is age-restricted (cookies needed)."),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "That source requires a login (cookies needed)."),
    ("ERROR: Requested format is not available", "No playable audio format for that video."),
])
def test_friendly_known_cases(raw, expected):
    assert _friendly(raw) == expected


def test_friendly_strips_the_ytdlp_prefix_and_bounds_length():
    out = _friendly("ERROR: [youtube] dQw4w9WgXcQ: " + "x" * 500)
    assert not out.startswith("ERROR")
    assert len(out) <= 200


def test_friendly_never_returns_empty():
    assert _friendly("") and _friendly("ERROR: ")


@pytest.mark.parametrize("q,is_url,is_playlist", [
    ("https://youtu.be/x", True, False),
    ("https://www.youtube.com/playlist?list=PL1", True, True),
    ("https://www.youtube.com/watch?v=a&list=PL1", True, True),
    ("https://soundcloud.com/u/sets/mix", True, True),
    ("never gonna give you up", False, False),
    ("  https://x/  ", True, False),
])
def test_url_shape_helpers(q, is_url, is_playlist):
    assert looks_like_url(q) is is_url
    assert looks_like_playlist(q) is is_playlist


@pytest.mark.parametrize("secs,text", [
    (None, "live/unknown"), (0, "live/unknown"), (9, "0:09"),
    (65, "1:05"), (600, "10:00"), (3661, "1:01:01"),
])
def test_fmt_duration(secs, text):
    assert fmt_duration(secs) == text


def test_track_link_prefers_the_webpage_url():
    assert Track(title="t", webpage_url="https://y/1", source_url="https://s/1").link == "https://y/1"
    assert Track(title="t", webpage_url="", source_url="https://s/1").link == "https://s/1"
    assert Track(title="t", webpage_url="").link == ""


# --- resolve / fetch_stream through a real YoutubeDL ------------------------------------
# Only the site extractors are fake (no network); option handling, flat extraction,
# playlistend, the playlist recursion guard and error wrapping are yt-dlp's own.

class _FakeVideoIE(InfoExtractor):
    _VALID_URL = r"https://fake\.test/v/(?P<id>\w+)"
    IE_NAME = "fakevideo"
    extractions = 0

    def _real_extract(self, url):
        type(self).extractions += 1
        vid = self._match_id(url)
        return {"id": vid, "title": f"t{vid}", "duration": 4 * 3600 if vid.startswith("long") else 200,
                "url": f"https://cdn.fake.test/{vid}.m4a", "ext": "m4a", "acodec": "opus", "vcodec": "none"}


class _FakeChannelIE(InfoExtractor):
    """A collection URL with no playlist hint in it, like youtube.com/@name."""
    _VALID_URL = r"https://fake\.test/@chan"
    IE_NAME = "fakechannel"

    def _real_extract(self, url):
        return self.playlist_result(
            (self.url_result(f"https://fake.test/v/{i}", _FakeVideoIE) for i in range(500)), "chan", "Channel")


class _FakeSearchIE(SearchInfoExtractor):
    """Shaped like YoutubeSearchIE: a lazy generator consumed inside process_ie_result."""
    _SEARCH_KEY = "ytsearch"
    IE_NAME = "fakesearch"
    barrier = None

    def _search_results(self, query):
        if query == "zero hits":
            return
        if self.barrier:
            self.barrier.wait()          # both identical searches must be in flight at once
        yield self.url_result("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Youtube", "dQw4w9WgXcQ",
                              query, duration=213)


class _FakeErrorIE(InfoExtractor):
    _VALID_URL = r"https://fake\.test/(?P<id>private|agegate)"
    IE_NAME = "fakeerror"

    def _real_extract(self, url):
        if self._match_id(url) == "private":
            raise ExtractorError("Private video. Sign in if you've been granted access to this video", expected=True)
        raise ExtractorError("Sign in to confirm your age. This video may be inappropriate", expected=True)


class _FakeCrashyIE(InfoExtractor):
    """Breaks with a non-ExtractorError on the default client; the android client works."""
    _VALID_URL = r"https://fake\.test/crashy"
    IE_NAME = "fakecrashy"

    def _real_extract(self, url):
        if "android" not in str(self._downloader.params.get("extractor_args")):
            raise TypeError("'NoneType' object is not subscriptable")
        return {"id": "c", "title": "crashy", "url": "https://cdn.fake.test/c.m4a", "ext": "m4a",
                "acodec": "opus", "vcodec": "none"}


class _FakeSitesYDL(yt_dlp.YoutubeDL):
    """A real YoutubeDL that tries the fake extractors before the built-in ones."""

    def __init__(self, params=None, auto_init=True):
        super().__init__(params, auto_init=False)
        for ie in (_FakeSearchIE, _FakeChannelIE, _FakeVideoIE, _FakeErrorIE, _FakeCrashyIE):
            self.add_info_extractor(ie())
        self.add_default_info_extractors()


@pytest.fixture
def fake_sites(monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeSitesYDL)
    monkeypatch.setattr(_FakeVideoIE, "extractions", 0)
    monkeypatch.setattr(_FakeSearchIE, "barrier", None)


def _no_probe(ytdl):
    probed = []

    async def probe(url, headers):
        probed.append(url)
        return True
    ytdl._url_streamable = probe
    return probed


async def test_a_channel_url_resolves_flat_and_capped(fake_sites):
    """A collection URL without a playlist hint used to be fully extracted entry by entry,
    with no MAX_QUEUE_SIZE cap: hours of requests on a worker thread for a big channel."""
    y = YTDL(max_playlist=50)
    tracks = await y.resolve("https://fake.test/@chan")
    assert len(tracks) == 50
    assert _FakeVideoIE.extractions == 0


async def test_a_single_video_url_is_still_fully_extracted(fake_sites):
    tracks = await YTDL(max_playlist=50).resolve("https://fake.test/v/7")
    assert [(t.title, t.duration) for t in tracks] == [("t7", 200)]
    assert _FakeVideoIE.extractions == 1


async def test_fetch_stream_does_not_walk_a_queued_collection(fake_sites):
    """A flat listing can yield entries that are collections themselves (channel tabs);
    fetch_stream must fail fast on one, not extract every video in it in the player loop."""
    with pytest.raises(LookupError, match="No playable stream found"):
        await YTDL().fetch_stream(Track(title="Channel - Videos", webpage_url="https://fake.test/@chan"))
    assert _FakeVideoIE.extractions == 0


async def test_fetch_stream_fills_in_what_a_flat_entry_lacked(fake_sites):
    t = Track(title="Unknown title", webpage_url="https://fake.test/v/3")
    await YTDL().fetch_stream(t)
    assert (t.title, t.duration, t.stream_url) == ("t3", 200, "https://cdn.fake.test/3.m4a")


async def test_concurrent_identical_searches_both_resolve(fake_sites):
    """yt-dlp's playlist recursion guard is per YoutubeDL instance and a search is a
    playlist: on a shared instance the second of two identical /play searches got None,
    i.e. "No results."."""
    _FakeSearchIE.barrier = threading.Barrier(2, timeout=2)
    y = YTDL()
    results = await asyncio.gather(y.resolve("never gonna give you up", 1),
                                   y.resolve("never gonna give you up", 2), return_exceptions=True)
    assert [[t.title for t in r] if isinstance(r, list) else r for r in results] == \
        [["never gonna give you up"]] * 2


async def test_concurrent_identical_spotify_matches_both_resolve(fake_sites):
    _FakeSearchIE.barrier = threading.Barrier(2, timeout=2)
    y = YTDL()
    tracks = [Track(title="Song — A", webpage_url="", search_query="A - Song") for _ in range(2)]
    results = await asyncio.gather(*(y._resolve_search(t) for t in tracks), return_exceptions=True)
    assert results == [None, None]
    assert [t.webpage_url for t in tracks] == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] * 2


async def test_a_search_with_no_hits_says_no_results(fake_sites):
    with pytest.raises(LookupError) as exc:
        await YTDL().resolve("zero hits")
    assert str(exc.value) == "No results."         # not "Playlist is empty or unavailable."


async def test_resolve_reports_why_a_video_cannot_be_used(fake_sites):
    """ignoreerrors made yt-dlp swallow the ExtractorError and return None, so a private
    video was answered with "No results."."""
    with pytest.raises(LookupError) as exc:
        await YTDL().resolve("https://fake.test/private")
    assert str(exc.value) == "That video is private."


async def test_fetch_stream_reports_why_a_video_cannot_be_used(fake_sites):
    with pytest.raises(LookupError) as exc:
        await YTDL().fetch_stream(Track(title="x", webpage_url="https://fake.test/agegate"))
    assert str(exc.value) == "That video is age-restricted (cookies needed)."


async def test_an_extractor_crash_still_falls_back_to_the_next_client(fake_sites):
    """Without ignoreerrors a non-ExtractorError escapes extract_info as itself; it must not
    end the client loop before the android client gets its turn."""
    y = YTDL()
    _no_probe(y)
    t = Track(title="x", webpage_url="https://fake.test/crashy", extractor="youtube")
    await y.fetch_stream(t)
    assert t.stream_url == "https://cdn.fake.test/c.m4a"


async def test_fetch_stream_rejects_a_track_over_the_limit_before_probing(fake_sites):
    """Flat entries and Spotify matches have no trustworthy length until fetch_stream, so
    MAX_SONG_DURATION has to be enforced here as well."""
    y = YTDL(max_duration=7200)
    probed = _no_probe(y)
    t = Track(title="Unknown title", webpage_url="https://fake.test/v/long1", extractor="youtube")
    with pytest.raises(TooLong, match=r"^Too long \(4:00:00; max 2:00:00\)\.$"):
        await y.fetch_stream(t)
    assert probed == [] and t.stream_url is None
    assert issubclass(TooLong, LookupError)
    ok = Track(title="Unknown title", webpage_url="https://fake.test/v/3", extractor="youtube")
    await y.fetch_stream(ok)                        # under the limit: plays
    assert ok.stream_url


async def test_no_duration_limit_by_default(fake_sites):
    y = YTDL()
    _no_probe(y)
    t = Track(title="x", webpage_url="https://fake.test/v/long1", extractor="youtube")
    await y.fetch_stream(t)
    assert t.duration == 4 * 3600 and t.stream_url


async def test_resolving_never_rewrites_the_cookie_file(fake_sites, tmp_path):
    """Each resolve builds its own YoutubeDL; closing one saves the jar back to the cookie
    file, which raced concurrent resolves and failed on a read-only mount."""
    jar = tmp_path / "cookies.txt"
    text = ("# Netscape HTTP Cookie File\n# the operator's own notes\n"
            ".fake.test\tTRUE\t/\tFALSE\t2147483647\tsid\tabc\n")
    jar.write_text(text)
    y = YTDL(cookies_file=jar)
    assert y._opts["cookiefile"] == str(jar)
    await y.resolve("https://fake.test/v/1")
    await y.resolve("never gonna give you up")
    assert jar.read_text() == text
