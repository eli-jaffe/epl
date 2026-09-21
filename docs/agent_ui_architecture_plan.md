# Agent + UI Architecture Plan (components 2 & 3)

**Status: agent backend built and verified end-to-end for real, including
its MCP surface; a minimal chat UI is built and verified end-to-end.** This
doc is both the original design plan and the running record
of what happened building it — decisions are dated, and later entries
("Corrected", "Superseded", "Implemented") amend earlier ones in place
rather than rewriting history. Assumes the reader has read the main
`README.md` (schema, data flow, design principles) and `CLAUDE.md` first.
Covers components 2 (agent) and 3 (UI) from `CLAUDE.md`'s build order;
component 1 (data pipeline) is built and out of scope here except as the
thing this layer queries.

## Implementation status (2026-09-21)

What actually exists right now, as a scannable summary — details and the
"why" behind each are in the sections below.

**Built and verified against real Postgres + real Claude API calls, not
just imports:**
- `agent/main.py` — FastAPI app; real `fastapi-users` auth (register, JWT
  login/logout, user management) against self-hosted Postgres.
- `agent/loop.py` — the full Reason→Plan→Execute→Reflect→Synthesize agent
  loop, using Claude's native tool-calling (not regex-parsed free text).
- `agent/tools/players.py` + `agent/tools/sql.py` — 7 fixed-function tools
  (`get_fixture_difficulty` removed 2026-09-20, see "Known issues" below),
  `get_schema()`, and `run_sql()` run real queries against `db/epl.duckdb`.
  `agent/tools/registry.py` has real JSON-Schema `input_schema` for every
  tool plus `dispatch()`'s uniform result envelope.
- `agent/observability.py` / `agent/db/observability_models.py` — every
  query/step/LLM call logged live to Postgres (not DuckDB directly — see
  round-2 decision 5's "Superseded" note for why).
  `agent/observability_sync.py` periodically copies that into DuckDB's
  `agent_query`/`agent_step`/`agent_llm_call` tables (including
  `tool_result`, the actual data a tool call returned, not just its args)
  for query-pattern-mining/cost analysis.
- `agent/mcp_server.py` — registers `TOOL_REGISTRY` (the raw tools) **plus
  `ask_epl_agent`** (added 2026-09-20), the agent-level MCP tool that runs
  the full loop instead of exposing bare data access.
  `agent/auth/service_user.py` backs it with a fixed, lazily-created
  service-account Postgres user, since MCP calls carry no JWT but
  `agent_query.user_id` has a real FK constraint.
- **`ui/` (new, 2026-09-20/21)** — a minimal SvelteKit + TypeScript frontend:
  `/login` (register + JWT login against `/auth/*`) and `/` (a chat page
  posting to `/chat`, rendering the answer, keeping scrollback). Vite's dev
  proxy forwards `/auth`, `/chat`, `/users` to the FastAPI backend so the
  browser never needs CORS configured — dev-only, production will need a
  real reverse proxy or CORS setup once this stops being local-only.

**Confirmed working by actually running real questions through it** — both
via direct calls (e.g. "who are some good differential midfielder picks
right now", "which team has conceded the fewest goals so far") and via
`ask_epl_agent` and the `ui/` chat page end-to-end (register → login → ask →
real answer, verified through the actual Vite proxy path a browser would
use, though the rendered page itself hasn't been visually confirmed — no
browser-automation tool is connected to this environment yet).

**Known issues (not fixed, deliberately deferred):**
- **`get_fixture_difficulty` removed from `TOOL_REGISTRY`, 2026-09-20** —
  it took one `team_code` per call, and answering "which teams have the
  easiest fixtures" burned most of the agent loop's execute-round budget on
  every outer retry re-discovering the schema/team-code mapping from scratch
  before ever reaching it; by the 3rd retry only 1 of 20 teams had been
  checked. Compounding cause: `run_agent_query`'s outer retry only carries
  forward `failed_calls` (failure descriptions), not data already
  successfully fetched, so a 2nd attempt's partial results (7 of 20 teams)
  were discarded when a 3rd attempt started over. Cross-team fixture
  questions are pushed to the `run_sql` escape hatch for now instead of
  fixing this, so as not to hold up the rest of the tool set — accepted
  tradeoff: without a tool naming `fact_fixture` explicitly, the agent
  sometimes fails to discover that table via `run_sql` and incorrectly
  claims no fixture data exists at all. Candidate v2 fixes: cache schema
  across rounds/retries, carry forward partial tool results on retry,
  and/or a multi-team `get_fixture_difficulty_all_teams` fixed tool.
- Three `agent/loop.py` bugs found and fixed while building `ask_epl_agent`
  (2026-09-20) are worth knowing about even though they're resolved:
  `_synthesize` could answer the internal reflection status instead of the
  user's question, `_execute`'s observability logging crashed on `datetime`
  values from tool/`run_sql` results, and `_reflect`'s forced tool call
  could lose its required `reasoning` field when `max_tokens` was too tight
  for a real-sized tool-result summary. See this doc's git history / commit
  log for the exact fixes if a similar symptom reappears.

**Not built yet:**
- `run_sql`'s per-session rate limiting (the one deferred piece of its
  sandbox — needs session context threaded into the tool layer, a real
  architecture decision, not bundled into `run_sql`'s implementation).
- The `clarify` tool (category E).
- Any chart/dashboard generation (category C/D) — no chart tool exists yet.
- The observability sync job's cron/launchd schedule (the script works when
  run manually; not wired to run automatically).
- Alembic migrations (`main.py` still uses `create_all()` at startup).
- Visual/interactive verification of `ui/` in a real browser (no
  browser-automation tool connected to this environment yet — see this doc's
  commit history for what that'd take to set up).
- A production-appropriate auth/proxy story for `ui/` (JWT currently in
  `localStorage`, dev-only Vite proxy in place of real CORS/reverse-proxy
  config).

## Goal

An LLM-driven agent that takes a natural-language fantasy question (transfers,
captaincy, differentials), pulls the relevant data from `db/epl.duckdb`, runs
the right analysis, visualizes it, and returns an insight — plus, on request,
spins up an on-demand interactive dashboard. Chat-first web app as the
persistent entry point, with an MCP connection so the same tools are usable
from Claude Code/Desktop.

## Decisions made (2026-09-16)

- **Multi-user from the start**, not a solo local tool — affects session
  isolation and makes SQL sandboxing load-bearing rather than optional
  hardening.
- **Local web app first**, built so it can become hosted later if it proves
  successful — avoid hardcoding local-only assumptions that would block that
  migration.
- **MCP connection alongside the web app**, specifically so the same tool
  layer works from Claude Code/Desktop, not just the web UI. Implies the
  shared-tool-layer pattern (see References below): plain Python functions,
  wrapped separately for in-process web app use and for MCP's `@tool`
  transport — fetch/query logic stays single-sourced.
- **LLM provider: Claude API.** Loop shape modeled loosely on a
  Reason→Plan→Execute→Reflect→Synthesize pattern (see References).
- **Tool-calling boundary: hybrid.** Agent can write/execute SQL directly
  against DuckDB (sandboxed, read-only connection + query safety), *plus* a
  fixed set of parameterized functions for commonly-occurring questions, for
  speed/cost/consistency. See Tool Set below.
- **Dashboards: both modes.** A static/generated report for report-style
  asks, and an interactive dashboard on request for "build me a dashboard
  about X" — interactive meaning click-to-filter/drill-down and tooltips on
  data points, not just a static chart grid (see round 2 decision 4 below for
  how this is implemented without a server-side app process).
- **Persistent UI: chat-first**, not a stats dashboard with chat bolted on.
- **Live FPL API connector is explicitly deferred** — this layer can be built
  and demoed against historical data now; live data is a parallel track, see
  `docs/live_api_connector_plan.md`.
- **Query tracking and observability are one system, not two.** Every user
  query, agent step/decision, tool call, and output is logged from the start.
  "Which questions recur enough to become fixed functions" is just an
  aggregation over that same log, not separate infrastructure.
- **Observability store: homegrown in DuckDB**, extending the existing
  `ingest_runs` lineage-log convention rather than an off-the-shelf tool
  (Langfuse, etc.). Keeps "find common queries" a native SQL query against the
  same warehouse; no new hosted dependency. Trade-off accepted: no free
  trace-timeline UI — a lightweight internal viewer would need to be built
  later if wanted.
- **Prompt/token/cost tracking is in scope for the homegrown store**: log
  every Claude API call's `usage` (input/output/cache tokens), model,
  timestamp, session/query ID, and agent-loop phase; cost computed via a
  small versioned pricing-rate lookup table; cost/usage views built the same
  way as the existing `v_player_*` consumption views.
- **Charts: Hex-style editable.** The agent returns both the rendered chart
  and its underlying definition, and the user can tweak the definition
  directly without re-prompting the agent. Chosen format: **declarative JSON
  chart spec (Vega-Lite)**, not editable Python/code — keeps edits
  client-side, browser-rendered, and safe by construction (no second
  code-execution sandbox needed on top of the SQL one).
  - **Starting implementation: the LLM emits the raw Vega-Lite spec
    directly.** Simpler to build first, maximally flexible.
  - **Possible future implementation, if raw LLM-authored specs prove too
    variable, costly, or unreliable in practice**: a `render_chart(data_ref,
    chart_type, x, y, color?, ...)` tool where the LLM emits structured
    intent instead of the spec itself, and a deterministic Python function
    builds the actual valid Vega-Lite spec from that. Trades some flexibility
    for guaranteed-valid specs and one place to enforce consistent visual
    style across every chart. Not built now — revisit only if the raw-spec
    approach shows real problems (malformed specs, inconsistent chart
    styling, high token cost from full specs in every response).

## Tool set

Confirmed 2026-09-16. All tools sit behind the shared tool layer (plain
Python functions; wrapped separately for the web app and for MCP). Every
call, across all categories, gets wrapped with observability logging (see
below) — not a tool itself, just instrumentation around every invocation.

### A. Fixed fast-path functions (deterministic, built on `v_player_*` views)

**Implemented 2026-09-18 — `agent/tools/players.py`.** Real signatures/
parameter names differ slightly from the sketch below (settled during
implementation, e.g. `get_top_performers` takes `metric, gw_start, gw_end,
position=None, limit=10`) — the code is the source of truth for exact
signatures now; this list is still accurate for *what* each tool does and
*why* it exists.

- `search_players(name_fragment)` — resolves a typed name to `player_code`;
  the entry point most other tools depend on, since users type names, not
  codes.
- `get_player_summary(player_code, season?)` — season totals, price,
  ownership, form, position.
- `get_player_gameweek_history(player_code, gw_range)` — points/minutes/xG
  trend over a range; uses `v_player_gameweek_totals` so double gameweeks sum
  correctly rather than showing as two separate rows.
- `compare_players(player_codes[], metric, gw_range?)` — side-by-side
  comparison, the core "who do I pick" primitive.
- `get_top_performers(position?, metric, gw_range, limit)` — e.g. "best
  midfielders last 5 GWs."
- `get_differentials(max_ownership_pct, min_form, position?)` — low-ownership,
  high-form filter; a named fantasy concept worth a dedicated function rather
  than having the agent reconstruct the filter logic ad hoc each time.
- `get_value_analysis(player_code_or_position)` — points per million, for
  budget decisions.

This list is a first cut based on the fantasy decisions named in
`README.md`'s opening line (transfers, captaincy, differentials), not derived
from real usage data yet — expected to evolve once the query log (below) has
enough volume to show what's actually asked. Revisit this list once
observability data exists.

**`get_fixture_difficulty(team_code, gw_range)` removed 2026-09-20 — known
issue, deferred to v2.** Tested for real via `ask_epl_agent`
(`agent/mcp_server.py`): answering a "which teams have the easiest
fixtures" style question needs this called once per club (single-team
signature), and the agent loop burned most of its 5-round execute budget on
*every* outer retry re-discovering the schema/team-code mapping from scratch
via `get_schema` + several `run_sql` calls before ever reaching it — by the
third retry only 1 of 20 teams had been checked. Compounding cause: each
outer retry in `agent/loop.py`'s `run_agent_query` only carries forward
`failed_calls` (descriptions of failures), not data already successfully
fetched, so a second attempt's `get_fixture_difficulty` results (7 of 20
teams, from batching 8 tool calls in one round) were discarded when a third
attempt started over. Rather than holding up other tool work on a fix
(schema caching across rounds/retries, and/or carrying forward partial
results, and/or a multi-team `get_fixture_difficulty_all_teams` fixed tool),
cross-team fixture-difficulty questions are pushed to the `run_sql` escape
hatch for now — the agent can already write one join/aggregate query against
`fact_fixture`/`dim_team` instead of many fixed-tool calls. Revisit as a
dedicated v2 fixed tool once/if the query log shows this is asked often
enough to justify it.

### B. SQL escape hatch

**Both implemented — `agent/tools/sql.py`.** See round-2 decision 5's final
"Implemented" entry below for `run_sql`'s exact safety layers and what's
still deferred.

- `get_schema()` — **scaled down from the original per-column-profiling
  ambition** (distinct counts, null %, sample values) to current season +
  its gameweek range + column names/types for the three consumption views —
  cheap, real, and sufficient so far; the richer profiling version is real
  follow-up work if grounding ever proves insufficient without it, not
  something dropped silently.
- `run_sql(query)` — read-only DuckDB connection, `SELECT`-only, blocklist on
  `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/TRUNCATE/ATTACH`, row cap.
  Needs the domain glossary (DGW grain, `code` vs `id`, `xP` lookahead-bias
  caveat — see `CLAUDE.md`'s data-modeling gotchas) injected into its prompt
  every time, so the agent doesn't silently reinvent an already-documented
  mistake (e.g. summing raw `fact_player_gameweek` instead of
  `v_player_gameweek_totals` and double-counting a DGW).
  **Now load-bearing given multi-user**, not optional hardening — one user's
  query must not be able to affect another's session or the underlying data.

### C. Chart tool — not built yet

LLM emits a raw Vega-Lite JSON spec directly (see Decisions above for the
future `render_chart()` alternative if this proves insufficient).

### D. Dashboard spin-up — not built yet

Not a single LLM-invoked tool — an orchestration path. An intent check
(report vs. "build me a dashboard about X") that, on the dashboard branch,
bundles multiple chart specs plus a layout into a live interactive app.
Concrete design deferred until the web app framework is chosen.

### E. Meta — not built yet

- `clarify(question)` — human-in-the-loop escape hatch; prompt should bias
  toward stating an assumption over asking, to avoid over-clarifying while
  still allowing it when genuinely needed.

## Observability schema — built 2026-09-18/2026-09-20

Superseded from the original single-DuckDB-store sketch by the Postgres-
plus-sync design in round-2 decision 5's "Superseded" entry below — that's
the real architecture now. Actual schema:

- **Postgres** (`agent/db/observability_models.py` — `AgentQuery`,
  `AgentStep`, `AgentLlmCall`), the live write target, each with a nullable
  `synced_at` watermark. `agent_query`: query text, `user_id`, status,
  timestamps, final answer. `agent_step`: phase, step index, and — for
  Execute-phase tool calls — tool name/args/**result** (added 2026-09-20,
  the actual data a tool call returned, truncated to 2000 chars, not just
  whether it succeeded)/success/error. `agent_llm_call`: model, raw
  input/output/cache token counts (cost is *not* computed here — see below).
- **DuckDB** (`db/schema.sql` — same three table names, plus
  `agent_model_pricing`, a small table of Claude rates versioned by
  `effective_date`), the synced analytical copy, up to ~15 min stale.
  `agent/observability_sync.py` copies unsynced Postgres rows in here,
  computing `agent_llm_call.cost_usd` at sync time (joining against
  `agent_model_pricing` — deliberately not computed on the live request
  path, since the pricing table only exists in DuckDB and there's no reason
  to touch DuckDB from a live request anymore). One consumption view built
  so far: `v_agent_query_cost` (query + step count + total cost, same
  pattern as `v_player_season`) — richer analysis (recurring-question
  mining, cost-by-user) is real follow-up work once there's usage volume.

## Decisions made (2026-09-16, round 2 — resolving the open questions above)

1. **Web app framework: FastAPI backend + SvelteKit frontend.** Streamlit/Dash
   ruled out (rerun-on-interaction model fights a persistent chat UI and
   client-side spec editing). React was the other candidate; no strong
   technical reason to prefer it over SvelteKit at this app's size —
   SvelteKit has less boilerplate, smaller bundles, and a component model
   that maps cleanly onto "one component per chart/dashboard widget."
   `vega-embed` is framework-agnostic either way. The backend (tool layer,
   agent loop, MCP server) stays pure Python regardless of this choice — the
   split is clean since the tool layer was already designed to be
   transport-agnostic.
2. **Multi-user auth: self-hosted, no third-party identity service.**
   Considered and rejected hosted providers (Clerk, Supabase Auth) — even
   though both have free tiers large enough to never matter at this project's
   scale, the requirement is to avoid depending on an outside service at all.
   **Chosen: `fastapi-users`** (a self-hosted FastAPI library handling
   registration/login/password hashing/session-JWT within our own app — we
   don't hand-roll password/session security ourselves, but nothing leaves
   our infrastructure) **backed by a self-hosted Postgres instance** (run
   locally, e.g. via Docker — still "no outside service," just a database
   engine we run ourselves). Postgres is `fastapi-users`'s best-documented
   pairing (async SQLAlchemy + `asyncpg`).
   - **Scope of Postgres widened beyond auth**: since a real Postgres
     instance is being stood up anyway, it's the natural home for *all*
     small, frequently-written, user-specific app state — not just
     users/sessions, but also saved dashboards and user-edited chart specs
     (the Hex-style edits). Gives a clean three-way split: `db/epl.duckdb`
     (analytical warehouse + homegrown observability log, both OLAP-shaped
     and append-only/rebuilt) vs. Postgres (OLTP app state: users, sessions,
     saved dashboards/charts) vs. the SvelteKit frontend.
   - Rejected alternatives, for reference: fully hand-rolled auth (more
     security surface to own with no real benefit here); a full self-hosted
     identity server like Keycloak/Authentik/Ory Kratos (a whole extra
     service to run — overkill at this scale); a no-auth named-user picker
     (defers rather than avoids the work, and multi-user + eventual hosting
     were both explicit goals).
3. **MCP server shape: one server, exposing everything — including
   `run_sql`** — to start. Confirmed reversible later at near-zero cost:
   because of the shared-tool-layer design, "exposed via MCP" is just which
   functions are registered on the MCP server object; removing `run_sql` from
   that registration list has no effect on the web app's own use of the same
   underlying function.
   - **Refined 2026-09-17 — tool layer vs. agent layer are different things
     to share, and only the tool layer was shared by the above.** Registering
     `TOOL_REGISTRY` on the MCP server gives external MCP clients (e.g.
     Claude Code) raw data access, where *they* do their own reasoning over
     the tools — it does not route through this app's own agent loop, so MCP
     clients get none of the domain-glossary grounding, Reason→Plan→Execute→
     Reflect structure, observability logging, or hybrid fixed-function/SQL
     decision-making the loop will apply. For the SvelteKit UI and any future
     Slack bot, this gap doesn't exist — both talk to the agent over HTTP
     (`/chat`), the same "one API, multiple consumers" shape the user asked
     about, so a Slack bot is just another HTTP client of that endpoint, no
     new architecture needed.
   - **Decision: expose both, on MCP specifically.** Add an agent-level MCP
     tool (e.g. `ask_epl_agent(query: str) -> str`) that internally calls the
     same `/chat` endpoint the UI uses, as the default/primary way MCP
     clients interact with this app — giving every surface identical agent
     behavior, safety, and logging. Keep the raw `TOOL_REGISTRY` tools
     exposed alongside it as a power-user escape hatch (e.g. for composing a
     novel multi-step query the fixed loop doesn't support yet). Mirrors the
     fixed-function-plus-SQL-escape-hatch tradeoff already made for the tool
     layer itself — same shape, one level up. Structurally cheap: no change
     to `TOOL_REGISTRY` or the tool layer itself, since the agent loop calls
     it internally either way; `agent/mcp_server.py` just registers one more
     tool wired to `/chat` in addition to what it already registers.
4. **Dashboards need real interactivity**: click-to-filter/drill-down, and
   tooltips on data points — not just a static grid of charts. Covered by
   Vega-Lite's own grammar (`params`/`selection` for click/interval
   filtering, conditional encoding driven by a selection, and `tooltip` as a
   normal encoding channel), so this does **not** change the "no server-side
   app process, just a saved `{specs, layout}` bundle" architecture from the
   Chart tool decision — it's fuller use of that same spec format. Two
   follow-on implications:
   - The chart-generation prompt (and the possible future `render_chart()`
     builder) needs to cover the selection/param/tooltip vocabulary, not just
     basic mark/encoding.
   - External filter controls that aren't themselves Vega-Lite marks (e.g. a
     dropdown) need a thin SvelteKit wrapper around `vega-embed`'s `view` API
     to push values into a chart's named signals — the one piece of
     interactivity that isn't "for free" from the spec alone.
5. **SQL sandbox: locked in as originally proposed** — read-only connection
   opened per `run_sql` call, `SELECT`-only blocklist, row cap, a wall-clock
   timeout (via a cancel-on-timeout wrapper, since DuckDB has no native query
   timeout), and a per-session rate limit specifically on `run_sql` as the
   most expensive/abuse-prone tool.
   - **Corrected 2026-09-17, empirically.** While building the agent loop
     skeleton, opening a second `duckdb.connect(..., read_only=True)` in the
     same process while a read-write connection (needed for observability
     logging) was already open failed outright: *"Connection Error: Can't
     open a connection to same database file with a different configuration
     than existing connections."* DuckDB allows only one configuration per
     database file per process — a driver-level read-only connection for
     `run_sql` isn't achievable in this single-process architecture once
     anything else in the same process needs write access to the same file.
     Verified instead: one shared process-wide connection, with a fresh
     `.cursor()` per call (`agent/db/duckdb.py`), is safe for concurrent
     reads and writes from multiple threads with no extra locking. **This
     means the "read-only connection" layer of the SQL sandbox's
     defense-in-depth is not available as designed** — `run_sql` safety, when
     built, will rest on the app-level `SELECT`-only blocklist alone (plus
     the row cap/timeout/rate-limit), not blocklist-plus-driver-enforcement.
     Restoring a true driver-level read-only guarantee would require running
     the writer (observability) in a separate process from anything issuing
     `run_sql`, which is real added complexity — not done, and not currently
     planned; revisit if the blocklist-only guarantee proves insufficient in
     practice.
   - **Superseded 2026-09-18 — the "separate process" idea above got built.**
     Observability writes now go to Postgres instead of DuckDB
     (`agent/observability.py`, `agent/db/observability_models.py`), which was
     already open read-write for auth/app-state — no new connection on the
     live app's hot path. A periodic batch job
     (`agent/observability_sync.py`, intended to run every ~15 min via
     cron/launchd, not installed by this repo) copies unsynced rows into
     DuckDB's `agent_query`/`agent_step`/`agent_llm_call` tables through its
     own short-lived, retried connection. The live app's DuckDB connections
     are back to strictly read-only, short-lived, one per call
     (`agent/db/duckdb.py`) — **the driver-level read-only layer for
     `run_sql` is viable again.**
     - Second empirical correction along the way: DuckDB blocks a writer
       against *any* other open connection, even a read-only one, not just
       a differently-configured one (`Could not set lock on file... you
       would be able to open this database in read-only mode`) — meaning
       many readers can coexist, but a writer needs the file to itself.
       This is why the live app's DuckDB connections had to go back to
       short-lived (open, query, close) rather than one persistent shared
       connection: a persistent reader, even read-only, would starve the
       sync job forever.
     - New trade-off this introduces: DuckDB's observability tables are now
       a periodically-synced copy, not the live source of truth — up to
       ~15 minutes stale. Postgres is authoritative for "did this query
       finish yet, what did it cost so far." DuckDB stays the right place
       for query-pattern-mining/cost analysis (a native SQL query against
       the same warehouse as the fantasy data), just not for real-time
       state. `agent_llm_call.cost_usd` is computed at sync time (against
       DuckDB's `agent_model_pricing`), not on the live request path.
     - Considered and rejected: MotherDuck (DuckDB's hosted service — would
       give true concurrent multi-process read/write with no sync job, but
       is a third-party hosted service, which was already ruled out for
       auth for the same reason) and a single in-process DuckDB owner
       (removes the sync job, but its one connection would still need
       `read_only=False` to write, reopening this exact regression rather
       than fixing it).
   - **Implemented 2026-09-20 — `agent/tools/sql.py`.** 3 of the 4 layers
     above are real: single-`SELECT`-statement + keyword blocklist
     (word-boundary regex; also covers DuckDB-specific escape hatches —
     `ATTACH`/`DETACH`/`INSTALL`/`LOAD`/`PRAGMA`/`COPY`/`EXPORT`/`IMPORT` —
     beyond standard DML/DDL), a hard row cap (2000, regardless of the
     requested `row_limit`), and a wall-clock timeout (10s) via a watchdog
     thread calling `conn.interrupt()` — verified this genuinely cancels an
     in-progress query (a real 3-way self-cross-join on the 254K-row
     `fact_player_gameweek` table got interrupted at ~10.0s with
     `InterruptException`, not left to finish). Per-session rate limiting is
     the one deferred layer, for the reason already given above: it needs
     `user_id`/session context threaded into the tool-calling layer, which
     `dispatch()` doesn't currently pass to any tool. Real end-to-end proof
     it works: asked a question none of the fixed tools cover ("which team
     has conceded the fewest goals so far"), and the agent correctly reached
     for `run_sql` on its own, wrote a genuinely sensible query (grouping
     goalkeeper `goals_conceded` by team, filtered to `minutes > 0` to avoid
     double-counting substitutes), and gave an honest, correctly-caveated
     answer from it.

## References

Architecture patterns drawn on for these decisions, reviewed 2026-09-16:

- `references/workforce-data-explorer-architecture.md` — shared tool layer
  (one set of functions, wrapped for both in-app chat and MCP), token-economy
  patterns (summarized tool output, capped history), thread-local per-request
  state.
- `references/agentic_analyst_review_usmnt.md` — Reason→Plan→Execute→
  Reflect→Synthesize loop, tool envelope pattern, layered SQL safety
  (app-level blocklist + driver-level read-only connection), domain glossary
  injected into every prompt, `clarify` escape hatch.
