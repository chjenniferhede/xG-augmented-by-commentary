-- Operations database: pipeline runs, their steps, every outbound request, and the queue of
-- discovered replay videos. Kept separate from the warehouse (moneypuck.sqlite).

CREATE TABLE runs (
    run_id      INTEGER PRIMARY KEY,
    trigger     TEXT NOT NULL,               -- schedule | manual | ui | cli
    command     TEXT NOT NULL,               -- run | discover | rebuild | ...
    args        TEXT,                        -- JSON
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,               -- running | ok | partial | failed
    git_commit  TEXT,
    pid         INTEGER,
    summary     TEXT                         -- JSON
);

CREATE TABLE steps (
    step_id          INTEGER PRIMARY KEY,
    run_id           INTEGER REFERENCES runs (run_id),
    stage            TEXT NOT NULL,          -- discover | transcript | game_data | align
    video_id         TEXT,
    game_id          INTEGER,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,          -- running | ok | skipped | failed
    error            TEXT,
    youtube_requests INTEGER DEFAULT 0
);
CREATE INDEX ix_steps_run ON steps (run_id);

-- Every outbound request, written by the rate limiter (it also reads this to space requests).
CREATE TABLE requests (
    request_id INTEGER PRIMARY KEY,
    ts         DOUBLE PRECISION NOT NULL,    -- unix time when sent
    run_id     INTEGER,
    kind       TEXT NOT NULL,                -- yt_page | yt_caption | yt_frame | yt_api | nhl | moneypuck
    target     TEXT,
    status     TEXT,                         -- ok | error | blocked
    latency_ms INTEGER
);
CREATE INDEX ix_requests_kind_ts ON requests (kind, ts);

-- Replay videos found on YouTube, and how far each has been processed.
CREATE TABLE videos (
    video_id        TEXT PRIMARY KEY,
    title           TEXT,
    published_at    TEXT,
    discovered_at   TEXT,
    game_id         INTEGER,
    state           TEXT NOT NULL,           -- see pipeline.py
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT,                    -- NULL = due now; 'never' = needs a manual fix
    updated_at      TEXT
);
CREATE INDEX ix_videos_state ON videos (state, published_at);

-- Small key/value state: rate-limiter cooldown, discovery progress.
CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
