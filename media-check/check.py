#!/usr/bin/env python3
"""
media-check — Scans TV and movie files for corruption and language issues.
Notifies via Discord. Re-checks files every RECHECK_DAYS days.

Language logic:
  English originals  → English audio required
  Foreign originals  → native audio + English subtitles required
                       (English dub acceptable but not sought out)

Corruption logic: ffprobe metadata check — catches truncated files,
unreadable containers, and missing streams without a full decode.
"""

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import requests

SONARR_URL   = os.environ["SONARR_URL"].rstrip("/")
SONARR_KEY   = os.environ["SONARR_API_KEY"]
RADARR_URL   = os.environ["RADARR_URL"].rstrip("/")
RADARR_KEY   = os.environ["RADARR_API_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
DB_PATH      = Path("/app/data/media-check.db")
RECHECK_DAYS = int(os.environ.get("RECHECK_DAYS", "7"))

# Sonarr/Radarr originalLanguage.name → ISO 639 codes used by ffprobe
LANG_CODES: dict[str, set[str]] = {
    "english":    {"eng", "en"},
    "japanese":   {"jpn", "ja"},
    "french":     {"fre", "fra", "fr"},
    "german":     {"ger", "deu", "de"},
    "spanish":    {"spa", "es"},
    "italian":    {"ita", "it"},
    "korean":     {"kor", "ko"},
    "chinese":    {"chi", "zho", "zh"},
    "portuguese": {"por", "pt"},
    "russian":    {"rus", "ru"},
    "arabic":     {"ara", "ar"},
    "hindi":      {"hin", "hi"},
    "dutch":      {"dut", "nld", "nl"},
    "swedish":    {"swe", "sv"},
    "danish":     {"dan", "da"},
    "norwegian":  {"nor", "no"},
    "polish":     {"pol", "pl"},
    "czech":      {"cze", "ces", "cs"},
    "thai":       {"tha", "th"},
}
ENG_CODES = LANG_CODES["english"]


# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checked (
            path       TEXT PRIMARY KEY,
            checked_at TEXT,
            issues     TEXT
        )
    """)
    conn.commit()
    return conn


def needs_check(conn: sqlite3.Connection, path: str) -> bool:
    row = conn.execute(
        "SELECT checked_at FROM checked WHERE path = ?", (path,)
    ).fetchone()
    if not row:
        return True
    return datetime.now() - datetime.fromisoformat(row[0]) > timedelta(days=RECHECK_DAYS)


def mark_checked(conn: sqlite3.Connection, path: str, issues: list[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO checked (path, checked_at, issues) VALUES (?, ?, ?)",
        (path, datetime.now().isoformat(), json.dumps(issues)),
    )
    conn.commit()


# ── ffprobe ───────────────────────────────────────────────────────────────────

def probe(path: Path) -> dict | None:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def stream_langs(data: dict, codec_type: str) -> set[str]:
    return {
        s.get("tags", {}).get("language", "und").lower()
        for s in data.get("streams", [])
        if s.get("codec_type") == codec_type
    }


# ── Checks ────────────────────────────────────────────────────────────────────

def check_file(path: Path, orig_lang_name: str) -> list[str]:
    data = probe(path)
    if data is None:
        return ["ffprobe failed — file unreadable or corrupt"]

    issues = []
    streams = data.get("streams", [])
    has_video = any(s["codec_type"] == "video" for s in streams)
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    duration  = float(data.get("format", {}).get("duration", 0))

    if not has_video:
        issues.append("No video stream found")
    if not has_audio:
        issues.append("No audio stream found")
    if duration < 60:
        issues.append(f"Suspiciously short ({duration:.0f}s) — may be truncated")

    lang_key     = orig_lang_name.lower()
    native_codes = LANG_CODES.get(lang_key)
    audio_langs  = stream_langs(data, "audio")
    sub_langs    = stream_langs(data, "subtitle")

    if lang_key == "english" or not native_codes:
        if not audio_langs & ENG_CODES:
            issues.append(f"No English audio — found: {audio_langs or {'none'}}")
    else:
        # Foreign content: need native audio + English subtitles
        if not audio_langs & native_codes:
            issues.append(
                f"No {orig_lang_name} audio — found: {audio_langs or {'none'}}"
            )
        if not sub_langs & ENG_CODES:
            issues.append(
                f"No English subtitles for {orig_lang_name} content"
                + (f" — found: {sub_langs}" if sub_langs - {'und'} else "")
            )

    return issues


# ── API helpers ───────────────────────────────────────────────────────────────

def sonarr_get(endpoint: str) -> list:
    r = requests.get(
        f"{SONARR_URL}/api/v3/{endpoint}",
        headers={"X-Api-Key": SONARR_KEY}, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def radarr_get(endpoint: str) -> list:
    r = requests.get(
        f"{RADARR_URL}/api/v3/{endpoint}",
        headers={"X-Api-Key": RADARR_KEY}, timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Scans ─────────────────────────────────────────────────────────────────────

def scan_sonarr(conn: sqlite3.Connection) -> list[dict]:
    findings = []
    series_map = {s["id"]: s for s in sonarr_get("series")}

    for ef in sonarr_get("episodefile"):
        path = Path(ef["path"])
        if not path.exists() or not needs_check(conn, str(path)):
            continue

        series    = series_map.get(ef["seriesId"], {})
        orig_lang = series.get("originalLanguage", {}).get("name", "English")
        title     = f"{series.get('title', '?')} — {ef.get('relativePath', path.name)}"

        issues = check_file(path, orig_lang)
        mark_checked(conn, str(path), issues)
        if issues:
            findings.append({"title": title, "path": str(path), "issues": issues})

    return findings


def scan_radarr(conn: sqlite3.Connection) -> list[dict]:
    findings = []
    movie_map = {m["id"]: m for m in radarr_get("movie")}

    for mf in radarr_get("moviefile"):
        path = Path(mf["path"])
        if not path.exists() or not needs_check(conn, str(path)):
            continue

        movie     = movie_map.get(mf["movieId"], {})
        orig_lang = movie.get("originalLanguage", {}).get("name", "English")
        title     = f"{movie.get('title', path.stem)} ({movie.get('year', '?')})"

        issues = check_file(path, orig_lang)
        mark_checked(conn, str(path), issues)
        if issues:
            findings.append({"title": title, "path": str(path), "issues": issues})

    return findings


# ── Discord ───────────────────────────────────────────────────────────────────

def notify(findings: list[dict]) -> None:
    lines = ["🎬 **Media Check — Issues Found**\n"]
    for f in findings:
        lines.append(f"**{f['title']}**")
        for issue in f["issues"]:
            lines.append(f"  • {issue}")
        lines.append(f"  `{f['path']}`\n")

    messages, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            messages.append(current)
            current = ""
        current += line + "\n"
    if current:
        messages.append(current)

    for msg in messages:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Starting media check")
    conn = init_db()
    findings = []

    for name, fn in [("Sonarr", scan_sonarr), ("Radarr", scan_radarr)]:
        try:
            results = fn(conn)
            print(f"  {name}: {len(results)} issue(s) found")
            findings.extend(results)
        except Exception as e:
            print(f"  {name} error: {e}")

    print(f"Total: {len(findings)} issue(s)")
    if findings:
        notify(findings)
    conn.close()


if __name__ == "__main__":
    main()
