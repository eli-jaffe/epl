"""Helpers shared by the historical and live loaders."""


def upsert_df(con, table, df):
    """Upsert a DataFrame into `table` on its primary key (never delete-then-
    insert; see CLAUDE.md on DuckDB FK limits). The PK is read from the
    table's own constraints so callers don't restate it."""
    if len(df) == 0:
        return
    pk_cols = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchone()[0]
    update_cols = [c for c in df.columns if c not in pk_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    con.execute(
        f"INSERT INTO {table} BY NAME SELECT * FROM df "
        f"ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET {set_clause}"
    )
