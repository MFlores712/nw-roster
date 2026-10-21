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
            if suffix == "1v1" and s.get("lastmatchdate"):
                ratings["last_1v1"] = datetime.fromtimestamp(s["lastmatchdate"], timezone.utc).strftime("%Y-%m-%d")
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


LIVE_CHUNK = 300  # account ids per matches request (URL length)

INACTIVE_DAYS = 90
# Most recent ranked 1v1 date (YYYY-MM-DD) across each player's linked
# accounts, by owner official id; from links.json (refreshed daily).
_linked_last: dict[int, str] = {}
# owner official id -> their linked account ids (links.json)
_linked_accounts: dict[int, list[int]] = {}
_today = datetime.now(timezone.utc).date()


def resolve_live(found: dict[int, dict], listed: set[int], owners: dict[int, tuple[int, str]]) -> dict[int, dict]:
    """Map live accounts to listed players. A player's own listed account wins;
    otherwise a linked account (smurf) counts, recorded as "account"."""
    live = {pid: m for pid, m in found.items() if pid in listed}
    for pid, m in found.items():
        if pid not in listed and pid in owners:
            owner, name = owners[pid]
            live.setdefault(owner, {**m, "account": name})
    return live


def fetch_live(listed: set[int], owners: dict[int, tuple[int, str]], now: datetime) -> None:
    """Fill _live from listed accounts plus every linked account (links.json).
    A failed request just means fewer badges this run; never fatal."""
    accounts = sorted(set(listed) | set(owners))
    found: dict[int, dict] = {}
    for i in range(0, len(accounts), LIVE_CHUNK):
        chunk = accounts[i:i + LIVE_CHUNK]
        query = urlencode({"profile_ids": ",".join(map(str, chunk)), "per_page": MATCH_PAGE})
        req = Request(f"{MATCHES_URL}?{query}", headers={**HEADERS, "Accept": "application/json"})
        for attempt in (1, 2):
            try:
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for pid, m in parse_live(data, set(chunk), now).items():
                    found.setdefault(pid, m)
                break
            except TRANSIENT_ERRORS as e:
                print(f"  ! live-match request failed (attempt {attempt}): {e}", file=sys.stderr)
                if attempt == 1:
                    time.sleep(RETRY_DELAY_SECONDS)
    _live.update(resolve_live(found, listed, owners))
    via = sum(1 for v in _live.values() if "account" in v)
    print(f"== live now: {len(_live)} player(s) ({via} on a linked account; {len(accounts)} accounts checked)")


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


def days_ago(day: str | None) -> int | None:
    return (_today - datetime.fromisoformat(day).date()).days if day else None


def pick_substitute(player: dict) -> tuple[int, tuple] | None:
    """If the listed account hasn't played ranked 1v1 for INACTIVE_DAYS but a
    linked account has (more recently), return (account id, its stats): the
    one played most recently, highest rating breaking ties."""
    oid = official_id(player)
    own = _stats.get(oid)
    own_last = own[0].get("last_1v1") if own else None
    if own_last and days_ago(own_last) <= INACTIVE_DAYS:
        return None
    best = None
    for acct in _linked_accounts.get(oid, []):
        st = _stats.get(acct)
        last = st[0].get("last_1v1") if st else None
        if not last or days_ago(last) > INACTIVE_DAYS or (own_last and last <= own_last):
            continue
        key = (last, st[0].get("cur_1v1") or 0)
        if best is None or key > best[0]:
            best = (key, acct, st)
    return (best[1], best[2]) if best else None


def build_player(player: dict, prev: dict | None, prefixes: dict | None = None) -> dict:
    ratings, (clan, name) = fetch_profile(player)
    # Listed account inactive but a linked one isn't: show that account instead
    # (its name and ratings; the row keeps the player's id for arrows/history)
    sub = pick_substitute(player)
    if sub:
        listed_last = ratings.get("last_1v1")
        sub_id, (ratings, (clan, name)) = sub
    clan, name = apply_clan_rules(clan, name, player, prefixes or {})
    result = {"profile_id": player["id"]}
    if sub:
        result["via"] = {"id": sub_id, **({"listed_last": listed_last} if listed_last else {})}
    if "tag" in player:                       # name fixed in config (nW roster, or a short name like "Lobo")
        result["tag"] = player["tag"]
        if "country" in player:
            result["country"] = player["country"]
        if player.get("clan"):
            result["clan"] = player["clan"]
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
        if not result.get("last_1v1") and prev.get("last_1v1"):
            result["last_1v1"] = prev["last_1v1"]

    # Inactive: no ranked 1v1 on this account or any linked account for
    # INACTIVE_DAYS. The page keeps their place but fades them and leaves them
    # out of the rank numbers.
    seen = [d for d in (result.get("last_1v1"), _linked_last.get(official_id(player))) if d]
    last_seen = max(seen) if seen else None
    if last_seen is None or (_today - datetime.fromisoformat(last_seen).date()).days > INACTIVE_DAYS:
        result["inactive"] = True
        if last_seen:
            result["last_seen"] = last_seen

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
        return fit_roster(migrate_baseline_keys(baseline, prev_output.get("players") or results), results)
    order = prev_output.get("players") or results
    return snapshot(order, now)


def snapshot(order: list, now: datetime) -> dict:
    """Rank snapshot: active players numbered 1..n in order; inactive players
    get no rank (listed separately, so the page shows no arrow for them)."""
    active = [p for p in order if not p.get("inactive")]
    return {
        "taken_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ranks": {str(p["profile_id"]): i + 1 for i, p in enumerate(active)},
        "inactive": [str(p["profile_id"]) for p in order if p.get("inactive")],
    }


def fit_roster(baseline: dict, results: list) -> dict:
    """Keep the snapshot in step with config edits made since 08:00, so they
    don't cause arrows: removed players are dropped (closing their gap), and
    added players are slotted in at the place they first appear (no NEW badge,
    and nobody shows a ▼ for being passed by them)."""
    ids = [str(p["profile_id"]) for p in results if not p.get("inactive")]
    inactive = set(baseline.get("inactive", []))
    kept = sorted((k for k in baseline["ranks"] if k in ids), key=baseline["ranks"].get)
    for i, pid in enumerate(ids):
        if pid not in baseline["ranks"] and pid not in inactive:
            kept.insert(min(i, len(kept)), pid)
    ranks = {pid: i + 1 for i, pid in enumerate(kept)}
    if ranks == baseline["ranks"]:
        return baseline
    return {**baseline, "ranks": ranks, "inactive": [k for k in baseline.get("inactive", [])
                                                      if k in {str(p["profile_id"]) for p in results}]}


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
    # page_size: the page splits the shown players into pages of N.
    show_top = config.get("show_top")
    page_size = config.get("page_size")
    if (results == prev_output.get("players")
            and baseline == prev_output.get("baseline")
            and name == prev_output.get("name")
            and show_top == prev_output.get("show_top")
            and page_size == prev_output.get("page_size")):
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
        if page_size:
            output["page_size"] = page_size
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {out_path}")

    missing = [p["tag"] for p in results if p["max_1v1"] is None]
    if missing:
        print(f"WARNING: no 1v1 data for {missing}", file=sys.stderr)
    # Everyone empty means the page layout changed, not that players are unranked
    return len(missing) < len(results)


LINKS_PATH = ROOT / "docs" / "data" / "links.json"
LINKS_SUMMARY = ROOT / "LINKED_ACCOUNTS.md"


def listed_players() -> tuple[dict[int, str], list[tuple[str, list[int]]]]:
    """official id -> display name for every listed player, and the per-roster
    order (for the summary). Names come from the current data files."""
    names, rosters = {}, []
    for r in ROSTERS:
        cfg, data = load_json(r["config"]), load_json(r["output"])
        shown = {x["profile_id"]: ((x.get("clan") or "") + " " + x["tag"]).strip() for x in data.get("players", [])}
        ids = []
        for p in cfg["players"]:
            oid = official_id(p)
            if oid:
                names.setdefault(oid, shown.get(p["id"]) or p.get("tag") or str(p["id"]))
                ids.append(oid)
        rosters.append((cfg.get("name") or cfg.get("clan_name"), ids))
    return names, rosters


def refresh_links(now: datetime, force: bool) -> dict:
    """links.json, rebuilt once a day (first run after the 08:00 snapshot),
    when a listed player isn't in it yet, or with --links. On failure the
    previous file is kept."""
    links = load_json(LINKS_PATH)
    names, rosters = listed_players()
    built = links.get("updated_at")
    due = (force or not built or missing_from(links, names)
           or datetime.fromisoformat(built.replace("Z", "+00:00")) < last_baseline_time(now))
    if not due:
        return links
    import links as linkmap  # scripts/links.py
    try:
        t = time.time()
        links = linkmap.build_links(names, HEADERS, now)
        LINKS_PATH.write_text(json.dumps(links, indent=1, ensure_ascii=False), encoding="utf-8")
        linkmap.write_summary(links, rosters, LINKS_SUMMARY)
        n = sum(len(p["accounts"]) for p in links["players"].values())
        print(f"== linked accounts rebuilt: {n} accounts for {len(names)} players, "
              f"{len(links['ambiguous'])} ambiguous ({time.time() - t:.0f}s)")
    except Exception as e:  # never let the link map break the ratings update
        print(f"  ! linked-account rebuild failed, keeping the previous map: {e}", file=sys.stderr)
    return links


def missing_from(links: dict, names: dict[int, str]) -> bool:
    return any(str(o) not in links.get("players", {}) for o in names)


def main():
    now = datetime.now(timezone.utc)
    # One batched lookup for everyone on every roster (shared players once)
    everyone = {official_id(p) for r in ROSTERS for p in load_json(r["config"])["players"]} - {None}
    prefetch(everyone)
    links = refresh_links(now, "--links" in sys.argv[1:])
    global _today
    _today = now.date()
    for oid, p in links.get("players", {}).items():
        dates = [a["last"] for a in p["accounts"] if a.get("last")]
        if dates:
            _linked_last[int(oid)] = max(dates)
        _linked_accounts[int(oid)] = [a["id"] for a in p["accounts"]]
    # Players whose listed account is inactive may be shown on a linked
    # account instead: look those linked accounts up too (usually a handful)
    stale = [o for o in everyone
             if not (_stats.get(o) and _stats[o][0].get("last_1v1")
                     and days_ago(_stats[o][0]["last_1v1"]) <= INACTIVE_DAYS)]
    extra = {a for o in stale for a in _linked_accounts.get(o, [])} - set(_stats)
    if extra:
        prefetch(extra)
    from links import account_owners
    fetch_live(everyone, account_owners(links), now)
    ok = True
    for roster in ROSTERS:
        ok = update_roster(roster, now) and ok
    if not ok:
        print("ERROR: a roster came back with no data at all — check selectors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
