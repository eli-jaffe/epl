# Agent + UI Architecture Plan (components 2 & 3)

**Status: not built.** This is a design plan for a future session to pick up
and implement — written so it's self-contained even without this
conversation's history. Assumes the reader has read the main `README.md`
(schema, data flow, design principles) and `CLAUDE.md` first. Covers
components 2 (agent) and 3 (UI) from `CLAUDE.md`'s build order; component 1
(data pipeline) is built and out of scope here except as the thing this layer
queries.

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
- `get_fixture_difficulty(team_code, gw_range)` — upcoming fixture run, for
  transfer/captaincy timing.
- `get_value_analysis(player_code_or_position)` — points per million, for
  budget decisions.

This list is a first cut based on the fantasy decisions named in
`README.md`'s opening line (transfers, captaincy, differentials), not derived
from real usage data yet — expected to evolve once the query log (below) has
enough volume to show what's actually asked. Revisit this list once
observability data exists.

### B. SQL escape hatch

- `get_schema()` — dynamic schema introspection + per-column profiling
  (distinct counts, null %, sample values), injected as grounding context.
- `run_sql(query)` — read-only DuckDB connection, `SELECT`-only, blocklist on
  `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/TRUNCATE/ATTACH`, row cap.
  Needs the domain glossary (DGW grain, `code` vs `id`, `xP` lookahead-bias
  caveat — see `CLAUDE.md`'s data-modeling gotchas) injected into its prompt
  every time, so the agent doesn't silently reinvent an already-documented
  mistake (e.g. summing raw `fact_player_gameweek` instead of
  `v_player_gameweek_totals` and double-counting a DGW).
  **Now load-bearing given multi-user**, not optional hardening — one user's
  query must not be able to affect another's session or the underlying data.

### C. Chart tool

LLM emits a raw Vega-Lite JSON spec directly (see Decisions above for the
future `render_chart()` alternative if this proves insufficient).

### D. Dashboard spin-up

Not a single LLM-invoked tool — an orchestration path. An intent check
(report vs. "build me a dashboard about X") that, on the dashboard branch,
bundles multiple chart specs plus a layout into a live interactive app.
Concrete design deferred until the web app framework is chosen.

### E. Meta

- `clarify(question)` — human-in-the-loop escape hatch; prompt should bias
  toward stating an assumption over asking, to avoid over-clarifying while
  still allowing it when genuinely needed.

## Observability schema (sketch, not finalized)

Homegrown, in `db/epl.duckdb`, following the existing `ingest_runs`
lineage-log convention (append-only, one row per event — not upserted like
the dimension/fact tables).

- `agent_query` — one row per user-submitted question: query text, user/session
  id, timestamp, final status.
- `agent_step` — one row per agent-loop step (reason/plan/execute/reflect/
  synthesize): parent query id, phase, tool(s) called, decision/output
  summary, timestamp.
- `agent_llm_call` — one row per Claude API call: parent step id, model,
  input/output/cache token counts (from response `usage`), computed cost,
  timestamp.
- A small versioned pricing-rate lookup table to compute cost from token
  counts as rates change over time.
- Consumption views on top (`v_agent_query_frequency`,
  `v_agent_cost_by_session`, etc.) — same pattern as `v_player_season`.

Exact column lists TBD when this is actually built.

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
