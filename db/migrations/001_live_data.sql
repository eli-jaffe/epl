-- Brings an existing db/epl.duckdb in line with the schema.sql additions made
-- for the live API connector (docs/live_api_connector_plan.md, items S1/S2).
-- Idempotent: safe to run more than once. Fresh builds from schema.sql don't
-- need this.
ALTER TABLE fact_player_gameweek ADD COLUMN IF NOT EXISTS tackles INTEGER;
ALTER TABLE fact_player_gameweek ADD COLUMN IF NOT EXISTS recoveries INTEGER;
ALTER TABLE fact_player_gameweek ADD COLUMN IF NOT EXISTS clearances_blocks_interceptions INTEGER;
ALTER TABLE fact_player_gameweek ADD COLUMN IF NOT EXISTS defensive_contribution INTEGER;

CREATE TABLE IF NOT EXISTS fact_player_snapshot (
    season_id            VARCHAR NOT NULL REFERENCES dim_season(season_id),
    player_code          INTEGER NOT NULL REFERENCES dim_player(player_code),
    snapshot_at          TIMESTAMP NOT NULL,
    gameweek             INTEGER,
    now_cost             INTEGER,
    selected_by_percent  DOUBLE,
    total_players        BIGINT,
    transfers_in_event   BIGINT,
    transfers_out_event  BIGINT,
    cost_change_event    INTEGER,
    source               VARCHAR NOT NULL,
    source_ref           VARCHAR,
    loaded_at            TIMESTAMP NOT NULL,
    PRIMARY KEY (season_id, player_code, snapshot_at)
);
