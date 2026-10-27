-- Commentary-adjusted xG. See 0005_commentary_xg.postgres.sql for the full note; this is the
-- SQLite form used by the tests and offline work (TEXT for JSON, group_concat, no RLS).

CREATE VIEW shot_commentary_split AS
SELECT
    s.game_id,
    s.event_id,
    v.video_id,
    s.period,
    printf('%d:%02d', (s.time - (s.period - 1) * 1200) / 60, (s.time - (s.period - 1) * 1200) % 60) AS clock,
    s.team_code AS team,
    s.shooter_name AS shooter,
    s.event AS result,
    s.goal,
    s.shot_type,
    ROUND(s.shot_distance, 1) AS distance_ft,
    ROUND(s.shot_angle, 1) AS angle_deg,
    s.shot_rebound,
    s.shot_rush,
    s.x_goal,
    e.video_sec,
    printf('%d:%02d', CAST(e.video_sec AS INTEGER) / 60, CAST(e.video_sec AS INTEGER) % 60) AS video_time,
    v.url || '&t=' || MAX(CAST(e.video_sec AS INTEGER) - 5, 0) || 's' AS link,
    (SELECT group_concat(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.end_sec >= e.video_sec - 30
        AND c.start_sec <= e.video_sec + 1.5) AS commentary_pre,
    (SELECT group_concat(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.start_sec > e.video_sec + 1.5
        AND c.start_sec <= e.video_sec + CASE WHEN s.event = 'GOAL' THEN 75 ELSE 30 END) AS commentary_post
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed = 1 AND e.video_sec IS NOT NULL;

CREATE TABLE commentary_xg (
    game_id                INTEGER NOT NULL,
    event_id               INTEGER NOT NULL,
    video_id               TEXT NOT NULL REFERENCES videos (video_id),

    x_goal                 REAL,
    xg_adjusted_pre        REAL,
    xg_adjusted_full       REAL,
    delta_pre              REAL,
    delta_full             REAL,

    features               TEXT NOT NULL,
    summary                TEXT,
    explanation            TEXT,

    relevance              REAL,
    attribution_confidence REAL,
    commentary_quality     TEXT CHECK (commentary_quality IN ('clear', 'noisy', 'absent', 'unrelated')),
    outcome_leaked         INTEGER NOT NULL DEFAULT 0,
    leak_note              TEXT,

    youtube_url            TEXT,
    commentary_pre         TEXT,
    commentary_post        TEXT,

    provider               TEXT,
    model                  TEXT,
    prompt_version         TEXT,
    coeff_version          TEXT,
    calib_version          TEXT,
    input_hash             TEXT NOT NULL,
    tokens_in              INTEGER,
    tokens_out             INTEGER,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    PRIMARY KEY (game_id, event_id),
    FOREIGN KEY (game_id, event_id) REFERENCES shots (game_id, event_id),
    CHECK (relevance IS NULL OR (relevance >= 0 AND relevance <= 1))
);

CREATE INDEX commentary_xg_delta_idx ON commentary_xg (abs(delta_pre) DESC);
CREATE INDEX commentary_xg_game_idx ON commentary_xg (game_id);

CREATE TABLE calibrations (
    calib_version TEXT PRIMARY KEY,
    alpha         REAL NOT NULL,
    beta          REAL NOT NULL,
    n_shots       INTEGER NOT NULL,
    n_goals       INTEGER,
    brier_base    REAL,
    brier_adj     REAL,
    logloss_base  REAL,
    logloss_adj   REAL,
    reliability   TEXT,
    coeff_version TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
