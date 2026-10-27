"""Chart-mode tools (category C) -- see docs/dashboard_plan.md section 3.

Only offered to the agent when a run is in chart mode (the dashboard's
prompt box), never via /chat or MCP, so they live outside TOOL_REGISTRY.
Both are async: they read Postgres (the metric store) and read the
RunContext, unlike the sync DuckDB tools.

create_chart does not persist anything. The agent loop can retry its
Execute phase, and saving here would leave orphaned charts and half-defined
metrics from abandoned attempts -- so it validates, runs the SQL, and
stashes the result on the RunContext; agent/charts.py saves the final one
atomically after the run succeeds.
"""
from __future__ import annotations

import asyncio
import json

from agent import metrics
from agent.run_context import current_run
from agent.tools import sql as sql_tools

MAX_CHART_ROWS = 500
MAX_SPEC_BYTES = 50_000
# Keys that make a Vega-Lite spec fetch something or navigate: the browser
# must only ever render the inline rows we inject. `url` covers data.url,
# lookup/join sources and image marks; `href` is the clickable-mark channel.
_FORBIDDEN_SPEC_KEYS = {"url", "href"}


async def list_metrics() -> list[dict]:
    """Every known metric, compact -- id, name, one-line definition, grain."""
    return [
        {"metric_id": m.id, "name": m.name, "definition": m.definition, "grain": m.grain, "status": m.status}
        for m in await metrics.list_metrics()
    ]


def _find_forbidden(node, path: str = "spec") -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_SPEC_KEYS:
                return f"{path}.{key}"
            found = _find_forbidden(value, f"{path}.{key}")
            if found:
                return found
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found = _find_forbidden(value, f"{path}[{i}]")
            if found:
                return found
    return None


def _validate_spec(spec: object) -> dict:
    if isinstance(spec, str):  # tolerate a JSON-encoded spec
        spec = json.loads(spec)
    if not isinstance(spec, dict):
        raise ValueError("vega_lite_spec must be a JSON object.")
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        raise ValueError(f"vega_lite_spec is larger than {MAX_SPEC_BYTES} bytes.")
    bad = _find_forbidden(spec)
    if bad:
        raise ValueError(
            f"{bad} is not allowed: charts may only use the rows returned by `sql` "
            "(no urls or links). Remove it."
        )
    return spec


async def with_fresh_rows(spec: dict, sql: str) -> tuple[dict, list[dict]]:
    """Run `sql` through the run_sql sandbox and return the spec with those
    rows as its data. Shared by create_chart and the saved-chart refresh, so a
    refresh goes through exactly the same read-only checks as the original."""
    rows = await asyncio.to_thread(sql_tools.run_sql, sql, MAX_CHART_ROWS)
    if not rows:
        raise ValueError("The SQL returned no rows, so there is nothing to chart. Adjust the query.")
    # Round-trip through JSON so dates/decimals from DuckDB become plain
    # JSON values the browser can embed.
    rows = json.loads(json.dumps(rows, default=str))
    # The rows are injected here, not authored by the model: it never has to
    # echo data back, and the spec can't carry data that didn't come from sql.
    return {**{k: v for k, v in spec.items() if k not in ("data", "datasets")}, "data": {"values": rows}}, rows


async def create_chart(title: str, sql: str, vega_lite_spec: object, metrics_used: list[dict]) -> dict:
    """Validate a chart, run its SQL, and stash it on the run for saving."""
    run = current_run.get()
    if run is None or not run.chart_mode:
        raise RuntimeError("create_chart is only available when building a dashboard chart.")

    spec = _validate_spec(vega_lite_spec)
    resolved, new_definitions = await metrics.resolve_metrics(metrics_used)

    spec, rows = await with_fresh_rows(spec, sql)

    run.chart = {
        "title": title.strip(),
        "sql": sql.strip(),
        "spec": spec,
        "metrics": resolved,
        "new_metrics": new_definitions,
    }
    # Compact on purpose -- this goes back into the model's context and then
    # the answer; the rows themselves do not.
    return {
        "status": "chart created",
        "rows": len(rows),
        "columns": list(rows[0].keys()),
        "metrics": [
            {
                "metric_id": m["id"],
                "name": m["name"],
                "definition": m["definition"],
                "formula": m["formula"],
                "newly_defined": m["is_new"],
            }
            for m in resolved
        ],
    }
