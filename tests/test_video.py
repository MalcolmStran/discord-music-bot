"""Encoder planning and ffmpeg argument construction (no ffmpeg needed)."""
import pytest

from bot.core.video import (
    LADDER,
    MIN_VIDEO_BITRATE,
    EncodeStep,
    _friendly,
    _is_too_big,
    _mmss,
    build_pass_args,
    max_fittable_duration,
    plan_step,
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
    ("ERROR: this post is private", "That post is private/age-gated (needs cookies)."),
    ("HTTP Error 404: Not Found", "That post doesn't exist (or was deleted)."),
    ("something else entirely", "Couldn't download that video."),
])
def test_friendly_messages(raw, expected):
    assert _friendly(raw) == expected


@pytest.mark.parametrize("secs,text", [(65, "1:05"), (3600, "1:00:00"), (0, "0:00"), (419, "6:59")])
def test_mmss(secs, text):
    assert _mmss(secs) == text
