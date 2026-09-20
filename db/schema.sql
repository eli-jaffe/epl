-- ============================================================================
-- EPL Fantasy data warehouse schema (DuckDB)
--
-- Grain: fact_player_gameweek is one row per player per gameweek per season.
-- Keys: dim_player/dim_team use FPL's stable `code` (NOT the season-scoped
-- `id` — that field is reassigned every season, e.g. Salah's id changed
-- 283 -> 308 -> 328 -> 381 across four seasons while his code stayed 118748).
-- Season-scoped ids are still stored on fact_player_season/fact_team_season
-- because gws/*.csv and fixtures.csv reference *those*, not `code` — the
-- ingestion pipeline uses each season's players_raw.csv/teams.csv as the
-- (season, id) -> code crosswalk.
-- ============================================================================

-- ------------------------------------------------------------------
-- Lineage / audit
-- ------------------------------------------------------------------
CREATE TABLE ingest_runs (
    run_id          UUID PRIMARY KEY DEFAULT uuid(),
    source          VARCHAR NOT NULL,       -- 'github_repo' | 'live_api'
    source_ref      VARCHAR,                -- git commit SHA, or API fetch window
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          VARCHAR NOT NULL,       -- 'running' | 'success' | 'failed'
    rows_affected   BIGINT,
    notes           VARCHAR
);

-- ------------------------------------------------------------------
-- Dimensions
-- ------------------------------------------------------------------
CREATE TABLE dim_season (
    season_id       VARCHAR PRIMARY KEY,    -- e.g. '2025-26'
    start_year      INTEGER NOT NULL,
    is_current      BOOLEAN NOT NULL DEFAULT FALSE,
    source          VARCHAR NOT NULL,
    source_ref      VARCHAR,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE TABLE dim_team (
    team_code       INTEGER PRIMARY KEY,    -- stable FPL 'code'
    name            VARCHAR NOT NULL,
    short_name      VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    source_ref      VARCHAR,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE TABLE dim_player (
    player_code     INTEGER PRIMARY KEY,    -- stable FPL 'code'
    first_name      VARCHAR,
    second_name     VARCHAR,
    web_name        VARCHAR,
    birth_date      DATE,
    region          INTEGER,
    source          VARCHAR NOT NULL,
    source_ref      VARCHAR,
    loaded_at       TIMESTAMP NOT NULL
);

-- ------------------------------------------------------------------
-- Periodic snapshot facts (season-level)
-- ------------------------------------------------------------------
CREATE TABLE fact_team_season (
    season_id               VARCHAR NOT NULL REFERENCES dim_season(season_id),
    team_code               INTEGER NOT NULL REFERENCES dim_team(team_code),
    team_id_that_season     INTEGER NOT NULL,  -- crosswalk value for that season's raw files
    strength                INTEGER,
    strength_overall_home   INTEGER,
    strength_overall_away   INTEGER,
    strength_attack_home    INTEGER,
    strength_attack_away    INTEGER,
    strength_defence_home   INTEGER,
    strength_defence_away   INTEGER,
    played                  INTEGER,
    win                     INTEGER,
    draw                    INTEGER,
    loss                    INTEGER,
    points                  INTEGER,
    position                INTEGER,
    source                  VARCHAR NOT NULL,
    source_ref              VARCHAR,
    loaded_at               TIMESTAMP NOT NULL,
    PRIMARY KEY (season_id, team_code)
);

CREATE TABLE fact_player_season (
    season_id               VARCHAR NOT NULL REFERENCES dim_season(season_id),
    player_code             INTEGER NOT NULL REFERENCES dim_player(player_code),
    player_id_that_season   INTEGER NOT NULL,  -- crosswalk value for that season's raw files
    team_code               INTEGER REFERENCES dim_team(team_code),
    position                VARCHAR,           -- 'GK' | 'DEF' | 'MID' | 'FWD'
    start_cost              INTEGER,           -- price in GBP 0.1m units
    end_cost                INTEGER,
    total_points            INTEGER,
    minutes                 INTEGER,
    selected_by_percent     DOUBLE,
    now_cost_rank           INTEGER,
    points_per_game         DOUBLE,
    form                    DOUBLE,
    source                  VARCHAR NOT NULL,
    source_ref              VARCHAR,
    loaded_at               TIMESTAMP NOT NULL,
    PRIMARY KEY (season_id, player_code)
);

-- ------------------------------------------------------------------
-- Transaction facts
-- ------------------------------------------------------------------
CREATE TABLE fact_fixture (
    season_id           VARCHAR NOT NULL REFERENCES dim_season(season_id),
    fixture_id          INTEGER NOT NULL,
    gameweek             INTEGER,
    team_h_code         INTEGER NOT NULL REFERENCES dim_team(team_code),
    team_a_code         INTEGER NOT NULL REFERENCES dim_team(team_code),
    team_h_score        INTEGER,
    team_a_score        INTEGER,
    kickoff_time        TIMESTAMP,
    team_h_difficulty   INTEGER,
    team_a_difficulty   INTEGER,
    finished             BOOLEAN,
    source               VARCHAR NOT NULL,
    source_ref           VARCHAR,
    loaded_at            TIMESTAMP NOT NULL,
    PRIMARY KEY (season_id, fixture_id)
);

CREATE TABLE fact_player_gameweek (
    season_id                    VARCHAR NOT NULL REFERENCES dim_season(season_id),
    gameweek                     INTEGER NOT NULL,
    player_code                  INTEGER NOT NULL REFERENCES dim_player(player_code),
    team_code                    INTEGER REFERENCES dim_team(team_code),
    opponent_team_code           INTEGER REFERENCES dim_team(team_code),
    fixture_id                   INTEGER NOT NULL,
    was_home                     BOOLEAN,
    minutes                      INTEGER,
    starts                       BOOLEAN,
    total_points                 INTEGER,
    goals_scored                 INTEGER,
    assists                      INTEGER,
    clean_sheets                 INTEGER,
    goals_conceded               INTEGER,
    own_goals                    INTEGER,
    bonus                        INTEGER,
    bps                          INTEGER,
    influence                    DOUBLE,
    creativity                   DOUBLE,
    threat                       DOUBLE,
    ict_index                    DOUBLE,
    expected_goals                DOUBLE,
    expected_assists              DOUBLE,
    expected_goal_involvements    DOUBLE,
    expected_goals_conceded       DOUBLE,
    value                         INTEGER,   -- price at this GW, GBP 0.1m units
    selected                      BIGINT,
    transfers_in                  BIGINT,
    transfers_out                 BIGINT,
    transfers_balance             BIGINT,
    yellow_cards                  INTEGER,
    red_cards                     INTEGER,
    saves                         INTEGER,
    penalties_saved               INTEGER,
    penalties_missed              INTEGER,
    source                        VARCHAR NOT NULL,
    source_ref                    VARCHAR,
    loaded_at                     TIMESTAMP NOT NULL,
    -- Grain is player-per-fixture, not strictly player-per-gameweek: FPL
    -- double gameweeks mean a player can have two rows for the same
    -- (season_id, gameweek, player_code) — one per fixture played that week.
    PRIMARY KEY (season_id, gameweek, player_code, fixture_id)
);

-- ------------------------------------------------------------------
-- Consumption views (denormalized for ad hoc / LLM-generated SQL)
-- ------------------------------------------------------------------
CREATE OR REPLACE VIEW v_player_gameweek AS
SELECT
    pg.season_id,
    pg.gameweek,
    p.player_code,
    p.web_name,
    p.first_name,
    p.second_name,
    ps.position,
    t.name          AS team_name,
    t.short_name    AS team_short_name,
    opp.name        AS opponent_name,
    opp.short_name  AS opponent_short_name,
    pg.was_home,
    pg.minutes,
    pg.starts,
    pg.total_points,
    pg.goals_scored,
    pg.assists,
    pg.clean_sheets,
    pg.goals_conceded,
    pg.bonus,
    pg.bps,
    pg.influence,
    pg.creativity,
    pg.threat,
    pg.ict_index,
    pg.expected_goals,
    pg.expected_assists,
    pg.expected_goal_involvements,
    pg.expected_goals_conceded,
    pg.value / 10.0 AS price_millions,
    pg.selected,
    pg.transfers_in,
    pg.transfers_out,
    pg.transfers_balance,
    pg.yellow_cards,
    pg.red_cards,
    pg.saves,
    pg.penalties_saved,
    pg.penalties_missed
FROM fact_player_gameweek pg
JOIN dim_player p           ON p.player_code = pg.player_code
LEFT JOIN dim_team t        ON t.team_code = pg.team_code
LEFT JOIN dim_team opp      ON opp.team_code = pg.opponent_team_code
LEFT JOIN fact_player_season ps
       ON ps.season_id = pg.season_id AND ps.player_code = pg.player_code;

-- Weekly totals, summed across fixtures — use this (not v_player_gameweek)
-- for "how many points did X get in GW7" style questions, since a double
-- gameweek means v_player_gameweek has two rows for that player+GW.
CREATE OR REPLACE VIEW v_player_gameweek_totals AS
SELECT
    pg.season_id,
    pg.gameweek,
    p.player_code,
    p.web_name,
    ps.position,
    t.name AS team_name,
    t.short_name AS team_short_name,
    COUNT(*) AS fixtures_played,
    string_agg(opp.short_name, ', ' ORDER BY pg.fixture_id) AS opponents,
    SUM(pg.minutes) AS minutes,
    SUM(pg.total_points) AS total_points,
    SUM(pg.goals_scored) AS goals_scored,
    SUM(pg.assists) AS assists,
    SUM(pg.clean_sheets) AS clean_sheets,
    SUM(pg.goals_conceded) AS goals_conceded,
    SUM(pg.bonus) AS bonus,
    SUM(pg.bps) AS bps,
    SUM(pg.expected_goals) AS expected_goals,
    SUM(pg.expected_assists) AS expected_assists,
    max(pg.value) / 10.0 AS price_millions,
    SUM(pg.yellow_cards) AS yellow_cards,
    SUM(pg.red_cards) AS red_cards
FROM fact_player_gameweek pg
JOIN dim_player p           ON p.player_code = pg.player_code
LEFT JOIN dim_team t        ON t.team_code = pg.team_code
LEFT JOIN dim_team opp      ON opp.team_code = pg.opponent_team_code
LEFT JOIN fact_player_season ps
       ON ps.season_id = pg.season_id AND ps.player_code = pg.player_code
GROUP BY pg.season_id, pg.gameweek, p.player_code, p.web_name, ps.position, t.name, t.short_name;

CREATE OR REPLACE VIEW v_player_season AS
SELECT
    fs.season_id,
    p.player_code,
    p.web_name,
    p.first_name,
    p.second_name,
    fs.position,
    t.name AS team_name,
    t.short_name AS team_short_name,
    fs.start_cost / 10.0 AS start_price_millions,
    fs.end_cost / 10.0   AS end_price_millions,
    fs.total_points,
    fs.minutes,
    fs.selected_by_percent,
    fs.points_per_game,
    fs.form
FROM fact_player_season fs
JOIN dim_player p       ON p.player_code = fs.player_code
LEFT JOIN dim_team t    ON t.team_code = fs.team_code;

-- ------------------------------------------------------------------
-- Agent observability (component 2, see docs/agent_ui_architecture_plan.md)
--
-- Append-only, same convention as ingest_runs above -- not upserted.
-- user_id is a soft reference to fastapi-users' Postgres `user` table (no
-- cross-db FK possible/needed): Postgres owns identity, this warehouse owns
-- the trace log, joined only ever in application code, never in SQL.
-- ------------------------------------------------------------------
CREATE TABLE agent_query (
    query_id        UUID PRIMARY KEY DEFAULT uuid(),
    user_id         UUID NOT NULL,
    query_text      VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,      -- 'running' | 'success' | 'failed'
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    final_answer    VARCHAR
);

CREATE TABLE agent_step (
    step_id         UUID PRIMARY KEY DEFAULT uuid(),
    query_id        UUID NOT NULL REFERENCES agent_query(query_id),
    phase           VARCHAR NOT NULL,      -- 'reason'|'plan'|'execute'|'reflect'|'synthesize'
    step_index      INTEGER NOT NULL,      -- order within the query, 0-based
    -- populated only for Execute-phase tool calls:
    tool_name       VARCHAR,
    tool_args       VARCHAR,               -- JSON-encoded
    tool_result     VARCHAR,               -- JSON-encoded, truncated; only set on success
    tool_success    BOOLEAN,
    tool_error      VARCHAR,
    summary         VARCHAR,               -- short human-readable description
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP
);

CREATE TABLE agent_llm_call (
    call_id             UUID PRIMARY KEY DEFAULT uuid(),
    step_id             UUID NOT NULL REFERENCES agent_step(step_id),
    model               VARCHAR NOT NULL,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    cost_usd            DOUBLE,
    called_at           TIMESTAMP NOT NULL
);

-- Rates change over time -- versioned by effective_date rather than a
-- single mutable row, so historical cost_usd figures stay reproducible.
CREATE TABLE agent_model_pricing (
    model                   VARCHAR NOT NULL,
    effective_date          DATE NOT NULL,
    input_usd_per_mtok      DOUBLE NOT NULL,
    output_usd_per_mtok     DOUBLE NOT NULL,
    cache_read_usd_per_mtok  DOUBLE NOT NULL,
    cache_write_usd_per_mtok DOUBLE NOT NULL,
    PRIMARY KEY (model, effective_date)
);

CREATE OR REPLACE VIEW v_agent_query_cost AS
SELECT
    q.query_id,
    q.user_id,
    q.query_text,
    q.status,
    q.started_at,
    q.finished_at,
    COUNT(DISTINCT s.step_id) AS step_count,
    COUNT(l.call_id) AS llm_call_count,
    SUM(l.input_tokens) AS total_input_tokens,
    SUM(l.output_tokens) AS total_output_tokens,
    SUM(l.cost_usd) AS total_cost_usd
FROM agent_query q
LEFT JOIN agent_step s      ON s.query_id = q.query_id
LEFT JOIN agent_llm_call l  ON l.step_id = s.step_id
GROUP BY q.query_id, q.user_id, q.query_text, q.status, q.started_at, q.finished_at;
