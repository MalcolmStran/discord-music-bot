"""entrypoint.sh and run.sh, run for real under bash with the commands they call stubbed.

Each stub on PATH records its argv (shell-quoted, one call per line) and does nothing else, so
the scripts' own logic — which settings turn the yt-dlp self-update on, which volume run.sh
mounts — is what gets tested, without root, pip, network or a Docker daemon.

These are repository checks, so they skip where the scripts are not next to tests/ (the
Docker image ships bot/ and tests/ under /app, with the entrypoint at /entrypoint.sh).
"""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "entrypoint.sh"
RUN_SH = REPO / "run.sh"
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

