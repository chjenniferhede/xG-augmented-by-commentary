"""The weekly pipeline: discover NHL replays, then load and align them, slowly.

Commands:
    run [--trigger T] [--max-transcripts N] [--max-align M] [--no-discover] [--dry-run]
        1. discover new replays (YouTube Data API)
        2. captions for up to N queued videos, newest first        (YouTube, rate-limited)
        3. MoneyPuck events and shots for games with captions       (no YouTube)
        4. alignment for up to M games                              (YouTube frames, rate-limited)
    discover              stage 1 only
    add <link> [--game-id ID]   queue one video by hand
    process <captions|game_data|align> <video_id>...
                          take chosen videos through the stages they still need, up to this one
    prune-frames          delete frame images of aligned videos (automatic after each passing
                          alignment unless XG_KEEP_FRAMES=1; index.json keeps the OCR text)
    status                queue, cooldown and recent runs
    rebuild [--game-id ID]      rebuild the warehouse from data/raw with the network off

Video states: discovered -> transcribed -> game_loaded -> aligned, or no_captions /
game_unresolved / no_game_data / alignment_unverified. A failed step sets next_attempt_at for a later retry.
Only one run at a time, across machines on Postgres: a second one exits ("already running").
Every run, step and request is recorded in the ops database, with a log in data/logs/run_<id>.log.
"""
import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import db
import ops
import paths
import ratelimit

RETRY = {"no_captions": timedelta(days=30), "game_data_missing": timedelta(days=7),
         "error": timedelta(days=1)}


def later(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


LOCK_KEY = (7301, 1)  # Postgres advisory lock for "a pipeline run is in progress"


@contextmanager
def run_lock():
    """Held for a whole run. On Postgres a session advisory lock on its own connection (released
    when the run ends or its process dies, on any machine); on SQLite a lock on a local file."""
    if db.is_postgres():
        import psycopg

        with psycopg.connect(db.DATABASE_URL, autocommit=True, keepalives=1, keepalives_idle=60) as con:
            got = con.execute("SELECT pg_try_advisory_lock(%s, %s)", LOCK_KEY).fetchone()[0]
            yield got
        return
    import fcntl

    paths.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.LOCK_FILE, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        f.write(str(os.getpid()))
        f.flush()
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_running() -> bool:
    """True while some process holds the run lock (used by the dashboard)."""
    if db.is_postgres():
        return ops.connect().execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = ? AND objid = ?)", LOCK_KEY).fetchone()[0]
    import fcntl

    if not paths.LOCK_FILE.exists():
        return False
    with open(paths.LOCK_FILE) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def failed(video_id: str, e: Exception, attempts: int) -> None:
    ops.log.exception("video %s: unexpected error", video_id)
    ops.update_video(video_id, attempts=attempts + 1, last_error=f"{type(e).__name__}: {e}"[:500],
                     next_attempt_at=later(RETRY["error"]))


# Each do_* handles one video and returns False when YouTube is unavailable (stop the stage).

def do_transcript(v: dict, summary: dict) -> bool:
    import load_transcript as lt

    if not ratelimit.youtube_available():
        summary["youtube_stopped"] = "cooldown or budget"
        return False
    try:
        with ops.step("transcript", video_id=v["video_id"]) as s:
            try:
                game_id = lt.game_id_for(v["video_id"], v["game_id"])
                lt.load(v["video_id"], game_id)
                ops.update_video(v["video_id"], state="transcribed", game_id=game_id, last_error=None,
                                 next_attempt_at=None)
                summary["transcribed"] = summary.get("transcribed", 0) + 1
            except lt.NoCaptions as e:
                s.status, s.note = "skipped", str(e)
                ops.update_video(v["video_id"], state="no_captions", last_error=str(e),
                                 next_attempt_at=later(RETRY["no_captions"]))
            except lt.GameUnresolved as e:
                s.status, s.note = "skipped", str(e)
                ops.update_video(v["video_id"], state="game_unresolved", last_error=str(e)[:500],
                                 next_attempt_at="never")
            except (ratelimit.Blocked, ratelimit.BudgetExhausted) as e:
                s.status, s.note = "skipped", str(e)
                summary["youtube_stopped"] = str(e)
                return not ratelimit.enforce  # a run started by hand goes on to the next video
    except Exception as e:  # recorded on the step; retry the video tomorrow
        failed(v["video_id"], e, v["attempts"])
    return True


def stage_transcripts(limit: int, summary: dict) -> None:
    for v in ops.due_videos("discovered", limit):
        if not do_transcript(v, summary):
            return


def game_is_old(game_id: int) -> bool:
    """Played more than 60 days ago (MoneyPuck publishes within days of a game)."""
    row = db.connect("warehouse").execute("SELECT game_date FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not row:
        return False
    played = row[0] if isinstance(row[0], date) else date.fromisoformat(row[0])  # Postgres: date; SQLite: text
    return played < datetime.now(timezone.utc).date() - timedelta(days=60)


def do_game_data(v: dict, summary: dict) -> bool:
    import build_db

    try:
        with ops.step("game_data", video_id=v["video_id"], game_id=v["game_id"]) as s:
            try:
                build_db.build(v["game_id"])
                ops.update_video(v["video_id"], state="game_loaded", last_error=None, next_attempt_at=None)
                summary["games_loaded"] = summary.get("games_loaded", 0) + 1
            except build_db.GameDataMissing as e:
                s.status, s.note = "skipped", str(e)
                if game_is_old(v["game_id"]):  # MoneyPuck won't add it later
                    ops.update_video(v["video_id"], state="no_game_data", last_error=str(e),
                                     next_attempt_at="never")
                else:
                    ops.update_video(v["video_id"], last_error=str(e),
                                     next_attempt_at=later(RETRY["game_data_missing"]))
    except Exception as e:
        failed(v["video_id"], e, v["attempts"])
    return True


def stage_game_data(summary: dict) -> None:
    for v in ops.due_videos("transcribed", 100):
        do_game_data(v, summary)


def do_align(v: dict, summary: dict) -> bool:
    import align_video

    if not ratelimit.youtube_available():
        summary["youtube_stopped"] = "cooldown or budget"
        return False
    try:
        with ops.step("align", video_id=v["video_id"], game_id=v["game_id"]) as s:
            try:
                metrics = align_video.main(v["video_id"])
                state = "aligned" if metrics["passed"] else "alignment_unverified"
                ops.update_video(v["video_id"], state=state, next_attempt_at=None,
                                 last_error=None if metrics["passed"] else json.dumps(metrics))
                s.note = json.dumps(metrics)
                summary[state] = summary.get(state, 0) + 1
                if metrics["passed"] and os.environ.get("XG_KEEP_FRAMES") != "1":
                    align_video.prune_frames(v["video_id"])  # index.json keeps the OCR text
            except align_video.ScoreboardNotFound as e:
                s.status, s.note = "skipped", str(e)
                ops.update_video(v["video_id"], state="alignment_unverified", last_error=str(e))
            except (ratelimit.Blocked, ratelimit.BudgetExhausted) as e:
                # Frames read so far are kept; the next run continues from them.
                s.status, s.note = "skipped", str(e)
                summary["youtube_stopped"] = str(e)
                return not ratelimit.enforce  # a run started by hand goes on to the next video
    except Exception as e:
        failed(v["video_id"], e, v["attempts"])
    return True


def stage_align(limit: int, summary: dict) -> None:
    for v in ops.due_videos("game_loaded", limit):
        if not do_align(v, summary):
            return


# Chosen videos, by hand (the dashboard's file view): each is taken through the stages it still
# needs up to the one asked for, whatever its retry time. Alignment always re-runs.
STAGES = {"captions": do_transcript, "game_data": do_game_data, "align": do_align}
NEXT_STAGE = {"discovered": "captions", "no_captions": "captions", "game_unresolved": "captions",
              "transcribed": "game_data", "no_game_data": "game_data",
              "game_loaded": "align", "aligned": "align", "alignment_unverified": "align"}
REACHED = {"captions": ("transcribed",), "game_data": ("game_loaded",),
           "align": ("aligned", "alignment_unverified")}


def process_videos(video_ids: list, target: str, summary: dict) -> None:
    order = list(STAGES)
    for video_id in video_ids:
        v = ops.get_video(video_id)
        if v is None:
            ops.log.warning("video %s is not in the queue; add it first", video_id)
            continue
        first = order.index(NEXT_STAGE.get(v["state"], "captions"))
        if first > order.index(target):
            ops.log.info("video %s: already past %s (%s)", video_id, target, v["state"])
            continue
        for stage in order[first:order.index(target) + 1]:
            if not STAGES[stage](v, summary):
                return  # YouTube unavailable: the remaining videos would fail the same way
            v = ops.get_video(video_id)
            if v["state"] not in REACHED[stage]:
                break  # this stage didn't succeed; its state and error say why


def cmd_run(args) -> int:
    with run_lock() as acquired:
        if not acquired:
            ops.setup_logging()
            ops.log.info("a pipeline run is already in progress; exiting")
            return 1
        if args.dry_run:
            return dry_run(args)
        ops.start_run(args.trigger, "run", {k: v for k, v in vars(args).items() if k != "func"})
        summary, status = {}, "ok"
        try:
            if not args.no_discover:
                if os.environ.get("YOUTUBE_API_KEY"):
                    import discover

                    # A discovery failure (e.g. API quota) shouldn't stop work on queued videos.
                    for stage, fn in (("discover", discover.discover), ("discover_backfill", discover.backfill)):
                        try:
                            with ops.step(stage):
                                summary[stage] = fn()
                        except Exception:
                            ops.log.exception("%s failed; continuing with queued videos", stage)
                else:
                    ops.log.warning("YOUTUBE_API_KEY not set; skipping discovery")
            stage_transcripts(args.max_transcripts, summary)
            stage_game_data(summary)
            stage_align(args.max_align, summary)
            failures = ops.connect().execute(
                "SELECT COUNT(*) FROM steps WHERE run_id = ? AND status = 'failed'", (ops.current_run_id,)
            ).fetchone()[0]
            if failures or "youtube_stopped" in summary:
                status = "partial"
            summary["failed_steps"] = failures
        except BaseException as e:
            status = "failed"
            summary["error"] = f"{type(e).__name__}: {e}"
            ops.log.exception("run failed")
            raise
        finally:
            summary["youtube_requests"] = ratelimit.youtube_count
            ops.finish_run(status, summary)
    return 0


def dry_run(args) -> int:
    ops.setup_logging()
    print("Dry run: no network requests.")
    print(f"  discovery: {'would run' if not args.no_discover and os.environ.get('YOUTUBE_API_KEY') else 'skipped'}")
    for state, limit, label in (("discovered", args.max_transcripts, "captions"),
                                ("transcribed", 100, "MoneyPuck data"),
                                ("game_loaded", args.max_align, "alignment")):
        due = ops.due_videos(state, limit)
        print(f"  {label}: {len(due)} video(s)")
        for v in due:
            print(f"    {v['video_id']}  game {v['game_id'] or '?'}  {v['title']}")
    until = ratelimit.cooldown_until()
    if until > time.time():
        print(f"  YouTube cooldown until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}: "
              + ("caption and alignment stages would be skipped" if ratelimit.enforce
                 else "ignored, as this run wasn't started by the schedule"))
    return 0


def cmd_discover(args) -> int:
    import discover

    with run_lock() as acquired:
        if not acquired:
            print("a pipeline run is already in progress")
            return 1
        ops.start_run(args.trigger, "discover", {})
        try:
            with ops.step("discover"):
                result = {"recent": discover.discover()}
            with ops.step("discover_backfill"):
                result["backfill"] = discover.backfill()
            ops.finish_run("ok", result)
        except BaseException as e:
            ops.finish_run("failed", {"error": str(e)})
            raise
    return 0


def set_game(video_id: str, game_id: int) -> None:
    """Pin a video to an NHL game; it goes back to the caption stage to be loaded under that game.

    Also unsticks a video left as game_unresolved.
    """
    import load_transcript as lt

    ops.update_video(video_id, game_id=game_id, state="discovered", next_attempt_at=None, last_error=None)
    if lt.captions_path(video_id).exists():
        data = lt.fetch(video_id)
        data["game_id"] = game_id
        lt.save(data)


def cmd_add(args) -> int:
    import load_transcript as lt

    ops.setup_logging()
    video_id = lt.video_id_from(args.link)
    added = ops.add_video(video_id, None, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if args.game_id:
        set_game(video_id, args.game_id)
    print(f"{video_id}: {'queued' if added else 'already queued'}")
    return 0


def cmd_process(args) -> int:
    with run_lock() as acquired:
        if not acquired:
            ops.setup_logging()
            ops.log.info("a pipeline run is already in progress; exiting")
            return 1
        ops.start_run(args.trigger, "process", {"stage": args.stage, "videos": args.video_ids})
        summary, status = {}, "ok"
        try:
            process_videos(args.video_ids, args.stage, summary)
            failures = ops.connect().execute(
                "SELECT COUNT(*) FROM steps WHERE run_id = ? AND status = 'failed'", (ops.current_run_id,)
            ).fetchone()[0]
            if failures or "youtube_stopped" in summary:
                status = "partial"
            summary["failed_steps"] = failures
        except BaseException as e:
            status = "failed"
            summary["error"] = f"{type(e).__name__}: {e}"
            ops.log.exception("run failed")
            raise
        finally:
            summary["youtube_requests"] = ratelimit.youtube_count
            ops.finish_run(status, summary)
    return 0


def cmd_prune_frames(args) -> int:
    """Delete frame images of aligned videos (done automatically after each passing alignment)."""
    import align_video

    ops.setup_logging()
    rows = ops.connect().execute("SELECT video_id FROM videos WHERE state = 'aligned'").fetchall()
    files = size = 0
    for (video_id,) in rows:
        n, b = align_video.prune_frames(video_id)
        files, size = files + n, size + b
    print(f"{len(rows)} aligned video(s): deleted {files} frame images, freed {size / 1e6:.1f} MB")
    return 0


def cmd_status(args) -> int:
    con = ops.connect()
    print("Queue:")
    for state, n in con.execute("SELECT state, COUNT(*) FROM videos GROUP BY state ORDER BY state"):
        print(f"  {state:22} {n}")
    until = ratelimit.cooldown_until()
    if until > time.time():
        print(f"YouTube cooldown until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))} "
              f"({ops.kv_get('last_block_reason')})")
    hour = con.execute("SELECT COUNT(*) FROM requests WHERE kind IN ('yt_page','yt_caption','yt_frame') "
                       "AND ts > ?", (time.time() - 3600,)).fetchone()[0]
    print(f"YouTube scraping requests in the last hour: {hour}")
    print("Recent runs:")
    for row in con.execute("SELECT run_id, trigger, command, started_at, status, summary FROM runs "
                           "ORDER BY run_id DESC LIMIT 5"):
        print(f"  #{row[0]} {row[3]} {row[1]:8} {row[2]:9} {row[4]:8} {row[5] or ''}"[:160])
    print("Running now." if is_running() else "Idle.")
    return 0


def cmd_rebuild(args) -> int:
    """Rebuild the warehouse from data/raw with all network access refused."""
    import align_video
    import build_db
    import load_transcript as lt
    import raw_store

    with run_lock() as acquired:
        if not acquired:
            print("a pipeline run is already in progress")
            return 1
        ratelimit.offline = True
        ops.start_run(args.trigger, "rebuild", {"game_id": args.game_id})
        if args.game_id is None:
            # SQLite: the old file is kept as a .bak; Postgres: the warehouse tables are dropped
            # and re-created (data/raw on this machine is the source of truth).
            db.reset("warehouse")
            ops.log.info("warehouse emptied for a full rebuild")
        videos = []
        for path in sorted(paths.CAPTIONS.glob("*.json")):
            data = raw_store.read_json(path)
            if data.get("game_id") and (args.game_id is None or data["game_id"] == args.game_id):
                videos.append((data["video_id"], data["game_id"]))
        summary = {"games": 0, "transcripts": 0, "aligned": 0}
        for video_id, game_id in videos:
            with ops.step("transcript", video_id=video_id, game_id=game_id):
                lt.load(video_id, game_id)
                summary["transcripts"] += 1
            if build_db.events_source(game_id)[1].exists():
                with ops.step("game_data", video_id=video_id, game_id=game_id):
                    build_db.build(game_id)
                    summary["games"] += 1
                if (paths.FRAMES / video_id / "index.json").exists():
                    with ops.step("align", video_id=video_id, game_id=game_id) as s:
                        metrics = align_video.main(video_id)
                        s.note = json.dumps(metrics)
                        summary["aligned"] += 1
        ops.finish_run("ok", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trigger", default="cli", choices=["schedule", "manual", "ui", "cli"])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("--max-transcripts", type=int, default=20)
    p.add_argument("--max-align", type=int, default=3)
    p.add_argument("--no-discover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)
    sub.add_parser("discover").set_defaults(func=cmd_discover)
    p = sub.add_parser("add")
    p.add_argument("link")
    p.add_argument("--game-id", type=int)
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("process")
    p.add_argument("stage", choices=list(STAGES))
    p.add_argument("video_ids", nargs="+")
    p.set_defaults(func=cmd_process)
    sub.add_parser("prune-frames").set_defaults(func=cmd_prune_frames)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("rebuild")
    p.add_argument("--game-id", type=int)
    p.set_defaults(func=cmd_rebuild)

    # Allow `pipeline.py run --trigger schedule` as well as `pipeline.py --trigger schedule run`.
    argv = sys.argv[1:]
    if "--trigger" in argv[1:]:
        i = argv.index("--trigger")
        argv = argv[i:i + 2] + argv[:i] + argv[i + 2:]
    args = parser.parse_args(argv)
    ratelimit.enforce = args.trigger == "schedule"  # rate limits and cooldowns are for scheduled runs only
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
