-- shot_commentary: a wider commentary window, measured from the data (2026-10-06).
--
-- Across 1,055 shots in the 10 videos whose alignment passed, lines naming the shooter rise above
-- their background rate from about 30 s before the shot (the build-up) to about 30 s after it, and
-- for goals until about 60-70 s after (celebration, replays, talk about the scorer). The old
-- window (4 s before, 10 s after) caught only the peak. The commentary is read by an LLM, so the
-- window favours coverage: 30 s before; 30 s after a save, miss or block, 75 s after a goal.
-- Caption lines overlapping the window are included (they are 6 s long on average).

DROP VIEW IF EXISTS shot_commentary;

-- Each shot in a video whose alignment passed: its xG, a link to the moment (5 s early, for
-- the build-up), and the captions from 30 s before to 30 s after it (75 s after a goal).
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
        AND c.end_sec >= e.video_sec - 30
        AND c.start_sec <= e.video_sec + CASE WHEN s.event = 'GOAL' THEN 75 ELSE 30 END) AS commentary
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed AND e.video_sec IS NOT NULL;
