#!/bin/sh
# Detect the current Windows host IP from WSL2's routing table (visible because
# this container runs with network_mode: host).
WINDOWS_HOST=$(ip route show default | awk '{print $3; exit}')
echo "plex-proxy: forwarding 172.39.0.1:32400 -> $WINDOWS_HOST:32400"
exec socat TCP4-LISTEN:32400,fork,bind=172.39.0.1,reuseaddr TCP4:$WINDOWS_HOST:32400
