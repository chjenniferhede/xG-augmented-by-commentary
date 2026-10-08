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
CREATE VIEW shot_commentary AS
SELECT
    s.game_id,
    s.event_id,
    s.period,
    printf('%d:%02d', (s.time - (s.period - 1) * 1200) / 60, (s.time - (s.period - 1) * 1200) % 60) AS clock,
    s.team_code AS team,
    s.shooter_name AS shooter,
    s.event AS result,
    s.shot_type AS shot_type,
    ROUND(s.shot_distance, 1) AS distance_ft,
    ROUND(s.x_goal, 3) AS xg,
    printf('%d:%02d', CAST(e.video_sec AS INTEGER) / 60, CAST(e.video_sec AS INTEGER) % 60) AS video_time,
    v.url || '&t=' || MAX(CAST(e.video_sec AS INTEGER) - 5, 0) || 's' AS link,
    (SELECT group_concat(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.end_sec >= e.video_sec - 30
        AND c.start_sec <= e.video_sec + CASE WHEN s.event = 'GOAL' THEN 75 ELSE 30 END) AS commentary
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed = 1 AND e.video_sec IS NOT NULL;
