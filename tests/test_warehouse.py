"""The warehouse schema on SQLite: migrations apply from scratch, the MoneyPuck loader maps
camelCase columns and 0/1 flags, and shot_commentary joins shots to the commentary.

Run: .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

os.environ["XG_DATA_DIR"] = tempfile.mkdtemp(prefix="xg-test-")
os.environ["DATABASE_URL"] = ""  # SQLite in the temp dir, never the real database from .env
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pandas as pd  # noqa: E402

import align_video  # noqa: E402
import build_db  # noqa: E402
import db  # noqa: E402
import load_transcript  # noqa: E402
import ops  # noqa: E402
import pipeline  # noqa: E402


class WarehouseTest(unittest.TestCase):
    def test_snake_case_names(self):
        self.assertEqual(build_db.snake("homeTeamExpectedGoalsEVNonRebound"), "home_team_expected_goals_ev_non_rebound")
        self.assertEqual(build_db.snake("lastEventxCord_adjusted"), "last_event_x_cord_adjusted")
        self.assertEqual(build_db.snake("arenaAdjustedXCordABS"), "arena_adjusted_x_cord_abs")
        self.assertEqual(build_db.snake("xGoal"), "x_goal")
        self.assertEqual(build_db.snake("game_id"), "game_id")

    def test_load_and_shot_commentary(self):
        con = db.connect("warehouse")
        with con:
            con.execute("INSERT INTO games (game_id, season, game_type, game_date, away_team, home_team) "
                        "VALUES (2025020001, 20252026, 'regular', '2025-10-07', 'BOS', 'MTL')")
            build_db.insert_frame(con, "events", pd.DataFrame([
                {"game_id": 2025020001, "event_id": 1, "time": 0, "event": "FAC", "eventDescriptionRaw": "Faceoff"},
                {"game_id": 2025020001, "event_id": 2, "time": 65, "event": "SHOT", "shotRush": 1,
                 "eventDescriptionRaw": "Shot", "extraColumnMoneyPuckAdded": 5},
            ]))
            build_db.insert_frame(con, "shots", pd.DataFrame([
                {"game_id": 2025020001, "event_id": 2, "time": 65, "period": 1, "event": "SHOT", "xGoal": 0.12,
                 "shooterName": "A Shooter", "teamCode": "BOS", "shotRush": 1, "goal": 0},
            ]))
            con.execute("INSERT INTO videos (video_id, game_id, title) VALUES ('abcdefghijk', 2025020001, 't')")
            con.execute("INSERT INTO captions (video_id, seq, start_sec, end_sec, text) VALUES "
                        "('abcdefghijk', 1, 98, 104, 'what a chance')")
            con.execute("UPDATE events SET video_sec = time + 40 WHERE game_id = 2025020001")
            aid = con.execute("INSERT INTO alignments (video_id, created_at, passed) VALUES "
                              "('abcdefghijk', '2026-10-05T00:00:00Z', 1) RETURNING alignment_id").fetchone()[0]
            con.execute("UPDATE videos SET alignment_id = ? WHERE video_id = 'abcdefghijk'", (aid,))

        self.assertEqual(con.execute("SELECT shot_rush, goal, x_goal FROM shots").fetchone(), (1, 0, 0.12))
        row = con.execute("SELECT clock, shooter, xg, video_time, link, commentary FROM shot_commentary").fetchone()
        self.assertEqual(row, ("1:05", "A Shooter", 0.12, "1:45",
                               "https://www.youtube.com/watch?v=abcdefghijk&t=100s", "what a chance"))
        # a video whose alignment didn't pass is left out
        con.execute("UPDATE alignments SET passed = 0")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM shot_commentary").fetchone()[0], 0)


class MoneyPuckDuplicateEvents(unittest.TestCase):
    def load_csv(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            with patch.object(build_db, "events_source", return_value=("unused", path)):
                return build_db.load_events(2020020290)

    def test_identical_event_rows_are_loaded_once(self):
        events = self.load_csv([
            {"id": 1, "time": 0, "event": "FAC", "leadWinProbability": 0.5},
            {"id": 1, "time": 0, "event": "FAC", "leadWinProbability": 0.5},
            {"id": 2, "time": 12, "event": "SHOT", "leadWinProbability": 0.6},
        ])
        self.assertEqual(events["event_id"].tolist(), [1, 2])
        self.assertEqual(events["game_id"].tolist(), [2020020290, 2020020290])

    def test_conflicting_event_rows_are_rejected(self):
        with self.assertRaisesRegex(build_db.GameDataConflict, r"2020020290.*297"):
            self.load_csv([
                {"id": 297, "time": 3600, "event": "GOFF", "leadWinProbability": 0.9999},
                {"id": 297, "time": 3600, "event": "GEND", "leadWinProbability": 1.0},
            ])


class GameDataConflictHandling(unittest.TestCase):
    def test_conflicting_source_waits_for_manual_review(self):
        video_id = "conflict001"
        ops.add_video(video_id, "test", "2026-01-01T00:00:00Z", state="transcribed", game_id=2020020290)
        with patch.object(build_db, "build", side_effect=build_db.GameDataConflict("conflicting ID 297")):
            pipeline.do_game_data(ops.get_video(video_id), {})
        row = ops.connect().execute(
            "SELECT state, next_attempt_at, last_error, attempts FROM videos WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        self.assertEqual(row, ("transcribed", "never", "conflicting ID 297", 0))
        step = ops.connect().execute(
            "SELECT status FROM steps WHERE video_id = ? ORDER BY step_id DESC LIMIT 1", (video_id,)
        ).fetchone()
        self.assertEqual(step, ("skipped",))


class AlignmentSampling(unittest.TestCase):
    class CachedFrames:
        duration = 100.0
        workers = 1

        def __init__(self, clocks):
            self.clocks = [{"v": float(g), "g": float(g)} for g in clocks]
            self.batches = []
            self.read_calls = []

        def readings(self):
            return self.clocks

        def read(self, v):
            self.read_calls.append(float(v))
            return {"v": v, "g": None}

        def read_many(self, vs):
            self.batches.append(list(vs))

        def save(self):
            pass

    def test_skips_dense_scan_when_clock_coverage_already_passes(self):
        frames = self.CachedFrames(range(5, 95, 10))  # 9 of 10 segments
        segments = [(k * 10, (k + 1) * 10) for k in range(10)]
        rows = align_video.align(frames, segments, stop_after_coverage=True)
        self.assertEqual(sum(row[5] == "clock" for row in rows), 9)
        self.assertEqual(len(frames.batches), 1, "only the coarse pass should run")

    def test_cached_only_builds_rows_without_fetching_frames(self):
        frames = self.CachedFrames(range(5, 95, 10))
        segments = [(k * 10, (k + 1) * 10) for k in range(10)]
        rows = align_video.align(frames, segments, cached_only=True)
        self.assertEqual(sum(row[5] == "clock" for row in rows), 9)
        self.assertEqual(frames.batches, [])

    def test_dense_scan_still_runs_below_the_coverage_gate(self):
        frames = self.CachedFrames(range(5, 85, 10))  # 8 of 10 segments
        segments = [(k * 10, (k + 1) * 10) for k in range(10)]
        align_video.align(frames, segments, stop_after_coverage=True)
        self.assertIn(76.5, frames.read_calls, "the missing ninth segment should be scanned")

    def test_targeted_scan_stops_when_a_new_clock_pins_its_segment(self):
        class Frames(self.CachedFrames):
            def read(self, v):
                row = super().read(v)
                if abs(v - 76.5) < 0.01:
                    row = {"v": float(v), "g": 85.0}
                    self.clocks.append(row)
                return row

        frames = Frames(range(5, 85, 10))
        segments = [(k * 10, (k + 1) * 10) for k in range(10)]
        rows = align_video.align(frames, segments, stop_after_coverage=True)
        self.assertEqual(sum(row[5] == "clock" for row in rows), 9)
        self.assertIn(76.5, frames.read_calls)
        self.assertNotIn(79.5, frames.read_calls, "no more scans after coverage is sufficient")

    def test_other_failed_gate_falls_back_to_dense_scan(self):
        bug, segments, goals = object(), [(0, 10)], []
        cached, first, dense = [(0, 10, "cached")], [(0, 10, "sampled")], [(0, 10, "scanned")]
        with patch.object(align_video, "align", side_effect=[cached, first, dense]) as align_mock, \
                patch.object(align_video, "quality", side_effect=[
                    {"passed": False, "coverage": 0.1},
                    {"passed": False, "coverage": 0.9},
                    {"passed": True, "coverage": 0.9},
                ]):
            rows, metrics = align_video.validated_alignment(bug, segments, goals, "BOS", "MTL")
        self.assertEqual(rows, dense)
        self.assertTrue(metrics["passed"])
        self.assertEqual(align_mock.call_args_list, [
            call(bug, segments, cached_only=True),
            call(bug, segments, stop_after_coverage=True), call(bug, segments),
        ])

    def test_passing_cached_alignment_never_samples(self):
        bug, segments, goals = object(), [(0, 10)], []
        cached = [(0, 10, "cached")]
        with patch.object(align_video, "align", return_value=cached) as align_mock, \
                patch.object(align_video, "quality", return_value={"passed": True, "coverage": 0.9}):
            rows, metrics = align_video.validated_alignment(bug, segments, goals, "BOS", "MTL")
        self.assertEqual(rows, cached)
        self.assertTrue(metrics["passed"])
        align_mock.assert_called_once_with(bug, segments, cached_only=True)


if __name__ == "__main__":
    unittest.main()


class ClockCandidateRanking(unittest.TestCase):
    def test_ranked_percentage_split_across_tokens_is_not_a_clock(self):
        words = [["82.6", [413, 68, 483, 99]], ["%", [478, 70, 492, 83]],
                 ["RANK", [519, 69, 554, 82]], ["3RD", [519, 81, 554, 97]]]
        self.assertEqual(align_video.parse_clock(words, True), (None, None, None))

    def test_stylized_first_period_one_read_as_l(self):
        words = [["lst", [449, 24, 492, 54]], ["19:15", [510, 24, 578, 54]]]
        self.assertEqual(align_video.parse_clock(words, False), ("1ST", 1155, 45))
        self.assertEqual(align_video.parse_clock(["LAST 19:15"], False), (None, None, None))

    def test_shift_timer_below_scoreboard_does_not_replace_game_clock(self):
        words = [["1ST", [409, 36, 448, 60]], ["4:35", [463, 39, 529, 62]],
                 ["1:04", [424, 89, 458, 106]]]
        self.assertEqual(align_video.parse_clock(words, False)[2], 925)

    def test_shot_speed_does_not_replace_minute_second_clock(self):
        words = [["78.9", [344, 36, 385, 56]], ["3RD", [405, 38, 449, 61]],
                 ["16:39", [458, 40, 537, 61]]]
        self.assertEqual(align_video.parse_clock(words, False)[2], 2601)
        self.assertAlmostEqual(align_video.parse_clock(words[:2], False)[2], 3521.1)


class BroadcastPeriodCorrection(unittest.TestCase):
    def test_exact_stale_labels_are_corrected_without_changing_ocr(self):
        bug = align_video.Scoreboard("60-5C9m7lZo", playoff=False)
        source = {}
        for v, clock in align_video.PERIOD_CORRECTIONS[bug.video_id].items():
            tokens = [["2ND", [856, 35, 905, 60]],
                      [f"{clock // 60}:{clock % 60:02}", [898, 36, 974, 58]]]
            source[f"{v:.1f}"] = {"v": v, "tokens": tokens}
        bug.cache = source
        readings = bug.readings()
        self.assertEqual([r["g"] for r in readings],
                         [3600 - c for c in align_video.PERIOD_CORRECTIONS[bug.video_id].values()])
        self.assertTrue(all(r["period"] == "3RD" for r in readings))
        self.assertTrue(all(r["tokens"][0][0] == "2ND" for r in readings),
                        "preserve the broadcast's original label in cached OCR")

        other = align_video.Scoreboard("another_vid", playoff=False)
        self.assertEqual(other._parsed(source["3435.0"])["g"], 1244)
        wrong_clock = {"v": 3435.0, "tokens": [["2ND", [856, 35, 905, 60]],
                                                 ["19:15", [898, 36, 974, 58]]]}
        self.assertEqual(bug._parsed(wrong_clock)["g"], 1245)


class ScoreConsistency(unittest.TestCase):
    """consistent_scores drops the readings that contradict the rest of the video."""

    @staticmethod
    def _readings(seq):
        """seq: [(game_sec, away_label_score, home_label_score)] -> fake OCR readings."""
        out = []
        for i, (g, a, h) in enumerate(seq):
            # team label and its score on one line, same height, as the real scoreboard has them
            out.append({"v": float(i), "g": g, "tokens": [
                ["TOR", (100, 30, 160, 60)], [str(a), (165, 30, 185, 60)],
                ["NYI", (300, 30, 360, 60)], [str(h), (365, 30, 385, 60)],
            ]})
        return out

    def test_keeps_a_monotone_progression(self):
        rows = self._readings([(100, 0, 0), (900, 1, 0), (1800, 1, 1), (2700, 2, 1)])
        got = align_video.consistent_scores(rows, "TOR", "NYI")
        self.assertEqual(len(got), 4)
        self.assertEqual(got[3.0], (2, 1))

    def test_drops_a_shots_on_goal_misread(self):
        # 13 and 10 are shots-on-goal leaking in; they break the progression and must go.
        rows = self._readings([(100, 0, 0), (200, 0, 0), (300, 2, 13), (900, 1, 0), (1800, 1, 1)])
        got = align_video.consistent_scores(rows, "TOR", "NYI")
        self.assertNotIn(2.0, got, "the (2, 13) reading should have been discarded")
        self.assertEqual(len(got), 4)

    def test_drops_a_score_that_goes_backwards(self):
        rows = self._readings([(100, 0, 0), (900, 2, 1), (1000, 1, 0), (1800, 2, 2)])
        got = align_video.consistent_scores(rows, "TOR", "NYI")
        self.assertNotIn(2.0, got, "a score cannot go down")

    def test_rejects_impossible_totals_outright(self):
        rows = self._readings([(100, 0, 0), (900, 30, 28)])
        got = align_video.consistent_scores(rows, "TOR", "NYI")
        self.assertEqual(list(got), [0.0])

    def test_never_consults_the_real_goals(self):
        """The filter must not take the game's goals as an argument: the score check is meant to
        test the alignment independently, so fitting to the answer would make it meaningless."""
        import inspect
        params = inspect.signature(align_video.consistent_scores).parameters
        self.assertEqual(list(params), ["readings", "away", "home"])

    def test_ignores_readings_with_no_clock(self):
        rows = self._readings([(100, 0, 0), (900, 1, 0)])
        rows.append({"v": 9.0, "g": None, "tokens": rows[0]["tokens"]})
        got = align_video.consistent_scores(rows, "TOR", "NYI")
        self.assertNotIn(9.0, got)

    def test_empty_input(self):
        self.assertEqual(align_video.consistent_scores([], "TOR", "NYI"), {})


class GameResolution(unittest.TestCase):
    """The title narrowing added for replays that name an event instead of a date."""

    @staticmethod
    def _game(gid, date, away, home, last="REG", neutral=False, venue="Arena", gtype=2):
        return {"id": gid, "gameDate": date, "gameType": gtype,
                "awayTeam": {"abbrev": away}, "homeTeam": {"abbrev": home},
                "gameOutcome": {"lastPeriodType": last}, "neutralSite": neutral,
                "venue": {"default": venue}}

    def _resolve(self, title, games, snippets=()):
        """Run resolve_game_id with the NHL schedule stubbed out."""
        real = load_transcript.nhl_get_cached
        load_transcript.nhl_get_cached = lambda path: {"games": games}
        try:
            return load_transcript.resolve_game_id(
                {"title": title, "snippets": [{"text": t} for t in snippets], "video_id": "x"})
        finally:
            load_transcript.nhl_get_cached = real

    def test_nickname_is_recognised_as_a_team(self):
        games = [self._game(1, "2013-05-13", "TOR", "BOS", gtype=3)]
        # "Leafs", not "Maple Leafs" -- the form the replay titles actually use.
        self.assertEqual(self._resolve("FULL REPLAY: Leafs vs Bruins | 2013 Stanley Cup Playoffs", games), 1)

    def test_shootout_in_the_title_picks_the_shootout_game(self):
        games = [self._game(10, "2012-01-05", "WPG", "PHI"),
                 self._game(11, "2012-01-31", "WPG", "PHI", last="SO")]
        self.assertEqual(self._resolve("FULL REPLAY: Ultimate Shootout | Jets vs. Flyers | 2011-12 Regular Season", games), 11)

    def test_overtime_in_the_title_picks_the_overtime_game(self):
        games = [self._game(20, "2025-11-04", "NSH", "MIN"),
                 self._game(21, "2025-12-23", "NSH", "MIN", last="OT")]
        self.assertEqual(self._resolve("FULL REPLAY: OT Shocker | Predators vs Wild | 2025-26 Regular Season", games), 21)

    def test_an_outdoor_event_picks_the_neutral_site_game(self):
        games = [self._game(30, "2021-01-21", "PHI", "BOS"),
                 self._game(31, "2021-02-21", "PHI", "BOS", neutral=True, venue="Edgewood Tahoe Resort")]
        self.assertEqual(self._resolve("FULL REPLAY: Lake Tahoe | Bruins vs Flyers | 2021 NHL Outdoors", games), 31)

    def test_a_hint_the_schedule_contradicts_is_ignored(self):
        """No candidate went to overtime, so the hint must not throw every candidate away: it is
        better to list them for a human than to resolve to nothing or to the wrong game."""
        games = [self._game(40, "2025-11-04", "NSH", "MIN"), self._game(41, "2025-12-23", "NSH", "MIN")]
        with self.assertRaises(load_transcript.GameUnresolved) as e:
            self._resolve("FULL REPLAY: OT Shocker | Predators vs Wild | 2025-26 Regular Season", games)
        self.assertIn("40", str(e.exception))
        self.assertIn("41", str(e.exception))

    def test_the_listing_shows_the_outcome_and_venue(self):
        games = [self._game(50, "2025-11-04", "NSH", "MIN", last="OT", venue="Xcel Energy Center"),
                 self._game(51, "2025-12-23", "NSH", "MIN", last="SO", venue="Bridgestone Arena")]
        with self.assertRaises(load_transcript.GameUnresolved) as e:
            self._resolve("FULL REPLAY: Wild Night | Predators vs Wild | 2025-26 Regular Season", games)
        msg = str(e.exception)
        self.assertIn("Xcel Energy Center", msg)
        self.assertIn("SO", msg)

    def test_a_non_nhl_matchup_still_fails_clearly(self):
        with self.assertRaises(load_transcript.GameUnresolved) as e:
            self._resolve("FULL REPLAY: USA vs. Canada | 1996 World Cup of Hockey Final", [])
        self.assertIn("two NHL teams", str(e.exception))


class AbandonedRuns(unittest.TestCase):
    """A run whose process dies never records an outcome; the next run closes it out."""

    def setUp(self):
        self.con = ops.connect()
        ops.current_run_id = None

    def _insert_running(self, with_step=True):
        run_id = self.con.execute(
            "INSERT INTO runs (trigger, command, args, started_at, status, pid) "
            "VALUES ('cli', 'process', '{}', ?, 'running', 999999) RETURNING run_id",
            (db.now_iso(),)).fetchone()[0]
        if with_step:
            self.con.execute(
                "INSERT INTO steps (run_id, stage, status, started_at) VALUES (?, 'align', 'running', ?)",
                (run_id, db.now_iso()))
        return run_id

    def test_a_stranded_run_is_closed_and_its_step_too(self):
        run_id = self._insert_running()
        self.assertEqual(ops.close_abandoned_runs(), 1)
        status, finished = self.con.execute(
            "SELECT status, finished_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        self.assertEqual(status, "failed")
        self.assertIsNotNone(finished, "a closed run must have a finish time")
        step = self.con.execute(
            "SELECT status, error FROM steps WHERE run_id = ?", (run_id,)).fetchone()
        self.assertEqual(step[0], "failed")
        self.assertIn("abandoned", step[1])

    def test_nothing_to_close_is_not_an_error(self):
        self.con.execute("UPDATE runs SET status = 'ok' WHERE status = 'running'")
        self.assertEqual(ops.close_abandoned_runs(), 0)

    def test_starting_a_run_closes_the_previous_casualty(self):
        stranded = self._insert_running()
        new_id = ops.start_run("cli", "process", {"stage": "align"})
        self.assertEqual(
            self.con.execute("SELECT status FROM runs WHERE run_id = ?", (stranded,)).fetchone()[0],
            "failed")
        self.assertEqual(
            self.con.execute("SELECT status FROM runs WHERE run_id = ?", (new_id,)).fetchone()[0],
            "running", "the run being started must not close itself")
        ops.finish_run("ok", {})

    def test_an_existing_summary_is_not_overwritten(self):
        run_id = self._insert_running(with_step=False)
        self.con.execute("UPDATE runs SET summary = ? WHERE run_id = ?", ('{"kept": 1}', run_id))
        ops.close_abandoned_runs()
        self.assertIn("kept", self.con.execute(
            "SELECT summary FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])
