#!/bin/bash
# Equivalent of `docker compose up -d --build` for hosts where compose is broken (rock5: docker-compose 1.29 + http+docker bug).
# One difference left: `docker run --env-file` takes values verbatim, so unlike compose it keeps
# surrounding quotes and trailing comments. Keep .env values unquoted and comments on their own line.
set -e
cd "$(dirname "$0")"
# Mount the settings volume compose would. Compose names it <project>_bot-downloads, where the
# project is COMPOSE_PROJECT_NAME (environment first, then .env) or else the directory name,
# lowercased, stripped to [a-z0-9_-] and with leading -/_ trimmed. A hard-coded
# discord-music-bot_ prefix gave a clone in any other directory a different, empty volume here,
# so switching launchers looked like every server's settings had been reset.
project=${COMPOSE_PROJECT_NAME:-$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | tail -n 1 | tr -d '\r')}
project=${project:-$(basename "$PWD" | LC_ALL=C tr 'A-Z' 'a-z' | LC_ALL=C tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')}
docker build -t discord-music-bot:2 .
docker rm -f discord-music-bot 2>/dev/null || true
docker run -d --name discord-music-bot --restart unless-stopped --network host \
  --env-file .env \
  -v "$PWD/logs:/app/logs" -v "${project}_bot-downloads:/app/downloads" \
  --memory 768m --stop-timeout 30 \
  discord-music-bot:2
docker logs -f discord-music-bot
