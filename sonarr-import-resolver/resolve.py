#!/usr/bin/env python3
"""
sonarr-import-resolver — Finds stuck imports in Sonarr's queue (episodes that
were matched to a series by ID but can't auto-import due to S/E numbering
mismatches), asks Claude to identify the correct episode by title, then imports
automatically if confidence is high enough.

Confidence threshold (CONFIDENCE_THRESHOLD env, default 0.85):
  >= threshold      → auto-import, notify Discord
  0.5 – threshold   → Discord notification with proposed match, no import
  < 0.5             → Discord notification, no import
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import requests

SONARR_URL           = os.environ["SONARR_URL"].rstrip("/")
SONARR_KEY           = os.environ["SONARR_API_KEY"]
DISCORD_WEBHOOK      = os.environ["DISCORD_WEBHOOK"]
ANTHROPIC_KEY        = os.environ["ANTHROPIC_API_KEY"]
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.85"))
CLAUDE_MODEL         = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
RETRY_HOURS          = int(os.environ.get("RETRY_HOURS", "24"))
DB_PATH              = Path("/app/data/import-resolver.db")


# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS handled (
            download_id  TEXT PRIMARY KEY,
            handled_at   TEXT,
            action       TEXT,
            episode_id   INTEGER,
            confidence   REAL,
            notes        TEXT
        )
    """)
    conn.commit()
    return conn


def already_handled(conn: sqlite3.Connection, download_id: str) -> bool:
    row = conn.execute(
        "SELECT handled_at FROM handled WHERE download_id = ?", (download_id,)
    ).fetchone()
    if not row:
        return False
    return datetime.now() - datetime.fromisoformat(row[0]) < timedelta(hours=RETRY_HOURS)


def record(conn: sqlite3.Connection, download_id: str, action: str,
           episode_id: int | None, confidence: float, notes: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO handled
           (download_id, handled_at, action, episode_id, confidence, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (download_id, datetime.now().isoformat(), action, episode_id, confidence, notes),
    )
    conn.commit()


# ── Sonarr API ────────────────────────────────────────────────────────────────

def sonarr_get(endpoint: str, params: dict | None = None) -> any:
    r = requests.get(
        f"{SONARR_URL}/api/v3/{endpoint}",
        headers={"X-Api-Key": SONARR_KEY},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sonarr_post(endpoint: str, body: any) -> any:
    r = requests.post(
        f"{SONARR_URL}/api/v3/{endpoint}",
        headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_stuck_queue_items() -> list[dict]:
    data = sonarr_get("queue", {"pageSize": 200, "includeUnknownSeriesItems": True})
    records = data.get("records", []) if isinstance(data, dict) else data
    stuck = []
    for item in records:
        if item.get("trackedDownloadState") != "importPending":
            continue
        for msg in item.get("statusMessages", []):
            texts = msg.get("messages", [])
            if any(
                "Automatic import is not possible" in t or "matched to series by ID" in t
                for t in texts
            ):
                stuck.append(item)
                break
    return stuck


def get_import_candidates(download_id: str, series_id: int) -> list[dict]:
    return sonarr_get("manualimport", {
        "downloadId": download_id,
        "seriesId": series_id,
        "filterExistingFiles": False,
    })


def get_all_episodes(series_id: int) -> list[dict]:
    return sonarr_get("episode", {"seriesId": series_id})


def do_manual_import(candidate: dict, episode_id: int, download_id: str) -> bool:
    body = [{
        "path": candidate["path"],
        "seriesId": candidate["series"]["id"],
        "episodeIds": [episode_id],
        "quality": candidate["quality"],
        "languages": candidate.get("languages", [{"id": 1, "name": "English"}]),
        "downloadId": download_id,
        "releaseGroup": candidate.get("releaseGroup", ""),
        "indexerFlags": candidate.get("indexerFlags", 0),
    }]
    try:
        sonarr_post("manualimport", body)
        return True
    except Exception as e:
        print(f"    Manual import POST failed: {e}")
        return False


# ── Claude episode resolution ─────────────────────────────────────────────────

def resolve_with_claude(
    filename: str,
    series_title: str,
    episodes: list[dict],
    sonarr_hint: dict | None,
) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    episode_list = "\n".join(
        f"  ID={ep['id']} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
        f" — {ep.get('title', 'Untitled')}"
        for ep in sorted(episodes, key=lambda e: (e["seasonNumber"], e["episodeNumber"]))
    )

    hint_text = ""
    if sonarr_hint:
        eps = sonarr_hint.get("episodes", [])
        if eps:
            ep = eps[0]
            hint_text = (
                f"\nSonarr's best guess (numbering matched but import was blocked): "
                f"S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
                f" — \"{ep.get('title', 'Unknown')}\" (ID={ep['id']})"
            )

    prompt = f"""You are resolving a Sonarr import conflict. A TV episode was downloaded but Sonarr can't automatically import it because the S/E numbering in the filename doesn't match its database. Identify which episode the file contains.

Series: {series_title}
Filename: {filename}{hint_text}

Full episode list:
{episode_list}

Instructions:
1. Extract the episode title from the filename (dots are spaces; ignore quality tags, group names, and codec info after the title)
2. Find the best matching episode in the list by title
3. Note whether the S/E numbers in the filename agree with where you found the episode

Return ONLY valid JSON, no other text:
{{
  "episode_id": <Sonarr episode ID as integer, or null if no confident match>,
  "season": <season number as integer>,
  "episode": <episode number as integer>,
  "title": <episode title from the list>,
  "confidence": <0.0 to 1.0>,
  "reasoning": <one sentence explaining your match>
}}

Confidence guide:
- 0.95: title in filename is an exact or near-exact match for an episode title
- 0.80: title is a close match; S/E numbers disagree but you found a better match by title
- 0.60: partial title match or ambiguous — multiple episodes could fit
- 0.30: no clear title in filename, only S/E numbers with no match
- 0.10: cannot determine"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"Non-JSON response: {text[:200]}")


# ── Discord ───────────────────────────────────────────────────────────────────

def notify(content: str) -> None:
    requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)


# ── Processing ────────────────────────────────────────────────────────────────

def process_item(conn: sqlite3.Connection, item: dict) -> None:
    download_id = item.get("downloadId", "")
    title       = item.get("title", "unknown")
    series_id   = item.get("seriesId")

    print(f"  Processing: {title}")

    if not download_id or not series_id:
        print(f"    Skip — missing downloadId or seriesId")
        return

    if already_handled(conn, download_id):
        print(f"    Skip — already handled within last {RETRY_HOURS}h")
        return

    try:
        candidates = get_import_candidates(download_id, series_id)
    except Exception as e:
        print(f"    Failed to fetch candidates: {e}")
        return

    if not candidates:
        print(f"    No import candidates from Sonarr — skipping")
        record(conn, download_id, "skipped", None, 0.0, "no candidates")
        return

    candidate    = candidates[0]
    filename     = Path(candidate["path"]).name
    series_title = candidate.get("series", {}).get("title", "Unknown Series")
    sonarr_hint  = candidate if candidate.get("episodes") else None

    try:
        episodes = get_all_episodes(series_id)
    except Exception as e:
        print(f"    Failed to fetch episode list: {e}")
        return

    print(f"    Asking Claude to match: {filename}")
    try:
        result = resolve_with_claude(filename, series_title, episodes, sonarr_hint)
    except Exception as e:
        print(f"    Claude error: {e}")
        notify(
            f"⚠️ **Sonarr Import Resolver** — Claude error\n"
            f"`{filename}`\nError: `{e}`\nManual import: {SONARR_URL}"
        )
        record(conn, download_id, "error", None, 0.0, str(e))
        return

    confidence = float(result.get("confidence", 0.0))
    episode_id = result.get("episode_id")
    season     = result.get("season", "?")
    episode    = result.get("episode", "?")
    ep_title   = result.get("title", "Unknown")
    reasoning  = result.get("reasoning", "")

    se = f"S{season:02d}E{episode:02d}" if isinstance(season, int) else f"S{season}E{episode}"
    print(f"    → {se} \"{ep_title}\" (confidence={confidence:.0%})")
    print(f"      {reasoning}")

    if confidence >= CONFIDENCE_THRESHOLD and episode_id:
        success = do_manual_import(candidate, episode_id, download_id)
        if success:
            print(f"    ✅ Auto-imported")
            notify(
                f"✅ **Sonarr Import Resolver** — Auto-imported\n"
                f"`{filename}`\n"
                f"→ **{series_title}** {se} — {ep_title}\n"
                f"Confidence: {confidence:.0%} | {reasoning}"
            )
            record(conn, download_id, "auto-imported", episode_id, confidence, reasoning)
        else:
            print(f"    ❌ Import POST failed")
            notify(
                f"⚠️ **Sonarr Import Resolver** — Import attempt failed\n"
                f"`{filename}`\n"
                f"Proposed: **{series_title}** {se} — {ep_title} ({confidence:.0%})\n"
                f"Import manually: {SONARR_URL}"
            )
            record(conn, download_id, "import-failed", episode_id, confidence, reasoning)

    elif confidence >= 0.5 and episode_id:
        print(f"    ⚡ Below threshold — notifying Discord")
        notify(
            f"🔎 **Sonarr Import Resolver** — Below confidence threshold ({confidence:.0%})\n"
            f"`{filename}`\n"
            f"Proposed: **{series_title}** {se} — {ep_title}\n"
            f"{reasoning}\n"
            f"If correct, import manually: {SONARR_URL}"
        )
        record(conn, download_id, "notified", episode_id, confidence, reasoning)

    else:
        print(f"    ❓ Could not resolve")
        notify(
            f"❓ **Sonarr Import Resolver** — Could not resolve\n"
            f"`{filename}`\n"
            f"Series: **{series_title}** | Confidence: {confidence:.0%}\n"
            f"{reasoning or 'No confident match found'}\n"
            f"Manual import needed: {SONARR_URL}"
        )
        record(conn, download_id, "unresolved", episode_id, confidence, reasoning)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] sonarr-import-resolver starting")
    conn = init_db()

    try:
        stuck = get_stuck_queue_items()
    except Exception as e:
        print(f"Failed to fetch Sonarr queue: {e}")
        return

    print(f"Found {len(stuck)} stuck import(s)")
    for item in stuck:
        try:
            process_item(conn, item)
        except Exception as e:
            print(f"  Unhandled error on {item.get('title', '?')}: {e}")

    conn.close()
    print("Done")


if __name__ == "__main__":
    main()
