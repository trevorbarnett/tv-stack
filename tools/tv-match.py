#!/usr/bin/env python3
"""
tv-match.py — Interactive curses TUI to match local episode files to Plex ordering.

Local files on the left; Plex canonical episodes on the right.  Navigate the
left panel, the right panel auto-jumps to the best fuzzy suggestion.  Press
ENTER to accept the suggestion (or TAB over to pick a different one), then
confirm at the end to rename + force Plex refresh.

Usage (from WSL2 — Plex is at 172.28.32.1):
    python3 tools/tv-match.py \\
        --show "Thomas the Tank Engine" \\
        --season 23 \\
        --dir "/mnt/e/Media/TV/Thomas the Tank Engine & Friends (1984)/Season 23" \\
        --plex-token YOUR_TOKEN

Plex token: Web UI → any item → ··· → Get Info → View XML → X-Plex-Token= in URL.
"""

import argparse
import curses
import difflib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _load_env() -> dict[str, str]:
    """Walk up from this script's directory looking for a .env file."""
    here = Path(__file__).resolve().parent
    for directory in [here, *here.parents]:
        candidate = directory / '.env'
        if candidate.exists():
            env: dict[str, str] = {}
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
            return env
    return {}


_ENV = _load_env()


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PlexEp:
    season: int
    number: int
    title: str
    rating_key: str
    airdate: Optional[str] = None


@dataclass
class LocalFile:
    path: Path
    season: Optional[int]
    number: Optional[int]
    title: Optional[str]


# ── Filename parsing ──────────────────────────────────────────────────────────

_EP_RE = re.compile(
    r'[Ss](\d{1,2})[Ee](\d{1,3})'
    r'(?:\s*[-–]\s*(.+?))?'
    r'(?:\s+(?:WEBDL|WEBRip|BluRay|BDRip|HDTV|DVDRip|AMZN|NF|DSNP|HMAX|PCOK|'
    r'2160p|1080p|720p|480p|x264|x265|HEVC|AVC).*)?$',
    re.IGNORECASE,
)
_QUALITY_RE = re.compile(
    r'\s+(?:WEBDL|WEBRip|BluRay|BDRip|HDTV|DVDRip|AMZN|NF|DSNP|HMAX|PCOK|'
    r'2160p|1080p|720p|480p|x264|x265|HEVC|AVC).*$',
    re.IGNORECASE,
)


def _parse_file(path: Path) -> tuple[Optional[int], Optional[int], Optional[str]]:
    m = _EP_RE.search(path.stem)
    if not m:
        return None, None, None
    s, e = int(m.group(1)), int(m.group(2))
    title = (m.group(3) or '').strip()
    title = _QUALITY_RE.sub('', title).strip() or None
    return s, e, title


def scan_dir(directory: Path, season: int) -> list[LocalFile]:
    exts = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov'}
    files = []
    for p in sorted(directory.iterdir()):
        if p.suffix.lower() not in exts:
            continue
        s, e, t = _parse_file(p)
        if s is not None and s != season:
            continue
        files.append(LocalFile(path=p, season=s, number=e, title=t))
    return files


# ── Fuzzy matching ────────────────────────────────────────────────────────────

def _norm(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[''`]s\b", 's', t)
    t = re.sub(r'[^a-z0-9 ]', ' ', t)
    t = re.sub(r'\b(the|a|an)\b', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def best_plex_match(title: str, plex: list[PlexEp], skip: set[int]) -> Optional[int]:
    norm = _norm(title)
    best_i, best_r = None, 0.45
    for i, ep in enumerate(plex):
        if i in skip:
            continue
        r = difflib.SequenceMatcher(None, norm, _norm(ep.title)).ratio()
        if r > best_r:
            best_r, best_i = r, i
    return best_i


# ── Plex client ───────────────────────────────────────────────────────────────

class Plex:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip('/')
        self.token = token
        self._section: Optional[str] = None
        self._season_key: Optional[str] = None

    def _get(self, path: str, params: dict | None = None) -> dict:
        qs = urllib.parse.urlencode({**(params or {}), 'X-Plex-Token': self.token})
        req = urllib.request.Request(
            f"{self.base}{path}?{qs}",
            headers={'Accept': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            raise RuntimeError(f"Plex {path}: {e}") from e

    def _put(self, path: str, params: dict | None = None):
        qs = urllib.parse.urlencode({**(params or {}), 'X-Plex-Token': self.token})
        req = urllib.request.Request(f"{self.base}{path}?{qs}", method='PUT')
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except urllib.error.URLError as e:
            raise RuntimeError(f"Plex PUT {path}: {e}") from e

    def load(self, show_name: str, season_num: int) -> tuple[list[PlexEp], str]:
        # Find TV library section
        data = self._get('/library/sections')
        self._section = next(
            (s['key'] for s in data['MediaContainer']['Directory'] if s.get('type') == 'show'),
            None,
        )
        if not self._section:
            raise RuntimeError("No TV show library in Plex")

        # Find the show
        data = self._get(f'/library/sections/{self._section}/all', {'type': '2'})
        shows = data['MediaContainer'].get('Metadata', [])
        norm = show_name.lower()
        best = max(shows, key=lambda s: difflib.SequenceMatcher(None, norm, s['title'].lower()).ratio())
        show_title = best['title']
        show_key = best['ratingKey']

        # Find the season
        data = self._get(f'/library/metadata/{show_key}/children')
        season_key = next(
            (s['ratingKey'] for s in data['MediaContainer'].get('Metadata', [])
             if s.get('index') == season_num),
            None,
        )
        if not season_key:
            raise RuntimeError(f"Season {season_num} not found for '{show_title}'")
        self._season_key = season_key

        # Load episodes
        data = self._get(f'/library/metadata/{season_key}/children')
        eps = [
            PlexEp(
                season=season_num,
                number=ep.get('index', 0),
                title=ep.get('title', ''),
                rating_key=ep.get('ratingKey', ''),
                airdate=ep.get('originallyAvailableAt'),
            )
            for ep in data['MediaContainer'].get('Metadata', [])
        ]
        return sorted(eps, key=lambda e: e.number), show_title

    def refresh(self):
        if self._season_key:
            self._put(f'/library/metadata/{self._season_key}/refresh', {'force': '1'})
        elif self._section:
            self._get(f'/library/sections/{self._section}/refresh')


# ── Sonarr client ─────────────────────────────────────────────────────────────

class Sonarr:
    def __init__(self, base: str, api_key: str):
        self.base = base.rstrip('/')
        self.key = api_key

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Sonarr {method} {path}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Sonarr {method} {path}: {e}") from e

    def _find_series_id(self, show_name: str) -> int:
        series = self._req('GET', '/api/v3/series')
        norm = show_name.lower()
        best = max(series, key=lambda s: difflib.SequenceMatcher(None, norm, s['title'].lower()).ratio())
        return best['id']

    def find_episode(self, show_name: str, season: int, ep_title: str) -> dict:
        """Return the Sonarr episode record best matching the given title."""
        series_id = self._find_series_id(show_name)
        episodes = self._req('GET', f'/api/v3/episode?seriesId={series_id}&seasonNumber={season}')
        norm = ep_title.lower()
        return max(episodes, key=lambda e: difflib.SequenceMatcher(None, norm, e['title'].lower()).ratio())

    def delete_and_search(self, episode: dict) -> str:
        """Delete the episode file from disk and queue a fresh search in Sonarr."""
        file_id = episode.get('episodeFileId')
        if file_id:
            self._req('DELETE', f'/api/v3/episodefile/{file_id}')
        self._req('POST', '/api/v3/command', {'name': 'EpisodeSearch', 'episodeIds': [episode['id']]})
        s, e = episode['seasonNumber'], episode['episodeNumber']
        return f"Deleted file + queued search for S{s:02d}E{e:02d} - {episode['title']}"


# ── Rename helpers ────────────────────────────────────────────────────────────

def _build_new_path(old: Path, ep: PlexEp) -> Path:
    stem = old.stem
    m = re.search(r'[Ss]\d{1,2}[Ee]\d{1,3}', stem)
    if not m:
        return old.parent / (f"S{ep.season:02d}E{ep.number:02d} - {ep.title}{old.suffix}")
    prefix = stem[:m.start()]
    rest = stem[m.end():]
    qm = _QUALITY_RE.search(rest)
    quality = qm.group(0) if qm else ''
    new_stem = f"{prefix}S{ep.season:02d}E{ep.number:02d} - {ep.title}{quality}"
    return old.parent / (new_stem + old.suffix)


def _two_pass_rename(pairs: list[tuple[Path, Path]]):
    tmps = []
    for src, _ in pairs:
        tmp = src.parent / f"__tvmatch_{src.name}"
        src.rename(tmp)
        tmps.append(tmp)
    for tmp, (_, dst) in zip(tmps, pairs):
        tmp.rename(dst)


# ── Curses colors ─────────────────────────────────────────────────────────────
#  1 = normal   2 = header    3 = sel-focused   4 = sel-blurred
#  5 = matched (correct)      6 = unmatched     7 = status    8 = dim
#  9 = matched but will rename (cyan)

_HDR  = 2
_SELF = 3
_SELB = 4
_OK   = 5   # assigned, filename already correct — green
_BAD  = 6   # no assignment — red
_STAT = 7
_DIM  = 8
_CHG  = 9   # assigned, will be renamed — cyan


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE,  -1)
    curses.init_pair(_HDR,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(_SELF, curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(_SELB, curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(_OK,   curses.COLOR_GREEN,  -1)
    curses.init_pair(_BAD,  curses.COLOR_RED,    -1)
    curses.init_pair(_STAT, curses.COLOR_YELLOW, -1)
    curses.init_pair(_DIM,  curses.COLOR_WHITE,  -1)
    curses.init_pair(_CHG,  curses.COLOR_CYAN,   -1)


# ── Match App ─────────────────────────────────────────────────────────────────

class App:
    def __init__(self, files: list[LocalFile], plex: list[PlexEp], show_title: str, season: int,
                 sonarr: Optional['Sonarr'] = None):
        self.files = files
        self.plex = plex
        self.title = show_title
        self.season = season
        self.sonarr = sonarr

        # Bidirectional assignment map
        self.f2p: dict[int, Optional[int]] = {i: None for i in range(len(files))}
        self.p2f: dict[int, Optional[int]] = {i: None for i in range(len(plex))}

        self.li = 0        # left cursor (file index)
        self.pi = 0        # right cursor (plex index)
        self.ls = 0        # left scroll
        self.rs = 0        # right scroll
        self.focus = 'L'   # 'L' | 'R'
        self.status = "↑↓ nav   TAB switch panel   ENTER assign   U unassign   A auto-fill   C confirm   Q quit"
        self.done = False

    # ── state ─────────────────────────────────────────────────────────────────

    def _assign(self, fi: int, pi: int):
        old_pi = self.f2p[fi]
        if old_pi is not None:
            self.p2f[old_pi] = None
        old_fi = self.p2f[pi]
        if old_fi is not None:
            self.f2p[old_fi] = None
        self.f2p[fi] = pi
        self.p2f[pi] = fi

    def _unassign(self, fi: int):
        pi = self.f2p[fi]
        if pi is not None:
            self.p2f[pi] = None
        self.f2p[fi] = None

    def _suggest(self, fi: int) -> Optional[int]:
        lf = self.files[fi]
        if not lf.title:
            return None
        skip = {v for v in self.f2p.values() if v is not None and v != self.f2p[fi]}
        return best_plex_match(lf.title, self.plex, skip)

    def _auto_fill(self):
        used: set[int] = {v for v in self.f2p.values() if v is not None}
        for fi, lf in enumerate(self.files):
            if self.f2p[fi] is not None or not lf.title:
                continue
            m = best_plex_match(lf.title, self.plex, used)
            if m is not None:
                self._assign(fi, m)
                used.add(m)

    def _jump_right(self):
        assigned = self.f2p[self.li]
        target = assigned if assigned is not None else self._suggest(self.li)
        if target is not None:
            self.pi = target

    def _advance_left(self):
        for i in range(self.li + 1, len(self.files)):
            if self.f2p[i] is None:
                self.li = i
                return
        for i in range(0, self.li + 1):
            if self.f2p[i] is None:
                self.li = i
                return

    # ── scroll helpers ────────────────────────────────────────────────────────

    def _clamp(self, idx: int, scroll: int, height: int) -> int:
        if idx < scroll:
            return idx
        if idx >= scroll + height:
            return idx - height + 1
        return scroll

    # ── main curses loop ──────────────────────────────────────────────────────

    def run(self) -> bool:
        curses.wrapper(self._loop)
        return self.done

    def _loop(self, scr):
        _init_colors()
        curses.curs_set(0)
        scr.keypad(True)
        while True:
            h, w = scr.getmaxyx()
            scr.erase()
            self._draw(scr, h, w)
            scr.refresh()
            key = scr.getch()
            result = self._key(key, scr, h, w)
            if result == 'quit':
                return
            if result == 'confirmed':
                self.done = True
                return

    # ── drawing ───────────────────────────────────────────────────────────────

    def _draw(self, scr, h: int, w: int):
        matched = sum(1 for v in self.f2p.values() if v is not None)
        total = len(self.files)
        mid = w // 2
        lw = mid - 1         # left panel width
        rw = w - mid - 1     # right panel width
        list_h = h - 5       # header(1) + col-hdr(1) + divider(1) + keys(1) + status(1)

        # Header ──────────────────────────────────────────────────────────────
        lind = '▶ ' if self.focus == 'L' else '  '
        rind = '▶ ' if self.focus == 'R' else '  '
        hdr = f" {self.title} — S{self.season:02d}   {matched}/{total} matched   {lind}LOCAL  {rind}PLEX "
        _attr(scr, 0, 0, hdr.ljust(w - 1), curses.color_pair(_HDR) | curses.A_BOLD)

        # Column headers ──────────────────────────────────────────────────────
        _attr(scr, 1, 0, ' LOCAL FILES'.ljust(lw), curses.A_BOLD)
        _attr(scr, 1, mid + 1, ' PLEX EPISODES'.ljust(rw), curses.A_BOLD)

        # Vertical separator ──────────────────────────────────────────────────
        for r in range(1, h - 3):
            try:
                scr.addch(r, mid, curses.ACS_VLINE)
            except curses.error:
                pass

        # Scroll clamping ─────────────────────────────────────────────────────
        self.ls = self._clamp(self.li, self.ls, list_h)
        self.rs = self._clamp(self.pi, self.rs, list_h)

        # Left panel (local files) ────────────────────────────────────────────
        for i, lf in enumerate(self.files):
            row = 2 + i - self.ls
            if row < 2 or row > h - 4:
                continue
            pi_val = self.f2p[i]
            if pi_val is not None:
                ep = self.plex[pi_val]
                will_rename = _build_new_path(lf.path, ep).name != lf.path.name
                tag = f"→ E{ep.number:02d}"
                base = curses.color_pair(_CHG if will_rename else _OK)
            else:
                tag = "→ ?  "
                base = curses.color_pair(_BAD)
            t = lf.path.name[:lw - len(tag) - 3]
            line = f" {t.ljust(lw - len(tag) - 2)}{tag}"
            if i == self.li:
                attr = curses.color_pair(_SELF if self.focus == 'L' else _SELB) | curses.A_BOLD
            else:
                attr = base
            _attr(scr, row, 0, line[:lw].ljust(lw), attr)

        # Right panel (plex episodes) ─────────────────────────────────────────
        for i, ep in enumerate(self.plex):
            row = 2 + i - self.rs
            if row < 2 or row > h - 4:
                continue
            fi_val = self.p2f[i]
            if fi_val is not None:
                fname = self.files[fi_val].path.name[:20]
                mark = f" ← {fname}"
                base = curses.color_pair(_OK)
            else:
                mark = ''
                base = curses.color_pair(1)
            ep_text = f" E{ep.number:02d} - {ep.title}"
            max_ep = rw - len(mark) - 1
            line = f"{ep_text[:max_ep].ljust(max_ep)}{mark}"
            if i == self.pi:
                attr = curses.color_pair(_SELF if self.focus == 'R' else _SELB) | curses.A_BOLD
            else:
                attr = base
            _attr(scr, row, mid + 1, line[:rw].ljust(rw), attr)

        # Footer ──────────────────────────────────────────────────────────────
        try:
            scr.addstr(h - 3, 0, '─' * (w - 1))
        except curses.error:
            pass
        sonarr_hint = "   R redownload" if self.sonarr else ""
        keys = f" ↑↓ navigate   TAB switch panel   ENTER assign   U unassign   A auto{sonarr_hint}   C confirm   Q quit"
        _attr(scr, h - 2, 0, keys[:w - 1], curses.A_DIM)
        _attr(scr, h - 1, 0, (' ' + self.status)[:w - 1].ljust(w - 1), curses.color_pair(_STAT))

    # ── key handling ──────────────────────────────────────────────────────────

    def _key(self, key: int, scr, h: int, w: int) -> Optional[str]:
        if key in (ord('q'), ord('Q'), 27):
            return 'quit'

        if key in (ord('c'), ord('C')):
            return self._confirm(scr, h, w)

        if key == ord('\t'):
            if self.focus == 'L':
                self.focus = 'R'
                self._jump_right()
            else:
                self.focus = 'L'

        elif key in (curses.KEY_UP, ord('k')):
            if self.focus == 'L':
                self.li = max(0, self.li - 1)
                self._jump_right()
            else:
                self.pi = max(0, self.pi - 1)

        elif key in (curses.KEY_DOWN, ord('j')):
            if self.focus == 'L':
                self.li = min(len(self.files) - 1, self.li + 1)
                self._jump_right()
            else:
                self.pi = min(len(self.plex) - 1, self.pi + 1)

        elif key == curses.KEY_PPAGE:
            if self.focus == 'L':
                self.li = max(0, self.li - 10)
                self._jump_right()
            else:
                self.pi = max(0, self.pi - 10)

        elif key == curses.KEY_NPAGE:
            if self.focus == 'L':
                self.li = min(len(self.files) - 1, self.li + 10)
                self._jump_right()
            else:
                self.pi = min(len(self.plex) - 1, self.pi + 10)

        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            if self.focus == 'L':
                # Accept the auto-suggestion shown on the right
                self._assign(self.li, self.pi)
                ep = self.plex[self.pi]
                lf = self.files[self.li]
                self.status = f"✓ {lf.path.name[:45]} → E{ep.number:02d} - {ep.title}"
                self._advance_left()
                self._jump_right()
            else:
                # Assign right-cursor to current left-cursor file
                self._assign(self.li, self.pi)
                ep = self.plex[self.pi]
                lf = self.files[self.li]
                self.status = f"✓ {lf.path.name[:45]} → E{ep.number:02d} - {ep.title}"
                self._advance_left()
                self._jump_right()
                self.focus = 'L'

        elif key in (ord('u'), ord('U'), curses.KEY_BACKSPACE, 127):
            lf = self.files[self.li]
            self._unassign(self.li)
            self._jump_right()
            self.status = f"Cleared assignment for {lf.path.name[:50]}"

        elif key in (ord('a'), ord('A')):
            self._auto_fill()
            self._jump_right()
            self.status = "Auto-filled all unmatched files using fuzzy title matching"

        elif key in (ord('r'), ord('R')):
            self._redownload(scr, h, w)

        return None

    # ── sonarr redownload ─────────────────────────────────────────────────────

    def _redownload(self, scr, h: int, w: int):
        if not self.sonarr:
            self.status = "Sonarr not configured — pass --sonarr-url and --sonarr-token"
            return

        lf = self.files[self.li]
        pi_val = self.f2p[self.li]

        # Determine which episode title to search for in Sonarr
        if pi_val is not None:
            ep_title = self.plex[pi_val].title
            ep_label = f"E{self.plex[pi_val].number:02d} - {ep_title}"
        elif lf.title:
            ep_title = lf.title
            ep_label = ep_title
        else:
            self.status = "Can't redownload — no episode title to search for in Sonarr"
            return

        # Confirmation prompt inline in status bar
        prompt = f"Delete {lf.path.name[:40]} + requeue '{ep_label}' in Sonarr? [y/N] "
        _attr(scr, h - 1, 0, (' ' + prompt)[:w - 1].ljust(w - 1), curses.color_pair(_STAT) | curses.A_BOLD)
        scr.refresh()
        confirm_key = scr.getch()
        if confirm_key not in (ord('y'), ord('Y')):
            self.status = "Redownload cancelled"
            return

        self.status = f"Searching Sonarr for '{ep_title}'…"
        _attr(scr, h - 1, 0, (' ' + self.status)[:w - 1].ljust(w - 1), curses.color_pair(_STAT))
        scr.refresh()

        try:
            episode = self.sonarr.find_episode(self.title, self.season, ep_title)
            msg = self.sonarr.delete_and_search(episode)
            # Remove the local file from our list so it no longer appears
            self._unassign(self.li)
            self.files.pop(self.li)
            new_f2p = {}
            new_p2f = dict(self.p2f)
            for fi, piv in self.f2p.items():
                new_fi = fi if fi < self.li else fi - 1
                if fi == self.li:
                    continue
                new_f2p[new_fi] = piv
                if piv is not None:
                    new_p2f[piv] = new_fi
            self.f2p = {i: None for i in range(len(self.files))}
            self.f2p.update(new_f2p)
            self.p2f = new_p2f
            self.li = min(self.li, len(self.files) - 1)
            self.status = f"✓ {msg}"
        except RuntimeError as e:
            self.status = f"Sonarr error: {e}"

    # ── confirmation screen ───────────────────────────────────────────────────

    def _confirm(self, scr, h: int, w: int) -> Optional[str]:
        items = self._rename_pairs()
        n_rename = sum(1 for _, dst, changed in items if changed)
        n_same   = sum(1 for _, dst, changed in items if not changed)
        n_skip   = len(self.files) - len(items)
        scroll = 0
        vis = h - 9

        while True:
            scr.erase()
            _attr(scr, 0, 0, ' Confirm & Apply '.center(w - 1), curses.color_pair(_HDR) | curses.A_BOLD)
            try:
                scr.addstr(2, 2, f"{n_rename} rename(s)   {n_same} already correct   {n_skip} unmatched (skipped)")
                scr.addstr(h - 5, 0, '─' * (w - 1))
                scr.addstr(h - 4, 2, "ENTER / Y   rename files + force Plex refresh")
                scr.addstr(h - 3, 2, "ESC   / N   go back")
                scr.addstr(h - 1, 0, " ↑↓ scroll", curses.A_DIM)
            except curses.error:
                pass

            for idx, (src, dst, changed) in enumerate(items[scroll:scroll + vis]):
                row = 4 + idx
                if row >= h - 5:
                    break
                half = (w - 6) // 2
                if changed:
                    line = f"  {src.name[:half]}  →  {dst.name[:half]}"
                    _attr(scr, row, 0, line[:w - 1], curses.color_pair(_OK))
                else:
                    line = f"  {src.name[:w - 20]}  (unchanged)"
                    _attr(scr, row, 0, line[:w - 1], curses.A_DIM)

            scr.refresh()
            key = scr.getch()
            if key in (curses.KEY_UP, ord('k')):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                scroll = min(max(0, len(items) - vis), scroll + 1)
            elif key in (curses.KEY_ENTER, ord('\n'), ord('\r'), ord('y'), ord('Y')):
                return 'confirmed'
            elif key in (27, ord('n'), ord('N')):
                return None

    def _rename_pairs(self) -> list[tuple[Path, Path, bool]]:
        out = []
        for fi, lf in enumerate(self.files):
            pi_val = self.f2p[fi]
            if pi_val is None:
                continue
            ep = self.plex[pi_val]
            new = _build_new_path(lf.path, ep)
            out.append((lf.path, new, new.name != lf.path.name))
        return out

    def renames(self) -> list[tuple[Path, Path]]:
        return [(s, d) for s, d, ch in self._rename_pairs() if ch]


# ── helpers ───────────────────────────────────────────────────────────────────

def _attr(scr, row: int, col: int, text: str, attr: int):
    try:
        scr.attron(attr)
        scr.addstr(row, col, text)
        scr.attroff(attr)
    except curses.error:
        pass


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Interactive episode matcher: local files ↔ Plex ordering.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('--show',        required=True, help='Show name to search in Plex')
    ap.add_argument('--season',      required=True, type=int, help='Season number')
    ap.add_argument('--dir',         required=True, help='Local episode directory')
    ap.add_argument('--plex-url',    default='http://172.28.32.1:32400',
                    help='Plex URL (default: http://172.28.32.1:32400 — Windows host in WSL2)')
    ap.add_argument('--plex-token',
                    default=_ENV.get('PLEX_TOKEN') or None,
                    help='Plex token — falls back to PLEX_TOKEN in .env')
    ap.add_argument('--sonarr-url',   default='http://localhost:8989',
                    help='Sonarr URL (default: http://localhost:8989)')
    ap.add_argument('--sonarr-token',
                    default=_ENV.get('SONARR_API_KEY') or None,
                    help='Sonarr API key — falls back to SONARR_API_KEY in .env')
    args = ap.parse_args()

    if not args.plex_token:
        ap.error("Plex token required — set PLEX_TOKEN in .env or pass --plex-token")

    local_dir = Path(args.dir)
    if not local_dir.is_dir():
        sys.exit(f"[error] Not a directory: {local_dir}")

    print("Connecting to Plex…", end=' ', flush=True)
    client = Plex(args.plex_url, args.plex_token)
    try:
        plex_eps, show_title = client.load(args.show, args.season)
    except RuntimeError as e:
        sys.exit(f"\n[error] {e}")
    print(f"found {len(plex_eps)} episodes for '{show_title}'")

    print("Scanning local files…", end=' ', flush=True)
    files = scan_dir(local_dir, args.season)
    print(f"found {len(files)} files")
    if not files:
        sys.exit("[error] No video files found in directory")

    sonarr = Sonarr(args.sonarr_url, args.sonarr_token) if args.sonarr_token else None
    if sonarr:
        print(f"Sonarr configured at {args.sonarr_url}")

    app = App(files, plex_eps, show_title, args.season, sonarr=sonarr)
    app._auto_fill()   # pre-populate with fuzzy suggestions

    confirmed = app.run()
    if not confirmed:
        print("\nCancelled.")
        return

    pairs = app.renames()
    if not pairs:
        print("\nAll files already match Plex ordering — nothing to rename.")
    else:
        print(f"\nRenaming {len(pairs)} file(s)…")
        _two_pass_rename(pairs)
        for src, dst in pairs:
            print(f"  {src.name}")
            print(f"  → {dst.name}\n")

    print("Forcing Plex refresh…", end=' ', flush=True)
    try:
        client.refresh()
        print("done.")
    except RuntimeError as e:
        print(f"warning: {e}")

    print(f"\nFinished. {len(pairs)} file(s) renamed.")


if __name__ == '__main__':
    main()
