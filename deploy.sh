#!/bin/bash
set -e

SERVER="${ACTIONDESIGN_DEPLOY_SERVER:-43.135.137.212}"
USER="${ACTIONDESIGN_DEPLOY_USER:-ubuntu}"
KEY="${ACTIONDESIGN_DEPLOY_KEY:-C:/Users/yuanchuan/Desktop/code_new.pem}"
REMOTE_DIR="/home/ubuntu/claude-agent-sdk"
PORT=8888
ARCHIVE="/tmp/claude-agent-sdk-deploy.tar.gz"
REMOTE_ARCHIVE="/tmp/claude-agent-sdk-deploy.tar.gz"
SSH_OPTIONS=(-i "$KEY" -o StrictHostKeyChecking=accept-new)

# Usage:
#   bash deploy.sh           # fast: package src/tests -> upload -> extract -> restart
#   bash deploy.sh --full    # full: also reinstall package / rewrite service

FULL_DEPLOY=0
if [ "${1:-}" = "--full" ]; then
  FULL_DEPLOY=1
fi

echo "=== Deploying to $SERVER (mode: $([ "$FULL_DEPLOY" -eq 1 ] && echo full || echo fast)) ==="

if [ ! -f "$KEY" ]; then
  echo "Deployment key does not exist: $KEY" >&2
  exit 1
fi

echo "Checking protected production environment..."
ssh "${SSH_OPTIONS[@]}" "$USER@$SERVER" bash -s << 'REMOTE_PREFLIGHT'
set -e
ENV_FILE=/home/ubuntu/actiondesign-agent-gateway/.env
# .env is root:root 600 in production; preflight must use sudo to read it.
test -f "$ENV_FILE"
sudo python3 - "$ENV_FILE" << 'PY'
from pathlib import Path
import sys

values = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    values[key.strip()] = value

if values.get("ACTIONDESIGN_SERVICE_AUTH_MODE") != "required":
    raise SystemExit(
        "ACTIONDESIGN_SERVICE_AUTH_MODE must be required before production deploy"
    )
if len(values.get("ACTIONDESIGN_SERVICE_AUTH_TOKEN", "")) < 32:
    raise SystemExit(
        "ACTIONDESIGN_SERVICE_AUTH_TOKEN must contain at least 32 characters"
    )
PY
REMOTE_PREFLIGHT

# 1) Local package
echo "Packaging..."
tar -czf "$ARCHIVE" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude=".pytest_cache" \
  src tests scripts pyproject.toml README.md claude-agent-sdk.md

SIZE=$(stat -c%s "$ARCHIVE" 2>/dev/null || stat -f%z "$ARCHIVE")
echo "  archive: $ARCHIVE ($((SIZE / 1024)) KB)"

# 2) Upload
echo "Uploading..."
scp "${SSH_OPTIONS[@]}" "$ARCHIVE" "$USER@$SERVER:$REMOTE_ARCHIVE"

# 3) Remote extract + restart
if [ "$FULL_DEPLOY" -eq 1 ]; then
  ssh "${SSH_OPTIONS[@]}" "$USER@$SERVER" bash -s << 'REMOTE_SCRIPT'
set -e
cd /home/ubuntu/claude-agent-sdk
mkdir -p .
tar -xzf /tmp/claude-agent-sdk-deploy.tar.gz -C /home/ubuntu/claude-agent-sdk
rm -f /tmp/claude-agent-sdk-deploy.tar.gz

python3 -m venv venv
source venv/bin/activate
pip install -e ".[actiondesign]"

sudo tee /etc/systemd/system/claude-agent-gateway.service > /dev/null << 'SERVICE'
[Unit]
Description=ActionDesign Thin Model Gateway
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/claude-agent-sdk
EnvironmentFile=/home/ubuntu/actiondesign-agent-gateway/.env
Environment="PATH=/home/ubuntu/claude-agent-sdk/venv/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/home/ubuntu/claude-agent-sdk/src"
Environment="PYTHONUNBUFFERED=1"
UMask=0077
ExecStart=/home/ubuntu/claude-agent-sdk/venv/bin/uvicorn claude_agent_sdk.actiondesign_gateway.app:app --host 0.0.0.0 --port 8888
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable claude-agent-gateway
sudo systemctl restart claude-agent-gateway
sleep 1
sudo systemctl status claude-agent-gateway --no-pager
REMOTE_SCRIPT
else
  ssh "${SSH_OPTIONS[@]}" "$USER@$SERVER" bash -s << 'REMOTE_SCRIPT'
set -e
cd /home/ubuntu/claude-agent-sdk
tar -xzf /tmp/claude-agent-sdk-deploy.tar.gz -C /home/ubuntu/claude-agent-sdk
rm -f /tmp/claude-agent-sdk-deploy.tar.gz
# Fast deploy intentionally does not resolve dependencies. Fail before
# restarting if the existing environment cannot load the current thin-gateway
# dependency set (notably the system Keyring client added for Profile secrets).
venv/bin/python -c 'import fastapi, httpx, jsonschema, keyring, pydantic'
sudo systemctl restart claude-agent-gateway
# uvicorn needs a moment after restart; retry health instead of a single sleep.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if sudo systemctl is-active --quiet claude-agent-gateway \
    && curl -sf http://127.0.0.1:8888/api/actiondesign-agent/health >/tmp/claude-agent-gateway-health.json; then
    cat /tmp/claude-agent-gateway-health.json
    echo
    break
  fi
  if [ "$i" -eq 10 ]; then
    echo "Health check failed after restart" >&2
    sudo systemctl status claude-agent-gateway --no-pager || true
    exit 1
  fi
  sleep 1
done
sudo systemctl is-active claude-agent-gateway
REMOTE_SCRIPT
fi

rm -f "$ARCHIVE"

echo "=== Deployment Complete ==="
echo "Service: http://$SERVER:$PORT"
echo "Health:  http://$SERVER:$PORT/api/actiondesign-agent/health"
