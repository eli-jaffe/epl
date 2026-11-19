-- Adds v_player_gameweek_market (docs/live_api_connector_plan.md, item S3) to an
-- existing db/epl.duckdb. A view only: no data changes. Idempotent (CREATE OR
-- REPLACE). Fresh builds from schema.sql already include it.
-- Market state (price, ownership, transfers) per player per gameweek, built
-- from fact_player_snapshot so it matches the shape of fact_player_gameweek's
-- value/selected/transfers_* columns. Grain is player-per-GAMEWEEK, not per
-- fixture: market data doesn't depend on which fixture it was.
--
-- A gameweek's row is the player's market state at its deadline, which is the
-- first kickoff minus 90 minutes (verified for all 38 gameweeks of 2026-27).
-- We take the most recent snapshot at or before that deadline; snapshot_at
-- shows how stale it is (snapshots are daily). The snapshot's own `gameweek`
-- column is not used: right after a deadline the API's transfer counters start
-- counting the *next* gameweek while "current event" still names the old one.
-- Rows exist for gameweeks whose deadline has passed and for the next upcoming
-- one (where "most recent snapshot" is simply the latest market). Gameweeks
-- with no snapshot at or before their deadline (all history before the first
-- snapshot) have no row: use fact_player_gameweek's backfilled columns there.
-- `selected` is estimated from the API's ownership percentage, which is
-- rounded to 0.1% (~11k managers), so it is approximate; the backfilled
-- fact_player_gameweek.selected is exact. transfers_balance is recomputed as
-- in - out because FPL's own balance field is occasionally inconsistent.
CREATE OR REPLACE VIEW v_player_gameweek_market AS
WITH deadlines AS (
    SELECT season_id, gameweek, min(kickoff_time) - INTERVAL 90 MINUTE AS deadline
    FROM fact_fixture
    WHERE gameweek IS NOT NULL AND kickoff_time IS NOT NULL
    GROUP BY season_id, gameweek
),
in_scope AS (
    SELECT d.* FROM deadlines d
    WHERE d.deadline <= timezone('UTC', current_timestamp)
       OR d.deadline = (SELECT min(d2.deadline) FROM deadlines d2
                        WHERE d2.season_id = d.season_id
                          AND d2.deadline > timezone('UTC', current_timestamp))
),
chosen AS (
    SELECT d.season_id, d.gameweek, d.deadline, max(s.snapshot_at) AS snapshot_at
    FROM in_scope d
    JOIN (SELECT DISTINCT season_id, snapshot_at FROM fact_player_snapshot) s
      ON s.season_id = d.season_id AND s.snapshot_at <= d.deadline
    GROUP BY d.season_id, d.gameweek, d.deadline
)
SELECT
    c.season_id,
    c.gameweek,
    s.player_code,
    c.deadline,
    c.snapshot_at,
    s.now_cost                                                   AS value,
    CAST(round(s.selected_by_percent * s.total_players / 100) AS BIGINT) AS selected,
    s.transfers_in_event                                         AS transfers_in,
    s.transfers_out_event                                        AS transfers_out,
    s.transfers_in_event - s.transfers_out_event                 AS transfers_balance,
    s.cost_change_event
FROM chosen c
JOIN fact_player_snapshot s
  ON s.season_id = c.season_id AND s.snapshot_at = c.snapshot_at;
