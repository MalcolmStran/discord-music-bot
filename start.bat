@echo off
echo Discord Music Bot - Quick Start
echo ===============================

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://python.org
    pause
    exit /b 1
)

REM Check if .env file exists
if not exist ".env" (
    echo .env file not found. Running setup...
    python setup.py
    if errorlevel 1 (
        echo Setup failed!
        pause
        exit /b 1
    )
)

REM Install requirements if needed
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Install/update requirements
echo Installing requirements...
pip install -r requirements.txt

REM Run the bot
echo Starting Discord Music Bot...
python main.py

pause
