#!/bin/bash
# Docker Compose health monitor — sends Discord alerts on container state changes,
# plus gluetun DNS health and Sonarr/Radarr indexer backoff state.
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
alerts_healed=()

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

# Gluetun DNS check — catches VPN tunnels that report "healthy" via Docker's
# healthcheck but have a broken internal DNS resolver (silent failure mode
# that doesn't show up as a container state change).
#
# Auto-remediation: `docker restart gluetun` followed by restarting the
# containers that share its network namespace (prowlarr/qbittorrent/
# flaresolverr). Verified this is required and sufficient: restarting gluetun
# alone gives it a *new* network namespace inode even though the container ID
# is unchanged, which silently orphans the dependents until they're restarted
# too. This is non-destructive (no config wipe, no recreate) — only escalate
# to the heavier `docker compose down gluetun && rm -rf gluetun/` fix manually
# if this doesn't clear it.
dns_state_file="$STATE_DIR/gluetun_dns"
dns_restart_cooldown_file="$STATE_DIR/gluetun_dns_restart_at"
prev_dns_ok="true"
if [[ -f "$dns_state_file" ]]; then
  prev_dns_ok=$(cat "$dns_state_file")
fi

check_gluetun_dns() {
  timeout 5 docker exec gluetun nslookup -timeout=3 google.com >/dev/null 2>&1
}

dns_ok="false"
dns_auto_healed="false"
if docker ps --format "{{.Names}}" 2>/dev/null | grep -qx "gluetun"; then
  if check_gluetun_dns; then
    dns_ok="true"
  else
    # Only attempt an auto-restart once every 15 minutes, so a persistently
    # broken VPN (bad creds, provider outage) doesn't restart-loop forever.
    last_restart=0
    if [[ -f "$dns_restart_cooldown_file" ]]; then
      last_restart=$(cat "$dns_restart_cooldown_file")
    fi
    now=$(date +%s)
    if (( now - last_restart > 900 )); then
      echo "$now" > "$dns_restart_cooldown_file"
      docker restart gluetun >/dev/null 2>&1 || true
      for _ in $(seq 1 12); do
        sleep 5
        h=$(docker inspect --format '{{.State.Health.Status}}' gluetun 2>/dev/null || true)
        if [[ "$h" == "healthy" ]]; then
          break
        fi
      done
      docker restart prowlarr qbittorrent flaresolverr >/dev/null 2>&1 || true
      sleep 5
      if check_gluetun_dns; then
        dns_ok="true"
        dns_auto_healed="true"
      fi
    fi
  fi
else
  dns_ok="true"  # container not running — already covered by the down alert above
fi

if [[ "$dns_auto_healed" == "true" ]]; then
  alerts_healed+=("**gluetun** — DNS resolver was broken; auto-restarted gluetun + dependents (prowlarr/qbittorrent/flaresolverr) and it recovered")
fi

if [[ "$dns_ok" == "false" && "$prev_dns_ok" == "true" ]]; then
  alerts_down+=("**gluetun** — VPN tunnel up but DNS resolution inside the container is broken (auto-restart attempted, did not clear it — see CLAUDE.md VPN reset steps)")
  echo "false" > "$dns_state_file"
elif [[ "$dns_ok" == "true" && "$prev_dns_ok" == "false" && "$dns_auto_healed" == "false" ]]; then
  alerts_recovered+=("**gluetun** — DNS resolution restored")
  echo "true" > "$dns_state_file"
else
  echo "$dns_ok" > "$dns_state_file"
fi

# *arr indexer backoff check — Sonarr/Radarr persist a long-term failure
# backoff per indexer that survives a plain restart (cleared only via
# /api/v3/indexer/testall). A VPN/network blip can leave indexers stuck
# "unavailable" long after connectivity is restored.
#
# Auto-remediation: call testall + trigger an RSS sync. Both are harmless,
# idempotent API calls (no restart/recreate), safe to retry every cycle.
# Reports per-indexer granularity and pulls Prowlarr log context on failure.
declare -A ARR_APPS=(
  [sonarr]="8989"
  [radarr]="7878"
)

# Extract names of down indexers from /api/v3/health JSON on stdin.
# Messages look like: "Indexer {name} is unavailable due to failures..."
parse_down_indexers() {
  python3 -c "
import sys, json, re
try:
    for item in json.load(sys.stdin):
        if item.get('source','') in ('IndexerLongTermStatusCheck','IndexerStatusCheck'):
            m = re.match(r'Indexer (.+?) is unavailable', item.get('message',''))
            if m: print(m.group(1))
except: pass
" 2>/dev/null
}

# Count total configured indexers via /api/v3/indexer.
count_indexers() {
  local app="$1" port="$2" key="$3"
  timeout 5 docker exec "$app" curl -s -H "X-Api-Key: $key" \
    "http://localhost:${port}/api/v3/indexer" 2>/dev/null \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?"
}

# Grep Prowlarr's current log for recent error lines mentioning a given indexer.
prowlarr_errors_for() {
  local name="$1"
  # Strip characters that would break the grep pattern inside the docker exec
  local safe_name
  safe_name=$(printf '%s' "$name" | tr -d "'\"\\\\" | cut -c1-60)
  docker exec prowlarr bash -c \
    "log=\$(find /config/logs -name '*.txt' 2>/dev/null | sort -r | head -1); \
     [[ -n \"\$log\" ]] && grep -i '$safe_name' \"\$log\" 2>/dev/null \
       | grep -iE 'error|fail|warn' | tail -3" \
    2>/dev/null || true
}

for app in "${!ARR_APPS[@]}"; do
  port="${ARR_APPS[$app]}"
  indexer_state_file="$STATE_DIR/${app}_indexers"
  prev_indexer_ok="true"
  [[ -f "$indexer_state_file" ]] && prev_indexer_ok=$(cat "$indexer_state_file")

  indexer_ok="true"
  indexer_auto_healed="false"
  indexer_alert_detail=""

  if docker ps --format "{{.Names}}" 2>/dev/null | grep -qx "$app"; then
    api_key=$(docker exec "$app" cat /config/config.xml 2>/dev/null | grep -oP '(?<=<ApiKey>)[^<]+' || true)
    if [[ -n "$api_key" ]]; then
      health=$(timeout 5 docker exec "$app" curl -s -H "X-Api-Key: $api_key" \
        "http://localhost:${port}/api/v3/health" 2>/dev/null || echo "[]")

      mapfile -t down_before < <(parse_down_indexers <<< "$health")

      if [[ ${#down_before[@]} -gt 0 ]]; then
        total=$(count_indexers "$app" "$port" "$api_key")

        # Auto-remediation
        timeout 10 docker exec "$app" curl -s -X POST -H "X-Api-Key: $api_key" -H "Content-Type: application/json" \
          "http://localhost:${port}/api/v3/indexer/testall" >/dev/null 2>&1 || true
        timeout 10 docker exec "$app" curl -s -X POST -H "X-Api-Key: $api_key" -H "Content-Type: application/json" \
          -d '{"name":"RssSync"}' "http://localhost:${port}/api/v3/command" >/dev/null 2>&1 || true
        sleep 3

        health2=$(timeout 5 docker exec "$app" curl -s -H "X-Api-Key: $api_key" \
          "http://localhost:${port}/api/v3/health" 2>/dev/null || echo "[]")
        mapfile -t down_after < <(parse_down_indexers <<< "$health2")

        if [[ ${#down_after[@]} -gt 0 ]]; then
          indexer_ok="false"

          # Build summary: "2 of 5 indexers down: `Name1`, `Name2`"
          down_names=$(printf '`%s`' "${down_after[@]}" | paste -sd ', ')
          indexer_alert_detail="${#down_after[@]} of ${total} indexers down: ${down_names}"
          healed_count=$(( ${#down_before[@]} - ${#down_after[@]} ))
          [[ $healed_count -gt 0 ]] && indexer_alert_detail+="; ${healed_count} recovered after testall"

          # Pull Prowlarr log context for each still-failing indexer
          if docker ps --format "{{.Names}}" 2>/dev/null | grep -qx "prowlarr"; then
            for idx in "${down_after[@]}"; do
              snippet=$(prowlarr_errors_for "$idx")
              if [[ -n "$snippet" ]]; then
                indexer_alert_detail+="\n**${idx}** (Prowlarr log):\n\`\`\`\n${snippet}\n\`\`\`"
              fi
            done
          fi
        else
          indexer_auto_healed="true"
        fi
      fi
    fi
  fi

  if [[ "$indexer_auto_healed" == "true" ]]; then
    alerts_healed+=("**$app** — indexers were stuck unavailable; ran testall + RSS sync and they recovered")
  fi

  if [[ "$indexer_ok" == "false" && "$prev_indexer_ok" == "true" ]]; then
    alerts_down+=("**$app** — ${indexer_alert_detail:-indexers stuck unavailable} (auto-remediation attempted, did not clear it)")
    echo "false" > "$indexer_state_file"
  elif [[ "$indexer_ok" == "true" && "$prev_indexer_ok" == "false" && "$indexer_auto_healed" == "false" ]]; then
    alerts_recovered+=("**$app** — indexers available again")
    echo "true" > "$indexer_state_file"
  else
    echo "$indexer_ok" > "$indexer_state_file"
  fi
done

# dlcache mount check — Samsung SSD 850 EVO 1TB passed through as raw ext4.
# Detaches when Windows reboots or WSL shuts down. Two-stage auto-remediation:
# 1) try a direct mount (handles wsl --shutdown where disk is still attached at
#    Windows level but the Linux mount is gone); 2) if that fails, fire the
# MountDLCache Windows scheduled task to reattach the raw disk, then mount.
# 15-minute cooldown prevents restart-looping if the disk is physically gone.
dlcache_state_file="$STATE_DIR/dlcache_mount"
dlcache_cooldown_file="$STATE_DIR/dlcache_mount_restart_at"
prev_dlcache_ok="true"
[[ -f "$dlcache_state_file" ]] && prev_dlcache_ok=$(cat "$dlcache_state_file")

check_dlcache() {
  mountpoint -q /mnt/dlcache 2>/dev/null
}

dlcache_ok="false"
dlcache_auto_healed="false"

if check_dlcache; then
  dlcache_ok="true"
else
  last_dlcache_attempt=0
  [[ -f "$dlcache_cooldown_file" ]] && last_dlcache_attempt=$(cat "$dlcache_cooldown_file")
  now=$(date +%s)
  if (( now - last_dlcache_attempt > 900 )); then
    echo "$now" > "$dlcache_cooldown_file"

    # Stage 1: disk still attached at Windows level, just unmounted
    sudo /usr/bin/mount /mnt/dlcache 2>/dev/null || true
    if check_dlcache; then
      dlcache_ok="true"
      dlcache_auto_healed="true"
    else
      # Stage 2: disk fully detached — fire MountDLCache Windows task to reattach
      /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe \
        -NonInteractive \
        -Command "Start-ScheduledTask -TaskName MountDLCache; Start-Sleep 5" \
        2>/dev/null || true
      for _ in $(seq 1 6); do
        sleep 5
        sudo /usr/bin/mount /mnt/dlcache 2>/dev/null && break || true
      done
      if check_dlcache; then
        dlcache_ok="true"
        dlcache_auto_healed="true"
      fi
    fi

    # Restart containers that bind-mount dlcache — Docker's rprivate propagation
    # means they captured the bare mountpoint at start and won't see the ext4
    # until restarted.
    if [[ "$dlcache_ok" == "true" ]]; then
      docker restart sabnzbd qbittorrent >/dev/null 2>&1 || true
    fi
  fi
fi

if [[ "$dlcache_auto_healed" == "true" ]]; then
  alerts_healed+=("**dlcache** — \`/mnt/dlcache\` was unmounted; auto-reattached, mounted, and restarted sabnzbd + qbittorrent")
fi

if [[ "$dlcache_ok" == "false" && "$prev_dlcache_ok" == "true" ]]; then
  alerts_down+=("**dlcache** — \`/mnt/dlcache\` not mounted (remediation attempted; check \`Get-Disk\` in PowerShell to confirm PHYSICALDRIVE3 is visible)")
  echo "false" > "$dlcache_state_file"
elif [[ "$dlcache_ok" == "true" && "$prev_dlcache_ok" == "false" && "$dlcache_auto_healed" == "false" ]]; then
  alerts_recovered+=("**dlcache** — \`/mnt/dlcache\` is mounted again")
  echo "true" > "$dlcache_state_file"
else
  echo "$dlcache_ok" > "$dlcache_state_file"
fi

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

if [[ ${#alerts_healed[@]} -gt 0 ]]; then
  body=$(printf '%s\n' "${alerts_healed[@]}" | paste -sd'\n' -)
  send_discord 3447003 "🔧 TV Stack — Auto-Remediated" "$body"
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
