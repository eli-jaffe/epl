# Agentic Analyst Review — USMNT_Employee_Pipeline

Source: https://github.com/eli-jaffe/USMNT_Employee_Pipeline (reviewed 2026-09-16)

Reference notes on the "agentic analyst" feature in the USMNT toy HR-analytics repo, for use when designing a similar natural-language query agent for the `epl` app.

The agent is a CLI (`cli.py` → `agent.py`) that answers natural-language questions about a player database using a Claude Opus-driven **Reason → Plan → Execute → Reflect → Synthesize** loop.

## Architecture

- **Orchestrator** (`agent.py`): a stateless 5-phase loop, each phase a single-turn Anthropic call via one shared `_llm()` wrapper. Phase outputs are plain structured text (`FIELD: value` lines), parsed with regex (`_parse_reason`, `_parse_plan`, `_parse_reflect`); only the Execute phase asks for raw JSON (`_parse_tool_call`).
- **Sub-agent pattern**: the orchestrator delegates all data access to `agents/data_agent.py` via a flat `TOOL_REGISTRY` + `dispatch()`. Every tool returns a uniform envelope: `{"success": bool, "data": ..., "error": ...}`, so `agent.py` never branches on tool-specific error shapes. Tools: `schema_lookup`, `run_sql`, `compute_stats`, `clarify`.
- **Core design principle**, stated explicitly in the code: *"the LLM reasons and plans; Python executes and validates. Nothing that requires deterministic correctness (SQL execution, math) is left to the LLM."* Aggregation, correlation (`compute_stats` implements Pearson's r from scratch, no scipy), and SQL execution are all deterministic Python, not LLM output.
- **Memory** (`memory.py`): a per-question `AgentMemory`/`StepRecord` dataclass pair, discarded after each answer — no cross-session persistence. Used to build the Reflect/retry prompts and truncates tool output to 500 chars to bound context growth.
- **Schema grounding** (`db.py`): `get_schema()` dynamically introspects `sqlite_master`/`PRAGMA table_info` and runs a generated per-column profiling query (distinct count, null %, min/max, 3 sample values) so the LLM gets rich grounding, not just column names. New tables are automatically surfaced with zero code changes.
- **SQL safety, defense-in-depth**: (1) regex blocklist on `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/TRUNCATE/ATTACH` + must start with `SELECT`; (2) the actual connection is opened in SQLite URI read-only mode (`file:...?mode=ro`), so even a successful injection past the regex is rejected by the driver itself; (3) row cap (`MAX_RESULT_ROWS=100`) to prevent context blowup.
- **Retry loop**: up to 5 full-loop retries. Reflect returns `SUCCESS | RETRY | CANNOT_ANSWER`; on `RETRY` it re-enters at *Reason* (not just re-execute), carrying the full step trace and a list of already-failed SQL strings forward so the model is explicitly told not to repeat them.
- **Domain glossary baked into every prompt**: `METRIC_DEFINITIONS` and `DATA_CAVEATS` (e.g. "minutes_played is TEXT and may contain '90+3'") are injected into all five phase prompts from one central file (`prompts/prompts.py`), keeping ambiguous business definitions ("best player," "regular starter") consistent everywhere instead of re-derived per call.

## Pros

- Clean separation of "fuzzy" reasoning (LLM) vs. "must be correct" execution (Python) — reduces hallucinated stats/SQL results.
- Genuinely layered SQL safety (app-level regex + driver-level read-only mode) — cheap trick worth reusing anywhere a SQL-writing LLM touches a database.
- Reflect phase does real root-cause-style retries, not blind retries — told to diagnose *why* (bad cast, wrong join) and explicitly forbidden from resubmitting the same query.
- `clarify` tool gives a human-in-the-loop escape hatch, but the prompt instructs the model to prefer a stated assumption over asking — avoids over-clarifying while still allowing it when truly needed.
- Full phase-by-phase trace printed to console (and captured in the README as example transcripts) — good transparency for debugging and for teaching/onboarding.

## Cons / gaps

- **`tools/data_agent_tools.py` is dead code** — duplicates `db.py`'s schema/profiling/safety logic almost line-for-line; `agent.py` actually imports `agents.data_agent`, which imports `db` directly. Leftover from a refactor.
- **`requirements.txt` is missing `anthropic` and `python-dotenv`** — both are imported by `agent.py`/`agents/data_agent.py`, so a fresh `pip install -r requirements.txt` won't be enough to run the agent as documented.
- **`DataAgentREADME.md` is an empty file** — presumably meant to hold what ended up embedded in the main `README.md`'s "Agentic Analyst" section instead.
- **Brittle sys.path import fallback** duplicated in two files instead of proper package imports — fragile and copy-pasted.
- **Regex field parsing fails silently**: if the LLM's output format drifts even slightly, the parsers just return `""` for that field rather than erroring, which can quietly degrade behavior instead of triggering a clear retry.
- **No model tiering**: every phase (including trivial JSON tool-call emission) uses `claude-opus-4-5`, and a single question can trigger up to ~20 LLM calls across 5 retries — costly/slow for phases that don't need frontier reasoning.
- **`compute_stats`'s "reference to previous step output"** in the Execute prompt template isn't actually resolved anywhere in `agent.py` — nothing wires a prior `run_sql` result into a later `compute_stats` call automatically; the LLM would have to paste the data itself, which looks undertested.
- **"Multiple pluggable agents" is aspirational, not yet real** — the README frames the architecture as letting many domain agents plug in, but today there's exactly one (`DataAgent`) and no router/selection logic between agents.
- Schema profiling re-runs on every single question, with no caching across turns in a CLI session.

## Tips / hacks worth reusing for epl's agent

1. Generate a **per-column statistical profile** (distinct count, null %, min/max, sample values) dynamically via SQL and inject it into the schema prompt — much better NL2SQL grounding than bare DDL, and self-updating as data changes.
2. Centralize a **metric glossary + data-quality caveats** block and inject it into every phase prompt verbatim — cheap way to keep ambiguous business definitions (e.g. "goals per 90", "regular starter") consistent without re-deriving them per call. For epl, this is where fantasy-specific definitions (e.g. "form", "value", gameweek boundaries) should live.
3. Layer SQL safety: app-level keyword blocklist *plus* opening the actual DB connection in driver-level read-only mode — the second layer holds even if the first is bypassed by prompt injection.
4. Have the Plan phase write out the literal SQL as part of the plan text, then have a separate Execute phase re-emit it as JSON — a cheap belt-and-suspenders check between intended and actual query.
5. Truncate tool outputs (e.g., to 500 chars) when building retry/reflect context to keep prompt size bounded across loop iterations.
6. Carry forward "queries that already failed" explicitly into the retry prompt, with an instruction not to resubmit them — simple, effective way to prevent retry loops from looping on the same mistake.

## Overall take

Framed explicitly as a "toy"/teaching example, and it holds up well as one: the reason-plan-execute-reflect-synthesize loop, tool envelope, and layered SQL safety are all solid, reusable patterns. The rough edges (dead duplicate file, missing deps in `requirements.txt`, empty doc file, no real multi-agent routing yet) look like artifacts of a fast, recent addition rather than fundamental design flaws — worth avoiding those specific mistakes (unpinned deps, dead duplicate modules, aspirational architecture docs that outpace the code) when building epl's version.
