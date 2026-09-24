"""Align a full-game replay video to game time by reading the broadcast scorebug clock.

Between faceoffs the game clock runs, so within each play segment
    video_sec = game_sec + offset
and one scorebug reading taken while the clock runs inside a segment pins its offset.
Frames are read only where needed, as single 720p frames pulled from the stream
(240p is too blurry to OCR the clock reliably): a coarse pass every 60 s, then
bisection on video time (monotone in game time) for segments still unpinned, then a
dense scan for any left over.

Writes to data/moneypuck.sqlite:
    events.calibrated_transcript_time   when each event happens on the transcript's
                                        (video) timeline
    transcript.calibrated_game_time     game time when each caption was spoken
    transcript.in_play                  1 if spoken while the clock was running; 0 during
                                        stoppages/replays (game time is then the frozen clock)

Every frame read is cached in data/raw/scorebug_{video_id}.json, so re-runs only
contact YouTube if a new frame is needed.

Needs the packages in requirements-video.txt (use a separate venv; rapidocr pulls in
numpy 2 / opencv, which conflicts with the anaconda base env).

Usage: python scripts/align_video.py [youtube_video_id]
"""
import json
import re
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import yt_dlp
from rapidocr_onnxruntime import RapidOCR

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "moneypuck.sqlite"
RAW_DIR = ROOT / "data" / "raw"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FORMAT = "298"  # 1280x720 mp4 video-only
SCOREBUG_BOX = (slice(18, 80), slice(0, 590))  # rows, cols of the 720p frame
MARGIN = 1.0  # a reading must be this far inside a segment to count as clock running
COARSE_STEP = 60
WORKERS = 6

_local = threading.local()


def ocr(img):
    if not hasattr(_local, "engine"):
        _local.engine = RapidOCR()
    res, _ = _local.engine(img)
    return [r[1] for r in res] if res else []


def parse_scorebug(tokens):
    """-> (period, clock_remaining_sec) from OCR tokens like ['2ND', '14:57'] or ['2ND', ':52.6']."""
    for i, tok in enumerate(tokens[:-1]):
        period = {"1ST": 1, "2ND": 2, "3RD": 3}.get(tok.upper().replace(" ", ""))
        if not period:
            continue
        nxt = tokens[i + 1]
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", nxt)
        if m and int(m.group(1)) <= 20 and int(m.group(2)) < 60:
            return period, int(m.group(1)) * 60 + int(m.group(2))
        m = re.fullmatch(r":?(\d{1,2})\.(\d)", nxt)
        if m:
            return period, int(m.group(1)) + int(m.group(2)) / 10
    return None, None


class Scorebug:
    def __init__(self, video_id):
        self.cache_path = RAW_DIR / f"scorebug_{video_id}.json"
        stored = json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        self.cache = stored.get("readings", {})
        self.duration = stored.get("duration")
        self.lock = threading.Lock()
        self._url = None
        self.video_id = video_id
        if self.duration is None:
            self._resolve()

    def _resolve(self):
        # Only contacts YouTube when a frame is needed that is not in the cache.
        with yt_dlp.YoutubeDL({"quiet": True, "format": FORMAT}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={self.video_id}", download=False)
        self.duration = float(info["duration"])
        self._url = info["url"]

    def read(self, v):
        key = f"{v:.1f}"
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            if self._url is None:
                self._resolve()
        raw = subprocess.run(
            [FFMPEG, "-loglevel", "error", "-ss", f"{v:.2f}", "-i", self._url, "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True,
        ).stdout
        out = {"v": round(v, 1), "tokens": [], "period": None, "clock": None, "g": None}
        if len(raw) == 720 * 1280 * 3:
            frame = np.frombuffer(raw, np.uint8).reshape(720, 1280, 3)
            out["tokens"] = ocr(frame[SCOREBUG_BOX])
            period, clock = parse_scorebug(out["tokens"])
            if period:
                out.update(period=period, clock=clock, g=(period - 1) * 1200 + 1200 - clock)
        with self.lock:
            self.cache[key] = out
        return out

    def read_many(self, vs):
        with ThreadPoolExecutor(WORKERS) as ex:
            return list(ex.map(self.read, vs))

    def readings(self):
        return sorted((r for r in self.cache.values() if r["g"] is not None), key=lambda r: r["v"])

    def save(self):
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.cache_path.write_text(json.dumps({"duration": self.duration, "readings": self.cache}))


def play_segments(events):
    faceoffs = sorted({t for _, t, ev in events if ev == "FAC"})
    end = max(t for _, t, _ in events)
    return [(a, b) for a, b in zip(faceoffs, faceoffs[1:] + [end]) if b > a]


def align(bug, segments):
    def seg_of(g):
        for k, (a, b) in enumerate(segments):
            if a + MARGIN < g < b - MARGIN:
                return k
        return None

    def bracket(k):
        a, b = segments[k]
        rs = bug.readings()
        lo = max([r for r in rs if r["g"] <= a + MARGIN], key=lambda r: r["v"], default=None)
        hi = min([r for r in rs if r["g"] >= b - MARGIN], key=lambda r: r["v"], default=None)
        return lo, hi

    def pinned():
        found = {}
        for r in bug.readings():
            k = seg_of(r["g"])
            if k is not None:
                found.setdefault(k, []).append(r["v"] - r["g"])
        return found

    def bisect(k, max_reads=10):
        for _ in range(max_reads):
            lo, hi = bracket(k)
            v_lo, v_hi = (lo["v"] if lo else 0.0), (hi["v"] if hi else bug.duration)
            if v_hi - v_lo < 1.0:
                return
            mid = (v_lo + v_hi) / 2
            r = bug.read(mid)
            for nudge in (2.5, 5.0, 7.5):  # no scorebug on screen (replay, crowd shot)
                if r["g"] is not None or mid + nudge >= v_hi:
                    break
                r = bug.read(mid + nudge)
            if r["g"] is None or seg_of(r["g"]) == k:
                return

    def scan(k, step=3.0):
        lo, hi = bracket(k)
        v_lo, v_hi = (lo["v"] if lo else 0.0), (hi["v"] if hi else bug.duration)
        bug.read_many(list(np.arange(v_lo + step / 2, v_hi, step)))

    bug.read_many([float(v) for v in np.arange(COARSE_STEP / 4, bug.duration, COARSE_STEP)])
    for stage in (bisect, scan):
        missing = [k for k in range(len(segments)) if k not in pinned()]
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(stage, missing))
        bug.save()

    found = pinned()
    rows = []
    for k, (a, b) in enumerate(segments):
        if k in found:
            rows.append((k, a, b, float(np.median(found[k])), len(found[k]), "clock"))
        else:
            # Segment too short to catch the clock running; bound it by the readings on either side.
            lo, hi = bracket(k)
            offset = float(np.mean([r["v"] - r["g"] for r in (lo, hi) if r])) if (lo or hi) else None
            rows.append((k, a, b, offset, 0, "bracket"))
    return rows


def calibrated_transcript_times(events, seg_rows):
    out = []
    for eid, t, ev in events:
        # An event at a segment boundary belongs to the segment it happens in: faceoffs and
        # period starts open the next segment; whistles, goals, penalties close the previous one.
        opens = ev in ("FAC", "PSTR")
        k = next((row for row in seg_rows if (row[1] <= t < row[2] if opens else row[1] < t <= row[2])), None)
        if k is None:
            k = seg_rows[0] if t <= seg_rows[0][1] else seg_rows[-1]
        out.append((eid, t + k[3] if k[3] is not None else None, k[0]))
    return out


def calibrated_game_times(transcript, seg_rows):
    out = []
    for seq, start in transcript:
        game_sec, in_play = None, 0
        for k, a, b, offset, _, _ in seg_rows:
            if offset is None:
                continue
            if a + offset <= start <= b + offset:
                game_sec, in_play = start - offset, 1
                break
            if start < a + offset:
                game_sec = a  # stoppage before this segment: clock frozen at its faceoff time
                break
        else:
            game_sec = seg_rows[-1][2]
        out.append((seq, game_sec, in_play))
    return out


def add_column(con, table, column, sql_type):
    if column not in [row[1] for row in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def main(video_id):
    con = sqlite3.connect(DB_PATH)
    game_id = con.execute("SELECT game_id FROM transcript WHERE video_id = ? LIMIT 1", (video_id,)).fetchone()[0]
    events = con.execute(
        "SELECT event_id, time, event FROM events WHERE game_id = ? ORDER BY event_id", (game_id,)
    ).fetchall()
    transcript = con.execute(
        "SELECT seq, start_sec FROM transcript WHERE video_id = ? ORDER BY seq", (video_id,)
    ).fetchall()

    bug = Scorebug(video_id)
    segments = play_segments(events)
    seg_rows = align(bug, segments)
    bug.save()

    with con:
        add_column(con, "events", "calibrated_transcript_time", "REAL")
        add_column(con, "transcript", "calibrated_game_time", "REAL")
        add_column(con, "transcript", "in_play", "INTEGER")
        con.executemany(
            "UPDATE events SET calibrated_transcript_time = ? WHERE game_id = ? AND event_id = ?",
            [(v, game_id, eid) for eid, v, _ in calibrated_transcript_times(events, seg_rows)],
        )
        con.executemany(
            "UPDATE transcript SET calibrated_game_time = ?, in_play = ? WHERE video_id = ? AND seq = ?",
            [(g, in_play, video_id, seq) for seq, g, in_play in calibrated_game_times(transcript, seg_rows)],
        )
        # Events and captions interleaved on one timeline, like a script of the broadcast.
        con.executescript(
            """
            DROP VIEW IF EXISTS timeline;
            CREATE VIEW timeline AS
            SELECT transcript_time, game_time, in_play, event_description, text FROM (
                SELECT calibrated_transcript_time AS transcript_time, time AS game_time,
                       NULL AS in_play, eventDescriptionRaw AS event_description, NULL AS text,
                       0 AS is_caption
                FROM events
                WHERE calibrated_transcript_time IS NOT NULL AND TRIM(COALESCE(eventDescriptionRaw, '')) != ''
                UNION ALL
                SELECT start_sec, calibrated_game_time, in_play, NULL, text, 1
                FROM transcript
            )
            ORDER BY transcript_time, is_caption;
            """
        )

    n_clock = sum(1 for r in seg_rows if r[5] == "clock")
    print(f"{len(bug.cache)} frames read; {n_clock}/{len(seg_rows)} play segments pinned by clock, "
          f"{len(seg_rows) - n_clock} bounded by neighbours")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fqSsQgFJA4g")
