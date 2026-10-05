"""Align a full-game replay video to game time by reading the broadcast scoreboard clock.

Between faceoffs the game clock runs, so within each play segment
    video_sec = game_sec + offset
and one scoreboard reading taken while the clock runs inside a segment pins its offset.
Frames are read only where needed, as single 720p frames pulled from the stream: a coarse
pass every 60 s, then bisection on video time (monotone in game time) for segments still
unpinned, then a dense scan for any left over.

The scoreboard is located automatically: a few full frames are OCR'd for a period label with a
clock beside, above or below it. If it sits in the top-left corner (nearly every broadcast) that
corner is the crop for every later frame, else a full-width strip around it. Each frame's crop is
kept as a JPEG in data/raw/youtube/frames/<video_id>/ (the raw input), with its OCR text and
boxes in index.json; the text is re-parsed on every run, so parser changes never need YouTube.
Once a video's alignment passes, the pipeline deletes its JPEGs (prune_frames) and keeps index.json.

A quality check runs before results are written: enough segments pinned by the clock, game
time read in video order that (almost) never runs backwards (cuts in the replay skip it forward,
a misread digit usually doesn't), and (where the scoreboard's score can be read) a score that
matches MoneyPuck.
The metrics are saved on the alignment's row in `loads`.

Writes to the warehouse:
    events.calibrated_transcript_time   when each event happens in the video
    transcript.calibrated_game_time     game time when each caption was spoken
    transcript.in_play                  1 = clock running; 0 = stoppage/replay (frozen clock)

Usage: python scripts/align_video.py <youtube_video_id> [--workers N]
"""
import argparse
import io
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import imageio_ffmpeg
import numpy as np
from PIL import Image

import db
import ops
import paths
import ratelimit
import raw_store

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FORMAT = "298/136/22"  # 1280x720 video (mp4 video-only, then fallbacks)
WIDTH, HEIGHT = 1280, 720
OCR_VERSION = 2   # bump when the OCR step changes: readings with crops are re-OCR'd offline
REGION = [0, 180, 0, 800]  # [y0, y1, x0, x1]: the top-left corner, where broadcasts put the scoreboard
MARGIN = 1.0      # a reading must be this far inside a segment to count as clock running
COARSE_STEP = 60
LOCATE_AT = (0.12, 0.2, 0.3, 0.42, 0.55, 0.7, 0.85)  # fractions of the video used to find the scoreboard
FRAME_WORKERS = int(os.environ.get("XG_ALIGN_WORKERS", "1"))  # parallel frame fetches
# max_reversals: share of readings whose game time is lower than the previous reading's (misreads);
# the median per segment absorbs an occasional one.
GATE = {"min_coverage": 0.9, "max_reversals": 0.01, "min_score_match": 0.9, "min_score_readings": 10}
TEAM_ALIASES = {"SJS": ["SJ"], "TBL": ["TB"], "NJD": ["NJ"], "LAK": ["LA"], "VGK": ["VEG", "VGK"]}

_local = threading.local()


class ScoreboardNotFound(Exception):
    """No period label + clock could be found in the sampled frames."""


def ocr_engine():
    if not hasattr(_local, "engine"):
        from rapidocr_onnxruntime import RapidOCR

        _local.engine = RapidOCR()
    return _local.engine


def ocr(img: np.ndarray) -> list:
    """-> [(text, (x0, y0, x1, y1)), ...] in reading order."""
    res, _ = ocr_engine()(img)
    out = []
    for box, text, _score in res or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append((text, (min(xs), min(ys), max(xs), max(ys))))
    return out


# ---- parsing the scoreboard text ----------------------------------------------------------

# A digit may follow the label directly: OCR often merges "1st 16:28" into "1st16:28".
# OCR also reads the O of "OT" as a zero ("0T17:20").
PERIOD_RE = re.compile(r"(?<![A-Z0-9])(1ST|2ND|3RD|P[1-3]|[2-5]?[O0]T)(?![A-Z])")
CLOCK_RE = re.compile(r"(?<![\d:.])(?:(\d{1,2}):(\d{2})|:?(\d{1,2})\.(\d))(?![\d])")


def _game_time(label: str, cm, playoff: bool):
    """Period label + clock match -> (label, clock_remaining_sec, game_sec), or None if impossible.

    Game seconds follow MoneyPuck: periods of 1200 s, then a 300 s overtime in the regular
    season, or 1200 s overtimes in the playoffs.
    """
    if cm.group(1):
        mm, ss = int(cm.group(1)), int(cm.group(2))
        if ss >= 60:
            return None
        clock = mm * 60 + ss
    else:
        clock = int(cm.group(3)) + int(cm.group(4)) / 10
    if label.endswith(("OT", "0T")):
        label = label[:-2] + "OT"
        k = int(label[:-2] or 1)
        length = 1200 if playoff else 300
        if clock > length or (not playoff and k > 1):
            return None
        return label, clock, 3600 + (k - 1) * 1200 + length - clock
    period = int(label[1]) if label.startswith("P") else int(label[0])
    if clock > 1200:
        return None
    return label, clock, (period - 1) * 1200 + 1200 - clock


def _clock_in_text(text: str, playoff: bool):
    """A period label with a clock just before or after it in one string: '2ND 14:57', '14:57 2ND'."""
    for pm in PERIOD_RE.finditer(text):
        after = CLOCK_RE.search(text, pm.end(), pm.end() + 12)
        before = [m for m in CLOCK_RE.finditer(text, max(0, pm.start() - 12), pm.start())]
        cm = after or (before[-1] if before else None)
        hit = cm and _game_time(pm.group(1), cm, playoff)
        if hit:
            return hit
    return None


def read_clock(words: list, playoff: bool):
    """[(text, box), ...] -> (label, clock_remaining_sec, game_sec, box) or None.

    Uses the layout, so any arrangement works: period and clock in one token ('1st16:28'),
    side by side ('2ND 14:57', '14:57 2ND'), or stacked ('1ST' over '2:29', '1.2' over '1ST').
    The clock nearest the period label wins; box covers both.
    """
    best = None
    for text, box in words:
        up = text.upper()
        hit = _clock_in_text(up, playoff)
        if hit:
            return (*hit, list(box))
        h = box[3] - box[1]
        cy = (box[1] + box[3]) / 2
        for pm in PERIOD_RE.finditer(up):
            for t2, b2 in words:
                cm = CLOCK_RE.fullmatch(t2.strip())
                if not cm or b2 is box:
                    continue
                same_line = abs((b2[1] + b2[3]) / 2 - cy) < 0.6 * h and max(b2[0] - box[2], box[0] - b2[2]) < 4 * h
                stacked = (min(box[2], b2[2]) - max(box[0], b2[0]) > -h / 2
                           and max(b2[1] - box[3], box[1] - b2[3]) < 1.5 * h)
                if not (same_line or stacked):
                    continue
                hit = _game_time(pm.group(1), cm, playoff)
                if not hit:
                    continue
                dist = abs((b2[0] + b2[2]) / 2 - (box[0] + box[2]) / 2) + abs((b2[1] + b2[3]) / 2 - cy)
                if best is None or dist < best[0]:
                    merged = [min(box[0], b2[0]), min(box[1], b2[1]), max(box[2], b2[2]), max(box[3], b2[3])]
                    best = (dist, (*hit, merged))
    return best[1] if best else None


def parse_clock(tokens: list, playoff: bool):
    """-> (period_label, clock_remaining_sec, game_sec) or (None, None, None).

    tokens are [text, box] pairs (read by layout), or plain strings from readings OCR'd before
    boxes were kept (joined in reading order; only a clock beside the period is found).
    """
    if tokens and not isinstance(tokens[0], str):
        hit = read_clock(tokens, playoff)
        return hit[:3] if hit else (None, None, None)
    return _clock_in_text(" ".join(tokens).upper(), playoff) or (None, None, None)


def parse_score(tokens: list, away: str, home: str):
    """-> (away_goals, home_goals) when both team labels are followed by a number, else None.

    The number may be glued to the label ('WSH1', 'PITO' with O for 0) or be the next token, never
    further on: if OCR drops a team's score, the next number belongs to the other team.
    """
    texts = [t if isinstance(t, str) else t[0] for t in tokens]
    boxed = bool(tokens) and not isinstance(tokens[0], str)

    def pattern(team):
        names = "|".join(sorted({team, *TEAM_ALIASES.get(team, [])}, key=len, reverse=True))
        return re.compile(rf"(?:{names})\s*([0-9O]{{0,2}})\W*$")

    def label(team):
        """The team label's (glued score or None, box), or None."""
        for text, box in tokens:
            m = pattern(team).search(text.upper().strip())
            if m:
                return (int(m.group(1).replace("O", "0")) if m.group(1) else None), box
        return None

    def gap(a, b):
        return max(b[0] - a[2], a[0] - b[2])

    def score_beside(team, other):
        """By layout: the nearest number on the team label's line, on either side ('VGK 1 2 MTL'
        puts the home score left of its label), at least as tall as the label (shots-on-goal
        figures are smaller), and nearer this label than the other team's: if OCR missed this
        team's score, the number beside it is the other team's."""
        found = label(team)
        if found is None:
            return None
        glued, box = found
        if glued is not None:
            return glued
        rival = label(other)
        h, cy = box[3] - box[1], (box[1] + box[3]) / 2
        best = None
        for t2, b2 in tokens:
            if b2 is box or not re.fullmatch(r"\d{1,2}", t2.strip()) or int(t2) > 15:
                continue
            if abs((b2[1] + b2[3]) / 2 - cy) > 0.6 * h or b2[3] - b2[1] < 0.9 * h:
                continue
            g = gap(box, b2)
            if g >= 4 * h or (rival and abs((b2[1] + b2[3]) / 2 - (rival[1][1] + rival[1][3]) / 2) < 0.6 * h
                              and gap(rival[1], b2) < g):
                continue
            if best is None or g < best[0]:
                best = (g, int(t2))
        return best[1] if best else None

    if boxed:
        a, h = score_beside(away, home), score_beside(home, away)
        return (a, h) if a is not None and h is not None else None

    def score_after(team):
        names = "|".join(sorted({team, *TEAM_ALIASES.get(team, [])}, key=len, reverse=True))
        for i, tok in enumerate(texts):
            # no check before the label: a team logo often reads as a letter ('OCAR')
            m = re.search(rf"(?:{names})\s*([0-9O]{{0,2}})\W*$", tok.upper().strip())
            if not m:
                continue
            num = m.group(1) or (texts[i + 1].upper().strip() if i + 1 < len(texts) else "")
            if re.fullmatch(r"[0-9O]{1,2}", num):
                return int(num.replace("O", "0"))
        return None

    a, h = score_after(away), score_after(home)
    return (a, h) if a is not None and h is not None else None


# ---- frames ------------------------------------------------------------------------------

class Scoreboard:
    """Frames of one video: fetched on demand (rate-limited), stored, OCR'd, parsed."""

    def __init__(self, video_id: str, playoff: bool, workers: int = 1):
        self.video_id = video_id
        self.playoff = playoff
        self.workers = workers
        self.dir = paths.FRAMES / video_id
        self.index_path = self.dir / "index.json"
        index = raw_store.read_json(self.index_path) if self.index_path.exists() else {}
        self.duration = index.get("duration")
        self.box = index.get("box")  # [y0, y1, x0, x1] in the 720p frame
        self.cache = index.get("readings", {})
        self.lock = threading.Lock()
        self._url = None
        self._url_at = 0.0
        self.new_frames = 0

    # -- stream access
    def _resolve(self):
        import yt_dlp

        with ratelimit.request("yt_page", f"stream url {self.video_id}") as r:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": FORMAT}) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={self.video_id}", download=False)
            except Exception as e:
                if ratelimit.is_block(e):
                    r["status"] = "blocked"
                    raise ratelimit.Blocked(str(e)[:200]) from e
                raise
        self.duration = float(info["duration"])
        self._url, self._url_at = info["url"], time.time()

    def _grab(self, v: float) -> np.ndarray:
        """One full 720p frame at video second v."""
        for attempt in (1, 2):
            with self.lock:
                if self._url is None:
                    self._resolve()
                url = self._url
            with ratelimit.request("yt_frame", f"{self.video_id}@{v:.1f}") as r:
                # A stalled stream read must not hang the run (one did, for two hours): ffmpeg gives
                # up on a read after 30 s, and the whole fetch after 90 s counts as a failed frame.
                try:
                    proc = subprocess.run(
                        [FFMPEG, "-loglevel", "error", "-rw_timeout", "30000000", "-ss", f"{v:.2f}", "-i", url,
                         "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                        capture_output=True, timeout=90,
                    )
                except subprocess.TimeoutExpired:
                    ops.log.warning("video %s: frame at %.1f s timed out", self.video_id, v)
                    r["status"] = "error"
                    return None
                if len(proc.stdout) == WIDTH * HEIGHT * 3:
                    with self.lock:
                        self.new_frames += 1
                        if self.new_frames % 25 == 0:
                            ops.log.info("video %s: %d frames fetched (%d cached in total)",
                                         self.video_id, self.new_frames, len(self.cache))
                    return np.frombuffer(proc.stdout, np.uint8).reshape(HEIGHT, WIDTH, 3)
                err = proc.stderr.decode(errors="replace")
                refused = any(s in err for s in ("403", "429", "Forbidden", "Too Many Requests"))
                if refused and attempt == 1:
                    r["status"] = "error"
                    with self.lock:
                        self._url = None  # stream URLs expire after a few hours; get a fresh one
                    continue
                if refused:
                    r["status"] = "blocked"
                    raise ratelimit.Blocked(f"frame request refused: {err.strip()[:200]}")
                r["status"] = "error"
                return None
        return None

    @staticmethod
    def _jpeg(img: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, "JPEG", quality=90)
        return buf.getvalue()

    def _full(self, v: float) -> np.ndarray:
        """A full frame for locating the scoreboard: the saved one if an earlier run fetched it."""
        path = self.dir / f"full_{v:.1f}.jpg"
        if path.exists():
            return np.array(Image.open(path))
        frame = self._grab(v)
        if frame is not None:
            raw_store.write_bytes(path, self._jpeg(frame))
        return frame

    # -- scoreboard location
    def locate(self) -> None:
        """Find the scoreboard from a few full frames, unless it's already known.

        The crop is the top-left REGION when the scoreboard is there (nearly every broadcast);
        otherwise a full-width strip around wherever the period and clock were found.
        """
        if self.box:
            return
        if self.duration is None:
            self._resolve()
            self.save()
        hits = []
        for frac in LOCATE_AT:
            frame = self._full(round(self.duration * frac, 1))
            if frame is None:
                continue
            hit = read_clock(ocr(frame), self.playoff)
            if hit:
                hits.append(hit[3])
            if len(hits) >= 3:
                break
        if len(hits) < 2:
            raise ScoreboardNotFound(f"no period + clock found in {len(LOCATE_AT)} sampled frames")
        y0, y1, x0, x1 = REGION
        if all(x0 <= b[0] and b[2] <= x1 and y0 <= b[1] and b[3] <= y1 for b in hits):
            self.box = list(REGION)
        else:
            top = int(np.median([b[1] for b in hits]))
            bottom = int(np.median([b[3] for b in hits]))
            pad = max(40, 2 * (bottom - top))
            self.box = [max(0, top - pad), min(HEIGHT, bottom + pad), 0, WIDTH]
        ops.log.info("video %s: scoreboard crop rows %d-%d, columns %d-%d", self.video_id, *self.box)
        self.save()

    # -- readings
    def read(self, v: float) -> dict:
        key = f"{v:.1f}"
        with self.lock:
            r = self.cache.get(key)
        if r is not None:
            # crops of aligned videos are deleted (prune_frames); their saved OCR text stays in use
            if r.get("crop") and r.get("ocr_version") != OCR_VERSION and (self.dir / r["crop"]).exists():
                img = np.array(Image.open(self.dir / r["crop"]))
                r.update(tokens=self._tokens(img), ocr_version=OCR_VERSION)
            return self._parsed(r)
        frame = self._grab(v)
        r = {"v": round(v, 1), "crop": None, "tokens": [], "ocr_version": OCR_VERSION}
        if frame is not None:
            y0, y1, x0, x1 = self.box
            crop = frame[y0:y1, x0:x1]
            r["crop"] = f"{key}.jpg"
            raw_store.write_bytes(self.dir / r["crop"], self._jpeg(crop))
            r["tokens"] = self._tokens(crop)
        with self.lock:
            self.cache[key] = r
        return self._parsed(r)

    @staticmethod
    def _tokens(img: np.ndarray) -> list:
        """OCR as [text, [x0, y0, x1, y1]] pairs: the parser reads the scoreboard by layout."""
        return [[t, [round(c) for c in b]] for t, b in ocr(img)]

    def _parsed(self, r: dict) -> dict:
        label, clock, g = parse_clock(r["tokens"], self.playoff)
        return {**r, "period": label, "clock": clock, "g": g}

    def read_many(self, vs):
        if self.workers <= 1:
            return [self.read(v) for v in vs]
        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(self.read, vs))

    def readings(self):
        with self.lock:  # other frame workers add readings meanwhile
            cached = list(self.cache.values())
        return sorted((p for p in (self._parsed(r) for r in cached) if p["g"] is not None),
                      key=lambda r: r["v"])

    def save(self) -> None:
        with self.lock:
            raw_store.write_json(self.index_path, {
                "video_id": self.video_id, "duration": self.duration, "box": self.box,
                "readings": self.cache,
            })


# ---- alignment ---------------------------------------------------------------------------

def play_segments(events):
    faceoffs = sorted({t for _, t, ev in events if ev == "FAC"})
    end = max(t for _, t, _ in events)
    return [(a, b) for a, b in zip(faceoffs, faceoffs[1:] + [end]) if b > a]


def align(bug: Scoreboard, segments):
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
            for nudge in (2.5, 5.0, 7.5):  # no scoreboard on screen (replay, crowd shot)
                if r["g"] is not None or mid + nudge >= v_hi:
                    break
                r = bug.read(mid + nudge)
            if r["g"] is None or seg_of(r["g"]) == k:
                return

    def scan(k, step=3.0):
        lo, hi = bracket(k)
        v_lo, v_hi = (lo["v"] if lo else 0.0), (hi["v"] if hi else bug.duration)
        bug.read_many(list(np.arange(v_lo + step / 2, v_hi, step)))

    try:
        bug.read_many([float(v) for v in np.arange(COARSE_STEP / 4, bug.duration, COARSE_STEP)])
        for stage in (bisect, scan):
            missing = [k for k in range(len(segments)) if k not in pinned()]
            if bug.workers > 1:  # segments are independent: search several at once
                with ThreadPoolExecutor(bug.workers) as ex:
                    list(ex.map(stage, missing))
            else:
                for k in missing:
                    stage(k)
            bug.save()
    finally:
        bug.save()  # keep every frame read, even if the run stops early (block, budget)

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


def quality(bug: Scoreboard, seg_rows, goals, away: str, home: str) -> dict:
    """Coverage, offset monotonicity and scoreboard-score agreement; 'passed' if all hold."""
    n_clock = sum(1 for r in seg_rows if r[5] == "clock")
    coverage = n_clock / len(seg_rows) if seg_rows else 0.0

    # Game time never runs backwards as the video plays: stoppages freeze it and cuts (replays edit
    # out stretches of play) skip it forward, but a misread digit usually breaks the order. So the
    # check counts readings lower than the one before; offset drops between segments are cuts.
    readings = bug.readings()
    reversals = sum(1 for a, b in zip(readings, readings[1:]) if b["g"] < a["g"] - 1.5)
    reversal_rate = reversals / len(readings) if readings else 0.0
    pinned = [r[3] for r in seg_rows if r[5] == "clock"]
    cuts = sum(1 for a, b in zip(pinned, pinned[1:]) if a - b > 3.0)

    def mp_score(g):  # (away, home) after all goals before game second g
        s = (0, 0)
        for t, a, h in goals:
            if t < g:
                s = (a, h)
        return s

    checked = matched = 0
    for r in bug.readings():
        # the scoreboard changes within a second or two of a goal, so the clock can't tell which score shows
        near_goal = any(abs(r["g"] - t) <= 3 for t, _, _ in goals)
        if not near_goal and any(a + MARGIN < r["g"] < b - MARGIN for _, a, b, *_ in seg_rows):
            seen = parse_score(r["tokens"], away, home)
            if seen is not None:
                checked += 1
                matched += seen == mp_score(r["g"])
    score_match = matched / checked if checked else None

    passed = (coverage >= GATE["min_coverage"] and reversal_rate <= GATE["max_reversals"]
              and (checked < GATE["min_score_readings"] or score_match >= GATE["min_score_match"]))
    return {"passed": bool(passed), "segments": len(seg_rows), "pinned_by_clock": n_clock,
            "coverage": round(coverage, 3), "clock_reversals": reversals, "readings": len(readings),
            "video_cuts": cuts,
            "score_readings": checked, "score_match": None if score_match is None else round(score_match, 3),
            "frames_cached": len(bug.cache), "frames_fetched": bug.new_frames}


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


def prune_frames(video_id: str) -> tuple:
    """Delete a video's frame JPEGs, keeping index.json: its OCR text and boxes are all a re-run or
    rebuild reads. After an OCR_VERSION bump these readings keep their older OCR text.
    -> (files deleted, bytes freed)."""
    n = size = 0
    d = paths.FRAMES / video_id
    for p in d.glob("*.jpg") if d.is_dir() else []:
        size += p.stat().st_size
        p.unlink()
        n += 1
    if n:
        ops.log.info("video %s: deleted %d frame images (%.1f MB)", video_id, n, size / 1e6)
    return n, size


def main(video_id: str, workers: int = FRAME_WORKERS) -> dict:
    """Align one video; returns the quality metrics (metrics['passed'] says if it's trusted)."""
    con = db.connect("warehouse")
    row = con.execute(
        "SELECT g.game_id, g.game_type, g.away_team, g.home_team FROM games g WHERE g.video_id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No game linked to video {video_id}; run load_transcript.py first.")
    game_id, game_type, away, home = row
    events = con.execute(
        "SELECT event_id, time, event FROM events WHERE game_id = ? ORDER BY event_id", (game_id,)
    ).fetchall()
    if not events:
        raise RuntimeError(f"No events for game {game_id}; run build_db.py first.")
    goals = con.execute(
        "SELECT time, awayTeamGoals, homeTeamGoals FROM events WHERE game_id = ? AND event = 'GOAL' "
        "ORDER BY event_id", (game_id,),
    ).fetchall()
    transcript = con.execute(
        "SELECT seq, start_sec FROM transcript WHERE video_id = ? ORDER BY seq", (video_id,)
    ).fetchall()

    bug = Scoreboard(video_id, playoff=game_type == "playoff", workers=workers)
    bug.locate()
    seg_rows = align(bug, play_segments(events))
    metrics = quality(bug, seg_rows, goals, away, home)

    with con:
        load_id = db.new_load(con, "alignment", game_id=game_id, video_id=video_id,
                              raw_files=[bug.index_path],
                              source_urls=[f"https://www.youtube.com/watch?v={video_id}"], details=metrics)
        con.executemany(
            "UPDATE events SET calibrated_transcript_time = ?, alignment_load_id = ? WHERE game_id = ? AND event_id = ?",
            [(v, load_id, game_id, eid) for eid, v, _ in calibrated_transcript_times(events, seg_rows)],
        )
        con.executemany(
            "UPDATE transcript SET calibrated_game_time = ?, in_play = ?, alignment_load_id = ? "
            "WHERE video_id = ? AND seq = ?",
            [(g, in_play, load_id, video_id, seq) for seq, g, in_play in calibrated_game_times(transcript, seg_rows)],
        )
    ops.log.info("video %s: %d/%d segments pinned by clock, %d frames (%d fetched); quality %s",
                 video_id, metrics["pinned_by_clock"], metrics["segments"], metrics["frames_cached"],
                 metrics["frames_fetched"], "passed" if metrics["passed"] else "NOT passed")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video_id")
    parser.add_argument("--workers", type=int, default=FRAME_WORKERS,
                        help="parallel frame reads (default: XG_ALIGN_WORKERS or 1)")
    args = parser.parse_args()
    ops.setup_logging()
    ratelimit.enforce = False  # run by hand: only scheduled runs are rate-limited
    print(main(args.video_id, args.workers))
