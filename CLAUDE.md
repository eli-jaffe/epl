# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

An agentic, LLM-driven tool for exploring English Premier League and Fantasy
Premier League data to support fantasy decisions (transfers, captaincy,
differentials). Three components, in build order:

1. **Data** (built) — historical player/team/fixture data in a DuckDB
   warehouse, refreshed periodically. Live in-season data is planned but not
   built.
2. **Agent** (not built) — takes a user query, pulls the relevant data, runs
   the appropriate analysis, visualizes it, and returns an insight. Will also
   be able to spin up an on-demand dashboard UI for the user to explore the
   underlying data further. The agent should work from a fixed framework for
   *how* to pull data and *how* to visualize it, plus a scaffold for what the
   on-demand dashboard looks like — it determines the data/analysis specifics
   per query but isn't reinventing that output architecture from scratch each
   time.
3. **UI** (not built) — the on-demand dashboard from component 2 comes first;
   a persistent entry-point web UI is a later, secondary goal.

All three are expected to live in this repo, in directories added as each
component is built (e.g. `ingestion/` today, `agent/` and `ui/` later) — this
is not a split-repo setup like `epl-data` (below).

Primary language is Python. Tooling for the agent/UI layers (frameworks,
frontend stack, etc.) is not yet decided — don't assume a choice has been
made.

## Repo split: code vs. data

This repo (`eli-jaffe/epl`) holds ingestion code, the DuckDB schema, and
docs — **no data**. Raw CSV snapshots live in a sibling repo,
`eli-jaffe/epl-data` (`~/epl-data`, outside this repo's tree entirely), which
doubles as the ingestion landing zone and a backup of the upstream source.
The local `db/epl.duckdb` file itself is gitignored and untracked anywhere —
it's rebuilt from `epl-data` plus the scripts here, so treat it as
disposable/regeneratable, never as a thing to hand-edit or back up.

Full data landscape, schema diagrams, and pipeline flow: `README.md`. Live
API connector design (planned, not built): `docs/live_api_connector_plan.md`.
Architecture research for the agent/UI layers: `references/`.

## Data-modeling gotchas (read before touching ingestion or schema code)

These are non-obvious and have already caused bugs once:

- **FPL's `id` is reassigned every season; `code` is the stable identity** for
  players and teams (e.g. Salah's `id` changed 283→308→328→381 across four
  seasons while `code` stayed 118748). Always key dimension tables on `code`.
  Season-scoped `id` is still stored on the fact tables because the raw
  per-season CSVs reference `id`, not `code` — see the header comment in
  `db/schema.sql`.
- **A club's display name can change between stints in the league** even
  though its `code` is stable (e.g. Ipswich → Ipswich Town). Prefer a
  season's own name mapping over a global one; fall back to a normalized-name
  match only when crosswalking across sources.
- **Real double gameweeks exist** (confirmed 2025-26 GW33) —
  `fact_player_gameweek` is grained player-per-*fixture*, not per-gameweek.
  Don't "simplify" this back to one row per player per gameweek.
- **Loads are idempotent upserts** (`INSERT ... ON CONFLICT DO UPDATE`), never
  delete-then-reinsert — DuckDB's FK checking has real limitations with
  same-transaction parent deletes even after children are cleared first.
- **Vectorize pandas transforms.** `.iterrows()` over the ~250K-row gameweek
  table caused a 5+ minute hang; the vectorized version runs in single-digit
  seconds. Don't reintroduce row-by-row loops on fact-table-sized data.
- Some (season, team) pairs have no resolvable `code` (clubs relegated before
  the earliest `teams.csv` on file) and load with a `NULL` team reference by
  design — not a bug to "fix" by dropping rows.

## Conventions

- Keep it simple: prefer simple solutions that work instead of overly engineered
  designs.
- No test suite or linter is configured yet — don't assume `pytest`/`ruff`
  exist; check before referencing them, and ask before adding new tooling
  choices for the agent/UI layers rather than assuming a stack.
- `venv/` and `db/*.duckdb` are gitignored; don't commit either.
- Match the existing style in `db/schema.sql` and `ingestion/*.py`: header
  comments explain *why* a grain/key choice was made, not what the SQL/code
  does line by line.

## Working with the user

- Only complete the task at hand. If additional tasks or work would help augment the
  solution to the current task, suggest it after completing the primary. Do not go too far down the possible follow up paths without checking in with the user.
- The user needs to conceptually understand the work at hand - the architecture, the
  the design, the code; this might include detailing how the given piece fits into the rest and how the component functions at a basic level.  Prioritize bringing the user along with. the journey instead of doing it all for them. If you go down complimentary paths, summarize them along with the 'why' after completing them.
- Only commit when explicitly asked. Working code can be committed freely
  once asked; plans/designs for not-yet-built work (like
  `docs/live_api_connector_plan.md`) stay local/uncommitted until the user
  says otherwise.
