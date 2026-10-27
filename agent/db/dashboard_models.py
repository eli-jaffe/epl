"""Postgres models for the dashboard's agent-generated charts -- see
docs/dashboard_plan.md (sections 3 and 4).

OLTP app state, so Postgres rather than DuckDB (same split as users and
observability). Three tables:
- `metric`: agent-defined metrics only. Curated metrics live in code
  (agent/metrics.py) and are never rows here.
- `chart_request`: one row per chart the agent produced, with the exact SQL
  and Vega-Lite spec. The spec includes the injected result rows (capped at
  500), so reopening a chart shows the data as it was when created rather
  than re-running the SQL against a warehouse that has since moved on.
- `chart_request_metric`: which metrics each chart used and whether the
  agent reused an existing one or defined it in that request. The metrics
  library ("how often is each metric requested") is a GROUP BY over this.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from agent.auth import models as _auth_models  # noqa: F401 -- FK to "user.id"
from agent.db.postgres import Base

_TZDateTime = DateTime(timezone=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MetricRow(Base):
    __tablename__ = "metric"

    id: Mapped[str] = mapped_column(primary_key=True)  # slug of the name
    name: Mapped[str] = mapped_column(nullable=False)
    # Lowercased alphanumerics of `name`: the uniqueness key, so "Goals per 90"
    # and "goals-per-90" collide instead of becoming two metrics.
    name_key: Mapped[str] = mapped_column(unique=True, nullable=False)
    definition: Mapped[str] = mapped_column(nullable=False)
    formula: Mapped[str] = mapped_column(nullable=False)
    sql_expression: Mapped[str | None] = mapped_column(default=None)
    grain: Mapped[str] = mapped_column(nullable=False)
    source_views: Mapped[list] = mapped_column(JSON, default=list)
    caveats: Mapped[list] = mapped_column(JSON, default=list)
    # 'proposed' | 'merged' | 'promoted'. Set by an admin in the metrics view:
    # 'merged' folds a near-duplicate into `merged_into` (links are left
    # untouched; demand is rolled up at read time so a merge can be undone);
    # 'promoted' marks one whose snippet is queued for the curated module.
    status: Mapped[str] = mapped_column(default="proposed", nullable=False)
    merged_into: Mapped[str | None] = mapped_column(default=None)
    # Soft reference (no FK) to agent_query.query_id, like the DuckDB side.
    created_by_query_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_now, nullable=False)


class ChartRequest(Base):
    __tablename__ = "chart_request"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    query_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True, default=None)
    prompt: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    sql: Mapped[str] = mapped_column(nullable=False)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_now, nullable=False)


class ChartRequestMetric(Base):
    __tablename__ = "chart_request_metric"

    chart_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chart_request.id"), primary_key=True
    )
    # Curated ids have no `metric` row, so this is deliberately not an FK.
    metric_id: Mapped[str] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(nullable=False)  # 'reused' | 'defined'
