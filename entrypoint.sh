#!/bin/bash
set -euo pipefail
# Volumes are mounted root-owned; make them writable by the unprivileged user.
chown -R app:app /app/downloads /app/logs 2>/dev/null || true
rm -f /app/logs/healthy
# YouTube breaks yt-dlp every few weeks; refresh it at start unless disabled.
# Parsed like config._bool (1/true/yes/on in any case, blank = default on): an exact
# `= "true"` match made YTDLP_AUTO_UPDATE=1 or =True switch the update off without a word.
# Whitespace (incl. a CRLF .env's \r) goes before the blank check, as _bool strips first.
auto_update=${YTDLP_AUTO_UPDATE-}
auto_update=${auto_update//[[:space:]]/}
case "${auto_update,,}" in
    ""|1|true|yes|on)
        # Keep the extras: a bare `yt-dlp` upgrade leaves the pinned yt-dlp-ejs behind, which a
        # newer yt-dlp rejects. requirements.txt lists deno on its own line behind a platform
        # marker, but this image is always Debian x86_64/aarch64 where that marker holds, so ask
        # for yt-dlp's `deno` extra here to keep deno at whatever floor the new yt-dlp sets.
        # pip's default only-if-needed strategy leaves the ~40 MB wheel alone otherwise.
        if timeout 90 pip install --quiet --no-cache-dir --upgrade "yt-dlp[default,deno]"; then
            echo "yt-dlp: $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')"
        else
            # don't hide the reason: a failed update is the usual cause of "YouTube stopped working"
            echo "yt-dlp self-update failed (exit $?); continuing with the bundled $(python -c 'import yt_dlp;print(yt_dlp.version.__version__)')" >&2
        fi
        ;;
    *)
        # say so: a restart picking up a fresh yt-dlp is the documented fix when YouTube breaks
        printf 'yt-dlp: self-update disabled (YTDLP_AUTO_UPDATE=%q)\n' "${YTDLP_AUTO_UPDATE-}"
        ;;
esac
exec setpriv --reuid=app --regid=app --init-groups env HOME=/home/app python -m bot
