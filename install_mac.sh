#!/bin/bash

# ViewSense Edge Worker - Automated Installer for macOS
# Usage: bash install_mac.sh
# Version: 1.1.0 
#
# Engine Capabilities (Baked into detector.py):
# - Asynchronous FFmpeg Drop-Old-Frame buffer (Zero-Delay Streaming)
# - Robust Occlusion Filter (ByteTrack with lost_track_buffer = 120 frames)
# - Lenient Visual Re-ID (match_thresh = 0.6)

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Starting ViewSense PM2 Edge Worker Installation (macOS)...${NC}"

if [ "$EUID" -eq 0 ]; then
  echo -e "${RED}Please DO NOT run as root on macOS (just use bash install_mac.sh)${NC}"
  exit 1
fi

APP_DIR=$(pwd)

echo -e "${GREEN}[1/5] Checking System Dependencies (Homebrew)...${NC}"
if ! command -v brew &> /dev/null; then
    echo -e "${RED}Homebrew is not installed. Installing Homebrew first...${NC}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

echo "Updating Homebrew and installing python, node, and ffmpeg..."
brew update
brew install python node ffmpeg

echo -e "${GREEN}[2/5] Setting up PM2...${NC}"
if ! command -v pm2 &> /dev/null; then
  echo "Installing PM2 globally..."
  npm install -g pm2
else
  echo "PM2 is already installed."
fi

echo -e "${GREEN}[3/5] Setting up Python Virtual Environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Virtual environment created."
fi

echo -e "${GREEN}[4/5] Installing Python Requirements...${NC}"
source venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    # OpenCV on Mac might need specific binaries, but pip handles it well nowadays
    pip install -r requirements.txt
else
    echo -e "${RED}requirements.txt not found! Please run inside edge-worker-mac folder.${NC}"
    exit 1
fi

echo -e "${GREEN}[5/5] Configuring PM2 Ecosystem...${NC}"
if [ ! -f "ecosystem.config.js" ]; then
    echo -e "${RED}ecosystem.config.js not found! Emitting defaut PM2 layout...${NC}"
    cat > ecosystem.config.js <<EOF
module.exports = {
  apps: [
    {
      name: "viewsense-ai-worker",
      script: "venv/bin/python",
      args: "main.py",
      cwd: "$APP_DIR",
      interpreter: "none",
      autorestart: true,
      max_restarts: 50,
      max_memory_restart: "1500M",
      watch: false,
      env: { PYTHONUNBUFFERED: "1" }
    }
  ]
};
EOF
fi

pm2 start ecosystem.config.js
pm2 save

echo "To ensure PM2 launches on macOS reboot, run the following command manually:"
pm2 startup

# ── Auto-Update Cron (roda toda madrugada às 03:00) ─────────────────────────
echo -e "${GREEN}[Extra] Registrando cron de atualização automática às 03:00...${NC}"

AUTO_UPDATE_SCRIPT="$APP_DIR/auto_update.sh"
chmod +x "$AUTO_UPDATE_SCRIPT" 2>/dev/null || true

CRON_LINE="0 3 * * * WORKER_DIR=\"$APP_DIR\" bash \"$AUTO_UPDATE_SCRIPT\" >> \"$APP_DIR/update.log\" 2>&1"
( crontab -l 2>/dev/null | grep -v "auto_update.sh" ; echo "$CRON_LINE" ) | crontab -

echo "✅ Cron registrado: todo dia às 03:00"
# ─────────────────────────────────────────────────────────────────────────────

echo -e "${GREEN}====================================================${NC}"
echo -e "${GREEN}ViewSense Edge Worker successfully installed & armed!${NC}"
echo "You can view logs actively by running: pm2 logs"
echo "To restart the worker: pm2 restart all"
echo "Update: Auto-update ativo (03:00). Manual: bash auto_update.sh"
echo "===================================================="
