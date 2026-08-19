FROM python:3.11-slim

# Install system dependencies for voice support
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    libopus-dev \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better cache usage
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security (entrypoint will drop to this user)
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
# Note: container starts as root so entrypoint.sh can fix volume permissions
# Then drops to 'app' user before running the bot

# Set container environment variable
ENV DOCKER_CONTAINER=true

# Copy and set up entrypoint script (runs as root to fix volume perms, then drops to app)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default command (entrypoint handles user switching)
CMD ["/entrypoint.sh"]
