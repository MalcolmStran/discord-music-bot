#!/bin/bash
set -euo pipefail
# Volumes are mounted root-owned; make them writable by the unprivileged user.
chown -R app:app /app/downloads /app/logs 2>/dev/null || true
rm -f /app/logs/healthy
# YouTube breaks yt-dlp every few weeks; refresh it at start unless disabled.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
    if timeout 90 pip install --quiet --no-cache-dir --upgrade yt-dlp; then
        echo "yt-dlp: $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')"
    else
        # don't hide the reason: a failed update is the usual cause of "YouTube stopped working"
        echo "yt-dlp self-update failed (exit $?); continuing with the bundled $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')" >&2
    fi
fi
exec setpriv --reuid=app --regid=app --init-groups env HOME=/home/app python -m bot
