#!/bin/sh
set -eu

SOURCE_ROOT=${1:-/home/ubuntu/claude-agent-sdk}
ENV_FILE=${2:-/home/ubuntu/actiondesign-agent-gateway/.env}
GATEWAY_USER=${3:-ubuntu}
GROUP_NAME=actiondesign-gateway
BRIDGE_PATH=/usr/local/libexec/actiondesign-mcp-stdio-bridge
NGINX_SITE=/etc/nginx/sites-available/actiondesign-gateway
NGINX_RUNTIME_DIR=/etc/nginx/actiondesign-mcp-runtime

if [ "$(id -u)" -ne 0 ]; then
  echo "installer must run as root" >&2
  exit 1
fi

upsert_env() {
  key=$1
  value=$2
  temp=$(mktemp "${ENV_FILE}.XXXXXX")
  awk -v key="$key" -v value="$value" '
    BEGIN { replaced = 0 }
    index($0, key "=") == 1 {
      if (!replaced) print key "=" value
      replaced = 1
      next
    }
    { print }
    END { if (!replaced) print key "=" value }
  ' "$ENV_FILE" > "$temp"
  chown --reference="$ENV_FILE" "$temp"
  chmod --reference="$ENV_FILE" "$temp"
  mv "$temp" "$ENV_FILE"
}

append_csv_env() {
  key=$1
  required=$2
  current=$(awk -F= -v key="$key" 'index($0, key "=") == 1 {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")
  case ",$current," in
    *",$required,"*) return ;;
  esac
  if [ -n "$current" ]; then
    upsert_env "$key" "$current,$required"
  else
    upsert_env "$key" "$required"
  fi
}

getent group "$GROUP_NAME" >/dev/null 2>&1 || groupadd --system "$GROUP_NAME"
usermod -a -G "$GROUP_NAME" "$GATEWAY_USER"
gateway_uid=$(id -u "$GATEWAY_USER")
gateway_gid=$(getent group "$GROUP_NAME" | cut -d: -f3)

token=$(awk -F= '/^ACTIONDESIGN_MCP_DEPLOYER_TOKEN=/{sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")
if [ "${#token}" -lt 32 ]; then
  token=$(openssl rand -hex 32)
fi

upsert_env ACTIONDESIGN_MCP_DEPLOYMENTS_ENABLED false
upsert_env ACTIONDESIGN_MCP_DEPLOYER_SOCKET /run/actiondesign-mcp-deployer/api.sock
upsert_env ACTIONDESIGN_MCP_DEPLOYER_TOKEN "$token"
upsert_env ACTIONDESIGN_MCP_STDIO_BRIDGE_PATH "$BRIDGE_PATH"
upsert_env ACTIONDESIGN_MCP_STDIO_BROKER_SOCKET /run/actiondesign-mcp-deployer/stdio.sock
upsert_env ACTIONDESIGN_MCP_STDIO_ALLOWED_UID "$gateway_uid"
upsert_env ACTIONDESIGN_MCP_STDIO_SOCKET_GID "$gateway_gid"
upsert_env ACTIONDESIGN_MCP_STDIO_MAX_SESSION_SECONDS 600
upsert_env ACTIONDESIGN_MCP_PUBLIC_BASE_URL https://43-135-137-212.sslip.io
upsert_env ACTIONDESIGN_MCP_DEPLOYMENT_STORE_PATH /var/lib/actiondesign-mcp/deployments.json
upsert_env ACTIONDESIGN_MCP_DEPLOY_WORK_ROOT /var/lib/actiondesign-mcp/work
upsert_env ACTIONDESIGN_MCP_DEPLOY_LOG_ROOT /var/log/actiondesign-mcp
upsert_env ACTIONDESIGN_MCP_NGINX_SNIPPET_ROOT "$NGINX_RUNTIME_DIR"
upsert_env ACTIONDESIGN_MCP_DEPLOY_ALLOWED_GIT_HOSTS github.com,gitlab.com,43-135-137-212.sslip.io
upsert_env ACTIONDESIGN_MCP_DEPLOY_ALLOWED_REGISTRIES docker.io,ghcr.io
upsert_env ACTIONDESIGN_MCP_DEPLOY_MAX_HTTP_RUNTIMES 2
upsert_env ACTIONDESIGN_MCP_DEPLOY_BUILD_TIMEOUT_SECONDS 900
upsert_env ACTIONDESIGN_MCP_BROWSER_VALIDATION_TIMEOUT_SECONDS 300
upsert_env ACTIONDESIGN_MANAGED_SECRET_STORE_PATH /home/ubuntu/claude-agent-sdk/config/actiondesign-managed-secrets.json
append_csv_env ACTIONDESIGN_MCP_ALLOWED_EXECUTABLE_PATHS "$BRIDGE_PATH"

install -d -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$SOURCE_ROOT/deploy/scripts/actiondesign-mcp-stdio-bridge" "$BRIDGE_PATH"
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/actiondesign-mcp-deployer.service" /etc/systemd/system/actiondesign-mcp-deployer.service
install -d -o root -g root -m 0700 "$NGINX_RUNTIME_DIR"
install -d -o root -g root -m 0700 /var/lib/actiondesign-mcp /var/log/actiondesign-mcp

if ! grep -Fq "include $NGINX_RUNTIME_DIR/*.conf;" "$NGINX_SITE"; then
  site_temp=$(mktemp "${NGINX_SITE}.XXXXXX")
  awk -v include_line="    include $NGINX_RUNTIME_DIR/*.conf;" '
    { print }
    !inserted && /ssl_dhparam/ { print include_line; inserted = 1 }
    END { if (!inserted) exit 2 }
  ' "$NGINX_SITE" > "$site_temp"
  chown root:root "$site_temp"
  chmod 0644 "$site_temp"
  site_backup="${NGINX_SITE}.pre-mcp-deployer-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$NGINX_SITE" "$site_backup"
  mv "$site_temp" "$NGINX_SITE"
  if ! nginx -t; then
    cp -a "$site_backup" "$NGINX_SITE"
    nginx -t
    exit 1
  fi
  systemctl reload nginx
fi

if ! docker buildx inspect actiondesign-mcp-builder >/dev/null 2>&1; then
  HOME=/var/lib/actiondesign-mcp docker buildx create \
    --name actiondesign-mcp-builder \
    --driver docker-container \
    --driver-opt network=bridge >/dev/null
fi

"$SOURCE_ROOT/venv/bin/python" -m pip install 'PyYAML>=6.0.2'
systemctl daemon-reload
systemctl enable --now actiondesign-mcp-deployer.service
