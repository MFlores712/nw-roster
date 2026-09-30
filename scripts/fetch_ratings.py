#!/usr/bin/env python3
"""
Fetch current and max ELO (RM 1v1 and RM Team) for the nW clan roster from
aoe2insights.com and write the result to docs/data/roster.json, sorted by
current 1v1 ELO descending.

USAGE:
    python scripts/fetch_ratings.py

NOTE — aoe2insights.com has no public/documented API. This script scrapes
the public profile page for each player. Each ladder (1v1 RM, Team RM, ...)
is a "card-ranking" card showing current rating, rank, and an "All Time High"
field. If the site redesigns and values come back None, open one profile
page's HTML and adjust the patterns below.
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "players.json"
OUTPUT_PATH = ROOT / "docs" / "data" / "roster.json"  # single source of truth — the live page reads this directly

BASE_URL = "https://www.aoe2insights.com/user/{id}/"
HEADERS = {"User-Agent": "Mozilla/5.0 (nW-roster-bot; personal clan tool)"}
REQUEST_DELAY_SECONDS = 2  # be polite — this is an unofficial community site

# --- Parsing patterns -------------------------------------------------
# Verified against live markup (2026-09-30). Each ladder is a card:
#   <div class="card card-ranking"> ... <h3 ...>Team RM</h3> ...
#   <div class="rating-big">1845</div>                       <- current
#   <span class="rating-detail" title="All Time High"><svg ...></svg>1854</span>
# We split the page into cards and read within each, so an unranked ladder
# can't bleed into the next card's numbers.
CARD_SPLIT = re.compile(r'<div class="card card-ranking">')
CARD_TITLE = re.compile(r"<h3[^>]*>\s*([^<]+?)\s*</h3>")
CURRENT = re.compile(r'<div class="rating-big">\s*(\d+)\s*<')
ALL_TIME_HIGH = re.compile(r'title="All Time High">(?:\s*<svg.*?</svg>)?\s*(\d+)\s*<', re.S)

LADDERS = {"1v1 RM": "1v1", "Team RM": "tg"}
FIELDS = ["cur_1v1", "max_1v1", "cur_tg", "max_tg"]


def fetch_profile_html(profile_id: int) -> str:
    req = Request(BASE_URL.format(id=profile_id), headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_ratings(html: str) -> dict:
    """Returns {field: int | None} for every name in FIELDS."""
    ratings = dict.fromkeys(FIELDS)
    for card in CARD_SPLIT.split(html)[1:]:
        title = CARD_TITLE.search(card)
        suffix = LADDERS.get(title.group(1)) if title else None
        if not suffix or ratings[f"max_{suffix}"] is not None:
            continue
        cur = CURRENT.search(card)
        peak = ALL_TIME_HIGH.search(card)
        ratings[f"cur_{suffix}"] = int(cur.group(1)) if cur else None
        ratings[f"max_{suffix}"] = int(peak.group(1)) if peak else None
    return ratings


def fetch_player(player: dict) -> dict:
    try:
        ratings = extract_ratings(fetch_profile_html(player["id"]))
    except (URLError, HTTPError, TimeoutError) as e:
        print(f"  ! fetch failed for {player['tag']}: {e}", file=sys.stderr)
        ratings = dict.fromkeys(FIELDS)

    return {
        "tag": player["tag"],
        "country": player["country"],
        "profile_id": player["id"],
        **ratings,
    }


def load_previous_output() -> dict:
    if OUTPUT_PATH.exists():
        return json.loads(OUTPUT_PATH.read_text())
    return {}


def main():
    config = json.loads(CONFIG_PATH.read_text())
    prev_output = load_previous_output()
    # Used as a fallback so one failed fetch doesn't blank out a player
    previous = {p["tag"]: p for p in prev_output.get("players", [])}

    results = []
    for i, player in enumerate(config["players"]):
        print(f"[{i+1}/{len(config['players'])}] fetching {player['tag']}...")
        result = fetch_player(player)

        # Fallback to last known good value for any field that came back empty
        prev = previous.get(player["tag"])
        if prev:
            stale = [f for f in FIELDS if result[f] is None and prev.get(f) is not None]
            for f in stale:
                result[f] = prev[f]
            if stale:
                result["stale"] = stale

        results.append(result)
        if i < len(config["players"]) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    # Sort by current 1v1 ELO descending, unranked (None) go last
    results.sort(key=lambda p: (p["cur_1v1"] is None, -(p["cur_1v1"] or 0)))

    # Only rewrite when something changed. updated_at would otherwise differ on
    # every run, forcing a commit + Pages rebuild each time the cron fires.
    if results == prev_output.get("players") and config["clan_name"] == prev_output.get("clan_name"):
        print("\nNo rating changes — leaving roster.json untouched")
    else:
        output = {
            "clan_name": config["clan_name"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "players": results,
        }
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(output, indent=2))
        print(f"\nWrote {OUTPUT_PATH}")

    missing = [p["tag"] for p in results if p["max_1v1"] is None]
    if missing:
        print(f"WARNING: no data for {missing} — check selectors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
