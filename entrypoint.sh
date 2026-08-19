#!/bin/bash
# Fix permissions on the downloads volume (mounted as root at runtime)
if [ -d /app/downloads ]; then
    chown -R app:app /app/downloads 2>/dev/null || true
fi
# Drop to app user and run the bot
exec su -s /bin/bash app -c 'cd /app && exec python main.py'
