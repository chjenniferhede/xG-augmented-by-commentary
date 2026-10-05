# xG augmented by commentary

Links every shot in an NHL game to what the broadcast commentators said about it, so that
expected-goals (xG) data can be studied alongside the play-by-play commentary.

Games come from the NHL's "FULL REPLAY" videos on YouTube. A weekly job finds new replays,
loads their captions and MoneyPuck's shot data, and aligns the video to the game clock, slowly
and with strict rate limits. A local dashboard shows what it did and can start a run.

## Data

All data lives under one folder, `data/` in the repo by default (set `XG_DATA_DIR` to use
another). None of it is in git.

```
data/
  raw/                          source data as downloaded (the database is rebuilt from this)
    moneypuck/                  season shot zips, games/<season>/<game_id>.csv
    nhl/                        game boxscores, schedule responses
    youtube/captions/           <video_id>.json: caption track + video metadata + NHL game ID
    youtube/frames/<video_id>/  scoreboard crops (JPEG) + index.json with their OCR text;
                                the JPEGs are deleted once alignment passes (XG_KEEP_FRAMES=1 keeps them)
  db/                           SQLite files, used only when DATABASE_URL is unset (tests, offline)
    moneypuck.sqlite            the warehouse (below)
    pipeline.sqlite             operations: runs, steps, requests, video queue
  logs/                         one log file per pipeline run
```

### Databases

With `DATABASE_URL` set in `.env`, both databases live in Postgres (Supabase): the warehouse in
schema `warehouse` and the operations data in schema `ops`. The pipeline connects as its own
login, `xg_pipeline`, which owns those two schemas and can't read or change anything else in
the database (the project shares it with another app). Neither schema is exposed through
Supabase's Data API, and row level security is on.

Without `DATABASE_URL`, the same tables are SQLite files in `data/db/`. The tests always use
SQLite. `scripts/db/copy_sqlite_to_postgres.py` copied the SQLite data into Postgres for the move.

Raw files stay on the machine that runs the pipeline: `data/raw/` is the source of truth, and
`pipeline.py rebuild` re-creates the warehouse from it.

### Warehouse

| Table | Contents |
|---|---|
| `games` | One row per game: season, regular season or playoff, date, venue, teams, final score, how it ended (`REG`, `OT`, `SO`), and the replay's `video_id` and title |
| `events` | Every game event (faceoffs, hits, shots, goals, whistles, penalties) from MoneyPuck, plus `calibrated_transcript_time` |
| `shots` | Shot attempts (shots on goal, misses, goals): location, shot type, game situation and MoneyPuck's xG (`xGoal`). Joins to `events` on `(game_id, event_id)` |
| `transcript` | Caption lines from the replay (`start_sec`, `end_sec`, `text`), plus `calibrated_game_time` and `in_play` |
| `loads` | Provenance: one row per batch of loaded data, with its source URLs, raw files and their SHA-256 hashes, code version (git commit) and pipeline run. Every row in the tables above has a `load_id` (the alignment columns have `alignment_load_id`); alignment loads also store quality metrics in `details` |
| `timeline` (view) | Events and caption lines on one timeline per game, sorted by transcript time |
| `shot_commentary` (view) | Each shot with its xG, a video link and the commentary around it |

Time columns:

- `events.time`: game seconds elapsed (the clock runs 20:00 → 0:00 in each period; overtime
  follows, 5 minutes in the regular season and 20 in the playoffs).
- `events.calibrated_transcript_time`: when the event happens in the video, on the same
  timeline as `transcript.start_sec`.
- `transcript.calibrated_game_time`: game time when the line was spoken.
- `transcript.in_play`: `1` if the line was spoken while the clock was running, `0` during a
  stoppage (whistle, goal celebration, replay). During a stoppage the game time is the frozen
  clock, and the commentary is often about a play that already happened.

`shot_commentary` lists each shot with its period clock, shooter, result, shot type, distance,
xG, the video time, a link that starts 5 s before the shot (so the build-up is included), and
the caption lines overlapping 4 s before to 10 s after it.

Example: every shot with the commentary spoken around it.

```sql
SELECT s.shooterName, s.event, ROUND(CAST(s.xGoal AS NUMERIC), 3) AS xg, t.in_play, t.text
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN transcript t
  ON t.game_id = s.game_id
 AND t.end_sec   >= e.calibrated_transcript_time - 3
 AND t.start_sec <= e.calibrated_transcript_time + 10
ORDER BY s.game_id, e.calibrated_transcript_time, t.start_sec;
```

Caption lines are 5–10 second blocks, so a line that starts a few seconds before the event can
still contain the call. Match on overlap, not only on start time.

### Schema changes

The schema is defined by numbered SQL files in `scripts/db/migrations/` (one folder per
database). Connecting applies any pending ones and records them in `schema_version`; the
scripts never create or alter tables themselves. To change the schema, add the next numbered
file. `.venv/bin/python scripts/db/migrate.py status` lists what's applied.

A file named `NNNN_x.postgres.sql` replaces `NNNN_x.sql` on Postgres. The initial migrations
have both versions (ids, views and row level security differ); a later change written in
plain SQL can be a single file. Postgres stores MoneyPuck's camelCase column names in
lowercase, so write them unquoted (`xGoal` and `xgoal` both work).

## The pipeline

[scripts/pipeline.py](scripts/pipeline.py) runs four stages, newest videos first:

1. **Discover** ([scripts/discover.py](scripts/discover.py)): the official YouTube Data API
   lists the NHL channel's uploads (1 quota unit per 50 videos) and queues titles starting with
   "FULL REPLAY". The API only returns the newest 20,000 uploads, so older replays are found by
   a slow backfill with search (100 units per page, 5 pages per run). Both stay far inside the
   free 10,000 units a day.
2. **Captions** ([scripts/load_transcript.py](scripts/load_transcript.py)): the video's English
   caption track, and the NHL game it shows, worked out from the title with the NHL's schedule
   API (teams, then the date, or the season and "Game N" for playoffs). Titles that fit several
   games are left as `game_unresolved` with the candidates listed.
3. **Game data** ([scripts/build_db.py](scripts/build_db.py)): MoneyPuck's play-by-play and the
   season's shot file. Older games MoneyPuck has no per-game file for (a 2012–13 playoff game,
   for one) become `no_game_data`.
4. **Alignment** ([scripts/align_video.py](scripts/align_video.py)): video time ↔ game time, by
   reading the scoreboard clock (below).

Each video moves through `discovered → transcribed → game_loaded → aligned`, or stops at
`no_captions` (retried after 30 days), `game_unresolved`, `no_game_data` or
`alignment_unverified`. A step that fails unexpectedly is retried the next day.

```sh
.venv/bin/python scripts/pipeline.py run                    # all four stages
.venv/bin/python scripts/pipeline.py run --max-align 0      # skip alignment (no frame reads)
.venv/bin/python scripts/pipeline.py run --dry-run          # show what would run; no network
.venv/bin/python scripts/pipeline.py status                 # queue, cooldown, recent runs
.venv/bin/python scripts/pipeline.py add "<youtube link>" --game-id <nhl_game_id>
.venv/bin/python scripts/pipeline.py process align <video_id> [<video_id> ...]
.venv/bin/python scripts/pipeline.py rebuild                # rebuild the warehouse from data/raw, offline
```

`add` queues a video by hand; with `--game-id` it also unsticks a `game_unresolved` video.
`process <captions|game_data|align>` takes chosen videos through whatever stages they still need
up to that one, ignoring queue order and retry dates; `align` always re-runs. The dashboard's
Videos page does the same from checkboxes, and each video's page can set its game ID.
Defaults per run: captions for up to 20 videos, alignment for up to 3 games. Only one run can
happen at a time.

### Rate limits

Every outbound request goes through [scripts/ratelimit.py](scripts/ratelimit.py). The limits
below apply to **scheduled runs only** (`--trigger schedule`). Runs started by hand (dashboard,
`make`, CLI, the standalone scripts) skip the spacing, caps, budget and cooldown. If YouTube
refuses one of their requests, only that video is skipped. The refusal still starts the cooldown,
once per run, so the next scheduled run backs off.

| Requests | Minimum spacing (±30% jitter) | Cap |
|---|---|---|
| YouTube pages and captions | 10 s | 60 per hour |
| YouTube video frames | 2 s, one at a time | 600 per hour |
| YouTube Data API (official) | 0.2 s | quota |
| NHL API, MoneyPuck | 1 s | |

- **Per-run budget:** at most 800 YouTube scraping requests per run (`XG_YOUTUBE_BUDGET`), about
  20 caption fetches plus 3 alignments.
- **Spacing holds across processes:** it's read from the logged requests, so a scheduled run and
  a dashboard run can't stack.
- **Circuit breaker:** if YouTube refuses a request (IP block, HTTP 429/403, bot check), all
  YouTube requests stop and a cooldown starts: 24 hours, doubling on each repeat block up to 7
  days. Discovery and MoneyPuck stages keep running. Work done before the block is kept. The
  block reason records YouTube's error (status code and message).

### Telemetry

The ops database records every run (trigger, arguments, status, summary), every step
(stage, video, game, status, error, YouTube requests used) and every outbound request (kind,
time, status, latency). Each run also writes `data/logs/run_<id>.log`.

### Dashboard

```sh
.venv/bin/python dashboard/app.py      # http://127.0.0.1:5057 (local only)
```

Shows recent runs, each run's steps, requests and log, the video queue, request usage against
the limits, and any YouTube cooldown. "Fetch new transcripts" starts a run without alignment;
"Full run" includes it. Buttons are disabled while a run is in progress.

### Weekly schedule (macOS)

```sh
.venv/bin/python scripts/install_schedule.py install     # every Monday 03:00
.venv/bin/python scripts/install_schedule.py status
.venv/bin/python scripts/install_schedule.py uninstall
launchctl kickstart gui/$(id -u)/com.xg-commentary.pipeline   # run it once now
```

If the Mac is asleep at 03:00, launchd runs the job when it wakes. Output goes to
`data/logs/launchd.log`. Because the repo is under `~/Documents`, macOS may block the background
job from reading it ("Operation not permitted" in that log); allow it by giving
`.venv/bin/python` access to the Documents folder (System Settings → Privacy & Security →
Files and Folders, or Full Disk Access), or move the repo elsewhere.

## Alignment

A replay runs longer than the game clock: stoppages, replays and celebrations add video while
the clock is stopped.

- Between two faceoffs the clock runs continuously, so for that stretch
  `video time = game time + offset`. One reading of the clock during live play pins a
  stretch's offset exactly.
- Frames are read only where needed: one every 60 s of video, then bisection for stretches with
  no reading yet (video time only moves forward with game time), then a scan every 3 s for any
  left, usually right after goals, where long replays hide the scoreboard. About 250 frames
  cover a game; at the rate limits, with each remote frame grab taking a few seconds, a game
  takes about 25 minutes.
- Frames are pulled one at a time from the 720p stream (`yt-dlp` for the stream URL, then
  `ffmpeg -ss <t> -frames:v 1`); the video is never downloaded in full.
- The scoreboard is found automatically: a few full frames are read with
  [RapidOCR](https://github.com/RapidAI/RapidOCR) (PaddleOCR models on ONNX Runtime, CPU only),
  and the strip holding a period label (`1ST`, `2ND`, `P3`, `OT`, `2OT`, …) next to a clock
  becomes the crop for every later frame. Each crop is saved, and its OCR text is re-parsed on
  every run, so parser changes never need YouTube again.
- **Quality check** before a game counts as `aligned`: at least 90% of stretches pinned by a
  clock reading, offsets never going backwards by more than 3 s, and, where the scoreboard's
  score can be read, at least 90% agreement with MoneyPuck's score. Otherwise the game is
  `alignment_unverified`: its values are written but flagged. The metrics are stored on the
  alignment's row in `loads`.

On the Canadiens–Sharks game (March 3, 2026), 63 of 64 stretches are pinned by the clock,
125 of 127 scoreboard scores match MoneyPuck, and goals land within a few seconds of their live
calls in the captions.

An earlier approach matched player names in the captions to events. It was off by 15–70 s,
because replay commentary repeats names, captions drop out in crowd noise, and names are often
misspelled. It is not included.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # add YOUTUBE_API_KEY (free: Google Cloud → YouTube Data API v3)
.venv/bin/python -m unittest discover tests
```

Use the virtual environment: the OCR packages need numpy 2 and OpenCV, which can conflict with
a base Anaconda install. The single-game scripts also run on their own:

```sh
.venv/bin/python scripts/load_transcript.py "https://www.youtube.com/watch?v=<id>" [--game-id <id>]
.venv/bin/python scripts/build_db.py <nhl_game_id>
.venv/bin/python scripts/align_video.py <video_id>
```

Quote links: in zsh, an unquoted `?` is read as a filename pattern and the command fails.

## Sources and terms

- Game events, shots and xG: [MoneyPuck.com](https://moneypuck.com/data.htm). MoneyPuck asks
  for credit when its data is used.
- Game details and schedules: the NHL's public API (`api-web.nhle.com`).
- Replay discovery: the official YouTube Data API.
- Captions and video frames: YouTube, fetched with `youtube-transcript-api` and `yt-dlp`.
  These tools use YouTube's web-player endpoints, not an official API, and YouTube's Terms of
  Service restrict this kind of access. The rate limits above keep the volume low; they don't
  change that. Keep the downloaded data private.
