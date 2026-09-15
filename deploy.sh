#!/bin/bash
# ModelLink - Oracle Cloud Deployment Script
# Run this on your Oracle Cloud VM after uploading the code

set -e

echo "=== ModelLink Server Setup ==="

# 1. Update system
echo "[1/6] Updating system..."
sudo apt update && sudo apt upgrade -y

# 2. Install Python and dependencies
echo "[2/6] Installing Python..."
sudo apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx

# 3. Setup app directory
echo "[3/6] Setting up app..."
sudo mkdir -p /opt/modellink
sudo cp -r . /opt/modellink/
cd /opt/modellink

# 4. Create virtual environment and install deps
echo "[4/6] Installing Python packages..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn uvicorn

# 5. Create .env from template if it doesn't exist
if [ ! -f .env ]; then
    echo "[5/6] Creating .env template..."
    cat > .env << 'ENVEOF'
# Add your API keys here (one per line)
# Get keys from:
# Groq: https://console.groq.com/keys
# Gemini: https://aistudio.google.com/apikey
# OpenAI: https://platform.openai.com/api-keys

GROQ_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
LANGUAGE=English
ENVEOF
    echo "  -> Edit /opt/modellink/.env to add your API keys!"
else
    echo "[5/6] .env already exists, skipping."
fi

# 6. Create systemd service
echo "[6/6] Setting up service..."
sudo cat > /etc/systemd/system/modellink.service << 'SERVICEEOF'
[Unit]
Description=ModelLink API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/modellink
Environment=PATH=/opt/modellink/venv/bin:/usr/bin
ExecStart=/opt/modellink/venv/bin/python -m uvicorn modellink.api_server:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable modellink
sudo systemctl start modellink

echo ""
echo "=== Setup Complete! ==="
echo "App is running on http://localhost:8080"
echo ""
echo "Next steps:"
echo "  1. Edit your API keys: sudo nano /opt/modellink/.env"
echo "  2. Restart after editing: sudo systemctl restart modellink"
echo "  3. Setup nginx + SSL with: sudo bash /opt/modellink/setup-nginx.sh"
