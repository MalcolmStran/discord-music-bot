"""`.env.example` is copied to `.env` and fed to `docker run --env-file` by run.sh.

That parser takes everything after the first `=` verbatim — it does NOT strip trailing
comments, unlike python-dotenv and compose's env_file. So a line like
`COMMAND_PREFIX=!  # note` silently makes the prefix "!  # note".
"""
from pathlib import Path

import pytest

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def docker_env_file_pairs(text: str):
    """Parse the way `docker run --env-file` does: full-line comments only."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            yield key.strip(), value


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file()


@pytest.mark.parametrize("key,value", list(docker_env_file_pairs(ENV_EXAMPLE.read_text())))
def test_no_trailing_comments_in_values(key, value):
    assert "#" not in value, (
        f"{key} has a trailing comment; `docker run --env-file` would fold it into the "
        f"value. Put the comment on its own line."
    )


def test_no_stray_whitespace_in_values():
    for key, value in docker_env_file_pairs(ENV_EXAMPLE.read_text()):
        assert value == value.strip(), f"{key} has surrounding whitespace in its value"


def test_documents_every_setting_config_reads():
    """Every env var Config.from_env() looks at should appear in the example file."""
    import re

    config_src = (ENV_EXAMPLE.parent / "bot" / "config.py").read_text()
    referenced = set(re.findall(r'(?:getenv|_int|_float|_bool)\(\s*"([A-Z][A-Z0-9_]*)"', config_src))
    documented = {k for k, _ in docker_env_file_pairs(ENV_EXAMPLE.read_text())}
    missing = referenced - documented
    assert not missing, f"undocumented settings: {sorted(missing)}"
