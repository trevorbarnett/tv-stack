# TV Stack

A self-hosted, fully automated media stack. You request something — it finds, downloads, organizes, and makes it available to stream. Everything routes through a VPN kill switch so your torrent traffic is always protected.

## What You Get

**Automated acquisition** — Sonarr (TV), Radarr (movies), and Lidarr (music) monitor RSS feeds and indexers. When a new episode airs or a release you've been waiting for drops, it's downloaded automatically without any manual intervention.

**Dual download sources** — SABnzbd handles Usenet downloads via Eweka; qBittorrent handles torrents. The \*arr apps prefer Usenet when an NZB is available (faster, more reliable) and fall back to torrents automatically. Neither requires manual selection.

**VPN kill switch** — qBittorrent and all indexer traffic route through Gluetun. If the VPN drops, traffic stops — it never falls back to your real IP. OpenVPN over TCP (port 443) is used for WSL2 compatibility; WireGuard is blocked at the Windows virtual switch level. SABnzbd does not need the VPN — Usenet uses encrypted SSL (port 563) and is not tracked like torrents.

**Centralized indexer management** — Prowlarr manages all your indexers in one place and syncs them to Sonarr, Radarr, and Lidarr automatically. Add an indexer once, not three times.

**Subtitle automation** — Bazarr watches your library and fetches matching subtitles automatically via OpenSubtitles.

**Request UI** — Seerr gives you (and anyone you share it with) a Netflix-style interface to browse and request media. Requests flow directly to Sonarr/Radarr for automated downloading.

**Hard links** — Downloaded files are hard-linked (not copied) into your media library. Seeding and streaming happen simultaneously with no wasted disk space and instant "moves."

**Remote access** — Tailscale exposes every service on a private WireGuard mesh. Access your stack from your phone, laptop, or anywhere else without port forwarding or exposing anything to the public internet.

**Dashboard** — Homepage gives you a live overview of all services, now-playing status, and quick links.

## Services

| Service | Port | Purpose |
|---------|------|---------|
| qBittorrent | 8080 | Torrent client (VPN protected) |
| Prowlarr | 9696 | Indexer manager (VPN protected) |
| FlareSolverr | 8191 | Cloudflare bypass (VPN protected) |
| SABnzbd | 8090 | Usenet client (Eweka) |
| Radarr | 7878 | Movie automation |
| Sonarr | 8989 | TV show automation |
| Lidarr | 8686 | Music automation |
| Bazarr | 6767 | Subtitle automation |
| Seerr | 5055 | Request UI (Netflix-like) |
| Homepage | 3000 | Dashboard |
| Tailscale | — | Remote access VPN |

> **Plex** runs as a Windows service (not in Docker). Add it to your Tailscale network separately.

---

## Prerequisites

- Docker + Docker Compose
- A Surfshark account (or adapt `.env` for any Gluetun-supported VPN)
- A Tailscale account (free tier works fine)

> **WSL2 note:** WireGuard does not work in WSL2 — UDP port 51820 is blocked at the Windows virtual switch. This stack uses **OpenVPN over TCP (port 443)**, which works reliably. The `.env.example` is already configured for this.

---

## Quick Start

```bash
git clone <this-repo>
cd tv

cp .env.example .env
# Edit .env — fill in your VPN credentials and Tailscale auth key

bash setup-folders.sh
docker compose up -d
bash test-stack.sh
```

---

## Step-by-Step Setup

### 1. Configure environment

```bash
cp .env.example .env
nano .env   # or your editor of choice
```

Fill in:

- **`MEDIA_BASE`** — path to your media drive (WSL: `/mnt/e/Media`, Linux: `/mnt/media`, etc.)
- **`PUID` / `PGID`** — your user/group ID (`id` in terminal to find them)
- **`OPENVPN_USER` / `OPENVPN_PASSWORD`** — Surfshark service credentials from [my.surfshark.com → VPN → Manual Setup → OpenVPN tab](https://my.surfshark.com/vpn/manual-setup/main). These are NOT your Surfshark login.
- **`TS_AUTHKEY`** — Tailscale auth key from [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)

### 2. Create media folders

```bash
bash setup-folders.sh
```

This creates the required directory tree under `MEDIA_BASE`:

```
$MEDIA_BASE/
├── TV/              ← Sonarr root folder
├── Movies/          ← Radarr root folder
├── Music/           ← Lidarr root folder
└── Downloads/
    ├── tv/
    ├── movies/
    ├── music/
    └── usenet/
        ├── incomplete/  ← SABnzbd working directory
        └── complete/    ← SABnzbd finished downloads
```

> All folders must be on the same filesystem for hard links to work (instant moves, no copying).

### 3. Start the stack

```bash
docker compose up -d
```

Wait 30–60 seconds for Gluetun to connect to the VPN. qBittorrent, Prowlarr, and FlareSolverr won't start until Gluetun is healthy.

### 4. Verify everything is working

```bash
bash test-stack.sh
```

This checks: Docker status, folder structure, container health, VPN connectivity (confirms qBittorrent IP ≠ your real IP), web UI accessibility, and hard link capability.

---

## Service Configuration

Use these **internal Docker IPs** when wiring services together — never use `localhost`:

| IP | Service |
|----|---------|
| `172.39.0.2` | Gluetun / qBittorrent / Prowlarr / FlareSolverr |
| `172.39.0.3` | Radarr |
| `172.39.0.4` | Sonarr |
| `172.39.0.5` | Lidarr |
| `172.39.0.6` | Bazarr |
| `172.39.0.8` | Seerr |
| `172.39.0.9` | Homepage |
| `172.39.0.10` | SABnzbd |

### qBittorrent (http://localhost:8080)

Get the temporary password: `docker logs qbittorrent 2>&1 | grep "temporary password"`

1. **Settings → Downloads** — Default save path: `/data/Downloads`
2. **Settings → Downloads → Categories** — Add three categories:
   - `movies` → `/data/Downloads/movies`
   - `tv` → `/data/Downloads/tv`
   - `music` → `/data/Downloads/music`
3. **Settings → BitTorrent → Seeding Limits** — Set ratio limit to `1.0`, time limit to `1440` min (24h), action: Pause torrent

### SABnzbd (http://localhost:8090)

Usenet download client. Connects to Eweka over SSL — no VPN needed.

1. **Config → Servers → Add Server**
   - Host: `news.eweka.nl`, Port: `563`, SSL: on
   - Enter your Eweka username and password
   - Connections: `30` (Eweka's maximum)
   - Click **Test Server** — should return green
2. **Config → Folders**
   - Temporary Download Folder: `/data/Downloads/usenet/incomplete`
   - Completed Download Folder: `/data/Downloads/usenet/complete`

API key is in **Config → General** — needed when wiring up Radarr/Sonarr/Lidarr.

> **Access denied on first load?** SABnzbd's host verification can block Docker-proxied requests. Stop the container, add `localhost` and your Tailscale IP to `host_whitelist` in `sabnzbd/sabnzbd.ini`, set `inet_exposure = 4`, then restart.

### Radarr (http://localhost:7878)

1. **Settings → Media Management** — Enable "Use Hardlinks instead of Copy"
2. **Settings → Media Management → Root Folders** — Add `/data/Movies`
3. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `movies`
4. **Settings → Download Clients** — Add SABnzbd: host `172.39.0.10`, port `8080`, category `movies`
5. **Settings → Connect → Add → Plex Media Server** — Host `host.docker.internal`, port `32400`, add your Plex token. Enable "Update Library" so Plex scans automatically after each download.

> **Plex token:** In Plex web, open any item → ··· → Get Info → View XML. The token is in the URL as `X-Plex-Token=XXXXXXX`.

### Sonarr (http://localhost:8989)

1. **Settings → Media Management** — Enable "Use Hardlinks instead of Copy"
2. **Settings → Media Management → Root Folders** — Add `/data/TV`
3. **Settings → Media Management → Season Folders** — Enable (organizes episodes into per-season subfolders)
4. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `tv`
5. **Settings → Download Clients** — Add SABnzbd: host `172.39.0.10`, port `8080`, category `tv`
6. **Settings → Connect → Add → Plex Media Server** — same as Radarr above

> **Reorganizing existing shows:** Series Editor → select all → Rename Files. This moves files on disk to match current folder settings.

### Lidarr (http://localhost:8686)

1. **Settings → Media Management → Root Folders** — Add `/data/Music`
2. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `music`
3. **Settings → Download Clients** — Add SABnzbd: host `172.39.0.10`, port `8080`, category `music`

### Prowlarr (http://localhost:9696)

1. **Settings → Apps** — Add Radarr: `http://172.39.0.3:7878` (get API key from Radarr → Settings → General)
2. **Settings → Apps** — Add Sonarr: `http://172.39.0.4:8989` (get API key from Sonarr → Settings → General)
3. **Settings → Download Clients** — Add SABnzbd: host `172.39.0.10`, port `8080`, category `prowlarr` (used for manual grabs from Prowlarr's search UI only — the \*arrs use their own download client configs)
4. **Indexers** — Add torrent and Usenet indexers; they sync automatically to Radarr/Sonarr/Lidarr. For Usenet, you need a separate indexer account (e.g. NZBFinder, NZBGeek) — Eweka is the download provider, not a search index.

> **Re-triggering searches after adding Usenet indexers:** Go to Radarr → Wanted → Missing → Search All, and Sonarr → Wanted → Missing → Search Selected. To force indexer sync first: Settings → Apps → Sync App Indexers.

### Bazarr (http://localhost:6767)

1. **Settings → Radarr** — Host `172.39.0.3`, port `7878`, API key from Radarr
2. **Settings → Sonarr** — Host `172.39.0.4`, port `8989`, API key from Sonarr
3. **Settings → Providers** — Add OpenSubtitles.com (free account required)

### Seerr (http://localhost:5055)

1. Connect to Plex on first-run wizard (use `host.docker.internal:32400` if Plex is on the same Windows machine)
2. **Settings → Radarr** — Host `172.39.0.3`, port `7878`
3. **Settings → Sonarr** — Host `172.39.0.4`, port `8989`

---

## Remote Access (Tailscale)

Once Tailscale is running (`docker compose up -d tailscale`), find your server's Tailscale IP:

```bash
docker exec tailscale tailscale ip -4
```

Access any service remotely at `http://<tailscale-ip>:<port>`. Works from any device on your Tailscale network.

---

## VPN Troubleshooting

```bash
# Check VPN logs
docker logs gluetun 2>&1 | tail -20

# Confirm qBittorrent is tunneled (IP should match Gluetun, not your real IP)
docker exec gluetun wget -qO- ipinfo.io/ip
docker exec qbittorrent wget -qO- ipinfo.io/ip

# Full reset
docker compose down gluetun
rm -rf gluetun/
docker compose up -d gluetun
```

---

## Common Commands

```bash
# Start / stop
docker compose up -d
docker compose down

# Restart a single service
docker compose restart sonarr

# Follow logs
docker logs sonarr -f

# Update all images
docker compose pull && docker compose up -d

# Health check
bash test-stack.sh
```
