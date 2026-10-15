-- Initial warehouse schema, Postgres version of 0001_initial.sql (schema `warehouse`).
--
-- Same tables and columns. Differences: ids are BIGINT; the views use Postgres functions; row
-- level security is on (no policies: only the owner, xg_pipeline, can read or write, and the
-- schema is not exposed through Supabase's Data API). MoneyPuck's camelCase column names are
-- unquoted, so Postgres stores them in lowercase; queries can use either spelling.
--
-- Dates stay ISO-8601 TEXT, as in SQLite, so the code compares them the same way on both.

-- One row per batch of data loaded: where it came from and which code loaded it.
CREATE TABLE loads (
    load_id      TEXT PRIMARY KEY,           -- uuid
    kind         TEXT NOT NULL,              -- games | events | shots | transcript | alignment
    game_id      BIGINT,
    video_id     TEXT,
    raw_files    TEXT,                       -- JSON list of {path, sha256}
    source_urls  TEXT,                       -- JSON list
    fetched_at   TEXT,                       -- newest raw file modification time (UTC ISO)
    loaded_at    TEXT NOT NULL,              -- UTC ISO
    code_version TEXT,                       -- git commit (+ "-dirty")
    run_id       BIGINT,                    -- pipeline run (ops.runs), if any
    details      TEXT                        -- JSON, e.g. alignment quality metrics
);
CREATE INDEX ix_loads_game ON loads (game_id, kind);

CREATE TABLE games (
    game_id     BIGINT PRIMARY KEY,
    season      INTEGER NOT NULL,
    game_type   TEXT NOT NULL,               -- preseason | regular | playoff
    game_date   TEXT NOT NULL,
    venue       TEXT,
    away_team   TEXT NOT NULL,
    home_team   TEXT NOT NULL,
    away_name   TEXT,
    home_name   TEXT,
    away_goals  INTEGER,
    home_goals  INTEGER,
    ended_in    TEXT,                        -- REG | OT | SO
    video_id    TEXT,
    video_title TEXT,
    load_id     TEXT REFERENCES loads (load_id)
);

-- MoneyPuck play-by-play: one row per game event.
CREATE TABLE events (
    game_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    time INTEGER,
    homeWinProbability DOUBLE PRECISION,
    homeTeamGoals INTEGER,
    awayTeamGoals INTEGER,
    event TEXT,
    eventDescriptionRaw TEXT,
    homePercentOfEventsInOffensiveZone DOUBLE PRECISION,
    homeTeamShootOutGoals INTEGER,
    homeTeamShootOutAttempts INTEGER,
    awayTeamShootOutGoals INTEGER,
    awayTeamShootOutAttempts INTEGER,
    leadWinProbability DOUBLE PRECISION,
    tieGameModelProbability DOUBLE PRECISION,
    homeFaceoffWinPercentage DOUBLE PRECISION,
    homeShareCloseShots DOUBLE PRECISION,
    penaltyTimeSumSquare INTEGER,
    game_date TEXT,
    preGameHomeTeamWinInRegScore DOUBLE PRECISION,
    preGameHomeTeamWinInOTScore DOUBLE PRECISION,
    preGameHomeTeamWinOverallScore DOUBLE PRECISION,
    preGameAwayTeamWinInRegScore DOUBLE PRECISION,
    preGameAwayTeamWinInOTScore DOUBLE PRECISION,
    preGameAwayTeamWinOverallScore DOUBLE PRECISION,
    liveHomeTeamWinInRegScore DOUBLE PRECISION,
    liveHomeTeamWinInOTScore DOUBLE PRECISION,
    liveHomeTeamWinOverallScore DOUBLE PRECISION,
    liveHomeTeamExpectedPoints DOUBLE PRECISION,
    liveAwayTeamWinInRegScore DOUBLE PRECISION,
    liveAwayTeamWinInOTScore DOUBLE PRECISION,
    liveAwayTeamWinOverallScore DOUBLE PRECISION,
    liveAwayTeamExpectedPoints DOUBLE PRECISION,
    liveHomeTeamMakeRound1 DOUBLE PRECISION,
    liveHomeTeamMakeRound2 DOUBLE PRECISION,
    liveHomeTeamMakeRound3 DOUBLE PRECISION,
    liveHomeTeamMakeRound4 DOUBLE PRECISION,
    liveHomeTeamWinCup DOUBLE PRECISION,
    liveAwayTeamMakeRound1 DOUBLE PRECISION,
    liveAwayTeamMakeRound2 DOUBLE PRECISION,
    liveAwayTeamMakeRound3 DOUBLE PRECISION,
    liveAwayTeamMakeRound4 DOUBLE PRECISION,
    liveAwayTeamWinCup DOUBLE PRECISION,
    xCord INTEGER,
    yCord INTEGER,
    xCordAdjusted INTEGER,
    yCordAdjusted INTEGER,
    shotAngle DOUBLE PRECISION,
    shotAngleAdjusted DOUBLE PRECISION,
    shotAnglePlusRebound DOUBLE PRECISION,
    shotAngleReboundRoyalRoad INTEGER,
    shotDistance DOUBLE PRECISION,
    shotType TEXT,
    shotOnEmptyNet INTEGER,
    shotRebound INTEGER,
    shotAnglePlusReboundSpeed DOUBLE PRECISION,
    shotRush INTEGER,
    speedFromLastEvent DOUBLE PRECISION,
    lastEventxCord INTEGER,
    lastEventyCord INTEGER,
    distanceFromLastEvent DOUBLE PRECISION,
    lastEventShotAngle DOUBLE PRECISION,
    lastEventShotDistance DOUBLE PRECISION,
    lastEventCategory TEXT,
    lastEventTeam TEXT,
    awayPenalty1TimeLeft INTEGER,
    awayPenalty1Length INTEGER,
    homePenalty1TimeLeft INTEGER,
    homePenalty1Length INTEGER,
    shotGoalProbability DOUBLE PRECISION,
    playerPositionThatDidEvent TEXT,
    homeSkatersOnIce INTEGER,
    awaySkatersOnIce INTEGER,
    homeTeamExpectedGoals DOUBLE PRECISION,
    awayTeamExpectedGoals DOUBLE PRECISION,
    homeTeamExpectedGoalsEV DOUBLE PRECISION,
    awayTeamExpectedGoalsEV DOUBLE PRECISION,
    homeTeamExpectedGoalsAdjEV DOUBLE PRECISION,
    awayTeamExpectedGoalsAdjEV DOUBLE PRECISION,
    homeTeamExpectedGoalsEVNonRebound DOUBLE PRECISION,
    awayTeamExpectedGoalsEVNonRebound DOUBLE PRECISION,
    homeTeamExpectedGoalsAdjEVNonRebound DOUBLE PRECISION,
    awayTeamExpectedGoalsAdjEVNonRebound DOUBLE PRECISION,
    homeTeamExpectedGoalsPP DOUBLE PRECISION,
    awayTeamExpectedGoalsPP DOUBLE PRECISION,
    homeShareOfExpectedGoals DOUBLE PRECISION,
    awayShareOfExpectedGoals DOUBLE PRECISION,
    homeShareOfExpectedGoalsEV DOUBLE PRECISION,
    awayShareOfExpectedGoalsEV DOUBLE PRECISION,
    homeTeamShareOfExpectedGoalsAdjEV DOUBLE PRECISION,
    awayTeamShareOfExpectedGoalsAdjEV DOUBLE PRECISION,
    homeTeamExpectedGoalsEVPer60 DOUBLE PRECISION,
    awayTeamExpectedGoalsEVPer60 DOUBLE PRECISION,
    homeShareOfExpectedGoalsPP DOUBLE PRECISION,
    awayShareOfExpectedGoalsPP DOUBLE PRECISION,
    homeTeamExpectedGoalsPPPer60 DOUBLE PRECISION,
    awayTeamExpectedGoalsPPPer60 DOUBLE PRECISION,
    homeTeamShareOfExpectedGoalsPPP60 DOUBLE PRECISION,
    awayTeamShareOfExpectedGoalsPPP60 DOUBLE PRECISION,
    homeShareOfExpectedGoalsEVNonRebound DOUBLE PRECISION,
    awayShareOfExpectedGoalsEVNonRebound DOUBLE PRECISION,
    homeTeamShareOfExpectedGoalsAdjEVNonRebound DOUBLE PRECISION,
    awayTeamShareOfExpectedGoalsAdjEVNonRebound DOUBLE PRECISION,
    homeTeamExpectedGoalsEVPer60NonRebound DOUBLE PRECISION,
    awayTeamExpectedGoalsEVPer60NonRebound DOUBLE PRECISION,
    homeTeamExpectedGoalsIncludingEmptyNet DOUBLE PRECISION,
    awayTeamExpectedGoalsIncludingEmptyNet DOUBLE PRECISION,
    homeTeamTotalPPTime DOUBLE PRECISION,
    awayTeamTotalPPTime DOUBLE PRECISION,
    homeTeamExpectedGoalsAdjEVNonReboundPer60 DOUBLE PRECISION,
    awayTeamExpectedGoalsAdjEVNonReboundPer60 DOUBLE PRECISION,
    homeTeamExpectedGoalsAdjEVPer60 DOUBLE PRECISION,
    awayTeamExpectedGoalsAdjEVPer60 DOUBLE PRECISION,
    homeTeamShareExpectedGoalsAdjEVNonReboundPer60 DOUBLE PRECISION,
    awayTeamShareExpectedGoalsAdjEVNonReboundPer60 DOUBLE PRECISION,
    homeTeamShareExpectedGoalsAdjEVPer60 DOUBLE PRECISION,
    awayTeamShareExpectedGoalsAdjEVPer60 DOUBLE PRECISION,
    team TEXT,
    replayId DOUBLE PRECISION,
    sequence INTEGER,
    sequenceScaledFactor DOUBLE PRECISION,
    homeTeamExpectedGoalsEVSequence DOUBLE PRECISION,
    awayTeamExpectedGoalsEVSequence DOUBLE PRECISION,
    homeTeamExpectedGoalsEVSequenceAdj DOUBLE PRECISION,
    awayTeamExpectedGoalsEVSequenceAdj DOUBLE PRECISION,
    homeTeamExpectedGoalsPPSequence DOUBLE PRECISION,
    awayTeamExpectedGoalsPPSequence DOUBLE PRECISION,
    homeTeamExpectedGoalsSequence DOUBLE PRECISION,
    awayTeamExpectedGoalsSequence DOUBLE PRECISION,
    homeTeamShareOfExpectedGoalsSequenceEV DOUBLE PRECISION,
    awayTeamShareOfExpectedGoalsSequenceEV DOUBLE PRECISION,
    homeTeamShareOfExpectedGoalsSequenceAdjEV DOUBLE PRECISION,
    awayTeamShareOfExpectedGoalsSequenceAdjEV DOUBLE PRECISION,
    homeShareOfExpectedGoalsPPSequenceP60 DOUBLE PRECISION,
    awayShareOfExpectedGoalsPPSequenceP60 DOUBLE PRECISION,
    shotFromLeftOrRightShooter TEXT,
    timeSinceFaceoff INTEGER,
    arenaAdjustedXCord DOUBLE PRECISION,
    arenaAdjustedYCord DOUBLE PRECISION,
    shotFrozeProbability DOUBLE PRECISION,
    shotReboundProbability DOUBLE PRECISION,
    shotContinueInZoneProbability DOUBLE PRECISION,
    shotContinueOutsideZoneProbability DOUBLE PRECISION,
    shotPlayStoppedProbability DOUBLE PRECISION,
    shotOnGoalProbability DOUBLE PRECISION,
    arenaAdjustedShotDistance DOUBLE PRECISION,
    shotActualReboundxGoals DOUBLE PRECISION,
    shotPredictedxGoalsFromRebounds DOUBLE PRECISION,
    totalShotCreditOfShot DOUBLE PRECISION,
    homeTeamTotalShotCredit DOUBLE PRECISION,
    awayTeamTotalShotCredit DOUBLE PRECISION,
    homeTeamTotalShotCreditEV DOUBLE PRECISION,
    awayTeamTotalShotCreditEV DOUBLE PRECISION,
    homeTeamTotalShotCreditEVScoreFlurryAdjusted DOUBLE PRECISION,
    awayTeamTotalShotCreditEVScoreFlurryAdjusted DOUBLE PRECISION,
    load_id TEXT REFERENCES loads (load_id),
    calibrated_transcript_time DOUBLE PRECISION,  -- when the event happens in the video
    alignment_load_id TEXT REFERENCES loads (load_id),
    PRIMARY KEY (game_id, event_id)
);

-- MoneyPuck shot attempts (shots on goal, misses, goals) with xG.
CREATE TABLE shots (
    game_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    moneypuck_shot_id BIGINT,
    arenaAdjustedShotDistance DOUBLE PRECISION,
    arenaAdjustedXCord DOUBLE PRECISION,
    arenaAdjustedXCordABS DOUBLE PRECISION,
    arenaAdjustedYCord DOUBLE PRECISION,
    arenaAdjustedYCordAbs DOUBLE PRECISION,
    averageRestDifference DOUBLE PRECISION,
    awayEmptyNet INTEGER,
    awayPenalty1Length INTEGER,
    awayPenalty1TimeLeft INTEGER,
    awaySkatersOnIce INTEGER,
    awayTeamCode TEXT,
    awayTeamGoals INTEGER,
    defendingTeamAverageTimeOnIce DOUBLE PRECISION,
    defendingTeamAverageTimeOnIceOfDefencemen DOUBLE PRECISION,
    defendingTeamAverageTimeOnIceOfDefencemenSinceFaceoff DOUBLE PRECISION,
    defendingTeamAverageTimeOnIceOfForwards DOUBLE PRECISION,
    defendingTeamAverageTimeOnIceOfForwardsSinceFaceoff DOUBLE PRECISION,
    defendingTeamAverageTimeOnIceSinceFaceoff DOUBLE PRECISION,
    defendingTeamDefencemenOnIce INTEGER,
    defendingTeamForwardsOnIce INTEGER,
    defendingTeamMaxTimeOnIce INTEGER,
    defendingTeamMaxTimeOnIceOfDefencemen INTEGER,
    defendingTeamMaxTimeOnIceOfDefencemenSinceFaceoff INTEGER,
    defendingTeamMaxTimeOnIceOfForwards INTEGER,
    defendingTeamMaxTimeOnIceOfForwardsSinceFaceoff INTEGER,
    defendingTeamMaxTimeOnIceSinceFaceoff INTEGER,
    defendingTeamMinTimeOnIce INTEGER,
    defendingTeamMinTimeOnIceOfDefencemen INTEGER,
    defendingTeamMinTimeOnIceOfDefencemenSinceFaceoff INTEGER,
    defendingTeamMinTimeOnIceOfForwards INTEGER,
    defendingTeamMinTimeOnIceOfForwardsSinceFaceoff INTEGER,
    defendingTeamMinTimeOnIceSinceFaceoff INTEGER,
    distanceFromLastEvent DOUBLE PRECISION,
    event TEXT,
    goal INTEGER,
    goalieIdForShot BIGINT,
    goalieNameForShot TEXT,
    homeEmptyNet INTEGER,
    homePenalty1Length INTEGER,
    homePenalty1TimeLeft INTEGER,
    homeSkatersOnIce INTEGER,
    homeTeamCode TEXT,
    homeTeamGoals INTEGER,
    homeTeamWon INTEGER,
    isHomeTeam INTEGER,
    isPlayoffGame INTEGER,
    lastEventCategory TEXT,
    lastEventShotAngle DOUBLE PRECISION,
    lastEventShotDistance DOUBLE PRECISION,
    lastEventTeam TEXT,
    lastEventxCord INTEGER,
    lastEventxCord_adjusted INTEGER,
    lastEventyCord INTEGER,
    lastEventyCord_adjusted INTEGER,
    location TEXT,
    offWing INTEGER,
    period INTEGER,
    playerNumThatDidEvent INTEGER,
    playerNumThatDidLastEvent INTEGER,
    playerPositionThatDidEvent TEXT,
    season INTEGER,
    shooterLeftRight TEXT,
    shooterName TEXT,
    shooterPlayerId BIGINT,
    shooterTimeOnIce INTEGER,
    shooterTimeOnIceSinceFaceoff INTEGER,
    shootingTeamAverageTimeOnIce DOUBLE PRECISION,
    shootingTeamAverageTimeOnIceOfDefencemen DOUBLE PRECISION,
    shootingTeamAverageTimeOnIceOfDefencemenSinceFaceoff DOUBLE PRECISION,
    shootingTeamAverageTimeOnIceOfForwards DOUBLE PRECISION,
    shootingTeamAverageTimeOnIceOfForwardsSinceFaceoff DOUBLE PRECISION,
    shootingTeamAverageTimeOnIceSinceFaceoff DOUBLE PRECISION,
    shootingTeamDefencemenOnIce INTEGER,
    shootingTeamForwardsOnIce INTEGER,
    shootingTeamMaxTimeOnIce INTEGER,
    shootingTeamMaxTimeOnIceOfDefencemen INTEGER,
    shootingTeamMaxTimeOnIceOfDefencemenSinceFaceoff INTEGER,
    shootingTeamMaxTimeOnIceOfForwards INTEGER,
    shootingTeamMaxTimeOnIceOfForwardsSinceFaceoff INTEGER,
    shootingTeamMaxTimeOnIceSinceFaceoff INTEGER,
    shootingTeamMinTimeOnIce INTEGER,
    shootingTeamMinTimeOnIceOfDefencemen INTEGER,
    shootingTeamMinTimeOnIceOfDefencemenSinceFaceoff INTEGER,
    shootingTeamMinTimeOnIceOfForwards INTEGER,
    shootingTeamMinTimeOnIceOfForwardsSinceFaceoff INTEGER,
    shootingTeamMinTimeOnIceSinceFaceoff INTEGER,
    shotAngle DOUBLE PRECISION,
    shotAngleAdjusted DOUBLE PRECISION,
    shotAnglePlusRebound DOUBLE PRECISION,
    shotAnglePlusReboundSpeed DOUBLE PRECISION,
    shotAngleReboundRoyalRoad INTEGER,
    shotDistance DOUBLE PRECISION,
    shotGeneratedRebound INTEGER,
    shotGoalieFroze INTEGER,
    shotOnEmptyNet INTEGER,
    shotPlayContinuedInZone INTEGER,
    shotPlayContinuedOutsideZone INTEGER,
    shotPlayStopped INTEGER,
    shotRebound INTEGER,
    shotRush INTEGER,
    shotType TEXT,
    shotWasOnGoal INTEGER,
    speedFromLastEvent DOUBLE PRECISION,
    team TEXT,
    teamCode TEXT,
    time INTEGER,
    timeDifferenceSinceChange INTEGER,
    timeSinceFaceoff INTEGER,
    timeSinceLastEvent INTEGER,
    timeUntilNextEvent INTEGER,
    xCord INTEGER,
    xCordAdjusted INTEGER,
    xFroze DOUBLE PRECISION,
    xGoal DOUBLE PRECISION,
    xPlayContinuedInZone DOUBLE PRECISION,
    xPlayContinuedOutsideZone DOUBLE PRECISION,
    xPlayStopped DOUBLE PRECISION,
    xRebound DOUBLE PRECISION,
    xShotWasOnGoal DOUBLE PRECISION,
    yCord INTEGER,
    yCordAdjusted INTEGER,
    load_id TEXT REFERENCES loads (load_id),
    PRIMARY KEY (game_id, event_id)
);
CREATE INDEX ix_shots_team ON shots (game_id, teamCode);
CREATE INDEX ix_shots_shooter ON shots (shooterPlayerId);

-- Caption lines of a game's replay video.
CREATE TABLE transcript (
    video_id             TEXT NOT NULL,
    game_id              BIGINT NOT NULL,
    seq                  INTEGER NOT NULL,
    start_sec            DOUBLE PRECISION NOT NULL,
    duration_sec         DOUBLE PRECISION NOT NULL,
    end_sec              DOUBLE PRECISION NOT NULL,
    text                 TEXT NOT NULL,
    load_id              TEXT REFERENCES loads (load_id),
    calibrated_game_time DOUBLE PRECISION,   -- game time when the line was spoken
    in_play              INTEGER,            -- 1 = clock running, 0 = stoppage/replay
    alignment_load_id    TEXT REFERENCES loads (load_id),
    PRIMARY KEY (video_id, seq)
);
CREATE INDEX ix_transcript_game ON transcript (game_id, start_sec);

-- Events and captions interleaved on one timeline, like a script of the broadcast.
CREATE VIEW timeline WITH (security_invoker = true) AS
SELECT game_id, transcript_time, game_time, in_play, event_description, text FROM (
    SELECT game_id, calibrated_transcript_time AS transcript_time, time AS game_time,
           NULL::integer AS in_play, eventDescriptionRaw AS event_description, NULL::text AS text,
           0 AS is_caption
    FROM events
    WHERE calibrated_transcript_time IS NOT NULL AND TRIM(COALESCE(eventDescriptionRaw, '')) <> ''
    UNION ALL
    SELECT game_id, start_sec, calibrated_game_time, in_play, NULL, text, 1
    FROM transcript
) t
ORDER BY game_id, transcript_time, is_caption;

-- Each shot with its xG, a link to the moment in the video (5 s early, for the build-up),
-- and the captions overlapping 4 s before to 10 s after it.
CREATE VIEW shot_commentary WITH (security_invoker = true) AS
SELECT
    s.game_id,
    s.event_id,
    s.period,
    format('%s:%s', (s.time - (s.period - 1) * 1200) / 60,
           lpad(((s.time - (s.period - 1) * 1200) % 60)::text, 2, '0')) AS clock,
    s.teamCode AS team,
    s.shooterName AS shooter,
    s.event AS result,
    s.shotType AS type,
    ROUND(s.shotDistance::numeric, 1) AS dist_ft,
    ROUND(s.xGoal::numeric, 3) AS xG,
    format('%s:%s', trunc(e.calibrated_transcript_time)::int / 60,
           lpad((trunc(e.calibrated_transcript_time)::int % 60)::text, 2, '0')) AS video,
    'https://www.youtube.com/watch?v=' || g.video_id || '&t='
        || GREATEST(trunc(e.calibrated_transcript_time)::int - 5, 0) || 's' AS link,
    (SELECT string_agg(t.text, ' ' ORDER BY t.start_sec)
       FROM transcript t
      WHERE t.video_id = g.video_id
        AND t.end_sec >= e.calibrated_transcript_time - 4
        AND t.start_sec <= e.calibrated_transcript_time + 10) AS commentary
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN games g USING (game_id)
WHERE e.calibrated_transcript_time IS NOT NULL;

-- Lookups the code makes: a video's game, a video's alignment loads.
CREATE INDEX ix_games_video ON games (video_id);
CREATE INDEX ix_loads_video ON loads (video_id, kind);

ALTER TABLE loads ENABLE ROW LEVEL SECURITY;
ALTER TABLE games ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE shots ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcript ENABLE ROW LEVEL SECURITY;
