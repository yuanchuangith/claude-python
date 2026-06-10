#!/bin/bash
set -e

SERVER="43.135.137.212"
USER="ubuntu"
KEY="F:/Desktop/code_new.pem"
REMOTE_DIR="/home/ubuntu/claude-agent-sdk"
PORT=8888

echo "=== Deploying to $SERVER ==="

# Create remote directory
ssh -i "$KEY" -o StrictHostKeyChecking=no "$USER@$SERVER" "mkdir -p $REMOTE_DIR"

# Copy project files using scp
echo "Copying files..."
scp -i "$KEY" -o StrictHostKeyChecking=no -r \
  src/ tests/ pyproject.toml README.md \
  "$USER@$SERVER:$REMOTE_DIR/"

# Install dependencies and setup service
ssh -i "$KEY" "$USER@$SERVER" << 'REMOTE_SCRIPT'
set -e
cd /home/ubuntu/claude-agent-sdk

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install uvicorn fastapi httpx python-multipart pydantic

# Create systemd service file
sudo tee /etc/systemd/system/claude-agent-gateway.service > /dev/null << 'SERVICE'
[Unit]
Description=Claude Agent Gateway
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/claude-agent-sdk
Environment="PATH=/home/ubuntu/claude-agent-sdk/venv/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/home/ubuntu/claude-agent-sdk/src"
ExecStart=/home/ubuntu/claude-agent-sdk/venv/bin/uvicorn claude_agent_sdk.actiondesign_gateway.app:app --host 0.0.0.0 --port 8888
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

# Reload and start service
sudo systemctl daemon-reload
sudo systemctl enable claude-agent-gateway
sudo systemctl restart claude-agent-gateway

echo "=== Service Status ==="
sudo systemctl status claude-agent-gateway --no-pager

REMOTE_SCRIPT

echo "=== Deployment Complete ==="
echo "Service running at: http://$SERVER:$PORT"
echo "Health check: http://$SERVER:$PORT/api/actiondesign-agent/health"
