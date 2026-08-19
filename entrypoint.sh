#!/bin/bash
set -e
# Volumes are mounted root-owned; make them writable by the unprivileged user.
chown -R app:app /app/downloads /app/logs 2>/dev/null || true
# YouTube breaks yt-dlp every few weeks; refresh it at start unless disabled.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
    timeout 90 pip install --quiet --no-cache-dir --upgrade yt-dlp 2>/dev/null \
        && echo "yt-dlp: $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')" \
        || echo "yt-dlp self-update skipped (offline?)"
fi
exec setpriv --reuid=app --regid=app --init-groups env HOME=/home/app python -m bot
