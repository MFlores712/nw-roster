"""
Linked-account map: which other accounts (smurfs, old accounts) belong to each
listed player, so the page can show them LIVE when they play on any of them.

Source: aoe2companion's profile API, whose "linkedProfiles" lists accounts it
attributes to the same person. Links are followed transitively, but each
account is given to its CLOSEST listed player: plain transitivity once joined
two different people (CL Rey Enigmaaa and hK Eren) through a chain of other
people's accounts. An account equally close to two listed players goes to
nobody and is reported as ambiguous.

Output: docs/data/links.json (read by fetch_ratings.py) and LINKED_ACCOUNTS.md
(a human-readable summary).
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

PROFILE_URL = "https://data.aoe2companion.com/api/profiles/{id}"
MAX_HOPS = 4                 # stop following links this far out (giant hubs)
REQUEST_DELAY_SECONDS = 0.6  # ~250 profiles per rebuild, once a day (~3 min);
                             # faster than ~4/s gets HTTP 429 from aoe2companion
ATTEMPTS = 4
TRANSIENT = (OSError, ValueError)


def assign_owners(listed: set[int], neighbours, max_hops: int = MAX_HOPS):
    """Multi-source BFS from every listed account at once.

    Returns (owners, ambiguous): owners maps account -> (owner, hops) where the
    owner is the closest listed account (listed accounts own themselves at 0
    hops); ambiguous maps account -> sorted owners it was equally close to.
    Ambiguous accounts are not expanded further, so they can't leak ownership.
    """
    owners = {pid: (pid, 0) for pid in listed}
    ambiguous: dict[int, list[int]] = {}
    frontier = sorted(listed)
    for hops in range(1, max_hops + 1):
        reached: dict[int, set[int]] = {}
        for pid in frontier:
            owner = owners[pid][0]
            for n in neighbours(pid):
                if n not in owners and n not in ambiguous:
                    reached.setdefault(n, set()).add(owner)
        frontier = []
        for n, who in sorted(reached.items()):
            if len(who) > 1:
                # Links aren't always two-way ("Eren (LE | Dimoking)" lists Eren,
                # Eren doesn't list it). Tie-break: an owner this account itself
                # links to directly wins, if exactly one does.
                direct = who & set(neighbours(n))
                if len(direct) == 1:
                    who = direct
            if len(who) == 1:
                owners[n] = (next(iter(who)), hops)
                frontier.append(n)
            else:
                ambiguous[n] = sorted(who)
        if not frontier:
            break
    return owners, ambiguous


class ProfileCache:
    def __init__(self, headers: dict):
        self.headers, self.data, self.failed = headers, {}, []

    def get(self, pid: int) -> dict:
        if pid not in self.data:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    req = Request(PROFILE_URL.format(id=pid), headers={**self.headers, "Accept": "application/json"})
                    with urlopen(req, timeout=30) as resp:
                        self.data[pid] = json.loads(resp.read().decode("utf-8"))
                    break
                except TRANSIENT as e:
                    if attempt == ATTEMPTS:
                        print(f"  ! profile {pid} failed: {e}", file=sys.stderr)
                        self.failed.append(pid)
                        self.data[pid] = {}
                    else:
                        # Rate limited: honour Retry-After, else back off 10s, 20s, 30s
                        retry_after = getattr(e, "headers", None) and e.headers.get("Retry-After")
                        wait = int(retry_after) if retry_after and retry_after.isdigit() else (
                            10 * attempt if getattr(e, "code", None) == 429 else 3)
                        time.sleep(min(wait, 60))
            time.sleep(REQUEST_DELAY_SECONDS)
        return self.data[pid]

    def neighbours(self, pid: int) -> list[int]:
        return [l["profileId"] for l in (self.get(pid).get("linkedProfiles") or []) if l.get("profileId")]

    def summary(self, pid: int) -> dict:
        p = self.get(pid)
        r1 = next((l for l in p.get("leaderboards", []) if l.get("leaderboardId") == "rm_1v1"), {})
        return {"name": account_name(p.get("name") or str(pid)), "rating": r1.get("rating"),
                "last": (r1.get("lastMatchTime") or "")[:10] or None}


# aoe2companion labels claimed accounts "Owner (account name)", e.g.
# "Keno (hK | Siesta)"; keep just the account's own name.
OWNER_LABEL = re.compile(r"^.+? \((.+)\)$")


def account_name(name: str) -> str:
    m = OWNER_LABEL.match(name)
    return m.group(1) if m else name


def build_links(listed: dict[int, str], headers: dict, now: datetime) -> dict:
    """listed: official id -> display name. Returns the links.json structure."""
    cache = ProfileCache(headers)
    owners, ambiguous = assign_owners(set(listed), cache.neighbours)
    if len(cache.failed) > len(owners) // 2:
        raise RuntimeError(f"too many profile lookups failed ({len(cache.failed)})")
    players = {str(o): {"name": n, "accounts": []} for o, n in listed.items()}
    for acct, (owner, hops) in sorted(owners.items(), key=lambda kv: (kv[1][1], kv[0])):
        if acct != owner:
            players[str(owner)]["accounts"].append({"id": acct, "hops": hops, **cache.summary(acct)})
    return {
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "players": players,
        "ambiguous": [{"id": a, **cache.summary(a), "owners": [listed[o] for o in who]}
                      for a, who in sorted(ambiguous.items())],
    }


def account_owners(links: dict) -> dict[int, tuple[int, str]]:
    """account id -> (owner official id, account name), for every linked account."""
    return {a["id"]: (int(o), a["name"]) for o, p in links.get("players", {}).items() for a in p["accounts"]}


def write_summary(links: dict, rosters: list[tuple[str, list[int]]], path: Path) -> None:
    """LINKED_ACCOUNTS.md: per roster, each listed player and their linked accounts."""
    players = links["players"]
    out = ["# Linked accounts", "",
           f"Generated {links['updated_at']} from aoe2companion's linked profiles (followed "
           f"up to {MAX_HOPS} links deep; each account belongs to the closest listed player).",
           "Used to show players LIVE when they play on any of their accounts. Ratings are RM 1v1.", ""]
    for title, ids in rosters:
        out += [f"## {title}", "", "| Player | Linked accounts |", "|---|---|"]
        for oid in ids:
            p = players.get(str(oid))
            if not p:
                continue
            accts = ", ".join(
                f"{a['name']} ({a['rating'] or '—'}{'' if a['hops'] == 1 else f', {a['hops']} links away'})"
                for a in p["accounts"]) or "—"
            out.append(f"| {p['name']} | {accts.replace('|', '/')} |")
        out.append("")
    if links.get("ambiguous"):
        out += ["## Ambiguous (equally close to two listed players, not assigned)", ""]
        out += [f"- {a['name']} (id {a['id']}): between {', '.join(a['owners'])}" for a in links["ambiguous"]]
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    sys.exit("run via: python scripts/fetch_ratings.py --links")
