#!/bin/bash
# Docker Compose health monitor — sends Discord alerts on container state changes.
# Runs every 5 minutes via cron. Only notifies on transitions (up→down, down→up)
# to avoid spam. State is persisted in /tmp/docker-health-state/.

set -euo pipefail

COMPOSE_DIR="/home/tbarnett/projects/tv"
STATE_DIR="/tmp/docker-health-state"
mkdir -p "$STATE_DIR"

# Load webhook from .env
WEBHOOK_URL=""
if [[ -f "$COMPOSE_DIR/.env" ]]; then
  WEBHOOK_URL=$(grep "^DISCORD_WEBHOOK=" "$COMPOSE_DIR/.env" | cut -d= -f2- | tr -d '[:space:]')
fi

if [[ -z "$WEBHOOK_URL" ]]; then
  echo "ERROR: DISCORD_WEBHOOK not set in $COMPOSE_DIR/.env" >&2
  exit 1
fi

# Expected containers managed by this stack
EXPECTED=(
  gluetun qbittorrent flaresolverr prowlarr sabnzbd
  radarr sonarr lidarr bazarr seerr tautulli
  homepage tailscale watchtower deunhealth
)
# media-check is a one-shot container — skip it in continuous monitoring

send_discord() {
  local color="$1"  # 16711680=red, 65280=green, 16776960=yellow
  local title="$2"
  local description="$3"
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  curl -s -o /dev/null -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "{
      \"embeds\": [{
        \"title\": \"$title\",
        \"description\": \"$description\",
        \"color\": $color,
        \"footer\": {\"text\": \"tv-stack · $(hostname)\"},
        \"timestamp\": \"$timestamp\"
      }]
    }"
}

# Collect current states
declare -A current_states

while IFS= read -r line; do
  name=$(echo "$line" | awk '{print $1}')
  status=$(echo "$line" | awk '{$1=""; print $0}' | xargs)
  current_states["$name"]="$status"
done < <(docker ps -a --format "{{.Names}} {{.Status}}" 2>/dev/null)

alerts_down=()
alerts_recovered=()

for svc in "${EXPECTED[@]}"; do
  state_file="$STATE_DIR/$svc"
  current="${current_states[$svc]:-missing}"

  # Determine if this state is healthy
  if echo "$current" | grep -qiE "^Up "; then
    healthy=true
  else
    healthy=false
  fi

  # Read previous state
  prev_healthy="true"
  if [[ -f "$state_file" ]]; then
    prev_healthy=$(cat "$state_file")
  fi

  if [[ "$healthy" == "false" && "$prev_healthy" == "true" ]]; then
    # Transition: healthy → down
    alerts_down+=("**$svc** — \`$current\`")
    echo "false" > "$state_file"
  elif [[ "$healthy" == "true" && "$prev_healthy" == "false" ]]; then
    # Transition: down → recovered
    alerts_recovered+=("**$svc** — $current")
    echo "true" > "$state_file"
  else
    # No change — write current state (handles first run)
    if [[ "$healthy" == "true" ]]; then
      echo "true" > "$state_file"
    else
      echo "false" > "$state_file"
    fi
  fi
done

# Check for containers in unhealthy health state (separate from exit state)
unhealthy_containers=()
while IFS= read -r line; do
  name=$(echo "$line" | awk '{print $1}')
  # Only alert on explicitly unhealthy, not just "no healthcheck"
  if docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null | grep -q "^unhealthy$"; then
    unhealthy_state_file="$STATE_DIR/${name}_health"
    prev="true"
    [[ -f "$unhealthy_state_file" ]] && prev=$(cat "$unhealthy_state_file")
    if [[ "$prev" == "true" ]]; then
      unhealthy_containers+=("**$name** — health check failing")
      echo "false" > "$unhealthy_state_file"
    fi
  else
    echo "true" > "$STATE_DIR/${name}_health" 2>/dev/null || true
  fi
done < <(docker ps --format "{{.Names}}" 2>/dev/null)

# Send notifications
if [[ ${#alerts_down[@]} -gt 0 ]] || [[ ${#unhealthy_containers[@]} -gt 0 ]]; then
  all_alerts=("${alerts_down[@]:-}" "${unhealthy_containers[@]:-}")
  body=$(printf '%s\n' "${all_alerts[@]}" | paste -sd'\n' -)
  send_discord 16711680 "🚨 TV Stack — Container Down" "$body"
fi

if [[ ${#alerts_recovered[@]} -gt 0 ]]; then
  body=$(printf '%s\n' "${alerts_recovered[@]}" | paste -sd'\n' -)
  send_discord 65280 "✅ TV Stack — Container Recovered" "$body"
fi

# Daily summary — fires once per day (around midnight) regardless of state changes
summary_file="$STATE_DIR/.daily_summary"
today=$(date +%Y-%m-%d)
last_summary=""
[[ -f "$summary_file" ]] && last_summary=$(cat "$summary_file")

if [[ "$last_summary" != "$today" ]]; then
  down_list=()
  up_list=()
  for svc in "${EXPECTED[@]}"; do
    current="${current_states[$svc]:-missing}"
    if echo "$current" | grep -qiE "^Up "; then
      up_list+=("✅ $svc")
    else
      down_list+=("❌ $svc — \`$current\`")
    fi
  done

  all_lines=("${down_list[@]:-}" "${up_list[@]:-}")
  body=$(printf '%s\n' "${all_lines[@]}" | paste -sd'\n' -)
  color=65280
  [[ ${#down_list[@]} -gt 0 ]] && color=16776960

  send_discord $color "📊 TV Stack — Daily Health Summary" "$body"
  echo "$today" > "$summary_file"
fi
