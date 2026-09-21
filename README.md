# EPL Fantasy Data Pipeline

Pulls, conforms, and stores Fantasy Premier League data to support decision-making
(transfers, captaincy, differentials) for a fantasy manager. This repo covers the
data layer; an LLM-driven agent for exploring the data, and a frontend for it, are
planned next.

## Repositories

This project is deliberately split across two repos:

| Repo | Contents |
|---|---|
| [`eli-jaffe/epl`](https://github.com/eli-jaffe/epl) (this repo) | Ingestion scripts, DuckDB schema, docs. No data. |
| [`eli-jaffe/epl-data`](https://github.com/eli-jaffe/epl-data) | Raw CSV snapshots pulled from the historical source. No code. |

`epl-data` lives outside this repo's directory tree entirely (`~/epl-data`, a
sibling of `~/epl`), not just gitignored inside it. It serves two purposes at
once: it's the landing zone the load script reads from, and — since it's the
most recent successful pull, version-controlled — it's a backup in case the
upstream GitHub repo ever disappears or restructures. The local DuckDB file
(`db/epl.duckdb`) itself is *not* tracked anywhere — it's rebuilt from this raw
data plus the scripts in this repo, so there's nothing precious to back up there.

## Data landscape

**Two sources, both public and unauthenticated** (no token, login, or API key
for either):

- **Historical seasons** — [`vaastav/Fantasy-Premier-League`](https://github.com/vaastav/Fantasy-Premier-League),
  a community-maintained mirror of the official FPL API. Covers 2016-17 through
  the current season. **Note:** as of the 2024-25 season the maintainer stopped
  weekly updates — this repo now only refreshes 3x/season (preseason, end of
  January transfer window, season end). Fine for historical backfill, not
  sufficient alone for in-season freshness.
- **Live current season** — `fantasy.premierleague.com/api/*` directly (the same
  API the repo above scrapes). Not yet integrated — this is the next milestone,
  needed to get gameweek-fresh data during the season.

**Currently loaded** (11 seasons, one full historical backfill run):

| Table | Rows | Grain |
|---|--:|---|
| `dim_season` | 11 | one row per season |
| `dim_team` | 30 | one row per club ever seen (any season) |
| `dim_player` | 2,743 | one row per player ever seen (any season) |
| `fact_team_season` | 160 | team x season |
| `fact_player_season` | 7,974 | player x season |
| `fact_fixture` | 3,346 | match x season |
| `fact_player_gameweek` | 254,500 | player x fixture (see grain note below) |

**Known, bounded data gaps** — real limitations in the source data, not bugs:

- **8 (season, team) references can't resolve a stable `code`**: Middlesbrough
  (2016-17), Stoke (2016-17, 2017-18), Swansea (2016-17, 2017-18), Huddersfield
  (2017-18, 2018-19), Cardiff (2018-19). These clubs were relegated before the
  earliest `teams.csv` we have (2019-20) and never returned to the league in any
  season fetched since, so no source file ever recorded their `code`. Affected
  gameweek/fixture rows load with a `NULL` team reference rather than being
  dropped.
- **Schema drift across seasons**: `gws/merged_gw.csv` for 2016-17 through
  2019-20 lacks fields the modern schema has (`team` name, `position`, `starts`,
  expected-goals stats); 2025-26+ adds newer defensive-contribution stats
  (`tackles`, `recoveries`, etc.) that don't exist in earlier seasons. Missing
  fields load as `NULL` rather than blocking the row.
- **`xP` (expected points) has a documented lookahead-bias caveat** in the
  source's own README — it's scraped post-gameweek and may not reflect what was
  actually knowable pre-deadline. Not used for anything in this schema yet; flag
  this if it's ever used as a model feature.
- **No Understat xG source** — documented in the source's data dictionary but
  not actually present in any season we fetched. Not in this schema; could be
  added as a separate source later.

## Data flow

```
 vaastav/Fantasy-Premier-League (GitHub)      fantasy.premierleague.com/api/*
              |                                    (live API -- not yet wired up)
              |  unauthenticated HTTPS, no credentials of any kind
              v
 +--------------------------------------------------------------+
 |  epl/ingestion/github_historical.py            (EXTRACT)      |
 |    - discovers season directories via api.github.com          |
 |    - downloads the known CSVs per season via                  |
 |      raw.githubusercontent.com                                |
 |    - records the source commit SHA + fetch time                |
 +--------------------------------------------------------------+
              |
              v
 ~/epl-data/raw/github/<season>/*.csv + manifest.json
   (landing zone AND backup snapshot -- lives outside this repo,
    tracked in its own repo: eli-jaffe/epl-data)
              |
              |  epl/ingestion/load_historical.py        (LOAD)
              |    - resolves each season's (id -> stable `code`) crosswalk
              |      for players and teams
              |    - vectorized pandas transform (no row-by-row loops)
              |    - upsert: INSERT ... ON CONFLICT DO UPDATE -- idempotent,
              |      nothing is ever deleted, safe to re-run anytime
              v
 ~/epl/db/epl.duckdb                    (tracked in eli-jaffe/epl: schema
                                          only, the .duckdb file is gitignored)
   dim_season, dim_team, dim_player
   fact_team_season, fact_player_season, fact_fixture, fact_player_gameweek
   ingest_runs   (append-only lineage log: source, source_ref, status per run)
              |
              v
 v_player_gameweek, v_player_gameweek_totals, v_player_season
   (denormalized consumption views -- what the LLM agent and frontend
    will query, so they don't have to hand-roll the dimension joins)
```

Every row in every table also carries `source` (`'github_repo'` | `'live_api'`),
`source_ref` (the commit SHA, or an API fetch timestamp), and `loaded_at` — so
once the live API path exists, historical and current-season rows sit side by
side in the same tables, each traceable back to exactly where and when it came
from.

## Database schema

**Design principles:**
- **Star schema**: 3 dimensions (`dim_season`, `dim_team`, `dim_player`) + 4 fact
  tables, keyed on FPL's stable `code` for players/teams — *not* the `id` field,
  which is reassigned every season (e.g. Salah's `id` changed 283 → 308 → 328 →
  381 across four seasons while his `code` stayed `118748`).
- **Grain of the core fact table is player-per-*fixture*, not strictly
  player-per-gameweek**: FPL double gameweeks mean a player can have two rows
  for the same `(season_id, gameweek, player_code)` — one per fixture. Use
  `v_player_gameweek_totals` (summed across fixtures) for "how many points did
  X get in gameweek N" style questions.
- **Upsert, never delete**: every load is `INSERT ... ON CONFLICT DO UPDATE`,
  keyed on each table's primary key. Re-running the loader after a fresh pull
  converges the warehouse to match the raw data — nothing is ever wiped first.
- **Lineage on every row**: `source` / `source_ref` / `loaded_at` columns plus
  the `ingest_runs` log.

### Table relationships

```
dim_season (season_id)  dim_team (team_code)  dim_player (player_code)
   |                        |                       |
   +------------------------+-----------------------+
                             |
    referenced by the fact tables below, via the FK columns noted on each:

    fact_team_season      (season_id, team_code)             [PK: both]

    fact_player_season    (season_id, player_code)           [PK]
                              -- FK: team_code -> dim_team

    fact_fixture           (season_id, fixture_id)            [PK]
                              -- FK: team_h_code, team_a_code -> dim_team

    fact_player_gameweek  (season_id, gameweek, player_code, [PK]
                            fixture_id)
                              -- FK: team_code, opponent_team_code -> dim_team
                              -- FK: fixture_id -> fact_fixture (same season_id)
```

### Table detail

```
+-------------------------------------------------------+
|                       dim_season                      |
+-------------------------------------------------------+
| PK season_id  VARCHAR     e.g. '2026-27'              |
|    start_year INTEGER                                 |
|    is_current BOOLEAN     true for exactly one row    |
|    source     VARCHAR     'github_repo' | 'live_api'  |
|    source_ref VARCHAR     commit SHA / API fetch ts   |
|    loaded_at  TIMESTAMP                                |
+-------------------------------------------------------+

+----------------------------------------------------+
|                      dim_team                       |
+----------------------------------------------------+
| PK team_code  INTEGER     stable FPL code, NOT id   |
|    name       VARCHAR                               |
|    short_name VARCHAR                               |
|    source     VARCHAR                               |
|    source_ref VARCHAR                               |
|    loaded_at  TIMESTAMP                             |
+----------------------------------------------------+

+-----------------------------------------------------+
|                      dim_player                      |
+-----------------------------------------------------+
| PK player_code INTEGER     stable FPL code, NOT id   |
|    first_name  VARCHAR                               |
|    second_name VARCHAR                               |
|    web_name    VARCHAR                               |
|    birth_date  DATE                                  |
|    region      INTEGER                               |
|    source      VARCHAR                               |
|    source_ref  VARCHAR                               |
|    loaded_at   TIMESTAMP                             |
+-----------------------------------------------------+

+--------------------------------------------------------------------------------+
|                                fact_team_season                                |
+--------------------------------------------------------------------------------+
| PK,FK season_id                     VARCHAR   -> dim_season                    |
| PK,FK team_code                     INTEGER   -> dim_team                      |
|       team_id_that_season           INTEGER   season-scoped crosswalk id       |
|       strength*                     INTEGER   6 home/away attack/defence cols  |
|       played/win/draw/loss          INTEGER                                    |
|       points/position               INTEGER   final league standing            |
|       source, source_ref, loaded_at                                            |
+--------------------------------------------------------------------------------+
Grain: one row per team per season (periodic snapshot fact)

+---------------------------------------------------------------------------+
|                             fact_player_season                            |
+---------------------------------------------------------------------------+
| PK,FK season_id                     VARCHAR   -> dim_season               |
| PK,FK player_code                   INTEGER   -> dim_player               |
|       player_id_that_season         INTEGER   season-scoped crosswalk id  |
| FK    team_code                     INTEGER   -> dim_team                 |
|       position                      VARCHAR   GK | DEF | MID | FWD        |
|       start_cost/end_cost           INTEGER   GBP 0.1m units              |
|       total_points/minutes          INTEGER                               |
|       selected_by_percent           DOUBLE                                |
|       now_cost_rank                 INTEGER                               |
|       points_per_game/form          DOUBLE                                |
|       source, source_ref, loaded_at                                       |
+---------------------------------------------------------------------------+
Grain: one row per player per season (periodic snapshot fact)

+----------------------------------------------------------------------+
|                             fact_fixture                              |
+----------------------------------------------------------------------+
| PK,FK season_id                     VARCHAR     -> dim_season        |
| PK    fixture_id                    INTEGER                          |
|       gameweek                      INTEGER     null if unscheduled  |
| FK    team_h_code                   INTEGER     -> dim_team          |
| FK    team_a_code                   INTEGER     -> dim_team          |
|       team_h_score/team_a_score     INTEGER                          |
|       kickoff_time                  TIMESTAMP                        |
|       team_h/a_difficulty           INTEGER     1-5                  |
|       finished                      BOOLEAN                          |
|       source, source_ref, loaded_at                                  |
+----------------------------------------------------------------------+
Grain: one row per scheduled match per season

+-------------------------------------------------------------------------------+
|                              fact_player_gameweek                              |
+-------------------------------------------------------------------------------+
| PK,FK season_id                      VARCHAR   -> dim_season                  |
| PK    gameweek                       INTEGER                                  |
| PK,FK player_code                    INTEGER   -> dim_player                  |
| PK,FK fixture_id                     INTEGER   -> fact_fixture (same season)  |
| FK    team_code                      INTEGER   -> dim_team                    |
| FK    opponent_team_code             INTEGER   -> dim_team                    |
|       was_home/starts/minutes                                                 |
|       total_points/goals/assists/...           ~20 performance stat cols      |
|       expected_goals/assists/...     DOUBLE    xG/xA/xGI/xGC                  |
|       value/selected/transfers_*               price + ownership that GW      |
|       source, source_ref, loaded_at                                           |
+-------------------------------------------------------------------------------+
Grain: one row per player per FIXTURE (not strictly per
gameweek -- a double gameweek gives a player two rows
for the same season_id+gameweek, one per fixture_id)

+-------------------------------------------------------------------+
|                            ingest_runs                             |
+-------------------------------------------------------------------+
| PK run_id                 UUID        default uuid()              |
|    source                 VARCHAR     'github_repo' | 'live_api'  |
|    source_ref             VARCHAR                                 |
|    started_at/finished_at TIMESTAMP                                |
|    status                 VARCHAR     running|success|failed      |
|    rows_affected          BIGINT                                   |
|    notes                  VARCHAR                                  |
+-------------------------------------------------------------------+
Append-only lineage log, one row per pipeline execution.
Not upserted like the other tables -- every run adds a new row.
```

### Consumption views

Built on top of the base tables above, purely for querying convenience (no
data duplication — recomputed on read):

- **`v_player_gameweek`** — `fact_player_gameweek` joined with player/team/
  opponent names. Fixture-level grain (double gameweeks show as two rows).
- **`v_player_gameweek_totals`** — the same, but summed per `(season, gameweek,
  player)` — use this for weekly point totals.
- **`v_player_season`** — `fact_player_season` joined with player/team names,
  with prices converted to £m.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install duckdb pandas requests

# Pull raw historical data into ~/epl-data (separate repo, see above)
python3 ingestion/github_historical.py

# Apply schema (first time only) and load
python3 -c "import duckdb; duckdb.connect('db/epl.duckdb').execute(open('db/schema.sql').read())"
python3 ingestion/load_historical.py
```

## Roadmap

- [ ] Live FPL API connector for current-season/gameweek refreshes (`source = 'live_api'`)
- [~] LLM-driven agent for exploring the data / decision support (`agent/`) —
  auth, tool layer, agent loop, observability, and an MCP server (including
  `ask_epl_agent`, the agent-level tool) are built and verified against live
  data; see `docs/agent_ui_architecture_plan.md` for status/known issues.
- [~] Frontend for delivering agent output (`ui/`, SvelteKit + TypeScript) —
  register/login + a basic chat page against `/chat` are built and verified
  end-to-end; still needs richer UI, the on-demand dashboard, and a
  production auth/proxy story (currently dev-only via Vite's proxy).
