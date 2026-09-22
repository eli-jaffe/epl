# Live FPL API Connector — Implementation Plan

**Status: not built.** This is a plan for a future session to pick up and
implement — written so it's self-contained even without this conversation's
history. It assumes the reader has this repo checked out and has read the main
`README.md` (schema, data flow, design principles) first.

## Why this is needed

The historical data source (`vaastav/Fantasy-Premier-League` on GitHub, see
`ingestion/github_historical.py`) stopped weekly updates after the 2024-25
season — it now only refreshes 3x/season (preseason, end of January transfer
window, season end). That's fine for backfilling past seasons, but useless for
in-season decisions, which need gameweek-fresh data. This connector talks
directly to the same API that repo scrapes, `fantasy.premierleague.com/api/*`,
to keep the *current* season's rows fresh between those infrequent repo updates.

Like the GitHub source, this API is **fully public and unauthenticated** — no
token, login, or API key. Confirmed with a plain `curl` returning HTTP 200 with
real data. (Only per-manager endpoints like `/api/entry/<team_id>/...` need
auth, and those aren't in scope here — this connector is about global
player/team/fixture data, not your personal team.)

## Endpoints (verified live during planning, 2026-09-15)

| Endpoint | Gives you | Verified shape |
|---|---|---|
| `GET /api/bootstrap-static/` | All players (`elements`), all teams (`teams`), gameweek metadata (`events`) — a live snapshot equivalent to a season's `players_raw.csv` + `teams.csv` combined | `elements[0]` has `id`, `code`, `team`, `element_type`, `now_cost`, and ~105 more fields — same field names as `players_raw.csv`. `teams[0]` has `id`, `code`, `name`, `short_name`, `strength*` — same as `teams.csv`. `events` has one entry per gameweek with `id`, `finished`, `is_current`, `deadline_time`. |
| `GET /api/fixtures/` | All fixtures for the current season | Same shape as `fixtures.csv` (`team_h`, `team_a`, `event`, scores, `*_difficulty`, `finished`). |
| `GET /api/event/<gw>/live/` | Every player's stats for one specific gameweek, in a single call | `elements[i]` = `{id, stats: {...}, explain: [...], modified}`. `stats` is the **aggregated** total for that gameweek (all ~28 fields we need — minutes, goals_scored, bps, ict_index, expected_goals, etc., matching our column names closely). `explain` is a list of `{fixture: <id>, stats: [{identifier, points, value, points_modification}, ...]}` — one entry per fixture the player had that gameweek, which is exactly the per-fixture breakdown our schema's grain needs for double gameweeks. |
| `GET /api/element-summary/<player_id>/` | One player's full current-season gameweek history (`history`) plus prior-season summaries (`history_past`) | Not deeply tested this session — documented via the source repo's own `getters.py`. `history` is per-fixture already. Only needed as a fallback (see Open Question 1 below) or if a full per-player rebuild is ever needed. |

## Design carried over from the historical pipeline — do not re-derive these

These were hard-won in building `load_historical.py` and apply identically here:

1. **Key on `code`, not `id`.** `id` is reassigned every season (verified: Salah's
   `id` changed across 4 seasons while `code` stayed `118748`). `bootstrap-static`
   elements/teams have both fields, same as the CSVs — reuse the same crosswalk
   pattern from `build_team_crosswalk()` / `build_player_and_season_data()` in
   `load_historical.py`, just fed from JSON instead of CSVs. Since this connector
   only ever deals with the *current* season, the crosswalk is simpler (no need
   to reconcile across 11 seasons' worth of renamed clubs like Ipswich/Hull —
   that whole problem was cross-season; a single season's own data is internally
   consistent).
2. **`fact_player_gameweek` grain is player-per-*fixture***, PK
   `(season_id, gameweek, player_code, fixture_id)` — not per-gameweek. Real
   double gameweeks exist (confirmed: 2025-26 GW33). This is exactly why
   `event/<gw>/live/`'s `explain` array (per-fixture) is the right building
   block, not just the aggregated `stats` object.
3. **Upsert, never delete.** Use the same
   `INSERT ... BY NAME SELECT * FROM df ON CONFLICT (<pk cols from
   duckdb_constraints()>) DO UPDATE SET ...` pattern. This matters even more
   here than for the historical backfill: a gameweek's stats can shift after
   it "finishes" (BPS/bonus recalculation lag), so re-running this connector
   against the same gameweek repeatedly should just converge to the latest
   truth, never error or duplicate.
4. **Vectorize the transform**, don't loop row-by-row with `.iterrows()` — that
   was the actual cause of a 5+ minute hang earlier in this project for a
   dataset an order of magnitude bigger than what this connector handles per
   run. Volume here is much smaller (~700 players x up to 38 gameweeks), but
   the habit is worth keeping.
5. **Lineage columns on every row**: `source='live_api'`, `source_ref` = the
   fetch timestamp (ISO 8601) since there's no commit SHA equivalent for a
   live API, `loaded_at`. Log one row per run to `ingest_runs`.
6. **Raw landing zone stays outside this repo.** Mirror the existing pattern:
   save the raw JSON responses to `~/epl-data/raw/live_api/<fetch_timestamp>/`
   (a new subtree next to `raw/github/` in the `epl-data` repo) before
   transforming — gives you a debuggable/replayable snapshot and keeps the
   "outside the app, doubles as backup" property consistent with the
   historical source.

## Proposed file layout

```
ingestion/
  live_api.py          # extract: hits the endpoints above, saves raw JSON
                        #   to ~/epl-data/raw/live_api/<timestamp>/, writes
                        #   a manifest.json (fetch timestamp, gameweeks pulled)
  load_live.py          # load: reads that raw JSON, transforms, upserts into
                        #   the same 7 tables + ingest_runs
```

Consider factoring the crosswalk-building and `bulk_insert`/`upsert_df` helpers
out of `load_historical.py` into a shared `ingestion/common.py` at this point,
since `load_live.py` will want the same upsert-by-primary-key helper — don't
copy-paste it a second time.

## Step-by-step build plan

1. **Confirm how to derive `season_id` from the live API alone.** Unlike the
   GitHub source (which has an explicit `data/<season>/` directory per season
   we can enumerate), the live API only ever exposes the *current* season —
   there's no "list of seasons" endpoint. Verify a reliable derivation, e.g.
   from `events[0].deadline_time`'s year/month (EPL seasons start in August:
   if deadline month >= July, `season_id = f"{year}-{(year+1)%100:02d}"`, else
   `f"{year-1}-{year%100:02d}"`). Cross-check against `dim_season.is_current`
   already in the warehouse (should agree — currently `2026-27`).
2. **Extract stage (`live_api.py`)**: fetch `bootstrap-static`, `fixtures`, and
   `event/<gw>/live/` for every gameweek from 1 up to the current one (cheap —
   don't bother with incremental/partial fetching, consistent with how the
   historical loader just reloads everything each run). Save raw JSON + a
   manifest with the fetch timestamp.
3. **Load stage (`load_live.py`)**:
   - Upsert `dim_season` (ensure the current season row exists, `is_current=true`,
     flip any previously-current season to `false`).
   - Upsert `dim_team` / `dim_player` from `bootstrap-static`'s `teams`/`elements`.
   - Upsert `fact_team_season` / `fact_player_season` from the same (this
     season only — don't touch other seasons' rows).
   - Upsert `fact_fixture` from `/api/fixtures/`.
   - Upsert `fact_player_gameweek` from each `event/<gw>/live/` response: for
     each player, iterate `explain` entries (one per fixture that gameweek),
     pivot each fixture's `stats: [{identifier, value}, ...]` list into a flat
     dict of column values, and combine with the top-level `stats` object for
     any fields missing from `explain` (see Open Question 1).
   - Log the run to `ingest_runs`.
4. **Validate** using the same pattern as the historical load: spot-check a
   known player's current-season totals against what the actual FPL site
   shows, confirm re-running the connector twice in a row produces identical
   row counts (idempotency), and confirm rows for the current season now show
   `source='live_api'` rather than the `'github_repo'` rows the historical
   backfill originally loaded for this same season (this is expected —
   upserting should overwrite them, since live data supersedes the repo's
   stale copy for the in-progress season).

## Open questions to resolve while building (not resolved in this plan)

1. **Does `explain`'s per-fixture stats list include *every* column we need,
   or only the ones that contributed points?** The one live example pulled
   during planning (GW4, a single-fixture gameweek) only listed 4 identifiers
   (`minutes`, `clean_sheets`, `penalties_saved`, `bonus`) — plausibly because
   `explain` is a *scoring* breakdown, not a full stats dump, and fields like
   `ict_index` or `expected_goals` (which don't themselves score points) might
   never appear there. This wasn't testable against an actual double-gameweek
   example this session (the live API only exposes the current season, which
   hasn't had one yet). **Before building the double-gameweek path**: check
   `explain` against a real double-gameweek once one occurs, or check it
   retroactively via a season/gameweek that's already finished. If `explain`
   turns out incomplete for non-scoring fields, fall back to
   `element-summary/<id>/history` (confirmed per-fixture-complete, since
   that's literally what the CSV export is built from) for just the subset of
   players who had >1 fixture that gameweek — not all ~700 players, only the
   double-gameweek subset, to keep the call count small.
2. **Refresh cadence** — manual/on-demand invocation, or a scheduled job
   (cron/launchd)? Given this is a local single-user tool, on-demand ("run it
   before making transfer decisions") is the simplest starting point; a
   schedule can be layered on later without changing the connector itself.
   This is a product decision for whoever picks this up to confirm with the
   user, not something to assume.
3. **Rate limiting / politeness.** No documented rate limit was hit during
   planning, but add basic retry-with-backoff on transient failures regardless
   — the source repo's own `getters.py` does the same for `element-summary`
   calls, and the live API is presumably under heavier load around gameweek
   deadlines.
4. **In-progress-gameweek data isn't final.** Stats (especially bonus points/
   BPS) can shift for a while after a gameweek's matches finish. Treat this as
   a non-issue rather than something to special-case: upserts are idempotent,
   so just re-running the connector after the dust settles naturally converges
   to the correct final values. Don't build separate "provisional vs. final"
   handling unless it turns out to actually matter in practice.

## Testing/validation checklist

- [ ] `season_id` derivation matches the already-loaded `dim_season.is_current`
      row
- [ ] Re-running the connector twice back-to-back produces identical row counts
      (idempotency)
- [ ] A known player's current-season `total_points`/`minutes` in
      `v_player_season` matches what the live FPL site shows
- [ ] `ingest_runs` gets a new row per execution with `source='live_api'`
- [ ] Once a double gameweek actually occurs this season: confirm
      `fact_player_gameweek` gets two distinct `fixture_id` rows for affected
      players, and `v_player_gameweek_totals` correctly sums them
