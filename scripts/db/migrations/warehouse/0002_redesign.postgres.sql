-- Warehouse redesign (Postgres version).
--
-- Same data, standard design:
--   * one table per thing: new `videos` (a game's replay) and `alignments` (each alignment run
--     with its quality metrics as columns, instead of JSON in loads.details);
--   * provenance stored once per batch: games.events_load_id / shots_load_id,
--     videos.captions_load_id, videos.alignment_id, instead of a load_id on every row;
--   * snake_case names; every MoneyPuck column kept (renamed with build_db.snake());
--   * real types: boolean flags, date, timestamptz, jsonb (SQLite keeps the names, stores 0/1 and text);
--   * foreign keys (indexed) and CHECK constraints;
--   * clearer names: transcript -> captions, calibrated_transcript_time -> video_sec,
--     calibrated_game_time -> game_sec, in_play -> live.
-- One video per game (videos.game_id is unique), so events.video_sec is unambiguous.
-- The old tables are renamed *_v1, copied from, then dropped, all in this migration's transaction.

DROP VIEW IF EXISTS shot_commentary;
DROP VIEW IF EXISTS timeline;

-- Free the names the new tables and their constraints will use.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
             WHERE connamespace = 'warehouse'::regnamespace AND contype = 'f' LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS ix_loads_game, ix_loads_video, ix_games_video, ix_shots_team, ix_shots_shooter, ix_transcript_game;
ALTER TABLE loads RENAME TO loads_v1;
ALTER TABLE loads_v1 RENAME CONSTRAINT loads_pkey TO loads_v1_pkey;
ALTER TABLE games RENAME TO games_v1;
ALTER TABLE games_v1 RENAME CONSTRAINT games_pkey TO games_v1_pkey;
ALTER TABLE events RENAME TO events_v1;
ALTER TABLE events_v1 RENAME CONSTRAINT events_pkey TO events_v1_pkey;
ALTER TABLE shots RENAME TO shots_v1;
ALTER TABLE shots_v1 RENAME CONSTRAINT shots_pkey TO shots_v1_pkey;
ALTER TABLE transcript RENAME TO transcript_v1;
ALTER TABLE transcript_v1 RENAME CONSTRAINT transcript_pkey TO transcript_v1_pkey;

-- One row per batch of data loaded: where it came from and which code loaded it.
CREATE TABLE loads (
    load_id      TEXT PRIMARY KEY,                 -- uuid
    kind         TEXT NOT NULL CHECK (kind IN ('games', 'events', 'shots', 'captions')),
    game_id      BIGINT,
    video_id     TEXT,
    raw_files    JSONB,                            -- [{path, sha256}]
    source_urls  JSONB,                            -- [url]
    fetched_at   TIMESTAMPTZ,                      -- newest raw file's modification time
    loaded_at    TIMESTAMPTZ NOT NULL,
    code_version TEXT,                             -- git commit (+ "-dirty")
    run_id       BIGINT                            -- pipeline run (ops.runs), if any
);

CREATE TABLE games (
    game_id        BIGINT PRIMARY KEY,
    season         INTEGER NOT NULL,               -- 20232024 style
    game_type      TEXT NOT NULL CHECK (game_type IN ('preseason', 'regular', 'playoff')),
    game_date      DATE NOT NULL,
    venue          TEXT,
    away_team      TEXT NOT NULL,                  -- abbreviation, e.g. BOS
    home_team      TEXT NOT NULL,
    away_name      TEXT,
    home_name      TEXT,
    away_goals     INTEGER,
    home_goals     INTEGER,
    ended_in       TEXT CHECK (ended_in IN ('REG', 'OT', 'SO')),
    load_id        TEXT REFERENCES loads (load_id),  -- the NHL boxscore load
    events_load_id TEXT REFERENCES loads (load_id),  -- the MoneyPuck play-by-play load
    shots_load_id  TEXT REFERENCES loads (load_id)   -- the MoneyPuck shots load
);

-- A game's full replay on YouTube (at most one per game).
CREATE TABLE videos (
    video_id         TEXT PRIMARY KEY,
    game_id          BIGINT NOT NULL UNIQUE REFERENCES games (game_id),
    title            TEXT,
    url              TEXT GENERATED ALWAYS AS ('https://www.youtube.com/watch?v=' || video_id) STORED,
    captions_load_id TEXT REFERENCES loads (load_id),
    alignment_id     BIGINT  -- the alignment in effect (latest)
);

-- Each alignment of a video to game time, with its quality check.
CREATE TABLE alignments (
    alignment_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos (video_id),
    created_at      TIMESTAMPTZ NOT NULL,
    passed          BOOLEAN NOT NULL,                 -- quality check passed: trust video_sec / game_sec
    segments        INTEGER,                         -- play segments (faceoff to faceoff)
    pinned_by_clock INTEGER,                         -- segments with a clock reading
    coverage        DOUBLE PRECISION CHECK (coverage BETWEEN 0 AND 1),
    clock_reversals INTEGER,                         -- readings lower than the one before
    readings        INTEGER,                         -- scoreboard readings with a clock
    video_cuts      INTEGER,
    score_readings  INTEGER,
    score_match     DOUBLE PRECISION CHECK (score_match BETWEEN 0 AND 1),
    frames_cached   INTEGER,
    frames_fetched  INTEGER,
    max_offset_drop DOUBLE PRECISION,                -- older quality check (alignments before 2026-10-03)
    raw_files       JSONB,                           -- the frames' index.json, with its sha256
    code_version    TEXT,
    run_id          BIGINT
);

ALTER TABLE videos ADD FOREIGN KEY (alignment_id) REFERENCES alignments (alignment_id);

-- MoneyPuck play-by-play (gameData/<season>/<game_id>.csv): one row per game event, every
-- column MoneyPuck provides (snake_case), plus when the event happens in the video.
CREATE TABLE events (
    game_id BIGINT NOT NULL REFERENCES games (game_id),
    event_id BIGINT NOT NULL,
    time INTEGER,
    home_win_probability DOUBLE PRECISION,
    home_team_goals INTEGER,
    away_team_goals INTEGER,
    event TEXT,
    event_description_raw TEXT,
    home_percent_of_events_in_offensive_zone DOUBLE PRECISION,
    home_team_shoot_out_goals INTEGER,
    home_team_shoot_out_attempts INTEGER,
    away_team_shoot_out_goals INTEGER,
    away_team_shoot_out_attempts INTEGER,
    lead_win_probability DOUBLE PRECISION,
    tie_game_model_probability DOUBLE PRECISION,
    home_faceoff_win_percentage DOUBLE PRECISION,
    home_share_close_shots DOUBLE PRECISION,
    penalty_time_sum_square INTEGER,
    game_date TEXT,
    pre_game_home_team_win_in_reg_score DOUBLE PRECISION,
    pre_game_home_team_win_in_ot_score DOUBLE PRECISION,
    pre_game_home_team_win_overall_score DOUBLE PRECISION,
    pre_game_away_team_win_in_reg_score DOUBLE PRECISION,
    pre_game_away_team_win_in_ot_score DOUBLE PRECISION,
    pre_game_away_team_win_overall_score DOUBLE PRECISION,
    live_home_team_win_in_reg_score DOUBLE PRECISION,
    live_home_team_win_in_ot_score DOUBLE PRECISION,
    live_home_team_win_overall_score DOUBLE PRECISION,
    live_home_team_expected_points DOUBLE PRECISION,
    live_away_team_win_in_reg_score DOUBLE PRECISION,
    live_away_team_win_in_ot_score DOUBLE PRECISION,
    live_away_team_win_overall_score DOUBLE PRECISION,
    live_away_team_expected_points DOUBLE PRECISION,
    live_home_team_make_round1 DOUBLE PRECISION,
    live_home_team_make_round2 DOUBLE PRECISION,
    live_home_team_make_round3 DOUBLE PRECISION,
    live_home_team_make_round4 DOUBLE PRECISION,
    live_home_team_win_cup DOUBLE PRECISION,
    live_away_team_make_round1 DOUBLE PRECISION,
    live_away_team_make_round2 DOUBLE PRECISION,
    live_away_team_make_round3 DOUBLE PRECISION,
    live_away_team_make_round4 DOUBLE PRECISION,
    live_away_team_win_cup DOUBLE PRECISION,
    x_cord INTEGER,
    y_cord INTEGER,
    x_cord_adjusted INTEGER,
    y_cord_adjusted INTEGER,
    shot_angle DOUBLE PRECISION,
    shot_angle_adjusted DOUBLE PRECISION,
    shot_angle_plus_rebound DOUBLE PRECISION,
    shot_angle_rebound_royal_road BOOLEAN,
    shot_distance DOUBLE PRECISION,
    shot_type TEXT,
    shot_on_empty_net BOOLEAN,
    shot_rebound BOOLEAN,
    shot_angle_plus_rebound_speed DOUBLE PRECISION,
    shot_rush BOOLEAN,
    speed_from_last_event DOUBLE PRECISION,
    last_event_x_cord INTEGER,
    last_event_y_cord INTEGER,
    distance_from_last_event DOUBLE PRECISION,
    last_event_shot_angle DOUBLE PRECISION,
    last_event_shot_distance DOUBLE PRECISION,
    last_event_category TEXT,
    last_event_team TEXT,
    away_penalty1_time_left INTEGER,
    away_penalty1_length INTEGER,
    home_penalty1_time_left INTEGER,
    home_penalty1_length INTEGER,
    shot_goal_probability DOUBLE PRECISION,
    player_position_that_did_event TEXT,
    home_skaters_on_ice INTEGER,
    away_skaters_on_ice INTEGER,
    home_team_expected_goals DOUBLE PRECISION,
    away_team_expected_goals DOUBLE PRECISION,
    home_team_expected_goals_ev DOUBLE PRECISION,
    away_team_expected_goals_ev DOUBLE PRECISION,
    home_team_expected_goals_adj_ev DOUBLE PRECISION,
    away_team_expected_goals_adj_ev DOUBLE PRECISION,
    home_team_expected_goals_ev_non_rebound DOUBLE PRECISION,
    away_team_expected_goals_ev_non_rebound DOUBLE PRECISION,
    home_team_expected_goals_adj_ev_non_rebound DOUBLE PRECISION,
    away_team_expected_goals_adj_ev_non_rebound DOUBLE PRECISION,
    home_team_expected_goals_pp DOUBLE PRECISION,
    away_team_expected_goals_pp DOUBLE PRECISION,
    home_share_of_expected_goals DOUBLE PRECISION,
    away_share_of_expected_goals DOUBLE PRECISION,
    home_share_of_expected_goals_ev DOUBLE PRECISION,
    away_share_of_expected_goals_ev DOUBLE PRECISION,
    home_team_share_of_expected_goals_adj_ev DOUBLE PRECISION,
    away_team_share_of_expected_goals_adj_ev DOUBLE PRECISION,
    home_team_expected_goals_ev_per60 DOUBLE PRECISION,
    away_team_expected_goals_ev_per60 DOUBLE PRECISION,
    home_share_of_expected_goals_pp DOUBLE PRECISION,
    away_share_of_expected_goals_pp DOUBLE PRECISION,
    home_team_expected_goals_pp_per60 DOUBLE PRECISION,
    away_team_expected_goals_pp_per60 DOUBLE PRECISION,
    home_team_share_of_expected_goals_ppp60 DOUBLE PRECISION,
    away_team_share_of_expected_goals_ppp60 DOUBLE PRECISION,
    home_share_of_expected_goals_ev_non_rebound DOUBLE PRECISION,
    away_share_of_expected_goals_ev_non_rebound DOUBLE PRECISION,
    home_team_share_of_expected_goals_adj_ev_non_rebound DOUBLE PRECISION,
    away_team_share_of_expected_goals_adj_ev_non_rebound DOUBLE PRECISION,
    home_team_expected_goals_ev_per60_non_rebound DOUBLE PRECISION,
    away_team_expected_goals_ev_per60_non_rebound DOUBLE PRECISION,
    home_team_expected_goals_including_empty_net DOUBLE PRECISION,
    away_team_expected_goals_including_empty_net DOUBLE PRECISION,
    home_team_total_pp_time DOUBLE PRECISION,
    away_team_total_pp_time DOUBLE PRECISION,
    home_team_expected_goals_adj_ev_non_rebound_per60 DOUBLE PRECISION,
    away_team_expected_goals_adj_ev_non_rebound_per60 DOUBLE PRECISION,
    home_team_expected_goals_adj_ev_per60 DOUBLE PRECISION,
    away_team_expected_goals_adj_ev_per60 DOUBLE PRECISION,
    home_team_share_expected_goals_adj_ev_non_rebound_per60 DOUBLE PRECISION,
    away_team_share_expected_goals_adj_ev_non_rebound_per60 DOUBLE PRECISION,
    home_team_share_expected_goals_adj_ev_per60 DOUBLE PRECISION,
    away_team_share_expected_goals_adj_ev_per60 DOUBLE PRECISION,
    team TEXT,
    replay_id DOUBLE PRECISION,
    sequence INTEGER,
    sequence_scaled_factor DOUBLE PRECISION,
    home_team_expected_goals_ev_sequence DOUBLE PRECISION,
    away_team_expected_goals_ev_sequence DOUBLE PRECISION,
    home_team_expected_goals_ev_sequence_adj DOUBLE PRECISION,
    away_team_expected_goals_ev_sequence_adj DOUBLE PRECISION,
    home_team_expected_goals_pp_sequence DOUBLE PRECISION,
    away_team_expected_goals_pp_sequence DOUBLE PRECISION,
    home_team_expected_goals_sequence DOUBLE PRECISION,
    away_team_expected_goals_sequence DOUBLE PRECISION,
    home_team_share_of_expected_goals_sequence_ev DOUBLE PRECISION,
    away_team_share_of_expected_goals_sequence_ev DOUBLE PRECISION,
    home_team_share_of_expected_goals_sequence_adj_ev DOUBLE PRECISION,
    away_team_share_of_expected_goals_sequence_adj_ev DOUBLE PRECISION,
    home_share_of_expected_goals_pp_sequence_p60 DOUBLE PRECISION,
    away_share_of_expected_goals_pp_sequence_p60 DOUBLE PRECISION,
    shot_from_left_or_right_shooter TEXT,
    time_since_faceoff INTEGER,
    arena_adjusted_x_cord DOUBLE PRECISION,
    arena_adjusted_y_cord DOUBLE PRECISION,
    shot_froze_probability DOUBLE PRECISION,
    shot_rebound_probability DOUBLE PRECISION,
    shot_continue_in_zone_probability DOUBLE PRECISION,
    shot_continue_outside_zone_probability DOUBLE PRECISION,
    shot_play_stopped_probability DOUBLE PRECISION,
    shot_on_goal_probability DOUBLE PRECISION,
    arena_adjusted_shot_distance DOUBLE PRECISION,
    shot_actual_reboundx_goals DOUBLE PRECISION,
    shot_predictedx_goals_from_rebounds DOUBLE PRECISION,
    total_shot_credit_of_shot DOUBLE PRECISION,
    home_team_total_shot_credit DOUBLE PRECISION,
    away_team_total_shot_credit DOUBLE PRECISION,
    home_team_total_shot_credit_ev DOUBLE PRECISION,
    away_team_total_shot_credit_ev DOUBLE PRECISION,
    home_team_total_shot_credit_ev_score_flurry_adjusted DOUBLE PRECISION,
    away_team_total_shot_credit_ev_score_flurry_adjusted DOUBLE PRECISION,
    video_sec DOUBLE PRECISION,                      -- when the event happens in the video (aligned)
    PRIMARY KEY (game_id, event_id)
);

-- MoneyPuck's shot dataset (shots_<season>.zip): one row per shot attempt, every column,
-- including MoneyPuck's xG (x_goal). The same event as events (game_id, event_id); columns that
-- both files have can differ slightly (two separate MoneyPuck datasets).
CREATE TABLE shots (
    game_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    moneypuck_shot_id BIGINT,
    arena_adjusted_shot_distance DOUBLE PRECISION,
    arena_adjusted_x_cord DOUBLE PRECISION,
    arena_adjusted_x_cord_abs DOUBLE PRECISION,
    arena_adjusted_y_cord DOUBLE PRECISION,
    arena_adjusted_y_cord_abs DOUBLE PRECISION,
    average_rest_difference DOUBLE PRECISION,
    away_empty_net BOOLEAN,
    away_penalty1_length INTEGER,
    away_penalty1_time_left INTEGER,
    away_skaters_on_ice INTEGER,
    away_team_code TEXT,
    away_team_goals INTEGER,
    defending_team_average_time_on_ice DOUBLE PRECISION,
    defending_team_average_time_on_ice_of_defencemen DOUBLE PRECISION,
    defending_team_average_time_on_ice_of_defencemen_since_faceoff DOUBLE PRECISION,
    defending_team_average_time_on_ice_of_forwards DOUBLE PRECISION,
    defending_team_average_time_on_ice_of_forwards_since_faceoff DOUBLE PRECISION,
    defending_team_average_time_on_ice_since_faceoff DOUBLE PRECISION,
    defending_team_defencemen_on_ice INTEGER,
    defending_team_forwards_on_ice INTEGER,
    defending_team_max_time_on_ice INTEGER,
    defending_team_max_time_on_ice_of_defencemen INTEGER,
    defending_team_max_time_on_ice_of_defencemen_since_faceoff INTEGER,
    defending_team_max_time_on_ice_of_forwards INTEGER,
    defending_team_max_time_on_ice_of_forwards_since_faceoff INTEGER,
    defending_team_max_time_on_ice_since_faceoff INTEGER,
    defending_team_min_time_on_ice INTEGER,
    defending_team_min_time_on_ice_of_defencemen INTEGER,
    defending_team_min_time_on_ice_of_defencemen_since_faceoff INTEGER,
    defending_team_min_time_on_ice_of_forwards INTEGER,
    defending_team_min_time_on_ice_of_forwards_since_faceoff INTEGER,
    defending_team_min_time_on_ice_since_faceoff INTEGER,
    distance_from_last_event DOUBLE PRECISION,
    event TEXT,
    goal BOOLEAN,
    goalie_id_for_shot BIGINT,
    goalie_name_for_shot TEXT,
    home_empty_net BOOLEAN,
    home_penalty1_length INTEGER,
    home_penalty1_time_left INTEGER,
    home_skaters_on_ice INTEGER,
    home_team_code TEXT,
    home_team_goals INTEGER,
    home_team_won BOOLEAN,
    is_home_team BOOLEAN,
    is_playoff_game BOOLEAN,
    last_event_category TEXT,
    last_event_shot_angle DOUBLE PRECISION,
    last_event_shot_distance DOUBLE PRECISION,
    last_event_team TEXT,
    last_event_x_cord INTEGER,
    last_event_x_cord_adjusted INTEGER,
    last_event_y_cord INTEGER,
    last_event_y_cord_adjusted INTEGER,
    location TEXT,
    off_wing BOOLEAN,
    period INTEGER,
    player_num_that_did_event INTEGER,
    player_num_that_did_last_event INTEGER,
    player_position_that_did_event TEXT,
    season INTEGER,
    shooter_left_right TEXT,
    shooter_name TEXT,
    shooter_player_id BIGINT,
    shooter_time_on_ice INTEGER,
    shooter_time_on_ice_since_faceoff INTEGER,
    shooting_team_average_time_on_ice DOUBLE PRECISION,
    shooting_team_average_time_on_ice_of_defencemen DOUBLE PRECISION,
    shooting_team_average_time_on_ice_of_defencemen_since_faceoff DOUBLE PRECISION,
    shooting_team_average_time_on_ice_of_forwards DOUBLE PRECISION,
    shooting_team_average_time_on_ice_of_forwards_since_faceoff DOUBLE PRECISION,
    shooting_team_average_time_on_ice_since_faceoff DOUBLE PRECISION,
    shooting_team_defencemen_on_ice INTEGER,
    shooting_team_forwards_on_ice INTEGER,
    shooting_team_max_time_on_ice INTEGER,
    shooting_team_max_time_on_ice_of_defencemen INTEGER,
    shooting_team_max_time_on_ice_of_defencemen_since_faceoff INTEGER,
    shooting_team_max_time_on_ice_of_forwards INTEGER,
    shooting_team_max_time_on_ice_of_forwards_since_faceoff INTEGER,
    shooting_team_max_time_on_ice_since_faceoff INTEGER,
    shooting_team_min_time_on_ice INTEGER,
    shooting_team_min_time_on_ice_of_defencemen INTEGER,
    shooting_team_min_time_on_ice_of_defencemen_since_faceoff INTEGER,
    shooting_team_min_time_on_ice_of_forwards INTEGER,
    shooting_team_min_time_on_ice_of_forwards_since_faceoff INTEGER,
    shooting_team_min_time_on_ice_since_faceoff INTEGER,
    shot_angle DOUBLE PRECISION,
    shot_angle_adjusted DOUBLE PRECISION,
    shot_angle_plus_rebound DOUBLE PRECISION,
    shot_angle_plus_rebound_speed DOUBLE PRECISION,
    shot_angle_rebound_royal_road BOOLEAN,
    shot_distance DOUBLE PRECISION,
    shot_generated_rebound BOOLEAN,
    shot_goalie_froze BOOLEAN,
    shot_on_empty_net BOOLEAN,
    shot_play_continued_in_zone BOOLEAN,
    shot_play_continued_outside_zone BOOLEAN,
    shot_play_stopped BOOLEAN,
    shot_rebound BOOLEAN,
    shot_rush BOOLEAN,
    shot_type TEXT,
    shot_was_on_goal BOOLEAN,
    speed_from_last_event DOUBLE PRECISION,
    team TEXT,
    team_code TEXT,
    time INTEGER,
    time_difference_since_change INTEGER,
    time_since_faceoff INTEGER,
    time_since_last_event INTEGER,
    time_until_next_event INTEGER,
    x_cord INTEGER,
    x_cord_adjusted INTEGER,
    x_froze DOUBLE PRECISION,
    x_goal DOUBLE PRECISION,
    x_play_continued_in_zone DOUBLE PRECISION,
    x_play_continued_outside_zone DOUBLE PRECISION,
    x_play_stopped DOUBLE PRECISION,
    x_rebound DOUBLE PRECISION,
    x_shot_was_on_goal DOUBLE PRECISION,
    y_cord INTEGER,
    y_cord_adjusted INTEGER,
    PRIMARY KEY (game_id, event_id),
    FOREIGN KEY (game_id, event_id) REFERENCES events (game_id, event_id),
    CHECK (event IN ('SHOT', 'MISS', 'GOAL'))
);

-- Caption lines of a replay video.
CREATE TABLE captions (
    video_id  TEXT NOT NULL REFERENCES videos (video_id),
    seq       INTEGER NOT NULL,
    start_sec DOUBLE PRECISION NOT NULL,             -- video seconds
    end_sec   DOUBLE PRECISION NOT NULL,
    text      TEXT NOT NULL,
    game_sec  DOUBLE PRECISION,                      -- game time when spoken (aligned)
    live      BOOLEAN,                                -- clock running (false: stoppage, replay)
    PRIMARY KEY (video_id, seq),
    CHECK (end_sec >= start_sec)
);

-- Move the data.
INSERT INTO loads (load_id, kind, game_id, video_id, raw_files, source_urls, fetched_at, loaded_at, code_version, run_id)
SELECT load_id, CASE kind WHEN 'transcript' THEN 'captions' ELSE kind END, game_id, video_id,
       (raw_files)::jsonb, (source_urls)::jsonb, (fetched_at)::timestamptz,
       (loaded_at)::timestamptz, code_version, run_id
FROM loads_v1 WHERE kind <> 'alignment';

INSERT INTO games (game_id, season, game_type, game_date, venue, away_team, home_team, away_name, home_name,
                   away_goals, home_goals, ended_in, load_id, events_load_id, shots_load_id)
SELECT g.game_id, g.season, g.game_type, (g.game_date)::date, g.venue, g.away_team, g.home_team,
       g.away_name, g.home_name, g.away_goals, g.home_goals, g.ended_in, g.load_id,
       (SELECT MIN(e.load_id) FROM events_v1 e WHERE e.game_id = g.game_id),
       (SELECT MIN(s.load_id) FROM shots_v1 s WHERE s.game_id = g.game_id)
FROM games_v1 g;

INSERT INTO videos (video_id, game_id, title, captions_load_id)
SELECT g.video_id, g.game_id, g.video_title, (SELECT MIN(t.load_id) FROM transcript_v1 t WHERE t.video_id = g.video_id)
FROM games_v1 g WHERE g.video_id IS NOT NULL;

INSERT INTO alignments (video_id, created_at, passed, segments, pinned_by_clock, coverage, clock_reversals, readings,
                        video_cuts, score_readings, score_match, frames_cached, frames_fetched, max_offset_drop,
                        raw_files, code_version, run_id)
SELECT video_id, (loaded_at)::timestamptz, (details::jsonb ->> 'passed')::boolean, (details::jsonb ->> 'segments')::integer,
       (details::jsonb ->> 'pinned_by_clock')::integer, (details::jsonb ->> 'coverage')::double precision, (details::jsonb ->> 'clock_reversals')::integer,
       (details::jsonb ->> 'readings')::integer, (details::jsonb ->> 'video_cuts')::integer, (details::jsonb ->> 'score_readings')::integer,
       (details::jsonb ->> 'score_match')::double precision, (details::jsonb ->> 'frames_cached')::integer, (details::jsonb ->> 'frames_fetched')::integer,
       (details::jsonb ->> 'max_offset_drop')::double precision, (raw_files)::jsonb, code_version, run_id
FROM loads_v1
WHERE kind = 'alignment' AND details IS NOT NULL AND video_id IN (SELECT video_id FROM videos)
ORDER BY loaded_at;

UPDATE videos SET alignment_id = (SELECT a.alignment_id FROM alignments a WHERE a.video_id = videos.video_id
                                  ORDER BY a.created_at DESC, a.alignment_id DESC LIMIT 1);

INSERT INTO events (game_id, event_id, time, home_win_probability, home_team_goals, away_team_goals, event, event_description_raw, home_percent_of_events_in_offensive_zone, home_team_shoot_out_goals, home_team_shoot_out_attempts, away_team_shoot_out_goals, away_team_shoot_out_attempts, lead_win_probability, tie_game_model_probability, home_faceoff_win_percentage, home_share_close_shots, penalty_time_sum_square, game_date, pre_game_home_team_win_in_reg_score, pre_game_home_team_win_in_ot_score, pre_game_home_team_win_overall_score, pre_game_away_team_win_in_reg_score, pre_game_away_team_win_in_ot_score, pre_game_away_team_win_overall_score, live_home_team_win_in_reg_score, live_home_team_win_in_ot_score, live_home_team_win_overall_score, live_home_team_expected_points, live_away_team_win_in_reg_score, live_away_team_win_in_ot_score, live_away_team_win_overall_score, live_away_team_expected_points, live_home_team_make_round1, live_home_team_make_round2, live_home_team_make_round3, live_home_team_make_round4, live_home_team_win_cup, live_away_team_make_round1, live_away_team_make_round2, live_away_team_make_round3, live_away_team_make_round4, live_away_team_win_cup, x_cord, y_cord, x_cord_adjusted, y_cord_adjusted, shot_angle, shot_angle_adjusted, shot_angle_plus_rebound, shot_angle_rebound_royal_road, shot_distance, shot_type, shot_on_empty_net, shot_rebound, shot_angle_plus_rebound_speed, shot_rush, speed_from_last_event, last_event_x_cord, last_event_y_cord, distance_from_last_event, last_event_shot_angle, last_event_shot_distance, last_event_category, last_event_team, away_penalty1_time_left, away_penalty1_length, home_penalty1_time_left, home_penalty1_length, shot_goal_probability, player_position_that_did_event, home_skaters_on_ice, away_skaters_on_ice, home_team_expected_goals, away_team_expected_goals, home_team_expected_goals_ev, away_team_expected_goals_ev, home_team_expected_goals_adj_ev, away_team_expected_goals_adj_ev, home_team_expected_goals_ev_non_rebound, away_team_expected_goals_ev_non_rebound, home_team_expected_goals_adj_ev_non_rebound, away_team_expected_goals_adj_ev_non_rebound, home_team_expected_goals_pp, away_team_expected_goals_pp, home_share_of_expected_goals, away_share_of_expected_goals, home_share_of_expected_goals_ev, away_share_of_expected_goals_ev, home_team_share_of_expected_goals_adj_ev, away_team_share_of_expected_goals_adj_ev, home_team_expected_goals_ev_per60, away_team_expected_goals_ev_per60, home_share_of_expected_goals_pp, away_share_of_expected_goals_pp, home_team_expected_goals_pp_per60, away_team_expected_goals_pp_per60, home_team_share_of_expected_goals_ppp60, away_team_share_of_expected_goals_ppp60, home_share_of_expected_goals_ev_non_rebound, away_share_of_expected_goals_ev_non_rebound, home_team_share_of_expected_goals_adj_ev_non_rebound, away_team_share_of_expected_goals_adj_ev_non_rebound, home_team_expected_goals_ev_per60_non_rebound, away_team_expected_goals_ev_per60_non_rebound, home_team_expected_goals_including_empty_net, away_team_expected_goals_including_empty_net, home_team_total_pp_time, away_team_total_pp_time, home_team_expected_goals_adj_ev_non_rebound_per60, away_team_expected_goals_adj_ev_non_rebound_per60, home_team_expected_goals_adj_ev_per60, away_team_expected_goals_adj_ev_per60, home_team_share_expected_goals_adj_ev_non_rebound_per60, away_team_share_expected_goals_adj_ev_non_rebound_per60, home_team_share_expected_goals_adj_ev_per60, away_team_share_expected_goals_adj_ev_per60, team, replay_id, sequence, sequence_scaled_factor, home_team_expected_goals_ev_sequence, away_team_expected_goals_ev_sequence, home_team_expected_goals_ev_sequence_adj, away_team_expected_goals_ev_sequence_adj, home_team_expected_goals_pp_sequence, away_team_expected_goals_pp_sequence, home_team_expected_goals_sequence, away_team_expected_goals_sequence, home_team_share_of_expected_goals_sequence_ev, away_team_share_of_expected_goals_sequence_ev, home_team_share_of_expected_goals_sequence_adj_ev, away_team_share_of_expected_goals_sequence_adj_ev, home_share_of_expected_goals_pp_sequence_p60, away_share_of_expected_goals_pp_sequence_p60, shot_from_left_or_right_shooter, time_since_faceoff, arena_adjusted_x_cord, arena_adjusted_y_cord, shot_froze_probability, shot_rebound_probability, shot_continue_in_zone_probability, shot_continue_outside_zone_probability, shot_play_stopped_probability, shot_on_goal_probability, arena_adjusted_shot_distance, shot_actual_reboundx_goals, shot_predictedx_goals_from_rebounds, total_shot_credit_of_shot, home_team_total_shot_credit, away_team_total_shot_credit, home_team_total_shot_credit_ev, away_team_total_shot_credit_ev, home_team_total_shot_credit_ev_score_flurry_adjusted, away_team_total_shot_credit_ev_score_flurry_adjusted, video_sec)
SELECT game_id, event_id, time, homeWinProbability, homeTeamGoals, awayTeamGoals, event, eventDescriptionRaw, homePercentOfEventsInOffensiveZone, homeTeamShootOutGoals, homeTeamShootOutAttempts, awayTeamShootOutGoals, awayTeamShootOutAttempts, leadWinProbability, tieGameModelProbability, homeFaceoffWinPercentage, homeShareCloseShots, penaltyTimeSumSquare, game_date, preGameHomeTeamWinInRegScore, preGameHomeTeamWinInOTScore, preGameHomeTeamWinOverallScore, preGameAwayTeamWinInRegScore, preGameAwayTeamWinInOTScore, preGameAwayTeamWinOverallScore, liveHomeTeamWinInRegScore, liveHomeTeamWinInOTScore, liveHomeTeamWinOverallScore, liveHomeTeamExpectedPoints, liveAwayTeamWinInRegScore, liveAwayTeamWinInOTScore, liveAwayTeamWinOverallScore, liveAwayTeamExpectedPoints, liveHomeTeamMakeRound1, liveHomeTeamMakeRound2, liveHomeTeamMakeRound3, liveHomeTeamMakeRound4, liveHomeTeamWinCup, liveAwayTeamMakeRound1, liveAwayTeamMakeRound2, liveAwayTeamMakeRound3, liveAwayTeamMakeRound4, liveAwayTeamWinCup, xCord, yCord, xCordAdjusted, yCordAdjusted, shotAngle, shotAngleAdjusted, shotAnglePlusRebound, (shotAngleReboundRoyalRoad)::boolean, shotDistance, shotType, (shotOnEmptyNet)::boolean, (shotRebound)::boolean, shotAnglePlusReboundSpeed, (shotRush)::boolean, speedFromLastEvent, lastEventxCord, lastEventyCord, distanceFromLastEvent, lastEventShotAngle, lastEventShotDistance, lastEventCategory, lastEventTeam, awayPenalty1TimeLeft, awayPenalty1Length, homePenalty1TimeLeft, homePenalty1Length, shotGoalProbability, playerPositionThatDidEvent, homeSkatersOnIce, awaySkatersOnIce, homeTeamExpectedGoals, awayTeamExpectedGoals, homeTeamExpectedGoalsEV, awayTeamExpectedGoalsEV, homeTeamExpectedGoalsAdjEV, awayTeamExpectedGoalsAdjEV, homeTeamExpectedGoalsEVNonRebound, awayTeamExpectedGoalsEVNonRebound, homeTeamExpectedGoalsAdjEVNonRebound, awayTeamExpectedGoalsAdjEVNonRebound, homeTeamExpectedGoalsPP, awayTeamExpectedGoalsPP, homeShareOfExpectedGoals, awayShareOfExpectedGoals, homeShareOfExpectedGoalsEV, awayShareOfExpectedGoalsEV, homeTeamShareOfExpectedGoalsAdjEV, awayTeamShareOfExpectedGoalsAdjEV, homeTeamExpectedGoalsEVPer60, awayTeamExpectedGoalsEVPer60, homeShareOfExpectedGoalsPP, awayShareOfExpectedGoalsPP, homeTeamExpectedGoalsPPPer60, awayTeamExpectedGoalsPPPer60, homeTeamShareOfExpectedGoalsPPP60, awayTeamShareOfExpectedGoalsPPP60, homeShareOfExpectedGoalsEVNonRebound, awayShareOfExpectedGoalsEVNonRebound, homeTeamShareOfExpectedGoalsAdjEVNonRebound, awayTeamShareOfExpectedGoalsAdjEVNonRebound, homeTeamExpectedGoalsEVPer60NonRebound, awayTeamExpectedGoalsEVPer60NonRebound, homeTeamExpectedGoalsIncludingEmptyNet, awayTeamExpectedGoalsIncludingEmptyNet, homeTeamTotalPPTime, awayTeamTotalPPTime, homeTeamExpectedGoalsAdjEVNonReboundPer60, awayTeamExpectedGoalsAdjEVNonReboundPer60, homeTeamExpectedGoalsAdjEVPer60, awayTeamExpectedGoalsAdjEVPer60, homeTeamShareExpectedGoalsAdjEVNonReboundPer60, awayTeamShareExpectedGoalsAdjEVNonReboundPer60, homeTeamShareExpectedGoalsAdjEVPer60, awayTeamShareExpectedGoalsAdjEVPer60, team, replayId, sequence, sequenceScaledFactor, homeTeamExpectedGoalsEVSequence, awayTeamExpectedGoalsEVSequence, homeTeamExpectedGoalsEVSequenceAdj, awayTeamExpectedGoalsEVSequenceAdj, homeTeamExpectedGoalsPPSequence, awayTeamExpectedGoalsPPSequence, homeTeamExpectedGoalsSequence, awayTeamExpectedGoalsSequence, homeTeamShareOfExpectedGoalsSequenceEV, awayTeamShareOfExpectedGoalsSequenceEV, homeTeamShareOfExpectedGoalsSequenceAdjEV, awayTeamShareOfExpectedGoalsSequenceAdjEV, homeShareOfExpectedGoalsPPSequenceP60, awayShareOfExpectedGoalsPPSequenceP60, shotFromLeftOrRightShooter, timeSinceFaceoff, arenaAdjustedXCord, arenaAdjustedYCord, shotFrozeProbability, shotReboundProbability, shotContinueInZoneProbability, shotContinueOutsideZoneProbability, shotPlayStoppedProbability, shotOnGoalProbability, arenaAdjustedShotDistance, shotActualReboundxGoals, shotPredictedxGoalsFromRebounds, totalShotCreditOfShot, homeTeamTotalShotCredit, awayTeamTotalShotCredit, homeTeamTotalShotCreditEV, awayTeamTotalShotCreditEV, homeTeamTotalShotCreditEVScoreFlurryAdjusted, awayTeamTotalShotCreditEVScoreFlurryAdjusted, calibrated_transcript_time
FROM events_v1;

INSERT INTO shots (game_id, event_id, moneypuck_shot_id, arena_adjusted_shot_distance, arena_adjusted_x_cord, arena_adjusted_x_cord_abs, arena_adjusted_y_cord, arena_adjusted_y_cord_abs, average_rest_difference, away_empty_net, away_penalty1_length, away_penalty1_time_left, away_skaters_on_ice, away_team_code, away_team_goals, defending_team_average_time_on_ice, defending_team_average_time_on_ice_of_defencemen, defending_team_average_time_on_ice_of_defencemen_since_faceoff, defending_team_average_time_on_ice_of_forwards, defending_team_average_time_on_ice_of_forwards_since_faceoff, defending_team_average_time_on_ice_since_faceoff, defending_team_defencemen_on_ice, defending_team_forwards_on_ice, defending_team_max_time_on_ice, defending_team_max_time_on_ice_of_defencemen, defending_team_max_time_on_ice_of_defencemen_since_faceoff, defending_team_max_time_on_ice_of_forwards, defending_team_max_time_on_ice_of_forwards_since_faceoff, defending_team_max_time_on_ice_since_faceoff, defending_team_min_time_on_ice, defending_team_min_time_on_ice_of_defencemen, defending_team_min_time_on_ice_of_defencemen_since_faceoff, defending_team_min_time_on_ice_of_forwards, defending_team_min_time_on_ice_of_forwards_since_faceoff, defending_team_min_time_on_ice_since_faceoff, distance_from_last_event, event, goal, goalie_id_for_shot, goalie_name_for_shot, home_empty_net, home_penalty1_length, home_penalty1_time_left, home_skaters_on_ice, home_team_code, home_team_goals, home_team_won, is_home_team, is_playoff_game, last_event_category, last_event_shot_angle, last_event_shot_distance, last_event_team, last_event_x_cord, last_event_x_cord_adjusted, last_event_y_cord, last_event_y_cord_adjusted, location, off_wing, period, player_num_that_did_event, player_num_that_did_last_event, player_position_that_did_event, season, shooter_left_right, shooter_name, shooter_player_id, shooter_time_on_ice, shooter_time_on_ice_since_faceoff, shooting_team_average_time_on_ice, shooting_team_average_time_on_ice_of_defencemen, shooting_team_average_time_on_ice_of_defencemen_since_faceoff, shooting_team_average_time_on_ice_of_forwards, shooting_team_average_time_on_ice_of_forwards_since_faceoff, shooting_team_average_time_on_ice_since_faceoff, shooting_team_defencemen_on_ice, shooting_team_forwards_on_ice, shooting_team_max_time_on_ice, shooting_team_max_time_on_ice_of_defencemen, shooting_team_max_time_on_ice_of_defencemen_since_faceoff, shooting_team_max_time_on_ice_of_forwards, shooting_team_max_time_on_ice_of_forwards_since_faceoff, shooting_team_max_time_on_ice_since_faceoff, shooting_team_min_time_on_ice, shooting_team_min_time_on_ice_of_defencemen, shooting_team_min_time_on_ice_of_defencemen_since_faceoff, shooting_team_min_time_on_ice_of_forwards, shooting_team_min_time_on_ice_of_forwards_since_faceoff, shooting_team_min_time_on_ice_since_faceoff, shot_angle, shot_angle_adjusted, shot_angle_plus_rebound, shot_angle_plus_rebound_speed, shot_angle_rebound_royal_road, shot_distance, shot_generated_rebound, shot_goalie_froze, shot_on_empty_net, shot_play_continued_in_zone, shot_play_continued_outside_zone, shot_play_stopped, shot_rebound, shot_rush, shot_type, shot_was_on_goal, speed_from_last_event, team, team_code, time, time_difference_since_change, time_since_faceoff, time_since_last_event, time_until_next_event, x_cord, x_cord_adjusted, x_froze, x_goal, x_play_continued_in_zone, x_play_continued_outside_zone, x_play_stopped, x_rebound, x_shot_was_on_goal, y_cord, y_cord_adjusted)
SELECT game_id, event_id, moneypuck_shot_id, arenaAdjustedShotDistance, arenaAdjustedXCord, arenaAdjustedXCordABS, arenaAdjustedYCord, arenaAdjustedYCordAbs, averageRestDifference, (awayEmptyNet)::boolean, awayPenalty1Length, awayPenalty1TimeLeft, awaySkatersOnIce, awayTeamCode, awayTeamGoals, defendingTeamAverageTimeOnIce, defendingTeamAverageTimeOnIceOfDefencemen, defendingTeamAverageTimeOnIceOfDefencemenSinceFaceoff, defendingTeamAverageTimeOnIceOfForwards, defendingTeamAverageTimeOnIceOfForwardsSinceFaceoff, defendingTeamAverageTimeOnIceSinceFaceoff, defendingTeamDefencemenOnIce, defendingTeamForwardsOnIce, defendingTeamMaxTimeOnIce, defendingTeamMaxTimeOnIceOfDefencemen, defendingTeamMaxTimeOnIceOfDefencemenSinceFaceoff, defendingTeamMaxTimeOnIceOfForwards, defendingTeamMaxTimeOnIceOfForwardsSinceFaceoff, defendingTeamMaxTimeOnIceSinceFaceoff, defendingTeamMinTimeOnIce, defendingTeamMinTimeOnIceOfDefencemen, defendingTeamMinTimeOnIceOfDefencemenSinceFaceoff, defendingTeamMinTimeOnIceOfForwards, defendingTeamMinTimeOnIceOfForwardsSinceFaceoff, defendingTeamMinTimeOnIceSinceFaceoff, distanceFromLastEvent, event, (goal)::boolean, goalieIdForShot, goalieNameForShot, (homeEmptyNet)::boolean, homePenalty1Length, homePenalty1TimeLeft, homeSkatersOnIce, homeTeamCode, homeTeamGoals, (homeTeamWon)::boolean, (isHomeTeam)::boolean, (isPlayoffGame)::boolean, lastEventCategory, lastEventShotAngle, lastEventShotDistance, lastEventTeam, lastEventxCord, lastEventxCord_adjusted, lastEventyCord, lastEventyCord_adjusted, location, (offWing)::boolean, period, playerNumThatDidEvent, playerNumThatDidLastEvent, playerPositionThatDidEvent, season, shooterLeftRight, shooterName, shooterPlayerId, shooterTimeOnIce, shooterTimeOnIceSinceFaceoff, shootingTeamAverageTimeOnIce, shootingTeamAverageTimeOnIceOfDefencemen, shootingTeamAverageTimeOnIceOfDefencemenSinceFaceoff, shootingTeamAverageTimeOnIceOfForwards, shootingTeamAverageTimeOnIceOfForwardsSinceFaceoff, shootingTeamAverageTimeOnIceSinceFaceoff, shootingTeamDefencemenOnIce, shootingTeamForwardsOnIce, shootingTeamMaxTimeOnIce, shootingTeamMaxTimeOnIceOfDefencemen, shootingTeamMaxTimeOnIceOfDefencemenSinceFaceoff, shootingTeamMaxTimeOnIceOfForwards, shootingTeamMaxTimeOnIceOfForwardsSinceFaceoff, shootingTeamMaxTimeOnIceSinceFaceoff, shootingTeamMinTimeOnIce, shootingTeamMinTimeOnIceOfDefencemen, shootingTeamMinTimeOnIceOfDefencemenSinceFaceoff, shootingTeamMinTimeOnIceOfForwards, shootingTeamMinTimeOnIceOfForwardsSinceFaceoff, shootingTeamMinTimeOnIceSinceFaceoff, shotAngle, shotAngleAdjusted, shotAnglePlusRebound, shotAnglePlusReboundSpeed, (shotAngleReboundRoyalRoad)::boolean, shotDistance, (shotGeneratedRebound)::boolean, (shotGoalieFroze)::boolean, (shotOnEmptyNet)::boolean, (shotPlayContinuedInZone)::boolean, (shotPlayContinuedOutsideZone)::boolean, (shotPlayStopped)::boolean, (shotRebound)::boolean, (shotRush)::boolean, shotType, (shotWasOnGoal)::boolean, speedFromLastEvent, team, teamCode, time, timeDifferenceSinceChange, timeSinceFaceoff, timeSinceLastEvent, timeUntilNextEvent, xCord, xCordAdjusted, xFroze, xGoal, xPlayContinuedInZone, xPlayContinuedOutsideZone, xPlayStopped, xRebound, xShotWasOnGoal, yCord, yCordAdjusted
FROM shots_v1;

INSERT INTO captions (video_id, seq, start_sec, end_sec, text, game_sec, live)
SELECT video_id, seq, start_sec, end_sec, text, calibrated_game_time, (in_play)::boolean
FROM transcript_v1;

DROP TABLE shots_v1;
DROP TABLE events_v1;
DROP TABLE transcript_v1;
DROP TABLE games_v1;
DROP TABLE loads_v1;

CREATE INDEX ix_games_load ON games (load_id);
CREATE INDEX ix_games_events_load ON games (events_load_id);
CREATE INDEX ix_games_shots_load ON games (shots_load_id);
CREATE INDEX ix_videos_captions_load ON videos (captions_load_id);
CREATE INDEX ix_videos_alignment ON videos (alignment_id);
CREATE INDEX ix_alignments_video ON alignments (video_id, created_at);
CREATE INDEX ix_loads_game ON loads (game_id, kind);
CREATE INDEX ix_shots_team ON shots (game_id, team_code);
CREATE INDEX ix_shots_shooter ON shots (shooter_player_id);
CREATE INDEX ix_captions_time ON captions (video_id, start_sec);

-- Events and captions interleaved on one timeline, like a script of the broadcast.
CREATE VIEW timeline WITH (security_invoker = true) AS
SELECT game_id, video_sec, game_sec, live, event_description, text FROM (
    SELECT e.game_id, e.video_sec, e.time AS game_sec, NULL::boolean AS live,
           e.event_description_raw AS event_description, NULL::text AS text, 0 AS is_caption
    FROM events e
    WHERE e.video_sec IS NOT NULL AND TRIM(COALESCE(e.event_description_raw, '')) <> ''
    UNION ALL
    SELECT v.game_id, c.start_sec, c.game_sec, c.live, NULL, c.text, 1
    FROM captions c JOIN videos v USING (video_id)
) t
ORDER BY game_id, video_sec, is_caption;

-- Each shot in a video whose alignment passed: its xG, a link to the moment (5 s early, for
-- the build-up), and the captions overlapping 4 s before to 10 s after it.
CREATE VIEW shot_commentary WITH (security_invoker = true) AS
SELECT
    s.game_id,
    s.event_id,
    s.period,
    format('%s:%s', (s.time - (s.period - 1) * 1200) / 60, lpad(((s.time - (s.period - 1) * 1200) % 60)::text, 2, '0')) AS clock,
    s.team_code AS team,
    s.shooter_name AS shooter,
    s.event AS result,
    s.shot_type AS shot_type,
    ROUND(s.shot_distance::numeric, 1) AS distance_ft,
    ROUND(s.x_goal::numeric, 3) AS xg,
    format('%s:%s', trunc(e.video_sec)::int / 60, lpad((trunc(e.video_sec)::int % 60)::text, 2, '0')) AS video_time,
    v.url || '&t=' || GREATEST(trunc(e.video_sec)::int - 5, 0) || 's' AS link,
    (SELECT string_agg(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.end_sec >= e.video_sec - 4
        AND c.start_sec <= e.video_sec + 10) AS commentary
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed AND e.video_sec IS NOT NULL;

ALTER TABLE loads ENABLE ROW LEVEL SECURITY;
ALTER TABLE games ENABLE ROW LEVEL SECURITY;
ALTER TABLE videos ENABLE ROW LEVEL SECURITY;
ALTER TABLE alignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE shots ENABLE ROW LEVEL SECURITY;
ALTER TABLE captions ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE games IS 'One row per NHL game: teams, date, final score; which loads supplied it.';
COMMENT ON TABLE videos IS 'A game''s full replay on YouTube (at most one per game); alignment_id is the alignment in effect.';
COMMENT ON TABLE alignments IS 'Each alignment of a video to game time, with its quality check; passed = trust video_sec and game_sec.';
COMMENT ON TABLE events IS 'MoneyPuck play-by-play, every column (snake_case), plus video_sec: when the event happens in the video.';
COMMENT ON TABLE shots IS 'MoneyPuck shot dataset, every column, including x_goal (xG). Joins events on (game_id, event_id).';
COMMENT ON TABLE captions IS 'Caption lines of a replay: start_sec/end_sec in video seconds, game_sec and live once aligned.';
COMMENT ON TABLE loads IS 'Provenance: one row per batch of data loaded (source URLs, raw files and hashes, code version, run).';
