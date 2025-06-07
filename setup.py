#!/usr/bin/env python3
"""
Setup script for Discord Music Bot
Helps with initial configuration and dependency checking
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

def check_python_version():
    """Check if Python version is 3.8+"""
    if sys.version_info < (3, 8):
        print("❌ Python 3.8 or higher is required!")
        print(f"Current version: {sys.version}")
        return False
    print(f"✅ Python version: {sys.version.split()[0]}")
    return True

def check_ffmpeg():
    """Check if FFmpeg is installed and available"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print("✅ FFmpeg is installed and available")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    print("❌ FFmpeg not found!")
    print("Please install FFmpeg and add it to your system PATH:")
    print("- Windows: https://ffmpeg.org/download.html")
    print("- macOS: brew install ffmpeg")
    print("- Linux: sudo apt install ffmpeg")
    return False

def install_requirements():
    """Install Python requirements"""
    try:
        print("📦 Installing Python dependencies...")
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'],
                              capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Python dependencies installed successfully")
            return True
        else:
            print(f"❌ Failed to install dependencies: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ Error installing dependencies: {e}")
        return False

def setup_env_file():
    """Setup .env file from template"""
    env_file = Path('.env')
    env_example = Path('.env.example')
    
    if env_file.exists():
        print("⚠️ .env file already exists")
        response = input("Do you want to recreate it? (y/N): ").lower().strip()
        if response != 'y':
            return True
    
    if not env_example.exists():
        print("❌ .env.example file not found!")
        return False
    
    try:
        # Copy template
        shutil.copy(env_example, env_file)
        print("✅ Created .env file from template")
        
        # Get Discord token
        print("\n🔑 Discord Bot Setup:")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Create a new application and bot")
        print("3. Copy the bot token")
        
        token = input("\nEnter your Discord bot token (or press Enter to skip): ").strip()
        if token:
            # Update .env file
            with open(env_file, 'r') as f:
                content = f.read()
            
            content = content.replace('your_discord_token_here', token)
            
            with open(env_file, 'w') as f:
                f.write(content)
            
            print("✅ Discord token added to .env file")
        
        # Get RapidAPI key (optional)
        print("\n📱 TikTok Support (Optional):")
        print("For TikTok video support, you need a RapidAPI key")
        print("1. Sign up at https://rapidapi.com/")
        print("2. Subscribe to 'TikTok Download Without Watermark' API")
        
        rapidapi_key = input("\nEnter your RapidAPI key (or press Enter to skip): ").strip()
        if rapidapi_key:
            with open(env_file, 'r') as f:
                content = f.read()
            
            content = content.replace('your_rapidapi_key_here', rapidapi_key)
            
            with open(env_file, 'w') as f:
                f.write(content)
            
            print("✅ RapidAPI key added to .env file")
        
        return True
        
    except Exception as e:
        print(f"❌ Error setting up .env file: {e}")
        return False

def create_directories():
    """Create necessary directories"""
    directories = ['downloads', 'logs']
    
    for directory in directories:
        try:
            Path(directory).mkdir(exist_ok=True)
            print(f"✅ Created directory: {directory}")
        except Exception as e:
            print(f"⚠️ Could not create directory {directory}: {e}")

def main():
    """Main setup function"""
    print("🤖 Discord Music Bot Setup")
    print("=" * 40)
    
    # Check prerequisites
    if not check_python_version():
        return False
    
    if not check_ffmpeg():
        print("\n⚠️ FFmpeg is required but not found.")
        response = input("Continue setup anyway? (y/N): ").lower().strip()
        if response != 'y':
            return False
    
    # Install dependencies
    if not install_requirements():
        return False
    
    # Setup environment
    if not setup_env_file():
        print("⚠️ Could not setup .env file. You'll need to configure it manually.")
    
    # Create directories
    create_directories()
    
    print("\n🎉 Setup completed!")
    print("\nNext steps:")
    if not Path('.env').exists() or 'your_discord_token_here' in Path('.env').read_text():
        print("1. Edit .env file and add your Discord bot token")
    print("2. Run the bot with: python main.py")
    print("3. Invite the bot to your server with proper permissions")
    print("\nFor detailed instructions, see README.md")
    
    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Setup cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
