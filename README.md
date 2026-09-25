# xG augmented by commentary

Links every shot in an NHL game to what the broadcast commentators said about it, so that
expected-goals (xG) data can be studied alongside the play-by-play commentary.

The first game is **Montréal Canadiens @ San Jose Sharks, March 3, 2026** (NHL game
`2025020969`, final 7–5 SJS), using the NHL's
[full-game replay](https://www.youtube.com/watch?v=fqSsQgFJA4g) on YouTube.

## Data

`data/moneypuck.sqlite` holds three tables and one view:

| Table | Rows | Contents |
|---|---|---|
| `events` | 339 | Every game event (faceoffs, hits, shots, goals, whistles, penalties) from MoneyPuck, plus `calibrated_transcript_time` |
| `shots` | 94 | Shot attempts (shots on goal, misses, goals): location, shot type, game situation and MoneyPuck's xG (`xGoal`). Joins to `events` on `(game_id, event_id)` |
| `transcript` | 516 | Caption lines from the replay (`start_sec`, `end_sec`, `text`), plus `calibrated_game_time` and `in_play` |
| `timeline` (view) | 852 | Events and caption lines on one timeline, sorted by transcript time |

Time columns:

- `events.time`: game seconds elapsed (0–3600; the clock runs 20:00 → 0:00 in each of 3 periods).
- `events.calibrated_transcript_time`: when the event happens in the video, on the same
  timeline as `transcript.start_sec`.
- `transcript.calibrated_game_time`: game time when the line was spoken.
- `transcript.in_play`: `1` if the line was spoken while the clock was running, `0` during a
  stoppage (whistle, goal celebration, replay). During a stoppage the game time is the frozen
  clock, and the commentary is often about a play that already happened.

`data/shot_commentary.csv` and `data/shot_commentary.md` list each shot with its xG, a link to
that moment in the video, and the nearby commentary.

Example: every shot with the commentary spoken in the 10 seconds after it.

```sql
SELECT s.shooterName, s.event, ROUND(s.xGoal, 3) AS xg, t.in_play, t.text
FROM shots s
JOIN events e USING (game_id, event_id)
JOIN transcript t
  ON t.game_id = s.game_id
 AND t.end_sec   >= e.calibrated_transcript_time - 3
 AND t.start_sec <= e.calibrated_transcript_time + 10
ORDER BY e.calibrated_transcript_time, t.start_sec;
```

Caption lines are 5–10 second blocks, so a line that starts a few seconds before the event can
still contain the call. Match on overlap, not only on start time.

## How it works

1. **Game data** ([scripts/build_db.py](scripts/build_db.py)): downloads MoneyPuck's
   play-by-play for the game and its 2025-26 season shot file, and loads `events` and `shots`.
2. **Transcript** ([scripts/load_transcript.py](scripts/load_transcript.py)): fetches the
   replay's English caption track and loads it into `transcript`.
3. **Alignment** ([scripts/align_video.py](scripts/align_video.py)): maps video time to game
   time by reading the game clock off the broadcast scoreboard.

### Alignment

The video runs 74 minutes for 60 minutes of game clock: the extra time is stoppages, replays and
celebrations, when the video keeps running and the clock does not.

- Between two faceoffs the clock runs continuously, so for that stretch
  `video time = game time + offset`. The game splits into 64 such stretches.
- One reading of the clock during live play pins a stretch's offset exactly. The script
  reads frames only where it needs them:
  1. a coarse pass, one frame every 60 s of video;
  2. bisection for stretches with no reading yet (video time only moves forward with game time,
     so each reading says whether to look earlier or later);
  3. a scan every 3 s for stretches still missing, usually right after goals, where long replays
     hide the scoreboard.
- Frames are pulled one at a time from the 720p stream (`yt-dlp` for the stream URL, then
  `ffmpeg -ss <t> -frames:v 1`), so the video is never downloaded in full. 240p was too blurry
  to read the clock digits reliably.
- The scoreboard strip is read with [RapidOCR](https://github.com/RapidAI/RapidOCR) (PaddleOCR
  models on ONNX Runtime, CPU only). The period (`1ST`/`2ND`/`3RD`) and clock (`M:SS`, or
  `SS.t` in the last minute) become game time: `(period − 1) × 1200 + (1200 − clock)`.

About 240 frames cover the whole game; a run from an empty cache takes about 3 minutes.

**Validation:** readings within the same stretch agree within 1 s. The score shown on the
scoreboard matches MoneyPuck in every reading. Goals land within a few seconds of their live
calls in the captions. 63 of 64 stretches are pinned by a clock reading; the last lasts 3 s of
play and is bounded to within about 1 s by the readings on either side.

An earlier approach matched player names in the captions to events. It was off by 15–70 s,
because replay commentary repeats names, captions drop out in crowd noise, and names are
often misspelled ("Asteroth" for Askarov). It is not included.

## Running it

The base scripts:

```sh
pip install -r requirements.txt
python scripts/build_db.py          # events, shots
python scripts/load_transcript.py   # transcript
```

The alignment needs OCR packages that pull in numpy 2 and OpenCV, so install them in a separate
virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-video.txt
.venv/bin/python scripts/align_video.py   # calibrated times, in_play, timeline view
```

Run them in this order. Reloading the transcript clears its calibrated columns; run the
alignment again afterwards. Each script accepts other IDs: `build_db.py <nhl_game_id>`,
`load_transcript.py <youtube_video_id> <nhl_game_id>`, `align_video.py <youtube_video_id>`.

Downloads are cached in `data/raw/` (git-ignored): MoneyPuck files, the captions, and every
scoreboard reading. Re-runs use the cache and do not contact YouTube unless a new frame is
needed.

## Sources and terms

- Game events, shots and xG: [MoneyPuck.com](https://moneypuck.com/data.htm). MoneyPuck asks
  for credit when its data is used.
- Captions and video frames: YouTube, fetched with `youtube-transcript-api` and `yt-dlp`.
  These tools use YouTube's web-player endpoints, not an official API, and YouTube's Terms of
  Service restrict this kind of access. YouTube rate-limits heavy use: many requests in a short
  time can get an IP temporarily blocked.
