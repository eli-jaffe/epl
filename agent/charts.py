"""Persist a finished chart run -- see docs/dashboard_plan.md section 4.

Called once by the /dashboard/chart endpoint after the agent loop succeeds,
so a retried Execute phase never leaves duplicates. One transaction: the
chart_request, any newly defined metrics (status 'proposed'), and the
chart_request_metric links, so a chart never exists without the metric
definitions it points at.
"""
from __future__ import annotations

import uuid

from agent.db.dashboard_models import ChartRequest, ChartRequestMetric, MetricRow
from agent.db.postgres import async_session_maker
from agent.run_context import RunContext


async def save_chart(run: RunContext, prompt: str) -> uuid.UUID:
    chart = run.chart
    assert chart is not None
    chart_id = uuid.uuid4()
    async with async_session_maker() as session:
        for m in chart["new_metrics"]:
            session.add(
                MetricRow(
                    id=m["id"],
                    name=m["name"],
                    name_key=m["name_key"],
                    definition=m["definition"],
                    formula=m["formula"],
                    sql_expression=m["sql_expression"],
                    grain=m["grain"],
                    source_views=m["source_views"],
                    caveats=m["caveats"],
                    status="proposed",
                    created_by_query_id=run.query_id,
                )
            )
        # Metric rows must exist before anything references them in the
        # same transaction's flush order -- flush explicitly.
        await session.flush()
        session.add(
            ChartRequest(
                id=chart_id,
                user_id=run.user_id,
                query_id=run.query_id,
                prompt=prompt,
                title=chart["title"],
                sql=chart["sql"],
                spec=chart["spec"],
            )
        )
        await session.flush()
        for m in chart["metrics"]:
            session.add(
                ChartRequestMetric(
                    chart_request_id=chart_id,
                    metric_id=m["id"],
                    role="defined" if m["is_new"] else "reused",
                )
            )
        await session.commit()
    return chart_id
