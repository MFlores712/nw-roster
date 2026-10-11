#!/usr/bin/env python3
"""
Fetch current and max ELO (RM 1v1 and RM Team) from aoe2insights.com for each
roster in ROSTERS and write docs/data/<roster>.json, sorted by current 1v1 ELO
descending.

USAGE:
    python scripts/fetch_ratings.py          # nW every run, México on the hourly run
    python scripts/fetch_ratings.py --all    # update every roster now

NOTE — aoe2insights.com has no public/documented API. This script scrapes
the public profile page for each player. Each ladder (1v1 RM, Team RM, ...)
is a "card-ranking" card showing current rating, rank, and an "All Time High"
field. If the site redesigns and values come back None, open one profile
page's HTML and adjust the patterns below.
"""

import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent.parent

# Each roster: player list in config/, output read directly by the live page.
# Players with a "tag" in config use it; others get their name scraped from
# their profile. Hourly rosters only update on the run in the first quarter of
# each hour (the timer fires every 15 min), to keep load on aoe2insights down.
ROSTERS = [
    {"config": ROOT / "config" / "players.json", "output": ROOT / "docs" / "data" / "roster.json", "hourly": False},
    {"config": ROOT / "config" / "mexico.json", "output": ROOT / "docs" / "data" / "mexico.json", "hourly": True},
]

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
# Display name, e.g. "VENÓN | El Claut_One" or "Oni.Kingstone" (verified 2026-10-10)
PROFILE_NAME = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

# Clan written as "[TAG] Name" instead of "TAG | Name"
BRACKET_CLAN = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

LADDERS = {"1v1 RM": "1v1", "Team RM": "tg"}
FIELDS = ["cur_1v1", "max_1v1", "cur_tg", "max_tg"]


def fetch_profile_html(profile_id: int) -> str:
    req = Request(BASE_URL.format(id=profile_id), headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_ratings(html_text: str) -> dict:
    """Returns {field: int | None} for every name in FIELDS."""
    ratings = dict.fromkeys(FIELDS)
    for card in CARD_SPLIT.split(html_text)[1:]:
        title = CARD_TITLE.search(card)
        suffix = LADDERS.get(title.group(1)) if title else None
        if not suffix or ratings[f"max_{suffix}"] is not None:
            continue
        cur = CURRENT.search(card)
        peak = ALL_TIME_HIGH.search(card)
        ratings[f"cur_{suffix}"] = int(cur.group(1)) if cur else None
        ratings[f"max_{suffix}"] = int(peak.group(1)) if peak else None
    return ratings


def extract_name(html_text: str) -> tuple[str | None, str | None]:
    """Returns (clan, name) from the profile heading; clan is None if untagged."""
    m = PROFILE_NAME.search(html_text)
    if not m:
        return None, None
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split())
    if " | " in text:
        clan, name = text.split(" | ", 1)
        return clan.strip() or None, name.strip()
    return None, text or None


_profile_cache: dict[int, tuple[dict, tuple]] = {}


def fetch_profile(profile_id: int) -> tuple[dict, tuple]:
    """(ratings, (clan, name)) for a profile, fetched at most once per run.

    Players on several rosters (nW members are also in the México ranking)
    share one request.
    """
    if profile_id not in _profile_cache:
        if _profile_cache:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            page = fetch_profile_html(profile_id)
            _profile_cache[profile_id] = (extract_ratings(page), extract_name(page))
        except (URLError, HTTPError, TimeoutError) as e:
            print(f"  ! fetch failed for {profile_id}: {e}", file=sys.stderr)
            _profile_cache[profile_id] = (dict.fromkeys(FIELDS), (None, None))
    return _profile_cache[profile_id]


def apply_clan_rules(clan: str | None, name: str | None, player: dict,
                     prefixes: dict) -> tuple[str | None, str | None]:
    """Apply a roster's clan_prefixes ("Ux'Uzzi" -> clan Ux, name Uzzi) and a
    player's own "clan" override from config."""
    if name and not clan:
        bracketed = BRACKET_CLAN.match(name)  # "[SotC] PerezPodrido"
        if bracketed:
            clan, name = bracketed.group(1), bracketed.group(2)
    if name and not clan:
        for prefix, tag in prefixes.items():
            if name.startswith(prefix) and len(name) > len(prefix):
                clan, name = tag, name[len(prefix):]
                break
    if player.get("clan"):
        clan = player["clan"]
    return clan, name


def build_player(player: dict, prev: dict | None, prefixes: dict | None = None) -> dict:
    ratings, (clan, name) = fetch_profile(player["id"])
    clan, name = apply_clan_rules(clan, name, player, prefixes or {})
    result = {"profile_id": player["id"]}
    if "tag" in player:                       # name fixed in config (nW roster)
        result["tag"] = player["tag"]
        if "country" in player:
            result["country"] = player["country"]
    else:                                     # name scraped from the profile
        if name is None and prev:             # fetch failed: keep last known name
            clan, name = prev.get("clan"), prev.get("tag")
        result["tag"] = name or str(player["id"])
        if clan:
            result["clan"] = clan
    result.update(ratings)

    # Fallback to last known good value for any field that came back empty
    if prev:
        stale = [f for f in FIELDS if result[f] is None and prev.get(f) is not None]
        for f in stale:
            result[f] = prev[f]
        if stale:
            result["stale"] = stale
    return result


# Rank-change arrows compare against the order at 08:00 Mexico City each day.
# Mexico City is UTC-6 year-round (Mexico dropped DST in 2022), so that's 14:00 UTC.
BASELINE_HOUR_UTC = 14


def last_baseline_time(now: datetime) -> datetime:
    """Most recent 08:00 Mexico City at or before `now`."""
    b = now.replace(hour=BASELINE_HOUR_UTC, minute=0, second=0, microsecond=0)
    return b if b <= now else b - timedelta(days=1)


def next_baseline(prev_output: dict, results: list, now: datetime) -> dict:
    """Keep the stored baseline unless 08:00 has passed since it was taken.

    On rollover, snapshot the order from the previous output — that's the
    ranking as it stood at 08:00 (nothing has been written since then, because
    the first run after 08:00 always lands here). Falls back to this run's
    order on the very first run. Ranks are keyed by profile ID (as a string),
    so a player renaming doesn't show up as NEW.
    """
    baseline = prev_output.get("baseline")
    taken = datetime.fromisoformat(baseline["taken_at"].replace("Z", "+00:00")) if baseline else None
    if taken and taken >= last_baseline_time(now):
        return migrate_baseline_keys(baseline, prev_output.get("players") or results)
    order = prev_output.get("players") or results
    return {
        "taken_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ranks": {str(p["profile_id"]): i + 1 for i, p in enumerate(order)},
    }


def migrate_baseline_keys(baseline: dict, players: list) -> dict:
    """Older snapshots were keyed by player name; convert them to profile IDs."""
    ranks = baseline["ranks"]
    if all(k.isdigit() for k in ranks):
        return baseline
    by_tag = {p["tag"]: str(p["profile_id"]) for p in players}
    return {**baseline, "ranks": {by_tag.get(k, k): v for k, v in ranks.items()}}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def update_roster(roster: dict, now: datetime) -> bool:
    """Fetch, sort and (if anything changed) write one roster. False if it came back empty."""
    config = load_json(roster["config"])
    out_path = roster["output"]
    name = config.get("name") or config.get("clan_name")
    prev_output = load_json(out_path)
    # Used as a fallback so one failed fetch doesn't blank out a player
    previous = {p["profile_id"]: p for p in prev_output.get("players", [])}

    print(f"== {name} ({len(config['players'])} players)")
    results = []
    for i, player in enumerate(config["players"]):
        print(f"[{i+1}/{len(config['players'])}] fetching {player.get('tag', player['id'])}...")
        results.append(build_player(player, previous.get(player["id"]), config.get("clan_prefixes")))

    # Sort by current 1v1 ELO descending, unranked (None) go last
    results.sort(key=lambda p: (p["cur_1v1"] is None, -(p["cur_1v1"] or 0)))

    baseline = next_baseline(prev_output, results, now)

    # Only rewrite when something changed. updated_at would otherwise differ on
    # every run, forcing a commit + Pages rebuild each time the cron fires.
    # The daily baseline rollover counts as a change (one commit a day).
    if (results == prev_output.get("players")
            and baseline == prev_output.get("baseline")
            and name == prev_output.get("name")):
        print(f"No rating changes — leaving {out_path.name} untouched")
    else:
        output = {
            "name": name,
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "baseline": baseline,
            "players": results,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {out_path}")

    missing = [p["tag"] for p in results if p["max_1v1"] is None]
    if missing:
        print(f"WARNING: no 1v1 data for {missing}", file=sys.stderr)
    # Everyone empty means the page layout changed, not that players are unranked
    return len(missing) < len(results)


def main():
    now = datetime.now(timezone.utc)
    run_all = "--all" in sys.argv[1:]
    ok = True
    for roster in ROSTERS:
        if roster["hourly"] and not run_all and now.minute >= 15:
            print(f"== skipping {roster['output'].name} (updates on the first run each hour)")
            continue
        ok = update_roster(roster, now) and ok
    if not ok:
        print("ERROR: a roster came back with no data at all — check selectors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
