#!/bin/bash

echo "Discord Music Bot - Quick Start"
echo "==============================="

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed"
    echo "Please install Python 3.8+ from your package manager"
    exit 1
fi

# Check Python version
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if [ $? -ne 0 ]; then
    echo "ERROR: Python 3.8+ is required"
    exit 1
fi

# Check if Deno is installed
if ! command -v deno &> /dev/null; then
    echo "ERROR: Deno runtime not found."
    echo "yt-dlp now requires Deno (or another JS runtime) for YouTube playback."
    echo "Install Deno from https://deno.land/#installation or set JS_RUNTIME_PATH before running."
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo ".env file not found. Running setup..."
    python3 setup.py
    if [ $? -ne 0 ]; then
        echo "Setup failed!"
        exit 1
    fi
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install/update requirements
echo "Installing requirements..."
pip install -r requirements.txt

# Run the bot
echo "Starting Discord Music Bot..."
python main.py
