"""`.env.example` is copied to `.env` and fed to `docker run --env-file` by run.sh.

That parser takes everything after the first `=` verbatim — it does NOT strip trailing
comments, unlike python-dotenv and compose's env_file. So a line like
`COMMAND_PREFIX=!  # note` silently makes the prefix "!  # note".

These are repository-hygiene checks, so they skip where the repo root is not present
(the Docker image ships bot/ and tests/ but no .env.example).
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / ".env.example"
CONFIG_PY = REPO / "bot" / "config.py"

pytestmark = pytest.mark.skipif(
    not ENV_EXAMPLE.is_file(), reason="no .env.example (running outside a source checkout)"
)


def docker_env_file_pairs(text: str):
    """Parse the way `docker run --env-file` does: full-line comments only."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            yield key.strip(), value


def env_pairs():
    if not ENV_EXAMPLE.is_file():
        return []
    return list(docker_env_file_pairs(ENV_EXAMPLE.read_text()))


def test_env_example_is_not_empty():
    assert env_pairs(), ".env.example defines no settings"


def test_no_trailing_comments_in_values():
    offenders = [k for k, v in env_pairs() if "#" in v]
    assert not offenders, (
        f"{offenders} have trailing comments; `docker run --env-file` folds them into the "
        f"value. Put the comment on its own line."
    )


def test_no_stray_whitespace_in_values():
    offenders = [k for k, v in env_pairs() if v != v.strip()]
    assert not offenders, f"{offenders} have surrounding whitespace in their values"


def test_documents_every_setting_config_reads():
    """Every env var Config.from_env() looks at should appear in the example file."""
    import re

    referenced = set(re.findall(r'(?:getenv|_int|_float|_bool)\(\s*"([A-Z][A-Z0-9_]*)"',
                                CONFIG_PY.read_text()))
    missing = referenced - {k for k, _ in env_pairs()}
    assert not missing, f"undocumented settings: {sorted(missing)}"
