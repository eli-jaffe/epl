"""Domain glossary + data caveats -- injected into every agent-loop phase's
system prompt (see references/agentic_analyst_review_usmnt.md's pattern:
centralize ambiguous domain definitions and data-quality caveats in one
place instead of re-deriving them per call).

Sourced from CLAUDE.md's "Data-modeling gotchas" -- keep in sync if that
section changes.
"""
from __future__ import annotations

DOMAIN_GLOSSARY = """\
Data warehouse conventions and caveats to respect when answering fantasy \
Premier League questions:

- Always resolve a player name to `player_code` via the `search_players` \
tool first. `player_code` is FPL's stable identity; the raw `id` field is \
reassigned every season and must never be used to identify a player or \
team across seasons.
- For "how many points did X get in gameweek N" style questions, gameweek \
totals must come from v_player_gameweek_totals (summed across fixtures), \
never raw fact_player_gameweek rows -- real double gameweeks exist, and a \
player can have two rows for the same (season, gameweek).
- `xP` (expected points) has a documented lookahead-bias caveat in the \
source data: it's scraped post-gameweek and may not reflect what was \
actually knowable before that gameweek's deadline. Flag this if a question \
is specifically about pre-deadline decision-making.
- A club's display name can change between stints in the league even \
though its `code` is stable (e.g. Ipswich -> Ipswich Town). Prefer \
matching on team_code, not name, when there's any ambiguity.
"""
