#!/usr/bin/env bash
# Run every check locally. This repo does not use GitHub Actions — hosted runners bill
# against the account, and Actions is not available here — so this script is the entry
# point that a CI workflow would otherwise be.
#
#   ./check.sh            lint + the offline test suite
#   ./check.sh --docker   also build the image and run the suite inside it
#   ./check.sh --install  install the dev dependencies first
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
status=0

for arg in "$@"; do
    if [ "$arg" = "--install" ]; then
        echo "== installing dev dependencies =="
        "$PY" -m pip install -q -r requirements-dev.txt
    fi
done

if command -v ruff >/dev/null 2>&1; then
    RUFF=(ruff)
else
    RUFF=("$PY" -m ruff)
fi

echo "== ruff =="
"${RUFF[@]}" check bot tests || status=1

echo "== pytest ($("$PY" -V 2>&1)) =="
"$PY" -m pytest || status=1

for arg in "$@"; do
    if [ "$arg" = "--docker" ]; then
        echo "== docker image =="
        docker build -t discord-music-bot:check .
        # .env.example is not shipped in the image, so its hygiene checks skip there
        docker run --rm --entrypoint sh discord-music-bot:check -c \
            "pip install --quiet --no-cache-dir pytest pytest-asyncio && python -m pytest" || status=1
    fi
done

if [ "$status" -eq 0 ]; then
    echo "All checks passed."
else
    echo "Some checks FAILED." >&2
fi
exit "$status"
