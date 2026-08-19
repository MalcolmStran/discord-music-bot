#!/bin/bash
# Equivalent of `docker compose up -d --build` for hosts where compose is broken (rock5: docker-compose 1.29 + http+docker bug).
set -e
cd "$(dirname "$0")"
docker build -t discord-music-bot:2 .
docker rm -f discord-music-bot 2>/dev/null || true
docker run -d --name discord-music-bot --restart unless-stopped --network host \
  --env-file .env -e DOCKER_CONTAINER=true -e YTDLP_AUTO_UPDATE=true \
  -v "$PWD/logs:/app/logs" -v discord-music-bot_bot-downloads:/app/downloads \
  --memory 768m --stop-timeout 20 \
  discord-music-bot:2
docker logs -f discord-music-bot
