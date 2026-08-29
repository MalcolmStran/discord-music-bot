FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOCKER_CONTAINER=true

# ffmpeg for audio/video, libopus for voice, nodejs as yt-dlp's JS runtime (YouTube signature solving)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libopus0 nodejs ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
# the README tells you to run the suite inside the image, so it has to be here
COPY tests/ ./tests/
COPY pyproject.toml ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/downloads /app/logs && chown -R app:app /app

# `python -m bot` exits on an unrecoverable error, so restart:unless-stopped covers a crash.
# This catches the other failure mode: the process alive but the gateway connection dead.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,pathlib,sys,time; \
p=pathlib.Path(os.environ.get('LOG_DIR') or '/app/logs')/'healthy'; \
sys.exit(0 if p.exists() and time.time()-p.stat().st_mtime < 180 else 1)"

# entrypoint starts as root (fixes volume ownership, optional yt-dlp self-update) then drops to `app`
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
