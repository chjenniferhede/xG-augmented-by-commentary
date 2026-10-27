-- Commentary-adjusted xG: what the broadcast said about a shot, as numbers.
--
-- Public xG models (MoneyPuck's included) are built from the NHL's public play-by-play, which has
-- location, angle, shot type, rebound/rush flags, strength and empty net -- but no screens, no
-- pre-shot passing, no goalie position and no shooter pressure. Those are the largest known gaps,
-- and they are what commentators say out loud. annotate/ extracts them into `features` and a
-- coefficient table turns them into an xG adjustment.
--
-- Two adjusted numbers, because the commentary window runs past the shot and contains the call:
--   xg_adjusted_pre   from commentary up to +1.5 s only. Honest; this is the one scored against goals.
--   xg_adjusted_full  also uses post-shot replay talk, with the outcome itself banned. Descriptive.

-- Pre- and post-shot commentary as separate columns, so the annotator can keep the outcome out of
-- the number it predicts. Same window and link as shot_commentary (0003).
CREATE VIEW shot_commentary_split WITH (security_invoker = true) AS
SELECT
    s.game_id,
    s.event_id,
    v.video_id,
    s.period,
    format('%s:%s', (s.time - (s.period - 1) * 1200) / 60, lpad(((s.time - (s.period - 1) * 1200) % 60)::text, 2, '0')) AS clock,
    s.team_code AS team,
    s.shooter_name AS shooter,
    s.event AS result,
    s.goal,
    s.shot_type,
    ROUND(s.shot_distance::numeric, 1) AS distance_ft,
    ROUND(s.shot_angle::numeric, 1) AS angle_deg,
    s.shot_rebound,
    s.shot_rush,
    s.x_goal,
    e.video_sec,
    format('%s:%s', trunc(e.video_sec)::int / 60, lpad((trunc(e.video_sec)::int % 60)::text, 2, '0')) AS video_time,
    v.url || '&t=' || GREATEST(trunc(e.video_sec)::int - 5, 0) || 's' AS link,
    (SELECT string_agg(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.end_sec >= e.video_sec - 30
        AND c.start_sec <= e.video_sec + 1.5) AS commentary_pre,
    (SELECT string_agg(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.start_sec > e.video_sec + 1.5
        AND c.start_sec <= e.video_sec + CASE WHEN s.event = 'GOAL' THEN 75 ELSE 30 END) AS commentary_post
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed AND e.video_sec IS NOT NULL;

-- One row per annotated shot. `features` is the model's structured output, kept so the xG can be
-- recomputed against a new coefficient table without calling the model again.
CREATE TABLE commentary_xg (
    game_id                BIGINT NOT NULL,
    event_id               BIGINT NOT NULL,
    video_id               TEXT NOT NULL REFERENCES videos (video_id),

    x_goal                 DOUBLE PRECISION,   -- MoneyPuck's xG, snapshotted at annotation time
    xg_adjusted_pre        DOUBLE PRECISION,
    xg_adjusted_full       DOUBLE PRECISION,
    delta_pre              DOUBLE PRECISION,   -- xg_adjusted_pre - x_goal
    delta_full             DOUBLE PRECISION,

    features               JSONB NOT NULL,     -- [{code, confidence, source, quote, corrected_value?}]
    summary                TEXT,               -- the model's one-line plain-English description
    explanation            TEXT,               -- generated from the arithmetic, not by the model

    relevance              DOUBLE PRECISION,   -- 0-1: is this commentary about THIS shot at all
    attribution_confidence DOUBLE PRECISION,
    commentary_quality     TEXT CHECK (commentary_quality IN ('clear', 'noisy', 'absent', 'unrelated')),
    outcome_leaked         BOOLEAN NOT NULL DEFAULT FALSE,
    leak_note              TEXT,

    youtube_url            TEXT,               -- snapshotted, so rows stay linkable if realigned
    commentary_pre         TEXT,
    commentary_post        TEXT,

    provider               TEXT,
    model                  TEXT,
    prompt_version         TEXT,
    coeff_version          TEXT,
    calib_version          TEXT,
    input_hash             TEXT NOT NULL,      -- unchanged hash = skip on re-run
    tokens_in              INTEGER,
    tokens_out             INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (game_id, event_id),
    FOREIGN KEY (game_id, event_id) REFERENCES shots (game_id, event_id),
    CHECK (relevance IS NULL OR (relevance >= 0 AND relevance <= 1))
);

CREATE INDEX commentary_xg_delta_idx ON commentary_xg (abs(delta_pre) DESC);
CREATE INDEX commentary_xg_game_idx ON commentary_xg (game_id);

-- One row per calibration run: the alpha/beta applied, and how the adjusted xG scored against
-- real goals versus MoneyPuck's baseline.
CREATE TABLE calibrations (
    calib_version TEXT PRIMARY KEY,
    alpha         DOUBLE PRECISION NOT NULL,
    beta          DOUBLE PRECISION NOT NULL,
    n_shots       INTEGER NOT NULL,
    n_goals       INTEGER,
    brier_base    DOUBLE PRECISION,
    brier_adj     DOUBLE PRECISION,
    logloss_base  DOUBLE PRECISION,
    logloss_adj   DOUBLE PRECISION,
    reliability   JSONB,          -- 10 bins: [{bin, n, mean_pred_base, mean_pred_adj, observed}]
    coeff_version TEXT,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE commentary_xg ENABLE ROW LEVEL SECURITY;
ALTER TABLE calibrations  ENABLE ROW LEVEL SECURITY;

-- Read access for xg_reader, per 0004 (new tables need their own policy; the SELECT grant comes
-- from that migration's default privileges). Does nothing if the role was never created.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xg_reader') THEN
        GRANT SELECT ON commentary_xg, calibrations, shot_commentary_split TO xg_reader;
        CREATE POLICY reader_select ON commentary_xg FOR SELECT TO xg_reader USING (true);
        CREATE POLICY reader_select ON calibrations  FOR SELECT TO xg_reader USING (true);
    END IF;
END $$;
