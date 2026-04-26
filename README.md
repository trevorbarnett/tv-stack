# TV Stack

Automated media server stack running in Docker (WSL2 or Linux). VPN-protected torrenting with automated movie, TV, and music management.

## Services

| Service | Port | Purpose |
|---------|------|---------|
| qBittorrent | 8080 | Torrent client (VPN protected) |
| Prowlarr | 9696 | Indexer manager (VPN protected) |
| FlareSolverr | 8191 | Cloudflare bypass (VPN protected) |
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
    └── music/
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

### qBittorrent (http://localhost:8080)

Get the temporary password: `docker logs qbittorrent 2>&1 | grep "temporary password"`

1. **Settings → Downloads** — Default save path: `/data/Downloads`
2. **Settings → Downloads → Categories** — Add three categories:
   - `movies` → `/data/Downloads/movies`
   - `tv` → `/data/Downloads/tv`
   - `music` → `/data/Downloads/music`
3. **Settings → BitTorrent → Seeding Limits** — Set ratio limit to `1.0`, time limit to `1440` min (24h), action: Pause torrent

### Radarr (http://localhost:7878)

1. **Settings → Media Management** — Enable "Use Hardlinks instead of Copy"
2. **Settings → Media Management → Root Folders** — Add `/data/Movies`
3. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `movies`

### Sonarr (http://localhost:8989)

1. **Settings → Media Management** — Enable "Use Hardlinks instead of Copy"
2. **Settings → Media Management → Root Folders** — Add `/data/TV`
3. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `tv`

### Lidarr (http://localhost:8686)

1. **Settings → Media Management → Root Folders** — Add `/data/Music`
2. **Settings → Download Clients** — Add qBittorrent: host `172.39.0.2`, port `8080`, category `music`

### Prowlarr (http://localhost:9696)

1. **Settings → Apps** — Add Radarr: `http://172.39.0.3:7878` (get API key from Radarr → Settings → General)
2. **Settings → Apps** — Add Sonarr: `http://172.39.0.4:8989` (get API key from Sonarr → Settings → General)
3. **Indexers** — Add your preferred indexers; they sync automatically to Radarr/Sonarr

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
