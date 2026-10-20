# nW Clan Roster

A live, auto-updating version of the new Wave clan poster, with tabs for the
clan and the México, Colombia and Perú rankings. Ratings come from the
official Age of Empires II ranked-stats API (World's Edge) every 5 minutes
via GitHub Actions; the page itself is a static HTML file on GitHub Pages.

## Structure

```
config/players.json          the clan members — profile IDs, tags, countries
config/{mexico,colombia,peru}.json   country rankings — IDs, clan rules, show_top
scripts/fetch_ratings.py     queries the stats API, writes docs/data/*.json
docs/index.html              the live poster page (reads docs/data/*.json)
docs/assets/logo_nW.png      clan logo
.github/workflows/update-roster.yml   runs the fetch script and commits the data
```

The page lives in `docs/` because GitHub Pages' "Deploy from a branch" mode
can only publish from the repo root or a `/docs` folder.

## First-time setup

1. **Push this to a new GitHub repo.**
   ```bash
   cd nw-roster
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create nw-roster --public --source=. --push
   # or create the repo on github.com and `git remote add origin ...` + push
   ```

2. **Run the fetch script once locally** (standard library only):
   ```bash
   python scripts/fetch_ratings.py
   ```

3. **Enable GitHub Pages.**
   Repo → Settings → Pages → Source: "Deploy from a branch" → branch `main`,
   folder `/docs`. Your live poster will be at
   `https://<your-username>.github.io/nw-roster/`.

4. **Enable the workflow.**
   It should already be picked up from `.github/workflows/update-roster.yml`.
   Go to the Actions tab, select "Update nW roster", and click "Run workflow"
   to trigger it manually the first time — don't wait a full day to find out
   if it works.

5. **Confirm the loop.** After the workflow run finishes, check that
   `docs/data/roster.json` in the repo has a fresh `updated_at` timestamp,
   then reload the Pages URL and confirm the numbers match.

## Update schedule (external timer)

GitHub's built-in `schedule:` trigger is unreliable — on this repo it fired
roughly every 3 hours instead of every 15 minutes. So an external timer at
[cron-job.org](https://cron-job.org) starts the workflow every 5 minutes via
GitHub's API. The `schedule:` in the workflow (every 15 minutes) stays as a
backup; overlapping runs are harmless (the workflow's `concurrency` setting
queues them, and each starts from the latest data).

Why not more often: a run only commits when a rating changed, and each commit
rebuilds the GitHub Pages site. Pages allows roughly 10 rebuilds an hour;
every 5 minutes stays under that, every 1–2 minutes would start getting
rebuilds delayed. Ratings only move when a game ends (25–40 min games), so
5 minutes already shows results within minutes.

**Token** — GitHub → Settings → Developer settings → Fine-grained tokens:
repository access *Only select repositories → nw-roster*, permission
*Actions: Read and write*, nothing else. Renew it before it expires.

**cron-job.org job:**

| Setting | Value |
|---|---|
| URL | `https://api.github.com/repos/MFlores712/nw-roster/actions/workflows/update-roster.yml/dispatches` |
| Schedule | every 5 minutes |
| Request method | `POST` |
| Headers | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <token>`<br>`X-GitHub-Api-Version: 2022-11-28` |
| Request body | `{"ref":"main"}` |

A successful call returns `204 No Content`. A `401` means the token expired
or was revoked; a `404` usually means the token lacks access to this repo.

## Updating the roster

To add, remove, or fix a player, edit the matching file in `config/` — the
next run picks it up. No need to touch the HTML or the workflow file.

Player `id`s are **aoe2insights** profile IDs (rows link to those profiles).
For most players that is also their official profile ID. Some newer accounts
differ; give those an `"official_id"` (find it with the API's `aliases`
lookup by in-game name). `"official_id": null` means "not found yet": the
player keeps their last known values.

## Known limitations

- aoe2insights.com was scraped until 2026-10-18, when it put its pages behind
  a Cloudflare bot challenge (HTTP 403 for scripts). The official stats API
  replaced it; it is unofficially documented, so if it changes, `parse_stats`
  in `fetch_ratings.py` may need updating.
- If a lookup fails, the script keeps the player's last known values (listed
  under `stale` in the JSON) rather than blanking them — the live page dims
  those numbers so it's visible when data is stale.
