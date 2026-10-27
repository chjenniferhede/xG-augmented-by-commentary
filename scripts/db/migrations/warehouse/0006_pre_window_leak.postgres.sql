-- Tighten commentary_pre so the goal call stops leaking into it.
--
-- 0005 cut the pre-shot window at `start_sec <= video_sec + 1.5`. Caption lines run ~6 s, so a
-- line that *starts* just before the shot still carries several seconds of what came after it --
-- including "scores!". Measured over all 1,469 shots, 4.4% of pre-windows contained an outcome
-- word (scores / goal! / what a save / denied / off the post).
--
-- Cutting on `end_sec` instead -- only lines that have fully finished by shot+1.5 s -- drops that
-- to 1.2%, costing 18% of the context (499 -> 409 chars on average). Worth it: the pre-shot number
-- is the one scored against real goals, so its honesty matters more than its richness. The line in
-- progress at the moment of the shot is lost, but that is the same line that holds the call, and
-- caption timing is line-level, so it cannot be split. The residual 1.2% is handled by the
-- annotator's outcome ban and its outcome_leaked flag.

DROP VIEW IF EXISTS shot_commentary_split;

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
        AND c.end_sec <= e.video_sec + 1.5) AS commentary_pre,
    (SELECT string_agg(c.text, ' ' ORDER BY c.start_sec)
       FROM captions c
      WHERE c.video_id = v.video_id
        AND c.end_sec > e.video_sec + 1.5
        AND c.start_sec <= e.video_sec + CASE WHEN s.event = 'GOAL' THEN 75 ELSE 30 END) AS commentary_post
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN videos v ON v.game_id = s.game_id
JOIN alignments a ON a.alignment_id = v.alignment_id
WHERE a.passed AND e.video_sec IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xg_reader') THEN
        GRANT SELECT ON shot_commentary_split TO xg_reader;
    END IF;
END $$;
