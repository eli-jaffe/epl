# Architecture reference: `thelancehaun/workforce-data-explorer`

Reviewed 2026-09-16 for patterns applicable to an EPL agentic assistant — an
LLM agent, MCP connector, and Streamlit-style UI wrapping our own data. Source:
[github.com/thelancehaun/workforce-data-explorer](https://github.com/thelancehaun/workforce-data-explorer).

A ~5,900-line Python project (Streamlit UI + Groq-powered chat + MCP server)
that wraps 9 government/private labor-market APIs behind three interfaces: a
live dashboard, an AI chat assistant, and a remote MCP connector.

---

## 1. The shared tool layer (the actual design insight) ⭐

**This is the highest-leverage pattern for EPL to copy.**

The core trick: **one tool layer, three consumers.** `workforce_data/chat.py`
defines a set of plain Python functions (`get_fred_data`, `get_bls_data`,
`search_onet_occupations`, etc.) that each:
- fetch from a source module (`sources/fred.py`, `sources/bls.py`, …),
- summarize the resulting DataFrame into compact JSON (stats + last ~12 rows,
  not the full series),
- and stash the full DataFrame in a **thread-local store** for chart
  rendering.

Both the in-app Groq chat engine and the standalone MCP server
(`mcp_server.py`) import and call these *same* functions. The MCP server is
essentially a thin adapter layer — decorator (`@mcp.tool()`), TTL caching,
docstring-as-tool-description — around the chat module's implementation
functions. Direct quote from the code: "The chat module's tool functions
already return compact, LLM-ready JSON... so the MCP tools reuse them
directly."

**For EPL:** don't write tool logic twice for "in-app LLM" vs "external MCP
client." Define tools once as plain functions returning LLM-shaped JSON (e.g.
`get_player_xg(player_id, gw_range)`, `get_fixture_difficulty(team, gws)`),
then wrap that same function for whichever transport we need:
- OpenAI/Anthropic/Groq tool-calling schema for the in-app chat engine
- MCP `@tool` decorator for an external connector (Claude Desktop, claude.ai)

The wrapper is where the two implementations diverge (MCP can add TTL
caching and a `full_data` CSV/JSON attachment; the in-app chat engine can
keep DataFrames in memory for direct chart rendering) — the
fetch/summarize/query logic against our DuckDB tables stays single-sourced.

---

## 2. The LLM agent (`chat.py`)

- **Model/provider:** `openai/gpt-oss-120b` via Groq's free tier — chosen for
  cost, not top-tier quality. Tool-calling in OpenAI function-call format.
- **Agentic loop:** a bounded `for _round in range(8)` loop — call model → if
  `tool_calls`, execute each, append `role: tool` results, loop again → if no
  tool_calls, break. Standard ReAct-style loop, nothing fancy.
- **Loop-cap escape hatch:** if 8 rounds pass and the model *still* wants to
  call tools, it injects a synthetic user message — "Stop calling tools.
  Answer now in plain text" — rather than truncating or erroring. Tool defs
  stay in the request (some providers error on a tool_call with
  `tool_choice="none"`), but the model is nudged to stop. **Good defensive
  pattern for capping runaway agentic loops without breaking the API
  contract.**
- **Retry wrapper:** catches specific known-transient failure modes
  (malformed tool-call JSON, 429 rate limits) and retries up to 4x with
  backoff; everything else re-raises immediately. Retry only known-transient
  error strings, not blanket `except: retry`.
- **Token economy is the design center, not an afterthought:**
  - System prompt is short and explicit about tool-calling policy, including
    a hardcoded fallback chain baked directly into the prompt text (e.g.
    "try X first, then search, only then say it's unavailable").
  - Conversation history capped to last 8 turns.
  - Tool results are *summaries* (stats + sample rows), never full series —
    full data lives in a separate thread-local store the UI pulls from, so
    the LLM's context window never sees the bulk of the data it just
    fetched.
- **Thread-local DataFrame store:** since one process serves many concurrent
  UI sessions and MCP requests, a plain module-level dict would leak data
  across users. `threading.local()` isolates per-request state cheaply
  (works because tool calls run synchronously on the caller's thread).

**Pros:** cheap to run, token-efficient, resilient to specific failures
they've actually hit in production.
**Cons:** weaker/cheaper model choice than top-tier alternatives, capped
history loses context in long conversations, shared-key rate limiting
required manual throttling UI (`CHAT_MAX_PER_WINDOW`).

**For EPL:** the token-economy discipline (summarize, don't dump raw rows;
cap history; store full data out-of-band for charts) directly applies to
serving large player/fixture/xG tables through a chat interface.

---

## 3. The MCP connector (`mcp_server.py`)

- **Dual transport, one codebase:** `python mcp_server.py` (stdio, for
  Claude Desktop) vs `python mcp_server.py --http` (streamable HTTP, for
  remote hosting) — same `FastMCP` instance, transport picked by a CLI flag.

- **TTL cache decorator** ⭐ — a hand-rolled `functools.wraps` decorator
  keyed on `(fn.__name__, args, kwargs)`, with **per-source TTLs matched to
  how often each upstream actually updates** (1hr for time series, 1 day for
  search/metadata/catalog, 30min for filings). Bounded entry count with lazy
  eviction of expired entries first, then oldest-inserted. Simple, no
  external cache dependency (no Redis) — appropriate for a single-process
  deploy.

  ```python
  HOUR, DAY = 3600, 86400
  _cache: dict = {}
  _cache_lock = threading.Lock()
  _CACHE_MAX = 256

  def _ttl_cache(seconds: int):
      def deco(fn):
          @functools.wraps(fn)
          def wrapper(*args, **kwargs):
              key = (fn.__name__, args, tuple(sorted(kwargs.items())))
              now = time.time()
              with _cache_lock:
                  hit = _cache.get(key)
                  if hit and hit[0] > now:
                      return hit[1]
              result = fn(*args, **kwargs)
              with _cache_lock:
                  if len(_cache) >= _CACHE_MAX:
                      for k in [k for k, v in _cache.items() if v[0] <= now]:
                          _cache.pop(k, None)
                      while len(_cache) >= _CACHE_MAX:
                          _cache.pop(next(iter(_cache)))
                  _cache[key] = (now + seconds, result)
              return result
          return wrapper
      return deco

  @mcp.tool()
  @_ttl_cache(HOUR)
  def get_fred_data(...): ...
  ```

  **For EPL:** TTL should track our own update cadence — e.g. long TTL
  (days) for historical/season data that never changes, short TTL (hours) on
  matchweek-of gameweek data, very short/no cache on live in-game stats if we
  ever pull those.

- **`full_data` pattern** ⭐ — tools return summarized JSON by default; a
  `full_data=true` arg appends the complete series (as CSV or JSON) pulled
  from the thread-local store. Same tool serves "just answer my question"
  and "give me everything to chart" without doubling the number of tool
  definitions — fewer tools for the LLM to choose between means fewer wrong
  picks. **Directly relevant to on-demand visualization:** the LLM calls one
  tool, gets a summary to reason/respond with, and the *full* dataset is
  already sitting in the store ready for the UI layer to chart — no second
  round-trip needed to render a graph.

- **Tool docstrings do double duty:** docstrings are literally the MCP tool
  descriptions shown to the calling LLM — they read like usage guidance for
  a model ("Call this first for any macro/labor statistic..."), including
  explicit fallback ordering. This is prompt engineering living inside code
  comments — write tool docstrings as instructions to the model, not just
  human documentation.

- **Stateless HTTP mode** (`mcp.settings.stateless_http = True`): each
  request is self-contained, no session affinity required — important if
  hosting MCP behind ephemeral/free-tier infra or a load balancer.

- **DNS-rebinding protection gotcha:** the MCP SDK's default protection only
  allows `localhost` Host headers and returns HTTP 421 for real domains.
  Must explicitly disable
  (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) when
  hosting a public HTTP MCP server on a real domain. **Worth remembering if
  we ever expose an EPL MCP server publicly.**

- **Distribution via `.mcpb` bundle:** ships a `.mcpb` file (manifest.json +
  entry point) as a GitHub release asset so Claude Desktop users can
  double-click to install rather than hand-editing
  `claude_desktop_config.json`. Still requires the user to have cloned the
  repo and built the venv (`${user_config.repo_dir}`) — a config-generation
  convenience, not a fully portable app.

- **Deployment evolution:** started on Render free tier (sleeps between
  uses, cold starts) then migrated to a self-managed always-on VM (Caddy
  reverse proxy + systemd service, app bound to `127.0.0.1` only,
  non-root service user, key-only SSH) to eliminate cold-start latency.
  **No auth on the MCP endpoint** — explicitly acknowledged as an accepted
  tradeoff since the data is all public/read-only. Not a pattern to copy if
  our EPL data or FPL account details are ever private/user-specific — would
  need auth on the MCP surface in that case.

**Pros:** genuinely portable tool logic, cheap TTL caching without infra,
thoughtful docstrings-as-agent-instructions, honest tradeoff documentation.
**Cons:** no auth (fine for public data only), free-tier hosting required
real migration effort, single-process in-memory cache doesn't share across
instances if scaled horizontally.

---

## 4. UI design (`app.py`, `ui_theme.py`)

- **Native page router:** sidebar-grouped sections built declaratively at
  the bottom of the file, *after* all `render_*` functions are defined —
  clean separation between page logic and routing.

- **Overview page as a landing/funnel surface:** headline metrics with
  sparklines, sample-question buttons that pre-populate the chat page via
  session state + a page-switch call, and an explicit "three ways to use
  this" section (dashboard / AI connector / run-it-yourself) — the landing
  page doubles as a funnel toward the AI/MCP surface, not just a data view.

- **Centralized chart theming** ⭐ — one `style_fig()` function applied to
  *every* chart: transparent backgrounds (inherits the app's theme surface),
  light/dark-aware color palettes explicitly validated for colorblind-safety
  ("slot order is the CVD-safety mechanism — never reorder or cycle" — a
  comment flagging a real prior mistake), consistent margins/fonts/gridlines.
  **Directly applicable to on-demand/dynamic visualizations:** because every
  chart — whether rendered from a dashboard page or dynamically from an LLM
  tool call — goes through the same `line()`/`bar()`/`style_fig()` helpers,
  ad hoc charts the agent decides to render on the fly look identical in
  quality/branding to hand-built dashboard charts. Two independent theme
  sources (Python palette + app config file) must be kept in sync — a
  fragile coupling worth avoiding or automating if we copy this.

- **Chart-type-driven dynamic rendering:** stored results carry a
  `chart_type` tag (`"line"`, `"bar"`, or `None` for a plain table) set at
  fetch time by the tool function itself (e.g. time series → `"line"`,
  cross-sectional ranking → `"bar"`). The chat UI then just switches on that
  tag to pick a renderer — the *tool* decides what kind of chart makes sense
  for the data it fetched, and the UI is a dumb dispatcher. **This is the
  core mechanism for "dynamic visualizations based on user query"**: the
  agent's tool call implicitly carries its own visualization hint, so no
  separate "decide how to chart this" LLM call or heuristic is needed at
  render time.

- **Caching layered on top of the same fetch functions:** the UI wraps each
  connector call in its own cache decorator, separate from the MCP server's
  TTL cache — two independent caching layers for the same underlying APIs
  since they're different processes/audiences. Worth being deliberate about
  if one app serves both a UI and an agent endpoint from the same process.

- **BYO-API-key pattern for chat:** shared server key is rate-limited; users
  can paste their own key to remove the limit, explicitly scoped to the
  browser session only, never stored. Reasonable pattern if we ever offer a
  shared/public EPL assistant endpoint.

---

## Key transferable takeaways for EPL

1. **Single tool-function layer, multiple thin adapters** — one set of
   Python functions against our DuckDB data, wrapped separately for chat
   tool-calling vs. MCP. Avoids logic drift between an in-app agent and an
   external MCP surface. *(Highest priority to adopt.)*
2. **Return summaries to the LLM, stash full data elsewhere** (thread-local
   or session store), with a `full_data`-style escape hatch — keeps token
   costs down without losing the ability to chart/export the full result.
3. **Tag each tool result with a chart hint at fetch time** (`"line"`,
   `"bar"`, `None`) so the UI/rendering layer can dynamically render
   whatever the agent just fetched without a second "how should I visualize
   this" decision step. This is the mechanism to build toward for on-demand
   dashboards driven by natural-language queries.
4. **Bound the agent loop and give it a graceful exit** (synthetic "stop
   calling tools, answer now" message) rather than a hard cutoff.
5. **Bake retrieval fallback chains into the system prompt/tool docstrings**,
   not just code — the LLM is the one making the routing decision, so it
   needs the guidance in natural language, not just in application logic.
6. **TTL cache keyed by how often each upstream/table actually changes**, not
   one global TTL — cheap, no infra required, meaningfully cuts redundant
   query load.
7. **One centralized chart-styling function** applied everywhere (dashboard
   pages and dynamically-rendered agent charts alike) so ad hoc visualizations
   look consistent with hand-built ones.
8. **Document security tradeoffs explicitly when made** (e.g. unauthenticated
   MCP endpoint) rather than silently shipping them.
9. **Watch for the DNS-rebinding-protection gotcha** if we ever stand up our
   own public MCP HTTP server on a real domain.
