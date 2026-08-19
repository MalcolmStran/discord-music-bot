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
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/downloads /app/logs && chown -R app:app /app

# entrypoint starts as root (fixes volume ownership, optional yt-dlp self-update) then drops to `app`
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
