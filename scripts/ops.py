"""Telemetry and the work queue, stored in the ops database (Postgres schema `ops`, or
data/db/pipeline.sqlite without DATABASE_URL).

- runs: one row per pipeline invocation (trigger, args, status, summary)
- steps: one row per unit of work in a run (stage, video, game, status, error, YouTube requests)
- requests: every outbound request (written by ratelimit.py)
- videos: discovered replay videos and how far each has been processed
- kv: small state (rate-limiter cooldown, discovery progress)

Each run also logs to data/logs/run_<id>.log.
"""
import json
import logging
import os
import sys
from contextlib import contextmanager

import db
import paths

log = logging.getLogger("xg")
current_run_id = None  # set by start_run; recorded on loads, steps and requests


def connect():
    """This thread's connection to the ops database."""
    return db.connect("ops")


def setup_logging(run_id=None) -> None:
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    if run_id is not None:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(paths.LOGS / f"run_{run_id}.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def start_run(trigger: str, command: str, args: dict) -> int:
    global current_run_id
    con = connect()
    with con:
        current_run_id = con.execute(
            "INSERT INTO runs (trigger, command, args, started_at, status, git_commit, pid) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?) RETURNING run_id",
            (trigger, command, json.dumps(args), db.now_iso(), db.code_version(), os.getpid()),
        ).fetchone()[0]
    setup_logging(current_run_id)
    log.info("run %s started: %s %s (trigger=%s)", current_run_id, command, args, trigger)
    return current_run_id


def finish_run(status: str, summary: dict) -> None:
    con = connect()
    with con:
        con.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE run_id = ?",
            (db.now_iso(), status, json.dumps(summary), current_run_id),
        )
    log.info("run %s finished: %s %s", current_run_id, status, summary)


class StepResult:
    def __init__(self):
        self.status = "ok"
        self.note = None


@contextmanager
def step(stage: str, video_id=None, game_id=None):
    """Record one unit of work. The body may set result.status = 'skipped' and result.note."""
    import ratelimit

    con = connect()
    with con:
        step_id = con.execute(
            "INSERT INTO steps (run_id, stage, video_id, game_id, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running') RETURNING step_id",
            (current_run_id, stage, video_id, game_id, db.now_iso()),
        ).fetchone()[0]
    yt_before = ratelimit.youtube_count
    result = StepResult()
    error = None
    try:
        yield result
    except BaseException as e:
        result.status = "failed"
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        with con:
            con.execute(
                "UPDATE steps SET finished_at = ?, status = ?, error = ?, youtube_requests = ? "
                "WHERE step_id = ?",
                (db.now_iso(), result.status, error or result.note,
                 ratelimit.youtube_count - yt_before, step_id),
            )


# ---- key/value state --------------------------------------------------------------------

def kv_get(key: str, default=None):
    row = connect().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value) -> None:
    con = connect()
    with con:
        con.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, None if value is None else str(value)))


# ---- video queue --------------------------------------------------------------------------

def add_video(video_id: str, title: str, published_at: str, state: str = "discovered",
              game_id=None) -> bool:
    """Queue a video if it's new. Returns True when it was added."""
    con = connect()
    with con:
        cur = con.execute(
            "INSERT INTO videos (video_id, title, published_at, discovered_at, game_id, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (video_id) DO NOTHING",
            (video_id, title, published_at, db.now_iso(), game_id, state, db.now_iso()),
        )
    return cur.rowcount > 0


def update_video(video_id: str, **fields) -> None:
    fields["updated_at"] = db.now_iso()
    sets = ", ".join(f"{k} = ?" for k in fields)
    con = connect()
    with con:
        con.execute(f"UPDATE videos SET {sets} WHERE video_id = ?", (*fields.values(), video_id))


def get_video(video_id: str):
    con = connect()
    con.row_factory = None
    row = con.execute("SELECT video_id, title, game_id, attempts, state FROM videos WHERE video_id = ?",
                      (video_id,)).fetchone()
    return dict(zip(("video_id", "title", "game_id", "attempts", "state"), row)) if row else None


def due_videos(state: str, limit: int):
    """Videos in a state whose retry time has come, newest first."""
    con = connect()
    con.row_factory = None
    rows = con.execute(
        "SELECT video_id, title, game_id, attempts FROM videos WHERE state = ? "
        "AND (next_attempt_at IS NULL OR (next_attempt_at != 'never' AND next_attempt_at <= ?)) "
        "ORDER BY published_at DESC LIMIT ?",
        (state, db.now_iso(), limit),
    ).fetchall()
    return [dict(zip(("video_id", "title", "game_id", "attempts"), r)) for r in rows]

