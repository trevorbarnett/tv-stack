#!/usr/bin/env python3
"""
tv-reconcile.py — Reconcile local episode file ordering with Plex or TVMaze.

Compares episode titles parsed from local filenames (or extracted via OCR from
title cards) against a canonical episode list from Plex or TVMaze, then
generates rename commands to fix mismatched episode numbers.

Usage:
    # Using Plex as the reference (recommended; from WSL2, Plex is at 172.28.32.1)
    python3 tv-reconcile.py --show "Thomas the Tank Engine" --season 23 \\
        --dir "/mnt/e/Media/TV/Thomas the Tank Engine & Friends (1984)/Season 23" \\
        --source plex \\
        --plex-url http://172.28.32.1:32400 \\
        --plex-token YOUR_TOKEN

    # Using TVMaze (no token needed; only has data through ~season 21)
    python3 tv-reconcile.py --show "Thomas the Tank Engine" --season 10 \\
        --dir "/mnt/e/Media/TV/.../Season 10" \\
        --source tvmaze

    # Using TVDB (requires free API key from thetvdb.com)
    python3 tv-reconcile.py --show "Thomas the Tank Engine" --season 23 \\
        --dir "/mnt/e/Media/TV/.../Season 23" \\
        --source tvdb \\
        --tvdb-token YOUR_TVDB_TOKEN

    # Enable OCR title-card extraction (requires: apt install ffmpeg tesseract-ocr)
    python3 tv-reconcile.py ... --extract-titles

    # Actually rename the files (default is dry run)
    python3 tv-reconcile.py ... --execute

Get your Plex token: In Plex Web, open any item → ··· → Get Info → View XML.
The token is in the URL as X-Plex-Token=XXXXXXXX.

Get a free TVDB API key: https://thetvdb.com/api-information
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CanonicalEpisode:
    season: int
    episode: int
    title: str
    airdate: Optional[str] = None

@dataclass
class LocalFile:
    path: Path
    season: Optional[int]
    episode: Optional[int]
    parsed_title: Optional[str]
    ocr_title: Optional[str] = None

    @property
    def best_title(self) -> Optional[str]:
        return self.parsed_title or self.ocr_title

# ──────────────────────────────────────────────────────────────────────────────
# Filename parser
# ──────────────────────────────────────────────────────────────────────────────

# Matches: "Show Name - S23E01 - Episode Title Quality.ext"
# or just:  "S23E01 - Episode Title.mkv"
_EPISODE_RE = re.compile(
    r'[Ss](\d{1,2})[Ee](\d{1,3})'     # S23E01
    r'(?:\s*[-–]\s*'                   # optional " - "
    r'(.+?))?'                         # title (lazy)
    r'(?:\s+(?:WEBDL|WEBRip|BluRay|BDRip|HDTV|DVDRip|'
    r'AMZN|NF|DSNP|HMAX|PCOK|'
    r'2160p|1080p|720p|480p|x264|x265|HEVC|AVC)'
    r'.*)?$',
    re.IGNORECASE,
)

def parse_filename(path: Path) -> tuple[Optional[int], Optional[int], Optional[str]]:
    stem = path.stem
    m = _EPISODE_RE.search(stem)
    if not m:
        return None, None, None
    season = int(m.group(1))
    episode = int(m.group(2))
    title = m.group(3).strip() if m.group(3) else None
    # Strip trailing quality tags that slipped through
    if title:
        title = re.sub(
            r'\s+(?:WEBDL|WEBRip|BluRay|BDRip|HDTV|DVDRip|'
            r'2160p|1080p|720p|480p|x264|x265|HEVC|AVC).*$',
            '', title, flags=re.IGNORECASE
        ).strip()
        if not title:
            title = None
    return season, episode, title


def scan_directory(directory: Path, season: int) -> list[LocalFile]:
    video_exts = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov'}
    files = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in video_exts:
            continue
        s, e, title = parse_filename(path)
        if s is not None and s != season:
            continue
        files.append(LocalFile(path=path, season=s, episode=e, parsed_title=title))
    return files

# ──────────────────────────────────────────────────────────────────────────────
# Title matching
# ──────────────────────────────────────────────────────────────────────────────

def _normalize(title: str) -> str:
    """Lowercase, strip punctuation/articles for fuzzy comparison."""
    t = title.lower()
    t = re.sub(r"[''`]s\b", 's', t)         # Thomas's → thomass
    t = re.sub(r"[^a-z0-9 ]", ' ', t)
    t = re.sub(r'\b(the|a|an)\b', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def best_match(
    local_title: str,
    candidates: list[CanonicalEpisode],
    threshold: float = 0.55,
) -> tuple[Optional[CanonicalEpisode], float]:
    norm_local = _normalize(local_title)
    best: Optional[CanonicalEpisode] = None
    best_ratio = 0.0
    for ep in candidates:
        norm_ep = _normalize(ep.title)
        ratio = difflib.SequenceMatcher(None, norm_local, norm_ep).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = ep
    if best_ratio < threshold:
        return None, best_ratio
    return best, best_ratio

# ──────────────────────────────────────────────────────────────────────────────
# Plex client
# ──────────────────────────────────────────────────────────────────────────────

class PlexClient:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip('/')
        self.token = token

    def _get(self, path: str, params: dict = None) -> dict:
        qs = urllib.parse.urlencode({**(params or {}), 'X-Plex-Token': self.token})
        url = f"{self.base}{path}?{qs}"
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            print(f"  [error] Plex request failed: {e}", file=sys.stderr)
            sys.exit(1)

    def _find_tv_section(self) -> str:
        data = self._get('/library/sections')
        for section in data['MediaContainer']['Directory']:
            if section.get('type') == 'show':
                return section['key']
        raise RuntimeError("No TV show library found in Plex")

    def _find_show(self, section_key: str, show_name: str) -> str:
        data = self._get(f'/library/sections/{section_key}/all', {'type': '2'})
        shows = data['MediaContainer'].get('Metadata', [])
        # Try exact match first
        norm = show_name.lower()
        for s in shows:
            if s['title'].lower() == norm:
                return s['ratingKey']
        # Fuzzy
        best_key = None
        best_ratio = 0.0
        for s in shows:
            r = difflib.SequenceMatcher(None, norm, s['title'].lower()).ratio()
            if r > best_ratio:
                best_ratio = r
                best_key = s['ratingKey']
        if best_ratio < 0.6:
            titles = [s['title'] for s in shows[:10]]
            raise RuntimeError(
                f"Show '{show_name}' not found in Plex.\n"
                f"  Closest matches: {titles}"
            )
        matched = next(s for s in shows if s['ratingKey'] == best_key)
        print(f"  Matched Plex show: {matched['title']!r} (score {best_ratio:.2f})")
        return best_key

    def _find_season(self, show_key: str, season_num: int) -> str:
        data = self._get(f'/library/metadata/{show_key}/children')
        for season in data['MediaContainer'].get('Metadata', []):
            if season.get('index') == season_num:
                return season['ratingKey']
        raise RuntimeError(f"Season {season_num} not found in Plex")

    def get_episodes(self, show_name: str, season_num: int) -> list[CanonicalEpisode]:
        print(f"  Connecting to Plex at {self.base} …")
        section = self._find_tv_section()
        show_key = self._find_show(section, show_name)
        season_key = self._find_season(show_key, season_num)
        data = self._get(f'/library/metadata/{season_key}/children')
        episodes = []
        for ep in data['MediaContainer'].get('Metadata', []):
            episodes.append(CanonicalEpisode(
                season=season_num,
                episode=ep.get('index', 0),
                title=ep.get('title', ''),
                airdate=ep.get('originallyAvailableAt'),
            ))
        return sorted(episodes, key=lambda e: e.episode)

# ──────────────────────────────────────────────────────────────────────────────
# TVMaze client (no API key needed)
# ──────────────────────────────────────────────────────────────────────────────

class TVMazeClient:
    BASE = 'https://api.tvmaze.com'

    def _get(self, path: str, params: dict = None) -> dict | list:
        qs = ('?' + urllib.parse.urlencode(params)) if params else ''
        url = f"{self.BASE}{path}{qs}"
        req = urllib.request.Request(url, headers={'User-Agent': 'tv-reconcile/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            print(f"  [error] TVMaze request failed: {e}", file=sys.stderr)
            sys.exit(1)

    def _find_show_id(self, show_name: str) -> int:
        results = self._get('/search/shows', {'q': show_name})
        if not results:
            raise RuntimeError(f"Show '{show_name}' not found on TVMaze")
        best = results[0]['show']
        print(f"  Matched TVMaze show: {best['name']!r} (id {best['id']})")
        return best['id']

    def get_episodes(self, show_name: str, season_num: int) -> list[CanonicalEpisode]:
        print(f"  Querying TVMaze …")
        show_id = self._find_show_id(show_name)
        all_eps = self._get(f'/shows/{show_id}/episodes')
        episodes = []
        for ep in all_eps:
            if ep.get('season') == season_num:
                episodes.append(CanonicalEpisode(
                    season=season_num,
                    episode=ep['number'],
                    title=ep['name'],
                    airdate=ep.get('airdate'),
                ))
        if not episodes:
            raise RuntimeError(f"No episodes found for season {season_num}")
        return sorted(episodes, key=lambda e: e.episode)

# ──────────────────────────────────────────────────────────────────────────────
# TVDB v4 client (free API key from thetvdb.com)
# ──────────────────────────────────────────────────────────────────────────────

class TVDBClient:
    BASE = 'https://api4.thetvdb.com/v4'

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._token: Optional[str] = None

    def _auth(self):
        data = json.dumps({'apikey': self.api_key}).encode()
        req = urllib.request.Request(
            f"{self.BASE}/login",
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
                self._token = resp['data']['token']
        except (urllib.error.URLError, KeyError) as e:
            print(f"  [error] TVDB authentication failed: {e}", file=sys.stderr)
            sys.exit(1)

    def _get(self, path: str, params: dict = None) -> dict:
        if not self._token:
            self._auth()
        qs = ('?' + urllib.parse.urlencode(params)) if params else ''
        url = f"{self.BASE}{path}{qs}"
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {self._token}',
                'Accept': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            print(f"  [error] TVDB request failed: {e}", file=sys.stderr)
            sys.exit(1)

    def _find_series_id(self, show_name: str) -> int:
        data = self._get('/search', {'query': show_name, 'type': 'series'})
        results = data.get('data', [])
        if not results:
            raise RuntimeError(f"Show '{show_name}' not found on TVDB")
        # Pick best name match
        norm = show_name.lower()
        best = max(results, key=lambda r: difflib.SequenceMatcher(
            None, norm, r.get('name', '').lower()).ratio())
        print(f"  Matched TVDB show: {best['name']!r} (id {best['tvdb_id']})")
        return int(best['tvdb_id'])

    def get_episodes(self, show_name: str, season_num: int) -> list[CanonicalEpisode]:
        print(f"  Querying TVDB …")
        series_id = self._find_series_id(show_name)
        # Page through episodes (TVDB paginates at 500)
        page, episodes = 0, []
        while True:
            data = self._get(
                f'/series/{series_id}/episodes/default',
                {'season': season_num, 'page': page},
            )
            batch = data.get('data', {}).get('episodes', [])
            for ep in batch:
                if ep.get('seasonNumber') == season_num:
                    episodes.append(CanonicalEpisode(
                        season=season_num,
                        episode=ep['number'],
                        title=ep.get('name', ''),
                        airdate=ep.get('aired'),
                    ))
            links = data.get('links', {})
            if not links.get('next'):
                break
            page += 1
        if not episodes:
            raise RuntimeError(
                f"No episodes found for season {season_num} on TVDB.\n"
                "  Note: TVDB may use a different season numbering than Plex."
            )
        return sorted(episodes, key=lambda e: e.episode)


# ──────────────────────────────────────────────────────────────────────────────
# OCR title card extractor (requires ffmpeg + tesseract)
# ──────────────────────────────────────────────────────────────────────────────

class TitleCardExtractor:
    # Sample these timestamps (seconds) looking for a title card
    PROBE_TIMES = [15, 30, 45, 60, 75, 90, 105, 120, 150, 180]

    def __init__(self, candidates: list[CanonicalEpisode]):
        self._check_deps()
        self.candidates = candidates
        self._norm_candidates = {_normalize(ep.title): ep for ep in candidates}

    @staticmethod
    def _check_deps():
        missing = [t for t in ('ffmpeg', 'tesseract') if not shutil.which(t)]
        if missing:
            print(
                f"\n[error] OCR title extraction requires: {', '.join(missing)}\n"
                "  Install with:  sudo apt install ffmpeg tesseract-ocr\n",
                file=sys.stderr,
            )
            sys.exit(1)

    def _extract_frame(self, video: Path, ts: int, out: Path) -> bool:
        result = subprocess.run(
            ['ffmpeg', '-y', '-ss', str(ts), '-i', str(video),
             '-frames:v', '1', '-q:v', '2', str(out)],
            capture_output=True,
        )
        return result.returncode == 0 and out.exists()

    def _ocr_frame(self, frame: Path) -> str:
        result = subprocess.run(
            ['tesseract', str(frame), 'stdout', '--psm', '3'],
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def extract(self, video: Path) -> Optional[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            for ts in self.PROBE_TIMES:
                frame = Path(tmpdir) / f"frame_{ts:04d}.jpg"
                if not self._extract_frame(video, ts, frame):
                    continue
                text = self._ocr_frame(frame)
                if not text:
                    continue
                # Check each line of OCR output for a title match
                for line in text.splitlines():
                    line = line.strip()
                    if len(line) < 4 or len(line) > 80:
                        continue
                    match, score = best_match(line, self.candidates, threshold=0.70)
                    if match:
                        return match.title
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Rename logic
# ──────────────────────────────────────────────────────────────────────────────

def build_new_filename(old: Path, new_ep: CanonicalEpisode) -> str:
    """Replace S##E## in old filename with the correct episode number."""
    stem = old.stem
    new_stem = _EPISODE_RE.sub(
        lambda m: (
            f"S{new_ep.season:02d}E{new_ep.episode:02d}"
            + (f" - {new_ep.title}" if new_ep.title else '')
        ),
        stem,
        count=1,
    )
    return new_stem + old.suffix


def _safe_rename(src: Path, dst: Path):
    if dst.exists():
        raise FileExistsError(f"Target already exists: {dst}")
    src.rename(dst)


def execute_renames(renames: list[tuple[Path, Path]]):
    """Two-pass rename to avoid collision when files swap episode numbers."""
    tmp_dir = renames[0][0].parent
    temps: list[tuple[Path, Path]] = []

    # Pass 1: rename all to temp names
    for src, dst in renames:
        tmp = tmp_dir / (f"__tmp_{src.name}")
        _safe_rename(src, tmp)
        temps.append((tmp, dst))

    # Pass 2: rename temp → final
    for tmp, dst in temps:
        _safe_rename(tmp, dst)

# ──────────────────────────────────────────────────────────────────────────────
# Output / report
# ──────────────────────────────────────────────────────────────────────────────

RESET = '\033[0m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
RED = '\033[31m'
BOLD = '\033[1m'
DIM = '\033[2m'


def print_report(
    local_files: list[LocalFile],
    canonical: list[CanonicalEpisode],
    matches: list[tuple[LocalFile, Optional[CanonicalEpisode], float]],
):
    col_file = 55
    col_title = 35
    sep = '─'

    header = (
        f"\n{'File':<{col_file}}  {'Parsed Title':<{col_title}}  "
        f"{'Plex/Ref Title':<{col_title}}  {'Ep':<5}  {'Score':<6}  Status"
    )
    print(BOLD + header + RESET)
    print(sep * (col_file + col_title * 2 + 30))

    needs_rename = []
    unmatched = []

    for lf, canon, score in matches:
        filename = lf.path.name[:col_file]
        local_title = (lf.best_title or '—')[:col_title]
        if canon:
            canon_title = canon.title[:col_title]
            ep_str = f"E{canon.episode:02d}"
            score_str = f"{score:.2f}"
            if lf.episode == canon.episode:
                status = f"{GREEN}✓ OK{RESET}"
            else:
                status = f"{YELLOW}⚡ RENAME{RESET}"
                needs_rename.append((lf, canon))
        else:
            canon_title = '—'
            ep_str = '—'
            score_str = f"{score:.2f}"
            status = f"{RED}✗ NO MATCH{RESET}"
            unmatched.append(lf)

        print(
            f"{DIM}{filename:<{col_file}}{RESET}  "
            f"{local_title:<{col_title}}  "
            f"{canon_title:<{col_title}}  "
            f"{ep_str:<5}  {score_str:<6}  {status}"
        )

    print(sep * (col_file + col_title * 2 + 30))

    # Summary
    ok = len(matches) - len(needs_rename) - len(unmatched)
    print(f"\n{BOLD}Summary:{RESET} {ok} correct, {len(needs_rename)} to rename, {len(unmatched)} unmatched\n")

    return needs_rename, unmatched


def print_rename_plan(needs_rename: list[tuple[LocalFile, CanonicalEpisode]]):
    if not needs_rename:
        return
    print(f"{BOLD}Rename plan:{RESET}")
    renames: list[tuple[Path, Path]] = []
    for lf, canon in needs_rename:
        new_name = build_new_filename(lf.path, canon)
        new_path = lf.path.parent / new_name
        print(f"  {DIM}{lf.path.name}{RESET}")
        print(f"  → {new_name}\n")
        renames.append((lf.path, new_path))
    return renames

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Reconcile local episode file ordering with Plex or TVMaze.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--show', required=True,
                        help='Show name to search (e.g. "Thomas the Tank Engine")')
    parser.add_argument('--season', required=True, type=int,
                        help='Season number')
    parser.add_argument('--dir', required=True,
                        help='Local directory containing episode files')
    parser.add_argument('--source', default='tvmaze',
                        choices=['plex', 'tvmaze', 'tvdb'],
                        help='Reference episode source (default: tvmaze; '
                             'tvmaze only covers ~season 1-21 for older shows)')
    parser.add_argument('--plex-url',
                        default='http://172.28.32.1:32400',
                        help='Plex base URL (default: http://172.28.32.1:32400 — '
                             'Windows host IP from WSL2)')
    parser.add_argument('--plex-token',
                        help='Plex auth token. Get it: Plex Web → any item → '
                             '··· → Get Info → View XML → X-Plex-Token= in URL')
    parser.add_argument('--tvdb-token',
                        help='TVDB v4 API key from thetvdb.com/api-information')
    parser.add_argument('--extract-titles', action='store_true',
                        help='Use ffmpeg+tesseract OCR to extract titles from video frames '
                             '(fallback for files without titles in filenames; requires '
                             'apt install ffmpeg tesseract-ocr)')
    parser.add_argument('--threshold', type=float, default=0.55,
                        help='Fuzzy-match confidence threshold 0–1 (default 0.55)')
    parser.add_argument('--execute', action='store_true',
                        help='Actually rename files (default is dry run)')
    args = parser.parse_args()

    local_dir = Path(args.dir)
    if not local_dir.is_dir():
        print(f"[error] Directory not found: {local_dir}", file=sys.stderr)
        sys.exit(1)

    # ── Load canonical episode list ───────────────────────────────────────────
    print(f"\n{BOLD}[1/3] Fetching reference episode list …{RESET}")
    if args.source == 'plex':
        if not args.plex_token:
            print(
                "[error] --source plex requires --plex-token\n"
                "  Get yours: Plex Web → any item → ··· → Get Info → View XML\n"
                "  Look for X-Plex-Token= in the URL",
                file=sys.stderr,
            )
            sys.exit(1)
        client = PlexClient(args.plex_url, args.plex_token)
    elif args.source == 'tvdb':
        if not args.tvdb_token:
            print(
                "[error] --source tvdb requires --tvdb-token\n"
                "  Get a free key: https://thetvdb.com/api-information",
                file=sys.stderr,
            )
            sys.exit(1)
        client = TVDBClient(args.tvdb_token)
    else:
        client = TVMazeClient()

    canonical = client.get_episodes(args.show, args.season)
    print(f"  Got {len(canonical)} episodes for season {args.season}")

    # ── Scan local files ──────────────────────────────────────────────────────
    print(f"\n{BOLD}[2/3] Scanning local files in:{RESET} {local_dir}")
    local_files = scan_directory(local_dir, args.season)
    print(f"  Found {len(local_files)} episode files")

    # ── Optional OCR ─────────────────────────────────────────────────────────
    extractor = None
    if args.extract_titles:
        print(f"\n  OCR title extraction enabled …")
        extractor = TitleCardExtractor(canonical)

    # ── Match titles ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}[3/3] Matching titles …{RESET}")

    # For OCR: only run on files with no parsed title
    if extractor:
        ocr_needed = [lf for lf in local_files if not lf.parsed_title]
        if ocr_needed:
            print(f"  Running OCR on {len(ocr_needed)} untitled files …")
            for lf in ocr_needed:
                print(f"    {lf.path.name} …", end=' ', flush=True)
                lf.ocr_title = extractor.extract(lf.path)
                print(lf.ocr_title or '(no match)')

    matches: list[tuple[LocalFile, Optional[CanonicalEpisode], float]] = []
    for lf in local_files:
        title = lf.best_title
        if title:
            canon, score = best_match(title, canonical, threshold=args.threshold)
        else:
            canon, score = None, 0.0
        matches.append((lf, canon, score))

    # ── Report ────────────────────────────────────────────────────────────────
    print()
    needs_rename, unmatched = print_report(local_files, canonical, matches)

    # ── Rename plan ───────────────────────────────────────────────────────────
    if not needs_rename:
        print("Everything looks correct — no renames needed.")
        return

    renames = print_rename_plan(needs_rename)

    if unmatched:
        print(f"{YELLOW}Warning:{RESET} {len(unmatched)} file(s) had no match "
              "and will not be renamed:\n")
        for lf in unmatched:
            print(f"  {lf.path.name}")
        print()

    if args.execute:
        print(f"{BOLD}Executing renames …{RESET}")
        try:
            execute_renames(renames)
            print(f"{GREEN}Done.{RESET} {len(renames)} file(s) renamed.")
        except FileExistsError as e:
            print(f"{RED}[error]{RESET} {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"{DIM}Dry run — pass --execute to apply renames.{RESET}")


if __name__ == '__main__':
    main()
