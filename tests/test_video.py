"""Encoder planning and ffmpeg argument construction (no ffmpeg needed)."""
import itertools

import pytest

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
