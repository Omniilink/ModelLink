#!/bin/bash
# ModelLink - Update Script
# Run this after pushing new code to GitHub

set -e

echo "=== Updating ModelLink ==="

cd /opt/modellink

# Pull latest code
sudo git pull origin main

# Install any new dependencies
source venv/bin/activate
pip install -r requirements.txt

# Restart the service
sudo systemctl restart modellink

echo "=== Update complete! ==="
