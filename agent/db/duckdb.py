"""Read access to the analytical warehouse (db/epl.duckdb) for the tool layer.

A fresh, short-lived read-only connection per call -- opened, used, and
closed by the caller -- not a persistent shared connection. This is
deliberate, not just the original pre-optimization design: observability
writes now go to Postgres (see agent/observability.py) and are periodically
synced into DuckDB by a separate process (agent/observability_sync.py), so
this process never needs write access to db/epl.duckdb at all, and can stay
strictly read-only.

That separation only works if these connections are short-lived. Verified
empirically (2026-09-17/18): DuckDB lets multiple read-only connections
coexist freely, but a writer needs the file to itself -- even an
already-open *read-only* connection from another process blocks a writer
from connecting at all. A single long-held read-only connection here would
starve the sync job forever; open-query-close per call leaves it real gaps
to acquire the write lock.

The reverse also happens, confirmed in the same test: a reader that tries
to connect during the sync job's brief write window fails outright with no
retry. Since that write window is real but short (a few seconds every
~15 minutes), get_readonly_connection() retries on that specific conflict
rather than surfacing it as an error on the rare request unlucky enough to
land in it.
"""
from __future__ import annotations

import time

import duckdb

from agent.config import settings

MAX_CONNECT_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.3


def get_readonly_connection() -> duckdb.DuckDBPyConnection:
    """Open a fresh read-only connection. Callers are responsible for
    closing it (e.g. via `with get_readonly_connection() as conn:`).
    """
    last_err: Exception | None = None
    for _attempt in range(MAX_CONNECT_ATTEMPTS):
        try:
            return duckdb.connect(str(settings.duckdb_path), read_only=True)
        except duckdb.IOException as e:
            last_err = e
            time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Could not open a read-only DuckDB connection after {MAX_CONNECT_ATTEMPTS} "
        "attempts -- the periodic observability sync job was likely mid-write. "
        "This should be rare (~seconds, every ~15 minutes)."
    ) from last_err
