#!/usr/bin/env python3
"""
Fetch current and max ELO (RM 1v1 and RM Team) for each roster in ROSTERS and
write docs/data/<roster>.json, sorted by current 1v1 ELO descending.

USAGE:
    python scripts/fetch_ratings.py          # update every roster

DATA SOURCE — the official Age of Empires II ranked-stats API run by World's
Edge (the same data aoe2insights and other community sites display). Every
player is looked up in a couple of batched requests per run.

aoe2insights.com used to be scraped instead, but since 2026-10-18 it sits
behind a Cloudflare bot challenge, so automated requests get HTTP 403.
Rows on the page still link to aoe2insights profiles for people to visit.

Player IDs in config are aoe2insights IDs (used for those links and as the
stable key in the data files). They are the official profile IDs too, except
for some newer accounts; those carry an extra "official_id" in config.
"""

import http.client
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent

# Each roster: player list in config/, output read directly by the live page.
# Players with a "tag" in config use it; others get their name from the API.
# Every roster updates on every run (the cron-job.org timer fires every 5 min).
ROSTERS = [
    {"config": ROOT / "config" / "players.json", "output": ROOT / "docs" / "data" / "roster.json"},
    {"config": ROOT / "config" / "mexico.json", "output": ROOT / "docs" / "data" / "mexico.json"},
    {"config": ROOT / "config" / "colombia.json", "output": ROOT / "docs" / "data" / "colombia.json"},
    {"config": ROOT / "config" / "peru.json", "output": ROOT / "docs" / "data" / "peru.json"},
]

STATS_URL = "https://aoe-api.worldsedgelink.com/community/leaderboard/GetPersonalStat"
HEADERS = {"User-Agent": "nw-roster (personal clan ranking page; github.com/MFlores712/nw-roster)"}
BATCH_SIZE = 50          # profiles per request
RETRY_DELAY_SECONDS = 5  # one retry per batch for transient errors

# leaderboard_id in the API -> our field suffix (3 = RM 1v1, 4 = RM Team)
LEADERBOARDS = {3: "1v1", 4: "tg"}
FIELDS = ["cur_1v1", "max_1v1", "cur_tg", "max_tg"]

# Clan written as "[TAG] Name" instead of "TAG | Name"
BRACKET_CLAN = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# Network-ish failures worth a retry: refused/reset/timed-out connections and
# responses cut off part-way (IncompleteRead), plus a garbled JSON body.
TRANSIENT_ERRORS = (OSError, http.client.HTTPException, ValueError)


def split_alias(alias: str | None) -> tuple[str | None, str | None]:
    """"VENÓN | El Claut_One" -> ("VENÓN", "El Claut_One"); no clan -> (None, alias)."""
    text = " ".join((alias or "").split())
    if " | " in text:
        clan, name = text.split(" | ", 1)
        return clan.strip() or None, name.strip()
    return None, text or None


def request_stats(official_ids: list[int]) -> dict:
    query = urlencode({"title": "age2", "profile_ids": json.dumps(official_ids)})
    req = Request(f"{STATS_URL}?{query}", headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("result", {}).get("code") != 0:
        raise ValueError(f"API error: {data.get('result')}")
    return data


def parse_stats(data: dict) -> dict[int, tuple[dict, tuple]]:
    """official id -> (ratings, (clan, name)) for every profile in the response."""
    members = {}
    for group in data.get("statGroups", []):
        for m in group.get("members", []):
            members[group["id"]] = m
    out = {m["profile_id"]: (dict.fromkeys(FIELDS), split_alias(m.get("alias")))
           for m in members.values()}
    for s in data.get("leaderboardStats", []):
        m = members.get(s.get("statgroup_id"))
        suffix = LEADERBOARDS.get(s.get("leaderboard_id"))
        if m and suffix:
            ratings = out[m["profile_id"]][0]
            ratings[f"cur_{suffix}"] = s.get("rating")
            ratings[f"max_{suffix}"] = s.get("highestrating") or None
    return out


_stats: dict[int, tuple[dict, tuple]] = {}


def prefetch(official_ids: set[int]) -> None:
    """Fill _stats for every profile, in batches. A batch that fails twice is
    skipped: its players keep their last known values (shown as stale)."""
    ids = sorted(official_ids)
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        for attempt in (1, 2):
            try:
                _stats.update(parse_stats(request_stats(batch)))
                break
            except TRANSIENT_ERRORS as e:
                print(f"  ! stats request failed (attempt {attempt}) for {len(batch)} players: {e}", file=sys.stderr)
                if attempt == 1:
                    time.sleep(RETRY_DELAY_SECONDS)
    missing = [i for i in ids if i not in _stats]
    if missing:
        print(f"  ! no stats for official ids {missing}", file=sys.stderr)


# Who is in a game right now. The official API has no live-match endpoint;
# aoe2companion's public API lists recent matches for many profiles in one
# request, and an ongoing one has no "finished" time yet.
MATCHES_URL = "https://data.aoe2companion.com/api/matches"
MATCH_PAGE = 100            # newest matches across all our players (~16h' worth)
LIVE_MAX_AGE = timedelta(hours=3)  # older "unfinished" matches never got closed

_live: dict[int, dict] = {}


def parse_live(data: dict, ours: set[int], now: datetime) -> dict[int, dict]:
    """official id -> {"match": id, "game": ladder name} for ongoing matches."""
    live = {}
    for m in data.get("matches", []):
        if m.get("finished") or m.get("abandoned") or not m.get("started"):
            continue
        started = datetime.fromisoformat(m["started"].replace("Z", "+00:00"))
        if now - started > LIVE_MAX_AGE:
            continue
        for team in m.get("teams", []):
            for p in team.get("players", []):
                pid = p.get("profileId")
                if pid in ours and pid not in live:  # newest match first
                    live[pid] = {"match": m["matchId"], "game": m.get("leaderboardName") or "Game"}
    return live


def fetch_live(official_ids: set[int], now: datetime) -> None:
    """Fill _live. On failure nobody shows as live this run; never fatal."""
    query = urlencode({"profile_ids": ",".join(map(str, sorted(official_ids))), "per_page": MATCH_PAGE})
    req = Request(f"{MATCHES_URL}?{query}", headers={**HEADERS, "Accept": "application/json"})
    for attempt in (1, 2):
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _live.update(parse_live(data, official_ids, now))
            print(f"== live now: {len(_live)} player(s)")
            return
        except TRANSIENT_ERRORS as e:
            print(f"  ! live-match request failed (attempt {attempt}): {e}", file=sys.stderr)
            if attempt == 1:
                time.sleep(RETRY_DELAY_SECONDS)


def official_id(player: dict) -> int | None:
    """None when config says the official account isn't known yet
    ("official_id": null): that player just keeps their last known values."""
    return player.get("official_id", player["id"])


def fetch_profile(player: dict) -> tuple[dict, tuple]:
    """(ratings, (clan, name)) for a config player, from the prefetched stats."""
    return _stats.get(official_id(player), (dict.fromkeys(FIELDS), (None, None)))


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
    ratings, (clan, name) = fetch_profile(player)
    clan, name = apply_clan_rules(clan, name, player, prefixes or {})
    result = {"profile_id": player["id"]}
    if "tag" in player:                       # name fixed in config (nW roster)
        result["tag"] = player["tag"]
        if "country" in player:
            result["country"] = player["country"]
    else:                                     # name from the API
        if name is None and prev:             # lookup failed: keep last known name
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

    live = _live.get(official_id(player))
    if live:
        result["live"] = live
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
    for player in config["players"]:
        results.append(build_player(player, previous.get(player["id"]), config.get("clan_prefixes")))
    stale = [p["tag"] for p in results if p.get("stale")]
    if stale:
        print(f"  {len(stale)} kept last known values: {stale}")

    # Sort by current 1v1 ELO descending, unranked (None) go last
    results.sort(key=lambda p: (p["cur_1v1"] is None, -(p["cur_1v1"] or 0)))

    baseline = next_baseline(prev_output, results, now)

    # Only rewrite when something changed. updated_at would otherwise differ on
    # every run, forcing a commit + Pages rebuild each time the cron fires.
    # The daily baseline rollover counts as a change (one commit a day).
    # show_top: the page shows only the best N, but every listed player stays
    # in the file (and in the rank snapshot) so anyone can climb into the top N
    # and their arrow reflects their real previous position.
    show_top = config.get("show_top")
    if (results == prev_output.get("players")
            and baseline == prev_output.get("baseline")
            and name == prev_output.get("name")
            and show_top == prev_output.get("show_top")):
        print(f"No rating changes — leaving {out_path.name} untouched")
    else:
        output = {
            "name": name,
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "baseline": baseline,
            "players": results,
        }
        if show_top:
            output["show_top"] = show_top
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
    # One batched lookup for everyone on every roster (shared players once)
    everyone = {official_id(p) for r in ROSTERS for p in load_json(r["config"])["players"]} - {None}
    prefetch(everyone)
    fetch_live(everyone, now)
    ok = True
    for roster in ROSTERS:
        ok = update_roster(roster, now) and ok
    if not ok:
        print("ERROR: a roster came back with no data at all — check selectors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
