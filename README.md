# nW Clan Roster

A live, auto-updating version of the new Wave clan poster. Data comes from
[aoe2insights.com](https://www.aoe2insights.com) once a day via GitHub Actions;
the page itself is a static HTML file hosted on GitHub Pages.

## Structure

```
config/players.json          the 8 clan members — profile IDs, tags, countries
scripts/fetch_ratings.py     scrapes aoe2insights.com, writes docs/data/roster.json
docs/index.html              the live poster page (reads docs/data/roster.json)
docs/assets/logo_nW.png      clan logo
.github/workflows/update-roster.yml   daily cron that runs the fetch script
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

2. **Verify the scraper before trusting it.** The selectors in
   `scripts/fetch_ratings.py` were written from reading rendered profile
   pages by hand, not the live HTML/DOM — they need a real check:
   ```bash
   pip install requests   # only needed if you swap urllib for requests
   python scripts/fetch_ratings.py
   cat docs/data/roster.json
   ```
   Compare the numbers against the values already seeded in
   `docs/data/roster.json`. If something's off (None values, wrong
   numbers), open one profile page's HTML directly and fix the regex
   patterns (`SECTION_1V1` / `SECTION_TEAM`) to match what's actually there.

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

## Updating the roster

To add, remove, or fix a player, edit `config/players.json` — the next
scheduled run (or a manual "Run workflow") will pick it up. No need to touch
the HTML or the workflow file.

## Known limitations

- aoe2insights.com has no public API; this scrapes their profile pages.
  If they redesign the site, the regex patterns in `fetch_ratings.py` will
  need updating.
- If a single player's fetch fails, the script falls back to their last
  known value (marked `stale_1v1` / `stale_tg` in the JSON) rather than
  blanking them out — the live page dims those numbers slightly so it's
  visible when data is stale.
