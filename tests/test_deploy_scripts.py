"""entrypoint.sh and run.sh, run for real under bash with the commands they call stubbed.

Each stub on PATH records its argv (shell-quoted, one call per line) and does nothing else, so
the scripts' own logic — which settings turn the yt-dlp self-update on, which volume run.sh
mounts — is what gets tested, without root, pip, network or a Docker daemon.

These are repository checks, so they skip where the scripts are not next to tests/ (the
Docker image ships bot/ and tests/ under /app, with the entrypoint at /entrypoint.sh).
"""
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "entrypoint.sh"
RUN_SH = REPO / "run.sh"
REQUIREMENTS = REPO / "requirements.txt"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    not (ENTRYPOINT.is_file() and RUN_SH.is_file() and BASH),
    reason="deploy scripts or bash not available (running outside a source checkout)",
)

RECORD = 'printf "%q " "$(basename "$0")" "$@" >> "$CALLS"; echo >> "$CALLS"\n'
STUBS = {
    # entrypoint.sh: no chown/rm of real paths, no network, and `exec setpriv` just ends it
    "chown": RECORD,
    "rm": RECORD,
    "timeout": RECORD + 'shift; exec "$@"\n',
    "pip": RECORD,
    "python": RECORD + '[ "${1-}" = -c ] && echo 9.9.9\n',
    "setpriv": RECORD,
    # run.sh
    "docker": RECORD,
}


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {k: v for k, v in os.environ.items() if k not in ("YTDLP_AUTO_UPDATE", "COMPOSE_PROJECT_NAME")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CALLS"] = str(calls)

    def run(script: Path, **extra_env):
        proc = subprocess.run([BASH, str(script)], env={**env, **extra_env},
                              capture_output=True, text=True, timeout=20)
        recorded = [shlex.split(line) for line in calls.read_text().splitlines() if line.strip()]
        return proc, recorded

    return run


def _ytdlp_extras(spec: str):
    m = re.match(r"\s*yt-dlp(?:\[([^\]]*)\])?", spec)
    if not m:
        return None
    return {e.strip() for e in (m.group(1) or "").split(",") if e.strip()}


def _requirements(machine: str):
    """What `pip install -r requirements.txt` asks for on a CPython 3.13 Linux `machine`."""
    env = {**default_environment(), "sys_platform": "linux", "platform_system": "Linux",
           "platform_machine": machine, "python_version": "3.13", "python_full_version": "3.13.0"}
    reqs = [Requirement(line) for line in REQUIREMENTS.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    return {r.name: r for r in reqs if r.marker is None or r.marker.evaluate(env)}


def _requirement_extras():
    return set(_requirements("x86_64")["yt-dlp"].extras)


def _installs_deno(machine: str) -> bool:
    reqs = _requirements(machine)
    return "deno" in reqs or "deno" in reqs["yt-dlp"].extras


# ---------------------------------------------------------------- entrypoint.sh self-update

def _pip_calls(recorded):
    return [c for c in recorded if c[0] == "pip"]


@pytest.mark.parametrize("value", [None, "", "  ", "true", "True", "TRUE", "1", "yes", "on", " On ", "true\r"])
def test_entrypoint_self_updates_for_every_value_config_bool_reads_as_true(sandbox, value):
    """Same semantics as config._bool: =1, =yes, =True used to switch it off silently."""
    extra = {} if value is None else {"YTDLP_AUTO_UPDATE": value}
    proc, recorded = sandbox(ENTRYPOINT, **extra)
    assert proc.returncode == 0, proc.stderr
    assert _pip_calls(recorded), f"YTDLP_AUTO_UPDATE={value!r} skipped the self-update"
    assert "disabled" not in proc.stdout
    assert recorded[-1][0] == "setpriv", "the bot was not started"


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "ture"])
def test_entrypoint_says_so_when_the_self_update_is_off(sandbox, value):
    proc, recorded = sandbox(ENTRYPOINT, YTDLP_AUTO_UPDATE=value)
    assert proc.returncode == 0, proc.stderr
    assert not _pip_calls(recorded)
    # without this line, "restart didn't fix YouTube" had nothing in the logs to explain it
    assert f"self-update disabled (YTDLP_AUTO_UPDATE={value})" in proc.stdout
    assert recorded[-1][0] == "setpriv", "the bot was not started"


# x86_64/aarch64 are the image's platforms (python:3.13-slim on amd64/arm64); the other two
# are how macOS arm64 and Windows x64 spell theirs, where deno also publishes wheels.
@pytest.mark.parametrize("machine", ["x86_64", "aarch64", "arm64", "AMD64"])
def test_requirements_ship_the_js_runtime_yt_dlp_uses_by_default(machine):
    """yt-dlp enables only Deno unless told otherwise (Debian's Node is below its 22+ floor
    anyway), and solves YouTube's challenges with the yt-dlp-ejs scripts from `default`."""
    assert "default" in _requirements(machine)["yt-dlp"].extras
    assert _installs_deno(machine), f"no Deno on {machine}, so no YouTube JS challenge solving"


@pytest.mark.parametrize("machine", ["armv7l", "armv6l", "i686"])
def test_requirements_do_not_ask_for_deno_where_it_has_no_wheels(machine):
    """deno publishes no wheel there and its sdist refuses to build, so an unconditional
    `yt-dlp[deno]` made `pip install -r requirements.txt` fail outright (32-bit Raspberry Pi OS)."""
    assert "default" in _requirements(machine)["yt-dlp"].extras
    assert not _installs_deno(machine)


def test_entrypoint_self_update_keeps_the_requirement_extras(sandbox):
    """A bare `pip install -U yt-dlp` upgrades yt-dlp past the yt-dlp-ejs it pins. The image is
    always x86_64/aarch64, so it keeps Deno too, via yt-dlp's own extra to track its floor."""
    _, recorded = sandbox(ENTRYPOINT)
    (pip,) = _pip_calls(recorded)
    specs = [a for a in pip if _ytdlp_extras(a) is not None]
    assert specs, pip
    assert _ytdlp_extras(specs[0]) >= _requirement_extras() | {"deno"}


# ---------------------------------------------------------------- run.sh settings volume

def _run_sh_in(tmp_path, dirname, dotenv=""):
    checkout = tmp_path / dirname
    checkout.mkdir()
    shutil.copy2(RUN_SH, checkout / "run.sh")
    (checkout / ".env").write_text(dotenv)
    return checkout / "run.sh"


def _downloads_volume(recorded):
    (run,) = [c for c in recorded if c[:2] == ["docker", "run"]]
    vols = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
    (vol,) = [v for v in vols if v.endswith(":/app/downloads")]
    return vol.removesuffix(":/app/downloads")


# Expected names are what `docker compose config` (v5.1.1) reports for a checkout in that
# directory: lowercase, keep only [a-z0-9_-], trim leading - and _.
@pytest.mark.parametrize(("dirname", "project"), [
    ("discord-music-bot", "discord-music-bot"),
    ("musicbot", "musicbot"),
    ("My.Music Bot", "mymusicbot"),
    ("__-Weird_Dir!", "weird_dir"),
    ("ÄbcDé", "bcd"),
])
def test_run_sh_mounts_the_volume_compose_would_for_the_checkout_dir(sandbox, tmp_path, dirname, project):
    """A hard-coded discord-music-bot_ prefix gave other clones an empty settings volume."""
    proc, recorded = sandbox(_run_sh_in(tmp_path, dirname))
    assert proc.returncode == 0, proc.stderr
    assert _downloads_volume(recorded) == f"{project}_bot-downloads"


def test_run_sh_honours_compose_project_name_from_the_environment(sandbox, tmp_path):
    script = _run_sh_in(tmp_path, "discord-music-bot", dotenv="COMPOSE_PROJECT_NAME=fromdotenv\n")
    proc, recorded = sandbox(script, COMPOSE_PROJECT_NAME="fromenv")
    assert proc.returncode == 0, proc.stderr
    assert _downloads_volume(recorded) == "fromenv_bot-downloads"


def test_run_sh_honours_compose_project_name_from_dotenv(sandbox, tmp_path):
    """Compose reads COMPOSE_PROJECT_NAME from the project's .env too (CRLF tolerated)."""
    script = _run_sh_in(tmp_path, "discord-music-bot",
                        dotenv="DISCORD_TOKEN=x\r\nCOMPOSE_PROJECT_NAME=fromdotenv\r\n")
    proc, recorded = sandbox(script)
    assert proc.returncode == 0, proc.stderr
    assert _downloads_volume(recorded) == "fromdotenv_bot-downloads"
