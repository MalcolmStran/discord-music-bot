"""Every link form the cog accepts must still reach one of video.ALLOWED_EXTRACTORS.

Checked against the installed yt-dlp's own URL patterns, because download() no longer falls
back to the generic extractor: a form nothing on the list matches now fails outright.
"""
import pytest
import yt_dlp

from bot.cogs.media import classify, normalise
from bot.core import video


@pytest.fixture(scope="module")
def allowed():
    ydl = yt_dlp.YoutubeDL({"quiet": True, "allowed_extractors": video.ALLOWED_EXTRACTORS})
    return list(ydl._ies.values())


def _handled(ies, url):
    return [ie.IE_NAME for ie in ies if ie.suitable(url)]


@pytest.mark.parametrize("posted", [
    "https://www.tiktok.com/@u/video/7123456789012345678",
    "https://tiktok.com/@u/video/7123456789012345678",          # bare host
    "https://m.tiktok.com/@u/video/7123456789012345678?lang=en",  # mobile host
    "https://vm.tiktok.com/ZM1abcd/",
    "https://vt.tiktok.com/ZS1abcd/",
    "https://www.tiktok.com/t/ZT1abcd/",
    "https://tiktok.com/t/ZT1abcd/",
    "https://vxtiktok.com/@u/video/7123456789012345678",          # fixer, via /convert
    "https://x.com/a/status/1234567890",
    "https://twitter.com/a/status/1234567890?s=20",
    "https://mobile.twitter.com/a/status/1234567890",
    "https://www.x.com/a/status/1234567890",
    "https://x.com/i/status/1234567890",
    "https://x.com/a/status/1234567890/video/1",
    "https://fxtwitter.com/a/status/1234567890",                  # fixer, via /convert
])
def test_every_accepted_post_link_reaches_an_allowed_extractor(allowed, posted):
    kind = classify(posted)
    assert kind is not None
    url = normalise(posted, kind)
    assert _handled(allowed, url), f"{url} would fail with 'No suitable extractor'"


@pytest.mark.parametrize("raw,expected", [
    ("https://tiktok.com/@u/video/1", "https://www.tiktok.com/@u/video/1"),
    ("https://m.tiktok.com/@u/video/1?lang=en", "https://www.tiktok.com/@u/video/1?lang=en"),
    ("http://TikTok.com/t/ZT1/).", "https://www.tiktok.com/t/ZT1/"),
])
def test_bare_and_mobile_tiktok_hosts_move_to_www(raw, expected):
    """yt-dlp's TikTok extractors only match www.tiktok.com; these used to work only
    because the generic extractor followed TikTok's redirect."""
    assert normalise(raw, "tiktok") == expected


@pytest.mark.parametrize("url", [
    "https://vm.tiktok.com/ZM1/",       # shorteners have their own extractor
    "https://vt.tiktok.com/ZS1/",
    "https://www.tiktok.com/@u/video/1",
])
def test_other_tiktok_hosts_are_left_alone(url):
    assert normalise(url, "tiktok") == url


@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@someartist",        # tiktok:user
    "https://www.tiktok.com/@u/live",            # tiktok:live
    "https://x.com/i/broadcasts/1ZkJzbdvLgyJv",  # twitter:broadcast
    "https://x.com/i/spaces/1zqKVPlQNApJB",      # twitter:spaces
])
def test_profile_live_and_broadcast_links_are_not_extracted(allowed, url):
    """A "follow me" link used to get one of that account's videos uploaded under it."""
    assert _handled(allowed, normalise(url, classify(url))) == []
