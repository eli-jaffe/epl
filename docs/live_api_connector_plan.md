# Live FPL API Connector — Plan, Findings & Build Checklist

**Status (2026-11-18): ingestion A (stats refresh) is built, merged (PRs #10,
#11) and was last run on the real DB through GW10. Ingestion C
(`element-summary` backfill, PR #14) is built and applied to the real DB: weekly
market columns are filled for all GW1-10 rows. Ingestion B (market snapshot)
extract + loader are built and the first snapshot is in the real DB (2026-11-18);
the market snapshot is scheduled daily via GitHub Actions (F2, 2026-11-19); the view S3 and the other follow-ups are NOT built.** Self-contained so any session can
pick up from here. Read the main `README.md` (schema, data flow) first, and
`CLAUDE.md` "Data-modeling gotchas". Local-only doc (not committed) per
`CLAUDE.md` — plans stay uncommitted until the user says otherwise.

## TL;DR for a new session

- Goal: keep **2026-27** rows in DuckDB fresh between the GitHub repo's
  infrequent refreshes. At the time of writing the warehouse had only **GW1**
  of 2026-27 (610 `fact_player_gameweek` rows, `source='github_repo'`) while
  the live API was at **GW9** (GW8 finished).
- The data is **real EPL footballers' stats and fantasy points** (plus club
  and fixture data). It is **not** data on FPL managers/squads — those
  endpoints (`/api/entry/<id>/...`) need auth and are out of scope. The only
  manager-side signal is crowd aggregates (ownership %, transfers, price).
- Decided architecture: **three separate ingestions with different cadences**
  (A stats, B market snapshot, C market backfill) — see "Design". Refresh is
  **on-demand** (manual command); scheduling can be layered on later.
- **How to refresh today** (stop the backend first — DuckDB lock):
  `cd ingestion && python live_api.py && python load_live.py` (venv active).
  Loads finished gameweeks only; re-runnable any time.
- **Where to pick up — suggested order:**
  1. ~~**C (backfill weekly price/ownership/transfers)**~~ — **done 2026-11-18**
     (C1-C4). Refresh after new gameweeks finish with
     `cd ingestion && python live_api.py && python load_live.py && python live_element_summary.py && python load_live_element_summary.py`
     (backend stopped). `live_element_summary.py` lands inside the latest pull dir
     and refuses to run if that pull already has `element_summary/`. Still needed
     before any double gameweek (D1).
  2. **B (daily market snapshot)** — B1/B2 built (see checklist). Take a snapshot
     with `cd ingestion && python live_snapshot.py && python load_live_snapshot.py`
     (backend stopped). It only helps if it runs regularly (a skipped day is lost
     for good), so it is **scheduled (F2, done)**. Next: the view S3 (B3). To bring the DB up
     to date: `ingestion/refresh_snapshots.sh` (pulls `epl-data`, loads any unloaded snapshots).
  3. **F7** standings from fixtures, **T1** club-transfer history, S4 README
     schema update, then F1-F6 housekeeping.
  Check the "Build checklist" below for exact state; `[x]` items are done and
  verified, `[~]` partially.
- Safety habits used so far: back up `db/epl.duckdb` (`cp`) before applying
  anything to the real file, test loaders on a copy first, and check idempotency
  by running twice. Several `db/*.bak` files exist from this work and can be deleted.

```mermaid
flowchart TD
    subgraph A["A. Stats refresh (~11 calls, on-demand)"]
        A1["bootstrap-static + fixtures + event/N/live"]
    end
    subgraph B["B. Market snapshot (1 call, daily)"]
        B1["bootstrap-static -> fact_player_snapshot (new, append-only)"]
    end
    subgraph C["C. Market backfill (~670 calls, once; optionally weekly)"]
        C1["element-summary/id -> value/selected/transfers per fixture"]
    end
    A --> W[("fact_player_gameweek / fact_fixture / dims")]
    C --> W
    B --> S[("fact_player_snapshot")]
    S -.->|"view: last snapshot per GW"| V["historical-shaped market columns"]
```

## Why this is needed

`vaastav/Fantasy-Premier-League` (see `ingestion/github_historical.py`)
stopped weekly updates after 2024-25; it now refreshes ~3x/season. The live
API `fantasy.premierleague.com/api/*` is what that repo scrapes. It is fully
public and unauthenticated (plain `curl` returns 200).

## API exploration results (2026-10-31, real data, GW9 in progress)

Raw sample pull: `~/epl-data/raw/live_api/20261031T130811Z/` (bootstrap 1.9 MB,
fixtures 336 KB, event_live 1..9 4.4 MB total). Saved as single-line JSON; view
with `jq`, VS Code "Format Document", or `duckdb read_json`.

### Endpoints

| Endpoint | Verified contents |
|---|---|
| `GET /bootstrap-static/` | Top-level keys: `elements` (669 players, 109 fields each), `teams` (20; `id`, `code`, `name`, `short_name`, strength fields), `events` (38 GWs: `id`, `deadline_time`, `finished`, `is_current`), `total_players`, plus config (`chips`, `phases`, ...). **Current snapshot only** — no history. |
| `GET /fixtures/` | 380 fixtures: `id`, `event`, `team_h`, `team_a`, `team_h_score`, `team_a_score`, `team_h_difficulty`, `team_a_difficulty`, `finished`, `kickoff_time`. |
| `GET /event/<gw>/live/` | `elements[i] = {id, stats, explain, modified}`. `stats` = per-player totals for the GW (**summed across fixtures**). `explain` = list of `{fixture, stats:[{identifier, points, value, points_modification}]}`, one entry per fixture. |
| `GET /element-summary/<player_id>/` | `history` (per-fixture rows, current season) + `history_past`. Verified fields per row: all the stats below **plus** `value`, `selected`, `transfers_in`, `transfers_out`, `transfers_balance`, `fixture`, `opponent_team`, `was_home`, `kickoff_time`, `round`, scores. This is the only source of weekly market data. |

### `event/<gw>/live` — what stats we get per footballer

`stats` keys (29): `minutes`, `starts`, `played`, `total_points`, `bonus`, `bps`,
`goals_scored`, `assists`, `clean_sheets`, `goals_conceded`, `own_goals`,
`saves`, `penalties_saved`, `penalties_missed`, `yellow_cards`, `red_cards`,
`influence`, `creativity`, `threat`, `ict_index`, `expected_goals`,
`expected_assists`, `expected_goal_involvements`, `expected_goals_conceded`,
`clearances_blocks_interceptions`, `recoveries`, `tackles`,
`defensive_contribution`, `in_dreamteam`. (ICT and expected_* values arrive
as **strings** — cast to numeric.)

- **23 map onto existing `fact_player_gameweek` columns**: minutes, starts,
  total_points, goals_scored, assists, clean_sheets, goals_conceded, own_goals,
  bonus, bps, influence, creativity, threat, ict_index, expected_goals,
  expected_assists, expected_goal_involvements, expected_goals_conceded,
  yellow_cards, red_cards, saves, penalties_saved, penalties_missed.
- **6 are NOT in our schema yet**: `tackles`, `recoveries`,
  `clearances_blocks_interceptions`, `defensive_contribution` (FPL's newer
  defensive-action stats; historical seasons would be NULL) and `played`,
  `in_dreamteam` (low value; likely skip).

### Gaps vs. the historical `fact_player_gameweek` (Open Question 1, resolved)

`explain` is a **scoring breakdown only**: in GW8 it contained just 11
identifiers (`assists, bonus, clean_sheets, defensive_contribution,
goals_conceded, goals_scored, minutes, own_goals, red_cards, saves,
yellow_cards`). It omits `bps`, `ict_index`, `influence`, `creativity`,
`threat`, all `expected_*`, `starts`, `tackles`, etc. Consequences:

| Column | Live source |
|---|---|
| 23 stats above | `event/live` `stats` (aggregate per GW). For single-fixture players (almost every row) merge directly. **For double-gameweek players the non-scoring fields cannot be split per fixture from this endpoint** → use `element-summary` for just those players. No DGW has occurred in 2026-27 yet, so that path is untestable until one does. |
| `fixture_id` | `explain[].fixture` |
| `opponent_team_code`, `was_home`, `team_code` | join `fixture_id` to `fixtures.json`; team from `bootstrap-static` element `team`/`team_code` |
| `value` (weekly price), `selected` (weekly ownership), `transfers_in/out/balance` | **No live-endpoint source.** `element-summary` `history` has them per fixture. `bootstrap-static` has only current snapshot fields (`now_cost`, `selected_by_percent`, `transfers_in_event`, `transfers_out_event`, `cost_change_event`). |
| `fact_player_season` fields | `bootstrap-static`: `element_type` (position), `team_code`, `now_cost`, `selected_by_percent`, `now_cost_rank`, `points_per_game`, `form`, `total_points`, `minutes`. `start_cost`/`end_cost` must be derived (historical loader backfills `start_cost` from earliest GW `value`). |

### Rate limits

None documented; none hit. The vaastav scraper has long made ~700 sequential
`element-summary` calls per run without known issues. Plan: sequential with a
short sleep (~0.2s → ~2–3 min for 670 calls), plus the retry/backoff already in
`live_api.py` (retries on connection error, timeout, 429, 5xx). Not stress
tested — time a small batch first if concerned.

## Identity-matching verification (2026-10-31, read-only against `db/epl.duckdb`)

Live API vs. the warehouse's 2026-27 historical rows — all keys line up:

| Check | Result |
|---|---|
| Team `code` -> `dim_team` | 20/20 matched, names identical |
| Team `id` vs `fact_team_season.team_id_that_season` (2026-27) | 20/20 equal |
| Player `code` -> `dim_player` | 634/669 matched; **35 are new players** (23 already have minutes) -> `dim_player` upsert must add them |
| Player `id` vs `fact_player_season.player_id_that_season` (2026-27) | 616/616 equal; all 616 warehouse players are in the live data |
| Player `id` across seasons | differs for 477/482 returning players -> confirms: key on `code`, never `id` |
| Fixture `id` -> `fact_fixture` (2026-27) | 380/380 matched, same home/away teams, same gameweek |
| GW1 `event_live` vs `fact_player_gameweek` GW1 | 610/610 rows match on (player_code, fixture_id); `total_points` and `minutes` equal on all 610 |

Surprises to handle in the loader:
- **15 players have changed club** since the warehouse's 2026-27 load (e.g.
  N.Jackson Chelsea -> Aston Villa, Ndiaye Everton -> Man City). The live
  `team` is the *current* club, so upserting `fact_player_season.team_code` is
  right. **But** for `fact_player_gameweek`, using the current club for earlier
  GWs would mislabel movers' earlier rows. Derive `team_code` per fixture
  instead (the player's club is whichever of `team_h`/`team_a` they played
  for — ambiguous from `event_live` alone; `element-summary` gives `was_home`
  + `opponent_team`, so C resolves it). Until then, for movers keep the
  existing GW rows' `team_code` where present and flag others.
- `web_name` can differ by formatting (`N.Angulo` live vs `Angulo` in
  `dim_player`); `code` is the key, so just let the upsert overwrite names.
- `fact_player_season` has a single `team_code` per season, so a mid-season
  mover keeps only their latest club there (pre-existing limitation).

## Design (decided with the user, 2026-10-31)

1. **Three ingestions, different cadences** (no need for one monolith):
   - **A. Stats refresh** — `bootstrap-static`, `fixtures`, `event/<gw>/live`
     for GW1..current. ~11 calls. On-demand. Fills performance stats, fixtures,
     dims, season facts.
   - **B. Market snapshot** — one `bootstrap-static` call, daily (on-demand
     first). Writes a **new append-only table `fact_player_snapshot`**, keyed
     `(player_code, snapshot_at)`: `now_cost`, `selected_by_percent`,
     `transfers_in_event`, `transfers_out_event`, `cost_change_event`,
     `gameweek` (current event), plus lineage columns. Finer-grained than the
     historical per-GW grain, so it is *not* forced into `fact_player_gameweek`;
     a view collapses the last snapshot per GW into the historical shape.
     Mapping notes: absolute `selected` ≈ `selected_by_percent` ×
     `total_players` / 100; historical `transfers_in/out` = per-GW flow =
     `transfers_in_event` / `transfers_out_event`; `value` = `now_cost`
     (tenths of £m, same as historical). New capability vs history: intra-week
     price/ownership movement.
   - **C. Market backfill** — `element-summary/<id>` for every player (~670
     calls), **one-time** to fill `value`/`selected`/`transfers_*` for the
     already-played GWs (snapshots can't recover the past). Optionally re-run
     weekly after each GW finishes to correct drift. Also the fallback for
     double-gameweek fixture-level stats.
2. **Add the four defensive-stat columns** (`tackles`, `recoveries`,
   `clearances_blocks_interceptions`, `defensive_contribution`) to
   `fact_player_gameweek` in the same schema change (NULL for old seasons).
   Skip `played`, `in_dreamteam`.
3. **Refresh cadence: on-demand** (manual command) first; cron/launchd later
   without connector changes.
4. In-progress GW data isn't final (BPS/bonus shift). Not special-cased:
   upserts converge on re-run.

## Design rules carried over from the historical pipeline — do not re-derive

1. **Key on `code`, not `id`** (`id` is reassigned every season). Season-scoped
   `id` stays on fact tables where raw data references it. Single-season data
   makes the crosswalk simpler than `build_team_crosswalk()` in
   `load_historical.py`.
2. **`fact_player_gameweek` grain is player-per-*fixture***, PK
   `(season_id, gameweek, player_code, fixture_id)`. Real double gameweeks exist.
3. **Upsert, never delete** — use `common.upsert_df(con, table, df)`.
4. **Vectorize** pandas transforms; no `.iterrows()`.
5. **Lineage columns** on every row: `source='live_api'`, `source_ref` = fetch
   timestamp (ISO 8601; no commit SHA exists), `loaded_at`. One `ingest_runs`
   row per run. Upserting overwrites the `github_repo` rows for 2026-27 — expected.
6. **Raw landing zone outside this repo**: `~/epl-data/raw/live_api/<UTC timestamp>/`
   (a new subtree next to `raw/github/` in the `epl-data` repo).
   Unlike the GitHub landing zone, each run keeps its own timestamped
   directory; pruning old ones is an open item.
7. Scripts run from `ingestion/` (they `import config` / `from common import ...`),
   with the repo `venv` active: `cd ingestion && python live_api.py`.

## Build checklist

Mark items `[x]` with the date as they're completed. Verify against the code,
not just this list.

### Done
- [x] API exploration against live endpoints (2026-10-31) — findings above
- [x] `ingestion/common.py` — `upsert_df(con, table, df)` extracted from
      `load_historical.run()`; `load_historical.py` imports it (import checked;
      **historical load not re-run** after the refactor — re-run or verify
      idempotently before relying on it)
- [x] `ingestion/config.py` — `FPL_API_BASE`, `LIVE_RAW_DATA_DIR`,
      `LIVE_MAX_ATTEMPTS`
- [x] `ingestion/live_api.py` (extract for A) — fetches bootstrap-static,
      fixtures, `event/N/live` for every current/finished GW; retry/backoff;
      derives `season_id` (`2026-27`, matches `dim_season.is_current`); writes
      `manifest.json` (`source`, `fetched_at`, `season_id`, `gameweeks`).
      Verified: ran successfully, GW1–9 landed.
- [x] PR #10 review fixes in `live_api.py` / `config.py` / `schema.sql`
      (2026-10-31): atomic landing (writes `<ts>.partial`, renames after the
      manifest); non-JSON 200 responses retried; clear error when `events` is
      empty (between seasons); `LIVE_REQUEST_PAUSE_SECONDS` (0.2s) after each
      successful call; `fact_player_snapshot` comment no longer implies the S3
      view exists. Checked by re-running the extract and unit-testing the retry
      and empty-events paths with mocks.

### Schema (do first; show the user the DDL before applying)
- [x] **S1** Four defensive-stat columns added to `fact_player_gameweek` in
      `db/schema.sql`; `db/migrations/001_live_data.sql` (idempotent `ALTER`s)
      brings an existing DuckDB file up to date. Tested on a copy (2026-10-31),
      run twice, views still work. **Applied to `db/epl.duckdb` 2026-10-31** (backup at
      `db/epl.duckdb.bak`, untracked — delete once satisfied). Verified: row
      counts, point/minute/goal/bps sums and view counts identical before/after;
      new columns all NULL; 2nd run a no-op.
- [x] **S2** `fact_player_snapshot` created in `db/schema.sql` + same migration
      file. PK `(season_id, player_code, snapshot_at)`; columns: `gameweek`,
      `now_cost`, `selected_by_percent`, `total_players`, `transfers_in_event`,
      `transfers_out_event`, `cost_change_event`, lineage. Fresh `schema.sql`
      build verified. Applied (see S1); `fact_player_snapshot` exists, empty, with its PK and 2 FKs.
- [ ] **S3** View collapsing last snapshot per GW into historical-shaped market
      columns (name suggestion: `v_player_gameweek_market`)
- [ ] **S4** Update `README.md` schema diagram/tables

### Ingestion A — stats refresh (`ingestion/load_live.py`)
- [x] **A1** (`latest_pull_dir()` in `load_live.py`) Read latest `raw/live_api/<ts>/` + manifest. Only consider
      directories **without** the `.partial` suffix (extract renames on success;
      a leftover `.partial` is a failed pull and safe to delete)
- [x] **A2** (in `load_live.py`) Upsert `dim_season` (current; flip previous `is_current` to false)
- [x] **A3** (in `load_live.py`) Upsert `dim_team`, `dim_player` from bootstrap `teams`/`elements`
- [x] **A4** (built in `load_live.py`, 2026-10-31; not yet run on the real DB)  Upsert `fact_team_season`, `fact_player_season` (2026-27 only;
      cast string numerics)
- [x] **A5** (built, same caveat)  Upsert `fact_fixture` from `fixtures.json` (scores NULL for
      unplayed fixtures)
- [x] **A6** (built, same caveat)  Upsert `fact_player_gameweek` from `event_live`: one row per
      `explain` entry; merge `stats`; derive opponent/home/team from fixtures;
      cast ICT/expected strings; `team_code` caveat for club-movers (see identity-matching section). Guard: if a player has >1 `explain` entry in a
      GW, fail loudly (or route to C) rather than loading aggregate stats onto
      each fixture row
- [x] **A7** (built, same caveat)  Log `ingest_runs` (`source='live_api'`)
- [x] **A9** (2026-10-31, user decision) Load **finished gameweeks only**: the
      loader intersects the manifest's gameweeks with `bootstrap-static` `events[].finished`
      and skips the in-progress one (the raw pull keeps it). `fact_player_season`
      `total_points`/`minutes` are summed from the loaded GW rows instead of taking the
      API's season totals, which include the in-progress GW. The skipped GW lands on the
      first run after it closes. Tested on a copy: GW1-8 loaded, GW9 skipped, season
      totals == sum of loaded GWs for 669/669, re-run identical.
- [~] **A8** Validated on a copy, then **run on the real `db/epl.duckdb` 2026-10-31**
      (backup `db/epl.duckdb.pre-facts.bak`; GW1-8 loaded, GW9 skipped, 259,110
      `fact_player_gameweek` rows, older seasons untouched at 253,890, re-run
      identical, 0 NULL club columns). Copy-run details:
      - GW1: all 610 existing rows unchanged across every stat column, `team_code`,
        `opponent_team_code`, `was_home` and the market columns (`value`, `selected`,
        `transfers_*`) — the live transform reproduces the GitHub rows exactly
      - 259,779 `fact_player_gameweek` rows afterwards (+5,279, GW2-9); re-run gives
        identical counts
      - (before A9) per-player sum of GW `total_points`/`minutes` equalled `bootstrap-static`'s
        season totals for 669/669 players; after A9 it equals the loaded-GW sum instead
      - 0 rows with unresolved club; no NULL `team_code`/`opponent_team_code`/`was_home`;
        0 NULL `start_cost` for 2026-27
      - Still to do: eyeball one player against the FPL website
      Implementation notes: club per row = current club if it is in the fixture, else
      the club already stored for that row, else the player's latest stored club (handles
      the 17 GW1 / 8 GW2 rows for players who moved clubs); `start_cost` = `now_cost -
      cost_change_start`; team standings columns (`played`/`win`/.../`position`) and the
      `strength` column are deliberately not written (API values are zero/null); a player
      with >1 fixture in a GW makes the loader raise (see C2/D1). Market columns on
      GW1 rows keep their GitHub values but their `source` now reads `live_api`.

### Ingestion B — market snapshot
- [x] **B1** (2026-11-18, `ingestion/live_snapshot.py`) Extract: one
      `bootstrap-static` fetch (reuses `live_api.py` helpers), landed in
      `~/epl-data/raw/live_snapshots/<ts>/` with a manifest, via a `.partial`
      rename. Deliberately a **separate subtree** (`config.LIVE_SNAPSHOT_RAW_DIR`):
      `load_live.latest_pull_dir()` takes the newest dir under `raw/live_api/` and
      would break on a snapshot-only directory. ~1.9 MB per run, so retention (F1)
      matters more once this runs daily.
- [x] **B2** (2026-11-18, `ingestion/load_live_snapshot.py`) Load into
      `fact_player_snapshot` (append; PK `(season_id, player_code, snapshot_at)`);
      `snapshot_at` = fetch time (naive UTC), so re-loading a pull is idempotent
      and a new pull appends. Players missing from `dim_player` are skipped and
      counted in `ingest_runs.notes` (so an unattended run survives a new signing;
      run `load_live.py` first to add them). Optional DB-path arg. Tested on a
      copy: same pull twice = 672 rows, a second pull = 1,344, values match the
      raw file. Applied to the real DB (backup `db/epl.duckdb.pre-snapshot.bak`):
      672 rows, 0 skipped. `gameweek` stores the API's current event, which still
      names the just-finished GW before the next opens, so S3 should match
      snapshots to gameweeks by time (last snapshot before the GW's first kickoff
      in `fact_fixture`), not by that column.
- [ ] **B3** Validate view `v_player_gameweek_market` against a backfilled GW
      (needs S3). Note `selected_by_percent` is rounded to 0.1% (~11k managers),
      so absolute `selected` derived from it is approximate; the backfill's
      `selected` is exact.
- Findings (2026-11-18): FPL prices change overnight on most nights, driven by
  transfer activity, ~0.1m per change; third-party sources report a ~0.3m/week
  cap (unverified for 2026-27; our data shows 0-3 step weekly moves, none
  larger). Transfer counters move continuously (395/669 players changed in 40
  min); `selected_by_percent` barely does. See the snapshot's `transfers_*_event`
  for the finer-grained signal. `transfers_balance` != in - out on a few rows in
  every season (FPL's own data), so recompute net from in - out.

### Ingestion C — `element-summary` backfill
- [x] **C1** (2026-11-18, `ingestion/live_element_summary.py`) Extract: all 672
      players sequentially (0.2s pause, `get_json` retries), landed **inside the
      latest complete pull dir** as `element_summary/<id>.json` + `manifest.json`
      (a separate dir would break `load_live.latest_pull_dir()`, which expects
      `bootstrap-static.json`). Atomic via `element_summary.partial` rename;
      refuses if `element_summary/` already exists. Ran in 3:49, 13 MB; 6,562
      history rows = the 6,562 stored 2026-27 rows.
- [x] **C2** (2026-11-18, `ingestion/load_live_element_summary.py`) Load: updates
      only `value`, `selected`, `transfers_in/out/balance` (+ lineage) via
      `upsert_df`, inner-joined on the PK (gameweek, player_code, fixture_id)
      so it can never insert rows; optional DB-path arg for testing on a copy.
      Deviation from the original plan: the four defensive stats are **not**
      overwritten (A already fills them); they and total_points/minutes are
      cross-checked instead. Tested on a copy twice (identical), then applied to
      the real DB (backup `db/epl.duckdb.pre-market.bak`): 6,562/6,562 rows
      matched, 0 stat mismatches, GW1 values equal the GitHub-sourced ones,
      market columns 6,562/6,562 populated, other seasons unchanged.
      Known source quirk: 2 rows (GW2) have `transfers_balance` != in - out; FPL
      publishes it that way and older seasons have the same (2-4 per season).
      Recompute net transfers from in - out rather than trusting `balance`.
      Still open: double-gameweek players (A6 guard still raises; D1).
- [x] **C3** (2026-11-18) No change needed: A's `start_cost = now_cost -
      cost_change_start` equals the earliest-GW `value` for 672/672 players,
      including the 62 who first appear after GW1. Don't add the earliest-GW
      step to the live loader.
- [x] **C4** (2026-11-18) 40 timed calls: all 200, no 429s, ~0.31s/call incl.
      the 0.2s pause, ~17.6 KB each; the full 672-call run took 3:49. Pause kept
      at 0.2s.

### Reconciliation & history (added 2026-10-31; A3 depends on R1)
- [x] **R1** (built 2026-10-31 in `ingestion/load_live.py`, dims only; tested on a
      DB copy, **not yet run against `db/epl.duckdb`**) New-player + `dim_player` reconciliation (needed inside A3). The
      live `elements` carry every `dim_player` column: `first_name`,
      `second_name`, `web_name`, `birth_date` (655/669 populated), `region`.
      Proposed rule (confirm with the user): upsert on `player_code`; **insert**
      the 35 unseen players (before any fact rows, FK order); for existing
      players **overwrite** names/`region` with live values (live is
      fresher/canonical — e.g. `Angulo` -> `N.Angulo`), but **never overwrite a
      non-null `birth_date` with NULL** (use COALESCE(live, existing)). Log
      counts of inserted vs. changed players in `ingest_runs.notes`, and
      consider reporting which fields changed. Note the generic
      `upsert_df` overwrites every non-PK column, so R1 needs either a
      pre-merge in pandas (coalesce, then upsert) or a dedicated SQL upsert.
- [x] **R2** (covered by A4: position/team/start_cost for all 669 players)  Also reconcile `fact_player_season` for new players (position
      from `element_type`, team, etc.) and make sure each new `player_code`
      has a `dim_player` row first.
- [ ] **T1** (future) Record club transfers. Today a player's club is only
      stored as one `team_code` per season on `fact_player_season` (latest
      wins) and per row on `fact_player_gameweek`, so a mid-season move
      (15 detected at 2026-10-31) loses *when* it happened and where from.
      Idea: a `fact_player_team_history` (`player_code`, `team_code`,
      `from_gameweek`/`valid_from`, `valid_to`, lineage), populated by diffing
      `bootstrap-static` team vs. the last known team each run (and/or from
      `team_join_date`, which the API exposes), with the first run seeding the
      15 known movers. Would also fix the per-gameweek `team_code` caveat in
      A6. Not scoped yet — confirm design with the user before building.

### After DGW occurs (not testable earlier)
- [ ] **D1** Confirm two distinct `fixture_id` rows per affected player and
      that `v_player_gameweek_totals` sums them correctly

### Follow-ups
- [ ] **F1** Prune old `raw/live_api/<ts>/` directories and stale `.partial`
      ones (policy TBD). Each run currently re-lands ~4.4 MB of `event_live`
      into `~/epl-data`, which is a git repo, so this grows
- [x] **F2** (2026-11-19) Scheduling for B, via **GitHub Actions** instead of launchd so it
      doesn't depend on the laptop being awake (a missed snapshot is unrecoverable).
      `.github/workflows/market-snapshot.yml` lives in the **private `epl-data` repo**
      (cron `0 13 * * *` UTC = 08:00 EST / 09:00 EDT, plus manual `workflow_dispatch`
      with an `epl_ref` input). It checks out `epl-data` and the public `epl` repo,
      symlinks `~/epl-data` to the checkout, runs `live_snapshot.py`, gzips the file
      (~178 KB, ~65 MB/year) and commits `raw/live_snapshots/<ts>/` as
      `github-actions[bot]` (`contents: write` only, no extra secrets). The laptop only
      loads: `load_live_snapshot.py` catches up on every unloaded snapshot dir (plain or
      `.json.gz`), retries on the DuckDB lock, and warns if the newest snapshot is >=2
      days old. Only B is scheduled; A and C stay manual (re-runnable, nothing lost).
      Verified: FPL API answers GitHub runners (200s); manual runs on the branch and on
      `main` succeeded in ~17s; pulled commit loaded into a DB copy.
      Risks to watch: GitHub cron is best-effort (late/skipped runs); GitHub may disable
      scheduled workflows after ~60 days of repo inactivity (unverified whether the bot's own
      commits count, so rely on the staleness warning); FPL could someday block runner IPs
      (the workflow fails loudly on non-200). Local-only snapshots from early testing
      (`20261119T013940Z`, `...014014Z`) are untracked in `epl-data`.
- [ ] **F3** Consider whether agent tools/views should surface the new defensive
      stats and snapshot data; update `docs/agent_ui_architecture_plan.md`
- [ ] **F5** Optional: skip re-downloading `event/<gw>/live` for gameweeks
      that were already finished in the previous complete pull (copy the files
      instead) — cuts calls and raw-dir growth. Raised in PR #10 review; not
      done (the plan's original choice was to reload everything each run)
- [ ] **F6** Minor, from review: `upsert_df` has no schema filter or column
      quoting and fails obscurely on a table with no PK (moved unchanged from
      `load_historical.py`)
- [ ] **F7** Compute `fact_team_season` standings (`played`, `win`, `draw`, `loss`,
      `points`, `position`) from `fact_fixture` — the API's values are zero/stale and
      the loader leaves those columns alone
- [ ] **F4** Optional: save raw JSON with `indent=2` for human readability

## Open questions

- Whether `played` / `in_dreamteam` are worth storing (default: skip).
- Raw-directory retention (F1).
- Whether weekly re-runs of C are needed or the daily snapshots suffice once
  the backfill is done (decide after seeing drift).
