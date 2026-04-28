#!/bin/bash
# TV Stack — Folder Structure Setup
#
# Ensures the required directory tree exists under your MEDIA_BASE path.
# TV and Movies dirs are assumed to already exist; this will create them
# if they don't. Run once before starting the stack for the first time.
#
# Usage: bash setup-folders.sh

set -e

# Load MEDIA_BASE from .env if present, otherwise use default
if [ -f .env ]; then
    MEDIA_BASE=$(grep -E "^MEDIA_BASE=" .env | cut -d= -f2)
fi
MEDIA_BASE=${MEDIA_BASE:-/mnt/e/Video}

echo ""
echo "=== TV Stack — Folder Setup ==="
echo ""
echo "Media base: $MEDIA_BASE"
echo ""
echo "Ensuring the following structure exists:"
echo ""
echo "  $MEDIA_BASE/"
echo "  ├── TV/          ← Sonarr root folder"
echo "  ├── Movies/      ← Radarr root folder"
echo "  ├── Music/       ← Lidarr root folder"
echo "  └── Downloads/"
echo "      ├── tv/"
echo "      ├── movies/"
echo "      ├── music/"
echo "      └── usenet/  ← SABnzbd incomplete/complete"
echo ""
echo "NOTE: For hard links to work, all directories must be on the same"
echo "drive/filesystem. Everything here lives under $MEDIA_BASE — OK."
echo ""

mkdir -p "$MEDIA_BASE/TV"
mkdir -p "$MEDIA_BASE/Movies"
mkdir -p "$MEDIA_BASE/Music"
mkdir -p "$MEDIA_BASE/Downloads/tv"
mkdir -p "$MEDIA_BASE/Downloads/movies"
mkdir -p "$MEDIA_BASE/Downloads/music"
mkdir -p "$MEDIA_BASE/Downloads/usenet/incomplete"
mkdir -p "$MEDIA_BASE/Downloads/usenet/complete"

echo "Done! Current structure:"
if command -v tree &> /dev/null; then
    tree -L 2 "$MEDIA_BASE"
else
    find "$MEDIA_BASE" -maxdepth 2 -type d | sort
fi
echo ""
