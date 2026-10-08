"""Local dashboard for the pipeline: runs, logs, and the videos as a file browser, where chosen
videos can be sent through any stage (captions, MoneyPuck data, alignment).

Usage: .venv/bin/python dashboard/app.py      then open http://127.0.0.1:5057
Binds to 127.0.0.1 only.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ops  # noqa: E402
import paths  # noqa: E402
import pipeline  # noqa: E402
import ratelimit  # noqa: E402

app = Flask(__name__)
app.secret_key = "local-dashboard"
app.config["TEMPLATES_AUTO_RELOAD"] = True  # template edits show on the next page load


@app.teardown_request
def close_connections(_exc):
    db.close_thread_connections()  # each request runs on its own thread: don't leave its connections open

MODES = {
    "transcripts": ["--max-align", "0"],   # discovery + captions + MoneyPuck data, no frame reads
    "full": [],                            # also aligns up to 3 games
}

# Sidebar folders of the video browser, in pipeline order: (key, label, states).
FOLDERS = [
    ("all", "All videos", None),
    ("captions", "Needs captions", ["discovered"]),
    ("game_data", "Needs MoneyPuck data", ["transcribed"]),
    ("align", "Ready to align", ["game_loaded"]),
    ("aligned", "Aligned", ["aligned"]),
    ("attention", "Needs attention", ["alignment_unverified", "game_unresolved", "no_captions", "no_game_data"]),
]
STAGE_LABELS = {"captions": "Fetch captions", "game_data": "Load MoneyPuck data", "align": "Align to video"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def spawn(*args: str) -> None:
    """Start a pipeline command in the background; it outlives this request."""
    subprocess.Popen(
        [sys.executable, str(SCRIPTS / "pipeline.py"), *args, "--trigger", "ui"],
        cwd=paths.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(1.0)  # let it take the lock, so the page shows it running


def warehouse():
    return db.connect("warehouse") if db.is_postgres() or paths.WAREHOUSE_DB.exists() else None


def game_files(game_id):
    """The raw files a game's MoneyPuck stage uses: (label, path)."""
    if not game_id:
        return []
    s = game_id // 1_000_000
    return [("NHL boxscore", paths.NHL / "boxscore" / f"{game_id}.json"),
            ("MoneyPuck play-by-play", paths.MONEYPUCK / "games" / f"{s}{s + 1}" / f"{game_id}.csv"),
            ("MoneyPuck shots (whole season)", paths.MONEYPUCK / f"shots_{s}.zip")]


def frame_count(video_id: str) -> int:
    d = paths.FRAMES / video_id
    return sum(1 for p in d.iterdir() if p.suffix == ".jpg" and not p.name.startswith("full_")) if d.is_dir() else 0


ALIGNMENT_COLUMNS = ("passed", "segments", "pinned_by_clock", "coverage", "clock_reversals", "readings",
                     "video_cuts", "score_readings", "score_match", "frames_cached", "frames_fetched",
                     "max_offset_drop", "created_at")


def alignment_details(w, video_id=None) -> dict:
    """video_id -> its alignment in effect (videos.alignment_id), as a dict of the metrics."""
    if w is None:
        return {}
    sql = (f"SELECT v.video_id, {', '.join('a.' + c for c in ALIGNMENT_COLUMNS)} FROM videos v "
           "JOIN alignments a ON a.alignment_id = v.alignment_id {}")
    rows = w.execute(sql.format("WHERE v.video_id = ?"), (video_id,)) if video_id else w.execute(sql.format(""))
    return {r[0]: dict(zip(ALIGNMENT_COLUMNS, r[1:])) for r in rows}


def youtube_usage() -> list:
    con = ops.connect()
    now = time.time()
    rows = []
    for group, (interval, _, per_hour) in ratelimit.SPACING.items():
        kinds = [k for k, g in ratelimit.GROUPS.items() if g == group]
        marks = ",".join("?" * len(kinds))
        hour = con.execute(f"SELECT COUNT(*) FROM requests WHERE kind IN ({marks}) AND ts > ?",
                           (*kinds, now - 3600)).fetchone()[0]
        day = con.execute(f"SELECT COUNT(*) FROM requests WHERE kind IN ({marks}) AND ts > ?",
                          (*kinds, now - 86400)).fetchone()[0]
        rows.append({"group": group, "interval": interval, "per_hour": per_hour, "hour": hour, "day": day})
    return rows


@app.route("/")
def index():
    con = ops.connect()
    con.row_factory = None
    runs = con.execute("SELECT run_id, trigger, command, started_at, finished_at, status, summary "
                       "FROM runs ORDER BY run_id DESC LIMIT 15").fetchall()
    queue = con.execute("SELECT state, COUNT(*) FROM videos GROUP BY state ORDER BY state").fetchall()
    totals = {}
    w = warehouse()
    if w is not None:
        totals = {
            "videos": w.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            "with_events": w.execute("SELECT COUNT(*) FROM games WHERE events_load_id IS NOT NULL").fetchone()[0],
            "aligned": w.execute("SELECT COUNT(*) FROM videos v JOIN alignments a ON a.alignment_id = v.alignment_id "
                                 "WHERE a.passed").fetchone()[0],
            "shots": w.execute("SELECT COUNT(*) FROM shots").fetchone()[0],
            # shots placed in a video whose alignment passed (the rows of shot_commentary)
            "aligned_shots": w.execute("SELECT COUNT(*) FROM shot_commentary").fetchone()[0],
        }
    return render_template(
        "index.html", runs=runs, queue=queue, usage=youtube_usage(), running=pipeline.is_running(),
        cooldown=cooldown_text(), block_reason=ops.kv_get("last_block_reason"), warehouse=totals,
        budget=ratelimit.RUN_BUDGET, full_scan_done=ops.kv_get("discover_full_scan_done") == "1",
    )


@app.route("/runs/<int:run_id>")
def run_page(run_id: int):
    con = ops.connect()
    con.row_factory = None
    run = con.execute("SELECT run_id, trigger, command, args, started_at, finished_at, status, git_commit, summary "
                      "FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    steps = con.execute("SELECT stage, video_id, game_id, started_at, finished_at, status, error, youtube_requests "
                        "FROM steps WHERE run_id = ? ORDER BY step_id", (run_id,)).fetchall()
    requests_by_kind = con.execute("SELECT kind, status, COUNT(*) FROM requests WHERE run_id = ? "
                                   "GROUP BY kind, status ORDER BY kind", (run_id,)).fetchall()
    log_path = paths.LOGS / f"run_{run_id}.log"
    log_tail = "\n".join(log_path.read_text().splitlines()[-200:]) if log_path.exists() else ""
    return render_template("run.html", run=run, steps=steps, requests_by_kind=requests_by_kind,
                           log_tail=log_tail, running=run[6] == "running")


VIDEO_COLUMNS = ("video_id", "title", "published_at", "game_id", "state", "attempts", "last_error",
                 "next_attempt_at")


@app.route("/videos")
def videos():
    folder = request.args.get("folder", "all")
    state = request.args.get("state")  # a single state, from the overview's queue cards
    q = request.args.get("q", "").strip()
    con = ops.connect()
    con.row_factory = None
    counts = dict(con.execute("SELECT state, COUNT(*) FROM videos GROUP BY state").fetchall())
    folders = [{"key": k, "label": label, "states": states,
                "count": sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states)}
               for k, label, states in FOLDERS]

    where, params = [], []
    states = [state] if state else dict((k, s) for k, _, s in FOLDERS).get(folder)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        params += states
    if q:
        # LOWER: SQLite's LIKE ignores case, Postgres's doesn't
        where.append("(LOWER(title) LIKE ? OR LOWER(video_id) LIKE ? OR CAST(game_id AS TEXT) LIKE ?)")
        params += [f"%{q.lower()}%"] * 3
    sql = (f"SELECT {', '.join(VIDEO_COLUMNS)} FROM videos {'WHERE ' + ' AND '.join(where) if where else ''} "
           "ORDER BY published_at DESC LIMIT 500")
    rows = [dict(zip(VIDEO_COLUMNS, r)) for r in con.execute(sql, params).fetchall()]

    w = warehouse()
    games = {}
    if w is not None and rows:
        ids = [r["game_id"] for r in rows if r["game_id"]]
        if ids:
            games = {g[0]: g for g in w.execute(
                f"SELECT game_id, game_date, away_team, home_team FROM games WHERE game_id IN ({','.join('?' * len(ids))})",
                ids)}
    aligned = alignment_details(w)
    for r in rows:
        r["game"] = games.get(r["game_id"])
        r["has_captions"] = (paths.CAPTIONS / f"{r['video_id']}.json").exists()
        r["has_events"] = bool(r["game_id"]) and game_files(r["game_id"])[1][1].exists()
        r["frames"] = frame_count(r["video_id"])
        r["has_readings"] = (paths.FRAMES / r["video_id"] / "index.json").exists()
        r["alignment"] = aligned.get(r["video_id"])
    return render_template("videos.html", rows=rows, folders=folders, folder=folder, state=state, q=q,
                           stages=STAGE_LABELS, busy=pipeline.is_running(), cooldown=cooldown_text())


@app.route("/videos/<video_id>")
def video_page(video_id: str):
    con = ops.connect()
    con.row_factory = None
    row = con.execute(f"SELECT {', '.join(VIDEO_COLUMNS)} FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if row is None:
        abort(404)
    v = dict(zip(VIDEO_COLUMNS, row))
    steps = con.execute("SELECT run_id, stage, started_at, status, error, youtube_requests FROM steps "
                        "WHERE video_id = ? ORDER BY step_id DESC LIMIT 50", (video_id,)).fetchall()

    w = warehouse()
    game, counts = None, {}
    if w is not None and v["game_id"]:
        game = w.execute("SELECT game_id, game_date, away_name, home_name, away_goals, home_goals, ended_in, "
                         "game_type, venue FROM games WHERE game_id = ?", (v["game_id"],)).fetchone()
        counts = {
            "caption lines": w.execute("SELECT COUNT(*) FROM captions WHERE video_id = ?", (video_id,)).fetchone()[0],
            "events": w.execute("SELECT COUNT(*) FROM events WHERE game_id = ?", (v["game_id"],)).fetchone()[0],
            "events placed in the video": w.execute(
                "SELECT COUNT(*) FROM events WHERE game_id = ? AND video_sec IS NOT NULL",
                (v["game_id"],)).fetchone()[0],
            "shots": w.execute("SELECT COUNT(*) FROM shots WHERE game_id = ?", (v["game_id"],)).fetchone()[0],
        }

    files = [("Captions", paths.CAPTIONS / f"{video_id}.json")] + game_files(v["game_id"])
    files.append(("Scoreboard readings", paths.FRAMES / video_id / "index.json"))
    files = [{"label": label, "path": p.relative_to(paths.DATA) if p.is_relative_to(paths.DATA) else p,
              "size": p.stat().st_size if p.exists() else None,
              "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)) if p.exists() else None}
             for label, p in files]
    return render_template("video.html", v=v, steps=steps, game=game, counts=counts, files=files,
                           frames=frame_count(video_id), alignment=alignment_details(w, video_id).get(video_id),
                           stages=STAGE_LABELS, busy=pipeline.is_running(), cooldown=cooldown_text())


@app.post("/videos/process")
def process_videos():
    stage = request.form.get("stage")
    ids = [i for i in request.form.getlist("video_ids") if VIDEO_ID_RE.fullmatch(i)]
    back = request.form.get("back") or url_for("videos")
    if stage not in STAGE_LABELS or not ids:
        flash("Choose at least one video and an action.")
        return redirect(back)
    if pipeline.is_running():
        flash("A run is already in progress; wait for it to finish.")
        return redirect(back)
    spawn("process", stage, *ids)
    flash(f"Started: {STAGE_LABELS[stage].lower()} for {len(ids)} video(s). Each video first gets any earlier "
          "stage it still needs. Progress is under Overview → Recent runs.")
    return redirect(back)


@app.post("/videos/<video_id>/game")
def set_game(video_id: str):
    back = url_for("video_page", video_id=video_id)
    try:
        game_id = int(request.form.get("game_id", "").strip())
    except ValueError:
        flash("A game ID is a number, like 2020020290.")
        return redirect(back)
    if pipeline.is_running():
        flash("A run is in progress; set the game when it has finished.")
        return redirect(back)
    pipeline.set_game(video_id, game_id)
    flash(f"Game set to {game_id}. The video is back at the caption stage; run any action to load it under this game.")
    return redirect(back)


def cooldown_text():
    until = ratelimit.cooldown_until()
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(until)) if until > time.time() else None


@app.post("/run")
def start_run():
    mode = request.form.get("mode", "transcripts")
    if mode not in MODES:
        abort(400)
    if pipeline.is_running():
        flash("A run is already in progress; wait for it to finish.")
        return redirect(url_for("index"))
    spawn("run", *MODES[mode])
    flash(f"Started a {mode} run.")
    return redirect(url_for("index"))


@app.template_filter("pretty_json")
def pretty_json(value):
    if not value:
        return ""
    try:
        return json.dumps(json.loads(value), indent=1)
    except (TypeError, ValueError):
        return value


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
