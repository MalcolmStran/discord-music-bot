"""Spotify metadata parsing, from a fixture of the embed page's __NEXT_DATA__ payload."""
import json

import pytest

from bot.core.spotify import Spotify, _api_track, _cover, _track, is_spotify, parse


@pytest.mark.parametrize("url,expected", [
    ("https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8?si=abc", ("track", "4PTG3Z6ehGkBFwjybzWkR8")),
    ("https://open.spotify.com/intl-de/album/5ht7ItJgpBH7W6vJ5BqpPr", ("album", "5ht7ItJgpBH7W6vJ5BqpPr")),
    ("https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("play.spotify.com/artist/1vCWHaC5f2uS3yhpwWbIA6", ("artist", "1vCWHaC5f2uS3yhpwWbIA6")),
    ("https://youtu.be/x", None),
    ("https://open.spotify.com/track/tooshort", None),
])
def test_parse(url, expected):
    assert parse(url) == expected
    assert is_spotify(url) is (expected is not None)


def test_track_builds_a_youtube_search_query():
    t = _track("Bohemian Rhapsody", "Queen", 354_000, "cover.jpg", "https://open.spotify.com/track/x", 42)
    assert t.search_query == "Queen - Bohemian Rhapsody"
    assert t.title == "Bohemian Rhapsody — Queen"
    assert t.duration == 354                    # ms -> s
    assert t.webpage_url == "" and t.extractor == "spotify"
    assert t.requester_id == 42 and t.thumbnail == "cover.jpg"


def test_track_without_an_artist():
    t = _track("Untitled", "", None, None, "u", None)
    assert t.search_query == "Untitled" and t.title == "Untitled" and t.duration == 0


def test_track_with_no_name_at_all():
    assert _track(None, "", None, None, "u", None).title == "Unknown"


def test_api_track_reads_the_web_api_shape():
    payload = {
        "name": "Song",
        "artists": [{"name": "A"}, {"name": "B"}],
        "duration_ms": 12_000,
        "album": {"images": [{"url": "big.jpg"}, {"url": "small.jpg"}]},
        "external_urls": {"spotify": "https://open.spotify.com/track/z"},
    }
    t = _api_track(payload, requester_id=7)
    assert t.search_query == "A, B - Song"
    assert t.duration == 12 and t.thumbnail == "big.jpg" and t.source_url.endswith("/track/z")


def test_api_track_survives_missing_fields():
    t = _api_track({"name": "S"}, requester_id=None)
    assert t.title == "S" and t.duration == 0 and t.thumbnail is None


@pytest.mark.parametrize("entity,expected", [
    ({"visualIdentity": {"image": [{"url": "a"}, {"url": "b"}]}}, "b"),
    ({"coverArt": {"sources": [{"url": "c"}]}}, "c"),
    ({}, None),
    ({"visualIdentity": {}}, None),
])
def test_cover_extraction(entity, expected):
    assert _cover(entity) == expected


EMBED_ENTITY = {
    "props": {"pageProps": {"state": {"data": {"entity": {
        "name": "Greatest Hits",
        "visualIdentity": {"image": [{"url": "small.jpg"}, {"url": "large.jpg"}]},
        "trackList": [
            {"uri": "spotify:track:1111111111111111111111", "title": "One", "subtitle": "Artist A", "duration": 60_000},
            {"uri": "spotify:track:2222222222222222222222", "title": "Two", "subtitle": "Artist B", "duration": 90_000},
        ],
    }}}}}
}


class _FakeResponse:
    def __init__(self, html, status=200):
        self._html, self.status = html, status

    async def text(self):
        return self._html

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, html, status=200):
        self._html, self._status = html, status

    def get(self, url, **kw):
        return _FakeResponse(self._html, self._status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _embed_html(payload):
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload) + "</script></body></html>")


async def test_via_embed_parses_a_playlist(monkeypatch):
    """Drives the real _via_embed. The previous version of this test re-implemented the
    key walk on a literal dict, so breaking the parser left it green."""
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession(_embed_html(EMBED_ENTITY)))
    tracks = await Spotify()._via_embed("playlist", "37i9dQZF1DXcBWIGoYBM5M", requester_id=9)
    assert [t.search_query for t in tracks] == ["Artist A - One", "Artist B - Two"]
    assert [t.duration for t in tracks] == [60, 90]
    assert all(t.thumbnail == "large.jpg" for t in tracks)
    assert all(t.extractor == "spotify" and t.requester_id == 9 for t in tracks)
    assert tracks[0].source_url.endswith("/track/1111111111111111111111")


async def test_via_embed_parses_a_single_track(monkeypatch):
    payload = {"props": {"pageProps": {"state": {"data": {"entity": {
        "name": "Solo", "artists": [{"name": "A"}], "duration": 5000,
        "visualIdentity": {"image": [{"url": "c.jpg"}]}}}}}}}
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession(_embed_html(payload)))
    tracks = await Spotify()._via_embed("track", "4PTG3Z6ehGkBFwjybzWkR8", requester_id=None)
    assert len(tracks) == 1
    assert tracks[0].search_query == "A - Solo" and tracks[0].duration == 5


async def test_via_embed_reports_a_layout_change(monkeypatch):
    """A Spotify redesign must surface as a clear LookupError, not a raw KeyError."""
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession(_embed_html({"props": {"nope": 1}})))
    with pytest.raises(LookupError, match="Couldn't parse"):
        await Spotify()._via_embed("track", "4PTG3Z6ehGkBFwjybzWkR8", None)


async def test_via_embed_reports_a_missing_script_block(monkeypatch):
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession("<html>nothing here</html>"))
    with pytest.raises(LookupError, match="layout changed"):
        await Spotify()._via_embed("track", "4PTG3Z6ehGkBFwjybzWkR8", None)


async def test_via_embed_reports_an_http_error(monkeypatch):
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession("", status=404))
    with pytest.raises(LookupError, match="404"):
        await Spotify()._via_embed("track", "4PTG3Z6ehGkBFwjybzWkR8", None)


async def test_via_embed_respects_max_tracks(monkeypatch):
    monkeypatch.setattr("bot.core.spotify.aiohttp.ClientSession",
                        lambda *a, **kw: _FakeSession(_embed_html(EMBED_ENTITY)))
    tracks = await Spotify(max_tracks=1)._via_embed("playlist", "37i9dQZF1DXcBWIGoYBM5M", None)
    assert len(tracks) == 1


async def test_artist_links_are_rejected_before_any_network_call():
    with pytest.raises(LookupError, match="Artist links"):
        await Spotify().resolve("https://open.spotify.com/artist/1vCWHaC5f2uS3yhpwWbIA6")


async def test_non_spotify_link_is_rejected():
    with pytest.raises(LookupError, match="Not a Spotify link"):
        await Spotify().resolve("https://youtu.be/x")
