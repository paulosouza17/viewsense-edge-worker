#!/bin/bash

# ViewSense Edge Worker - Automated Installer for Ubuntu/Debian
# Usage: sudo bash install_ubuntu.sh
# Installs: viewsense-ai-worker (Python AI) + viewsense-rtmp (RTMP/HLS ingest)
# Version: 1.1.0 
#
# Engine Capabilities (Baked into detector.py):
# - Asynchronous FFmpeg Drop-Old-Frame buffer (Zero-Delay Streaming)
# - Robust Occlusion Filter (ByteTrack with lost_track_buffer = 120 frames)
# - Lenient Visual Re-ID (match_thresh = 0.6)
# - Auto GOP-Cache disabled for Edge memory stabilization

set -e

VERSION="1.0.0"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}Starting ViewSense PM2 Edge Worker Installation (v${VERSION})...${NC}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Please run as root (sudo bash install_ubuntu.sh)${NC}"
  exit 1
fi

APP_DIR=$(pwd)
USER_NAME=${SUDO_USER:-$USER}

echo -e "${GREEN}[1/7] Installing System Dependencies...${NC}"
apt-get update
apt-get install -y curl build-essential python3 python3-venv python3-pip python3-dev libgl1 libglib2.0-0 ffmpeg

echo -e "${GREEN}[2/7] Setting up Node.js & PM2...${NC}"
if ! command -v pm2 &> /dev/null; then
  echo "Installing Node.js and PM2..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
  npm install -g pm2
else
  echo "PM2 is already installed."
fi

echo -e "${GREEN}[3/7] Setting up Python Virtual Environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    chown -R $USER_NAME:$USER_NAME venv
    echo "Virtual environment created."
fi

echo -e "${GREEN}[4/7] Installing Python Requirements...${NC}"
sudo -H -u $USER_NAME bash -c "source venv/bin/activate && pip install --upgrade pip"
if [ -f "requirements.txt" ]; then
    sudo -H -u $USER_NAME bash -c "source venv/bin/activate && pip install -r requirements.txt"
else
    echo -e "${RED}requirements.txt not found! Please run this script inside the edge-worker folder.${NC}"
    exit 1
fi

echo -e "${GREEN}[5/7] Installing RTMP Ingest Server (viewsense-rtmp)...${NC}"
RTMP_DIR="/opt/viewsense"
mkdir -p "$RTMP_DIR/scripts" "$RTMP_DIR/media/live" "$RTMP_DIR/snapshots"

cd "$RTMP_DIR"
if [ ! -f "package.json" ]; then
  npm init -y > /dev/null 2>&1
fi
npm install node-media-server@4.2.4 --save > /dev/null 2>&1
echo -e "${GREEN}  --> node-media-server installed${NC}"

# Copy RTMP ingest script
if [ -f "$APP_DIR/scripts/rtmp-ingest.cjs" ]; then
  cp "$APP_DIR/scripts/rtmp-ingest.cjs" "$RTMP_DIR/scripts/rtmp-ingest.cjs"
  echo -e "${GREEN}  --> rtmp-ingest.cjs copied${NC}"
else
  echo -e "${YELLOW}  ! rtmp-ingest.cjs not found in scripts/ -- downloading from GitHub...${NC}"
  curl -fsSL "https://raw.githubusercontent.com/paulosouza17/view-sense-server-ubuntu-install/main/scripts/rtmp-ingest.cjs" \
    -o "$RTMP_DIR/scripts/rtmp-ingest.cjs" 2>/dev/null || \
    echo -e "${YELLOW}  ! Could not download -- create $RTMP_DIR/scripts/rtmp-ingest.cjs manually${NC}"
fi

# Patch memory leak (disable gop_cache to prevent server crashes)
if [ -f "$RTMP_DIR/scripts/rtmp-ingest.cjs" ]; then
  sed -i "s/gop_cache: true/gop_cache: false/g" "$RTMP_DIR/scripts/rtmp-ingest.cjs"
fi

# active_streams.json: empty array = accept all valid hashes
if [ ! -f "$RTMP_DIR/active_streams.json" ]; then
  echo "[]" > "$RTMP_DIR/active_streams.json"
fi

chown -R root:root "$RTMP_DIR"
cd "$APP_DIR"
echo -e "${GREEN}  --> RTMP server ready at /opt/viewsense${NC}"

echo -e "${GREEN}[6/7] Configuring PM2 Ecosystem...${NC}"
cat > ecosystem.config.js << EOFPM2
module.exports = {
  apps: [
    {
      name: "viewsense-ai-worker",
      script: "venv/bin/python",
      args: "main.py",
      cwd: "${APP_DIR}",
      interpreter: "none",
      autorestart: true,
      max_restarts: 50,
      max_memory_restart: "1500M",
      watch: false,
      env: { PYTHONUNBUFFERED: "1" }
    },
    {
      name: "viewsense-rtmp",
      script: "/opt/viewsense/scripts/rtmp-ingest.cjs",
      cwd: "/opt/viewsense",
      interpreter: "node",
      instances: 1,
      exec_mode: "cluster",
      autorestart: true,
      max_restarts: 20,
      max_memory_restart: "1500M",
      watch: false,
      env: {
        NODE_ENV: "production",
        RTMP_PORT: "55935",
        HLS_PORT: "8001"
      }
    }
  ]
};
EOFPM2
chown $USER_NAME:$USER_NAME ecosystem.config.js

echo -e "${GREEN}[7/7] Starting PM2 services...${NC}"
sudo -H -u $USER_NAME bash -c "pm2 start ecosystem.config.js"
sudo -H -u $USER_NAME bash -c "pm2 save"

# Setup PM2 Startup script automatically
env PATH=$PATH:/usr/bin /usr/lib/node_modules/pm2/bin/pm2 startup systemd -u $USER_NAME --hp /home/$USER_NAME

SERVER_IP=$(hostname -I | awk '{print $1}')
echo -e "${GREEN}====================================================${NC}"
echo -e "${GREEN}ViewSense Edge Worker successfully installed & armed!${NC}"
echo -e "${GREEN}Services running:${NC}"
echo "  * viewsense-ai-worker  --> AI detection & counting"
echo "  * viewsense-rtmp       --> RTMP ingest + HLS"
echo ""
echo "RTMP endpoint:   rtmp://${SERVER_IP}:55935/live/{stream-key}"
echo "HLS  endpoint:   http://${SERVER_IP}:8001/live/{stream-key}/index.m3u8"
echo ""
echo "Logs:    pm2 logs"
echo "Restart: pm2 restart all"
echo "===================================================="
