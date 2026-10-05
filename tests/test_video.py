"""Encoder planning and ffmpeg argument construction (no ffmpeg needed), and download()
driven through the real yt-dlp against a loopback server."""
import asyncio
import itertools
import os
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yt_dlp
import yt_dlp.cookies
import yt_dlp.extractor.tiktok as yt_tiktok
import yt_dlp.extractor.twitter as yt_twitter

from bot.core import video
from bot.core.video import (
    GIF_LADDER,
    LADDER,
    MIN_VIDEO_BITRATE,
    EncodeStep,
    GifStep,
    Probe,
    _friendly,
    _is_too_big,
    _mmss,
    build_gif_args,
    build_pass_args,
    gif_scale,
    max_fittable_duration,
    plan_step,
    should_gif,
)

MB = 1024 * 1024


def test_short_clip_is_encodable_on_every_rung():
    for step in LADDER:
        plan = plan_step(step, 10 * MB, duration=200, has_audio=True)
        assert plan is not None
        vbr, abr = plan
        assert vbr >= MIN_VIDEO_BITRATE and abr == 64_000


@pytest.mark.parametrize("duration", [600, 3600, 7200])
def test_long_clip_is_rejected_instead_of_encoded(duration):
    """Regression: a negative computed bitrate was clamped up to a floor and encoded
    anyway, so all three rungs ran — six ffmpeg passes holding the encode semaphore —
    to produce files many times over the limit before finally giving up."""
    assert all(plan_step(step, 10 * MB, duration, True) is None for step in LADDER)


def test_a_planned_bitrate_actually_fits_the_limit():
    limit, duration = 10 * MB, 200
    for step in LADDER:
        vbr, abr = plan_step(step, limit, duration, True)
        predicted = (vbr + abr) * duration / 8
        assert predicted <= limit


def test_silent_video_spends_nothing_on_audio():
    vbr_audio, abr_audio = plan_step(LADDER[0], 10 * MB, 200, has_audio=True)
    vbr_silent, abr_silent = plan_step(LADDER[0], 10 * MB, 200, has_audio=False)
    assert abr_silent == 0 and abr_audio > 0
    assert vbr_silent > vbr_audio


def test_zero_duration_is_not_plannable():
    assert plan_step(LADDER[0], 10 * MB, 0, True) is None


def test_max_fittable_duration_is_the_boundary():
    limit = 10 * MB
    longest = max_fittable_duration(limit, has_audio=True)
    assert any(plan_step(s, limit, longest - 1, True) for s in LADDER)
    assert all(plan_step(s, limit, longest + 1, True) is None for s in LADDER)


def test_no_ladder_rung_puts_opus_in_an_mp4():
    """libopus in an .mp4 is poorly supported by players; every rung must use aac."""
    assert {s.acodec for s in LADDER} == {"aac"}


def test_x265_gets_an_explicit_stats_file():
    """libx265 does not read ffmpeg's -passlogfile. Without stats= and pass= in
    -x265-params, x265 reports neither stats-write nor stats-read and the two-pass
    silently becomes two independent single-pass encodes (verified against ffmpeg 6.1.1).
    The explicit path also keeps concurrent jobs off x265's default ./x265_2pass.log."""
    step = EncodeStep("libx265", "aac", 480, 0.88, "ultrafast")
    p1, p2 = build_pass_args("in.mp4", "out.mp4", step, 300_000, 64_000, 1080, "/tmp/job_pass")
    for argv in (p1, p2):
        i = argv.index("-x265-params")
        assert "stats=/tmp/job_pass-x265.log" in argv[i + 1]
    assert "pass=1" in p1[p1.index("-x265-params") + 1]
    assert "pass=2" in p2[p2.index("-x265-params") + 1]


def test_x265_stats_path_is_unique_per_job():
    """Two concurrent encodes must not share a stats file."""
    step = EncodeStep("libx265", "aac", 480, 0.88, "ultrafast")
    a = build_pass_args("i", "a.mp4", step, 1, 1, 1080, "/tmp/enc_aaaa_pass")
    b = build_pass_args("i", "b.mp4", step, 1, 1, 1080, "/tmp/enc_bbbb_pass")

    def stats(argv):
        return next(p for p in argv[argv.index("-x265-params") + 1].split(":") if p.startswith("stats="))

    assert stats(a[0]) != stats(b[0])
    assert "x265_2pass.log" not in stats(a[0])


def test_x264_does_not_get_x265_params():
    p1, p2 = build_pass_args("in.mp4", "out.mp4", LADDER[0], 300_000, 64_000, 720, "/tmp/j_pass")
    assert "-x265-params" not in p1 and "-x265-params" not in p2


def test_both_passes_share_identical_video_settings():
    """Two-pass only works if pass 1 and pass 2 encode with the same video parameters."""
    p1, p2 = build_pass_args("in.mp4", "out.mp4", LADDER[1], 250_000, 64_000, 1080, "/tmp/j_pass")

    def video_opts(argv):
        """Collect EVERY occurrence: ffmpeg honours the last one, and list.index() only
        finds the first, so a later overriding flag used to slip past this comparison."""
        out = {}
        for flag in ("-c:v", "-b:v", "-maxrate", "-bufsize", "-preset", "-pix_fmt", "-vf"):
            values = [argv[i + 1] for i, a in enumerate(argv) if a == flag]
            if values:
                out[flag] = values
        return out

    assert video_opts(p1) == video_opts(p2)
    # and a flag appended to only one pass must be detected
    assert video_opts(p1) != video_opts([*p2, "-bufsize", "1"])


def test_downscale_only_applies_when_the_source_is_taller():
    tall = build_pass_args("i", "o", LADDER[1], 1, 1, 1080, "/tmp/p")[1]
    short = build_pass_args("i", "o", LADDER[1], 1, 1, 360, "/tmp/p")[1]
    assert "-vf" in tall and "scale=-2:480" in tall
    assert "-vf" not in short          # never upscale a small source


def test_pass_one_writes_nothing_and_skips_audio():
    p1, _ = build_pass_args("in.mp4", "out.mp4", LADDER[0], 300_000, 64_000, 720, "/tmp/j")
    assert "-an" in p1 and p1[p1.index("-f") + 1] == "null"
    assert "out.mp4" not in p1


def test_silent_source_gets_an_explicit_no_audio_flag():
    _, p2 = build_pass_args("in.mp4", "out.mp4", LADDER[0], 300_000, 0, 720, "/tmp/j")
    assert "-an" in p2 and "-c:a" not in p2


@pytest.mark.parametrize("msg", [
    "ERROR: File is larger than max-filesize (600.00MiB > 500.00MiB)",
    "requested format is larger than max-filesize",
])
def test_too_big_detection(msg):
    assert _is_too_big(msg)


def test_too_big_does_not_match_unrelated_errors():
    assert not _is_too_big("ERROR: Unsupported URL: https://example.com")


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: Unsupported URL: https://x", "That link isn't supported."),
    # what yt-dlp says for a link ALLOWED_EXTRACTORS rules out (a profile, a broadcast)
    ("ERROR: No suitable extractor found for URL https://www.tiktok.com/@u", "That link isn't supported."),
    ("ERROR: this post is private", "That post is private/age-gated (needs cookies)."),
    ("HTTP Error 404: Not Found", "That post doesn't exist (or was deleted)."),
    ("something else entirely", "Couldn't download that video."),
])
def test_friendly_messages(raw, expected):
    assert _friendly(raw) == expected


@pytest.mark.parametrize("secs,text", [(65, "1:05"), (3600, "1:00:00"), (0, "0:00"), (419, "6:59")])
def test_mmss(secs, text):
    assert _mmss(secs) == text


# ------------------------------------------------- picking the right download output
def test_merged_output_wins_over_a_per_format_fragment(tmp_path):
    """yt-dlp writes "<stem>.f137.mp4" beside the merged "<stem>.mp4". Sorting the raw glob
    put the fragment first ("f" < "m"), so the video-only file was uploaded and _sweep()
    then deleted the real merge."""
    from bot.core.video import _output_candidates

    (tmp_path / "dl_x.f137.mp4").write_bytes(b"VIDEO-ONLY")
    (tmp_path / "dl_x.f140.m4a").write_bytes(b"AUDIO-ONLY")
    (tmp_path / "dl_x.mp4").write_bytes(b"MERGED")
    picked = _output_candidates(tmp_path, "dl_x")
    assert picked[0].name == "dl_x.mp4"
    assert picked[0].read_bytes() == b"MERGED"


def test_fragment_is_still_used_when_nothing_was_merged(tmp_path):
    """A single-format download never produces a merge; the fragment is all there is."""
    from bot.core.video import _output_candidates

    (tmp_path / "dl_y.f22.mp4").write_bytes(b"ONLY")
    assert [p.name for p in _output_candidates(tmp_path, "dl_y")] == ["dl_y.f22.mp4"]


def test_non_video_and_partial_files_are_never_candidates(tmp_path):
    from bot.core.video import _output_candidates

    (tmp_path / "dl_z.mp4.part").write_bytes(b"partial")
    (tmp_path / "dl_z.f140.m4a").write_bytes(b"audio")
    (tmp_path / "dl_z.info.json").write_bytes(b"{}")
    assert _output_candidates(tmp_path, "dl_z") == []


def test_other_jobs_are_not_candidates(tmp_path):
    from bot.core.video import _output_candidates

    (tmp_path / "dl_mine.mp4").write_bytes(b"mine")
    (tmp_path / "dl_other.mp4").write_bytes(b"someone else's job")
    assert [p.name for p in _output_candidates(tmp_path, "dl_mine")] == ["dl_mine.mp4"]


# ------------------------------------------------------------- silent clip -> GIF

def _probe(duration=8.0, width=1280, height=720, has_audio=False):
    return Probe(duration=duration, width=width, height=height, has_audio=has_audio)


@pytest.mark.parametrize("has_audio,duration,cap,expected", [
    (False, 8.0, 30, True),       # the whole point: a short silent clip
    (True, 8.0, 30, False),       # has sound, so it stays a video
    (False, 45.0, 30, False),     # too long to be a sane GIF
    (False, 30.0, 30, True),      # exactly at the cap is allowed
    (False, 8.0, 0, False),       # 0 disables the feature
    (False, 8.0, -1, False),
    (False, 0.0, 30, False),      # unknown duration
])
def test_should_gif(has_audio, duration, cap, expected):
    assert should_gif(_probe(duration=duration, has_audio=has_audio), cap) is expected


def test_gif_scale_bounds_the_longest_edge_not_the_width():
    """A portrait TikTok capped on width would still be 480x853; the cap must apply to
    whichever edge is longer."""
    assert gif_scale(1280, 720, 480) == (480, -1)      # landscape -> width driven
    assert gif_scale(1080, 1920, 480) == (-1, 480)     # portrait  -> height driven
    assert gif_scale(600, 600, 480) == (480, -1)       # square    -> either, width wins


@pytest.mark.parametrize("w,h,side", [
    (320, 240, 480),      # already smaller than the cap
    (480, 270, 480),      # exactly at the cap
    (1280, 720, None),    # no cap configured
    (0, 0, 480),          # unknown dimensions
])
def test_gif_scale_skips_pointless_rescaling(w, h, side):
    assert gif_scale(w, h, side) is None


def test_gif_args_carry_fps_palette_and_loop():
    step = GifStep(15, 400, 128)
    pal, render = build_gif_args("in.mp4", "out.gif", "p.png", step, 1280, 720)
    assert f"fps={step.fps}" in pal[pal.index("-vf") + 1]
    assert f"max_colors={step.colors}" in pal[pal.index("-vf") + 1]
    assert pal[-1] == "p.png"
    lavfi = render[render.index("-lavfi") + 1]
    assert f"fps={step.fps}" in lavfi and "paletteuse" in lavfi
    assert render[render.index("-loop") + 1] == "0"    # GIFs must loop forever
    assert "-an" in render and render[-1] == "out.gif"


def test_gif_args_use_the_palette_as_the_second_input():
    """paletteuse reads [1:v]; if the palette is not input 1 the render silently uses the
    wrong stream."""
    step = GIF_LADDER[0]
    _, render = build_gif_args("in.mp4", "out.gif", "p.png", step, 1280, 720)
    assert render.count("-i") == 2
    assert render[render.index("-i") + 1] == "in.mp4"
    assert render[-render[::-1].index("-i")] == "p.png"
    assert "[1:v]" in render[render.index("-lavfi") + 1]


def test_gif_args_omit_scaling_for_an_already_small_clip():
    step = GifStep(15, 480, 128)
    pal, render = build_gif_args("in.mp4", "out.gif", "p.png", step, 320, 240)
    # compare the filter CHAIN only: paletteuse carries an unrelated "bayer_scale=5"
    assert pal[pal.index("-vf") + 1] == f"fps={step.fps},palettegen=max_colors={step.colors}:stats_mode=diff"
    chain = render[render.index("-lavfi") + 1].split(" [x];")[0]
    assert chain == f"fps={step.fps}"


def test_gif_args_scale_portrait_by_height():
    step = GifStep(15, 400, 128)
    pal, _ = build_gif_args("in.mp4", "out.gif", "p.png", step, 1080, 1920)
    assert "scale=-1:400" in pal[pal.index("-vf") + 1]


def test_both_gif_passes_share_the_same_filter_chain():
    """The palette must be built from exactly the frames it will be applied to."""
    step = GIF_LADDER[2]
    pal, render = build_gif_args("in.mp4", "out.gif", "p.png", step, 1920, 1080)
    chain = pal[pal.index("-vf") + 1].split(",palettegen")[0]
    assert render[render.index("-lavfi") + 1].startswith(chain + " [x]")


def test_gif_ladder_degrades_monotonically():
    """Each rung must be cheaper than the last, or the search cannot converge."""
    for a, b in itertools.pairwise(GIF_LADDER):
        assert b.fps <= a.fps
        assert (b.max_side or 10**9) <= (a.max_side or 10**9)
        assert b.colors <= a.colors
    assert all(2 <= s.colors <= 256 for s in GIF_LADDER)
    assert all(s.fps > 0 for s in GIF_LADDER)


# ------------------------------------------------------------------ probe()
async def test_ffprobe_errors_stay_in_the_log_not_the_channel(tmp_path, monkeypatch, caplog):
    """/convert replies with the VideoError text, and ffprobe's stderr opens with the
    server's absolute temp path."""
    shim = tmp_path / "bin" / "ffprobe"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nfor a; do last=$a; done\n'
                    'echo "$last: Invalid data found when processing input" >&2\nexit 1\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "media_tmp" / "dl_3f9a1c2b7e.mp4"
    with pytest.raises(video.VideoError) as e, caplog.at_level("WARNING", logger="bot.core.video"):
        await video.probe(src)
    assert str(e.value) == "That file isn't a readable video."
    assert "Invalid data found" in caplog.text, "the operator still gets the reason"


# ------------------------------------------------- download(): real yt-dlp, local server
# Only TwitterIE's network call is replaced, with whatever a tweet would make it return;
# video.download(), yt-dlp's extractor hand-off and its HTTP downloader are all real, and
# nothing leaves the machine.
TWEET = "https://x.com/someone/status/1234567890123456789"


class _Server:
    """Streams `size` bytes of "video" with no Content-Length, counting what it sent."""

    def __init__(self, size):
        self.size, self.hits, self.sent = size, [], 0
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                srv.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
                chunk = b"\0" * 65536
                try:
                    while srv.sent < srv.size:
                        self.wfile.write(chunk)
                        srv.sent += len(chunk)
                except OSError:
                    pass                    # the client hung up on us

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/clip.mp4"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(size=256 * 1024):
        made.append(_Server(size))
        return made[-1]

    yield make
    for s in made:
        s.close()


def _tweet_returns(monkeypatch, result):
    """Make TwitterIE return `result(ie)` instead of calling the Twitter API."""
    monkeypatch.setattr(yt_twitter.TwitterIE, "_real_extract", lambda self, url: result(self))


def _video_at(url, **extra):
    return lambda ie: {"id": "1", "title": "t", "url": url, "ext": "mp4", **extra}


async def test_a_tweet_linking_elsewhere_never_makes_the_bot_fetch_that_link(tmp_path, monkeypatch, server):
    """A tweet with no video of its own makes TwitterIE hand its link to whichever extractor
    claims it. With every extractor enabled the generic one fetched the tweet author's
    chosen host (the LAN, cloud metadata) from the bot and uploaded the result."""
    internal = server()
    _tweet_returns(monkeypatch, lambda ie: ie.url_result(internal.url))
    with pytest.raises(video.VideoError) as e:
        await video.download(TWEET, tmp_path, 10 * MB)
    assert internal.hits == [], "the bot made a request to a host the tweet chose"
    assert str(e.value) == "That link isn't supported."
    assert list(tmp_path.iterdir()) == []


async def test_a_tiktok_profile_is_refused_without_spending_a_rapidapi_call(tmp_path, monkeypatch):
    """No allowed extractor takes a profile link, so the paid fallback could only fail too
    (or worse, resolve the profile to some video nobody asked for)."""
    called = []

    async def fallback(*a, **kw):
        called.append(a)
        raise AssertionError("must not be reached")

    monkeypatch.setattr(video, "_tiktok_rapidapi", fallback)
    with pytest.raises(video.VideoError, match="isn't supported"):
        await video.download("https://www.tiktok.com/@someartist", tmp_path, 10 * MB, rapidapi_key="k")
    assert called == []


@pytest.mark.parametrize("short", [
    "https://vm.tiktok.com/ZMabcdef/",
    "https://vt.tiktok.com/ZSe4FqkKd",
    "https://www.tiktok.com/t/ZTabcdef/",
])
async def test_a_short_link_tiktok_will_not_redirect_still_goes_to_rapidapi(tmp_path, monkeypatch, short):
    """When TikTok answers the short link's HEAD without redirecting (refusing the bot's
    IP), TikTokVMIE raises "Unsupported URL". That is when the paid fallback earns its keep,
    but it was skipped like a profile link and the user told the link wasn't supported."""
    called = []

    async def fallback(url, dest, max_bytes, key):
        called.append(url)
        dest.write_bytes(b"\0" * 1024)
        return dest

    monkeypatch.setattr(yt_tiktok.TikTokVMIE, "_request_webpage",
                        lambda self, req, *a, **kw: types.SimpleNamespace(url=req.url))
    monkeypatch.setattr(video, "_tiktok_rapidapi", fallback)
    out = await video.download(short, tmp_path, 10 * MB, rapidapi_key="k")
    assert called == [short]
    assert list(tmp_path.iterdir()) == [out]


def test_only_the_post_extractors_are_enabled():
    """Names are full-match regexes: "twitter" must not also enable twitter:broadcast."""
    ydl = yt_dlp.YoutubeDL({"quiet": True, "allowed_extractors": video.ALLOWED_EXTRACTORS})
    assert sorted(ie.IE_NAME.lower() for ie in ydl._ies.values()) == ["tiktok", "twitter", "vm.tiktok"]


async def test_a_stream_with_no_content_length_is_cut_off_at_the_cap(tmp_path, monkeypatch, server):
    """yt-dlp's max_filesize only reads Content-Length, so a chunked response (or HLS) was
    downloaded in full and refused only once all of it was on disk."""
    src = server(size=64 * MB)
    _tweet_returns(monkeypatch, _video_at(src.url))
    with pytest.raises(video.VideoError, match="larger than 1 MB"):
        await video.download(TWEET, tmp_path, 1 * MB)
    assert src.sent < 32 * MB, f"downloaded {src.sent / MB:.0f} MB past a 1 MB cap"
    assert list(tmp_path.iterdir()) == []


async def test_a_download_past_its_deadline_is_abandoned(tmp_path, monkeypatch, server):
    src = server(size=4 * MB)
    _tweet_returns(monkeypatch, _video_at(src.url))
    with pytest.raises(video.VideoError, match="too long to download"):
        await video.download(TWEET, tmp_path, 10 * MB, timeout=0)
    assert list(tmp_path.iterdir()) == []


async def test_time_spent_waiting_for_a_download_slot_does_not_count(tmp_path, monkeypatch, server):
    """The deadline caps how long a job may HOLD a slot. Started before the job queued, it
    failed a quick video that waited behind slow ones as "took too long to download"."""
    clock = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(video, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(video, "_download_sem", asyncio.Semaphore(1))
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url))
    await video._download_sem.acquire()             # a slow download holds the only slot
    job = asyncio.create_task(video.download(TWEET, tmp_path, 10 * MB, timeout=60))
    await asyncio.sleep(0)                          # queued for the slot
    assert not job.done()
    clock.now += 3600                               # an hour later the slot frees up
    video._download_sem.release()
    out = await job
    assert out.stat().st_size == src.size


async def test_a_live_stream_is_skipped_rather_than_recorded(tmp_path, monkeypatch, server):
    """FFmpegFD records a live stream until it ends and never calls the progress hook on
    the way, so neither the size cap nor the deadline could stop it."""
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url, is_live=True))
    with pytest.raises(video.VideoError, match="No video found"):
        await video.download(TWEET, tmp_path, 10 * MB)
    assert src.hits == []


async def test_an_ordinary_download_still_works(tmp_path, monkeypatch, server):
    src = server(size=256 * 1024)
    _tweet_returns(monkeypatch, _video_at(src.url))
    out = await video.download(TWEET, tmp_path, 10 * MB)
    assert out.stat().st_size == 256 * 1024
    assert list(tmp_path.iterdir()) == [out]


_COOKIES = "# Netscape HTTP Cookie File\n# the operator's own export\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tabc\n"


async def test_the_operators_cookies_file_is_read_but_never_rewritten(tmp_path, monkeypatch, server):
    """close() saves the cookie jar back to cookiefile, so every conversion rewrote the
    operator's file (two at once racing on it)."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(_COOKIES)
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url))
    await video.download(TWEET, tmp_path / "w", 10 * MB, cookies_file=cookies)
    assert cookies.read_text() == _COOKIES


async def test_a_cookies_path_that_is_a_directory_means_no_cookies_not_no_downloads(tmp_path, monkeypatch, server):
    """Bind-mounting a cookies file that doesn't exist makes Docker create a directory
    there. exists() let it through as cookiefile and every download failed with
    "Is a directory"."""
    cookies = tmp_path / "cookies.txt"
    cookies.mkdir()
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url))
    out = await video.download(TWEET, tmp_path / "w", 10 * MB, cookies_file=cookies)
    assert out.stat().st_size == src.size


async def test_yt_dlp_is_closed_after_every_download(tmp_path, monkeypatch, server):
    """close() releases yt-dlp's request handlers and their sockets; dropping the `with`
    for the cookie fix must not have dropped it too."""
    closed = []

    class Recording(yt_dlp.YoutubeDL):
        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Recording)
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url))
    await video.download(TWEET, tmp_path / "ok", 10 * MB)
    with pytest.raises(video.VideoError):
        await video.download(TWEET, tmp_path / "too-big", 1024)
    assert len(closed) == 2


async def test_a_read_only_cookies_file_does_not_throw_away_a_finished_download(tmp_path, monkeypatch, server):
    """On a :ro mount the write-back raised after the download had finished; the generic
    error path then deleted the file and said "Couldn't download that video"."""
    def read_only(self, *a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(yt_dlp.cookies.YoutubeDLCookieJar, "save", read_only)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(_COOKIES)
    src = server()
    _tweet_returns(monkeypatch, _video_at(src.url))
    out = await video.download(TWEET, tmp_path / "w", 10 * MB, cookies_file=cookies)
    assert out.exists()
