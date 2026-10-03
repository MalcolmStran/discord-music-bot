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


def _requirement_extras():
    for line in REQUIREMENTS.read_text().splitlines():
        extras = _ytdlp_extras(line)
        if extras is not None:
            return extras
    raise AssertionError("requirements.txt has no yt-dlp line")


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


def test_requirements_ship_the_js_runtime_yt_dlp_uses_by_default():
    """yt-dlp enables only Deno unless told otherwise (Debian's Node is below its 22+ floor
    anyway), and solves YouTube's challenges with the yt-dlp-ejs scripts from `default`."""
    assert {"default", "deno"} <= _requirement_extras()


def test_entrypoint_self_update_keeps_the_requirement_extras(sandbox):
    """A bare `pip install -U yt-dlp` upgrades yt-dlp past the yt-dlp-ejs it pins."""
    _, recorded = sandbox(ENTRYPOINT)
    (pip,) = _pip_calls(recorded)
    specs = [a for a in pip if _ytdlp_extras(a) is not None]
    assert specs, pip
    assert _ytdlp_extras(specs[0]) == _requirement_extras()
