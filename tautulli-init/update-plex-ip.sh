#!/bin/bash
# plex-proxy (172.39.0.1:32400) is a stable socat forwarder that routes to
# the Windows Plex service regardless of which IP WSL2 assigns on each reboot.
PLEX_PROXY="172.39.0.1"
CFG="/config/config.ini"

if [ -f "$CFG" ]; then
    sed -i "s/^pms_ip = .*/pms_ip = ${PLEX_PROXY}/" "$CFG"
    sed -i "s|^pms_url = http://.*:32400|pms_url = http://${PLEX_PROXY}:32400|" "$CFG"
    echo "[init] tautulli: pms_ip set to ${PLEX_PROXY}"
fi
