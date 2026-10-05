"""Rate limiter, circuit breaker and scoreboard parsing, with a fake clock and a temporary data root.

Run: .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["XG_DATA_DIR"] = tempfile.mkdtemp(prefix="xg-test-")
os.environ["DATABASE_URL"] = ""  # SQLite in the temp dir, never the real database from .env
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ops  # noqa: E402
import pipeline  # noqa: E402
import ratelimit  # noqa: E402
from align_video import parse_clock, parse_score  # noqa: E402


class IpBlocked(Exception):
    """Same name as youtube_transcript_api's error, which the limiter recognises."""


class FakeTime:
    def __init__(self, start=1_000_000.0):
        self.now = start
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class LimiterTest(unittest.TestCase):
    def setUp(self):
        con = ops.connect()
        with con:
            con.execute("DELETE FROM requests")
            con.execute("DELETE FROM kv")
        self.t = FakeTime()
        ratelimit.clock, ratelimit.sleep = self.t.clock, self.t.sleep
        ratelimit.youtube_count = 0
        ratelimit._tripped = False
        ratelimit.RUN_BUDGET = 800
        ratelimit.enforce = True

    def test_manual_runs_skip_the_rules(self):
        ratelimit.enforce = False
        ratelimit.RUN_BUDGET = 1
        ratelimit.trip("earlier block")
        ratelimit._tripped = False  # as in a new run
        for _ in range(3):  # no cooldown, budget or spacing
            with ratelimit.request("yt_frame"):
                pass
        self.assertEqual(self.t.slept, [])
        self.assertTrue(ratelimit.youtube_available())
        n = ops.connect().execute("SELECT COUNT(*) FROM requests WHERE kind = 'yt_frame'").fetchone()[0]
        self.assertEqual(n, 3)  # still recorded

    def test_manual_refusal_records_one_block_and_continues(self):
        ratelimit.enforce = False
        for _ in range(2):
            with self.assertRaises(IpBlocked):
                with ratelimit.request("yt_caption", "x"):
                    raise IpBlocked("HTTP 429")
        self.assertEqual(ops.kv_get("block_count"), "1")  # one cooldown, not doubled per refusal
        self.assertIn("IpBlocked: HTTP 429", ops.kv_get("last_block_reason"))
        with ratelimit.request("yt_caption"):  # the run can still make requests
            pass

    def test_spacing_with_jitter(self):
        with ratelimit.request("yt_frame"):
            pass
        self.t.now += 0.5
        with ratelimit.request("yt_frame"):
            pass
        interval, jitter, _ = ratelimit.SPACING["yt_frame"]
        self.assertEqual(len(self.t.slept), 1)
        self.assertGreaterEqual(self.t.slept[0] + 0.5, interval * (1 - jitter) - 1e-9)
        self.assertLessEqual(self.t.slept[0] + 0.5, interval * (1 + jitter) + 1e-9)

    def test_groups_are_independent(self):
        with ratelimit.request("yt_frame"):
            pass
        with ratelimit.request("nhl"):
            pass
        self.assertEqual(self.t.slept, [])

    def test_page_and_caption_share_spacing(self):
        with ratelimit.request("yt_page"):
            pass
        with ratelimit.request("yt_caption"):
            pass
        self.assertEqual(len(self.t.slept), 1)
        self.assertGreater(self.t.slept[0], 6.9)

    def test_hourly_cap(self):
        _, _, per_hour = ratelimit.SPACING["yt_web"]
        con = ops.connect()
        first = self.t.now - 3000
        with con:
            con.executemany("INSERT INTO requests (ts, kind, status) VALUES (?, 'yt_page', 'ok')",
                            [(first + i * 40,) for i in range(per_hour)])
        self.t.now = first + per_hour * 40 + 20
        with ratelimit.request("yt_page"):
            pass
        # waits until the oldest request in the window is an hour old
        self.assertAlmostEqual(self.t.now, first + 3600, delta=0.01)

    def test_run_budget(self):
        ratelimit.RUN_BUDGET = 2
        for _ in range(2):
            with ratelimit.request("yt_frame"):
                pass
        with self.assertRaises(ratelimit.BudgetExhausted):
            ratelimit.gate("yt_frame")
        ratelimit.gate("nhl")  # other sources are not budgeted

    def test_circuit_breaker_and_cooldown(self):
        with self.assertRaises(IpBlocked):
            with ratelimit.request("yt_caption"):
                raise IpBlocked("too many requests")
        status = ops.connect().execute("SELECT status FROM requests ORDER BY request_id DESC").fetchone()[0]
        self.assertEqual(status, "blocked")
        self.assertAlmostEqual(ratelimit.cooldown_until(), self.t.now + 24 * 3600, delta=1)
        with self.assertRaises(ratelimit.Blocked):
            ratelimit.gate("yt_frame")
        ratelimit.gate("moneypuck")  # non-YouTube sources keep working

        # A later process (new run): still blocked by the stored cooldown.
        ratelimit._tripped = False
        self.assertFalse(ratelimit.youtube_available())
        with self.assertRaises(ratelimit.Blocked):
            ratelimit.gate("yt_page")

        # A second block within 14 days doubles the cooldown.
        self.t.now += 25 * 3600
        ratelimit.trip("again")
        self.assertAlmostEqual(ratelimit.cooldown_until(), self.t.now + 48 * 3600, delta=1)

    def test_pipeline_skips_youtube_stages_during_cooldown(self):
        ops.add_video("AAAAAAAAAAA", "FULL REPLAY: test", "2026-01-01T00:00:00Z")
        ratelimit.trip("test")
        ratelimit._tripped = False  # as in a new run
        summary = {}
        pipeline.stage_transcripts(5, summary)
        self.assertIn("youtube_stopped", summary)
        state = ops.connect().execute("SELECT state FROM videos WHERE video_id = 'AAAAAAAAAAA'").fetchone()[0]
        self.assertEqual(state, "discovered")

    def test_offline_mode(self):
        ratelimit.offline = True
        try:
            with self.assertRaises(ratelimit.Offline):
                ratelimit.gate("nhl")
        finally:
            ratelimit.offline = False


class ParseTest(unittest.TestCase):
    def test_clock_formats(self):
        self.assertEqual(parse_clock(["2ND", "14:57"], False)[2], 1200 + 303)
        self.assertEqual(parse_clock(["2ND 14:23"], False)[2], 1200 + 337)
        self.assertEqual(parse_clock(["1st16:28"], True)[2], 212)
        self.assertEqual(parse_clock(["2N2NDIAVNEES"], True)[2], None)
        self.assertEqual(parse_clock(["3RD", ":52.6"], False)[2], 2400 + 1200 - 52.6)
        self.assertEqual(parse_clock(["14:57", "1ST"], False)[2], 303)
        self.assertEqual(parse_clock(["P2", "19:00"], False)[2], 1260)

    def test_overtime(self):
        self.assertEqual(parse_clock(["OT", "3:12"], False)[2], 3600 + 300 - 192)
        self.assertEqual(parse_clock(["2OT", "11:05"], True)[2], 3600 + 1200 + 1200 - 665)
        self.assertEqual(parse_clock(["OT", "15:00"], False)[2], None)  # regular-season OT is 5 min
        self.assertEqual(parse_clock(["0T17:20"], True)[:2], ("OT", 1040))  # O read as zero

    def test_stacked_layouts(self):
        # OCR boxes from real broadcasts: period over clock (TNT, ESPN), clock over period (Sportsnet)
        tnt = [["OCAR", [131, 56, 197, 75]], ["9", [254, 55, 265, 70]], ["1ST", [294, 58, 324, 75]],
               ["SOG", [248, 73, 273, 86]], ["2:29", [287, 77, 329, 96]], ["FLA", [155, 87, 196, 106]]]
        espn = [["STANLEYCUPFINALCOLLEADS2-1", [42, 26, 293, 39]], ["2ND", [112, 48, 152, 68]],
                ["COL", [232, 48, 275, 68]], ["0", [290, 43, 313, 73]], ["17:36", [104, 82, 161, 102]],
                ["TB", [238, 83, 269, 104]]]
        sn = [["FIRSTROUND-TORLEADS3-2", [131, 27, 297, 38]], ["TOR", [138, 50, 178, 71]],
              ["0", [185, 47, 206, 75]], ["1.2", [319, 50, 347, 70]], ["SHOTS10", [188, 81, 268, 96]],
              ["1ST", [319, 79, 346, 94]]]
        self.assertEqual(parse_clock(tnt, True), ("1ST", 149, 1051))
        self.assertEqual(parse_clock(espn, True), ("2ND", 1056, 1344))
        self.assertEqual(parse_clock(sn, True), ("1ST", 1.2, 1198.8))
        # side by side, as boxes
        self.assertEqual(parse_clock([["2ND", [10, 10, 40, 30]], ["14:57", [50, 10, 100, 30]]], False)[2], 1503)
        # a clock far from the period label isn't its clock
        self.assertEqual(parse_clock([["2ND", [10, 10, 40, 30]], ["14:57", [600, 200, 650, 220]]], False)[2], None)

    def test_rejects_garbage(self):
        self.assertEqual(parse_clock(["2ND", "19:62"], False)[2], None)
        self.assertEqual(parse_clock(["MTL", "2", "SHOTS"], False)[2], None)

    def test_score(self):
        tokens = ["MTL", "1", "SHOTS", "SJ", "1", "SHOTS", "8", "8", "2ND", "19:52"]
        self.assertEqual(parse_score(tokens, "MTL", "SJS"), (1, 1))
        self.assertIsNone(parse_score(["2ND", "19:52"], "MTL", "SJS"))
        self.assertEqual(parse_score(["WSH1", "PITO"], "WSH", "PIT"), (1, 0))  # glued, O for 0
        self.assertEqual(parse_score(["OCAR", "1", "FLA", "2"], "CAR", "FLA"), (1, 2))  # logo read as O
        # WSH's score was missed: PIT's 0 must not be taken for it
        self.assertIsNone(parse_score(["WSH", "PIT", "0"], "WSH", "PIT"))

    def test_score_by_layout(self):
        # OCR boxes from real broadcasts
        sn_mirrored = [["VGK", [442, 36, 509, 62]], ["1", [530, 36, 551, 62]], ["2", [597, 36, 618, 62]],
                       ["MTL", [642, 35, 708, 63]], ["3RD19:31", [812, 36, 930, 62]]]  # home score left of label
        self.assertEqual(parse_score(sn_mirrored, "VGK", "MTL"), (1, 2))
        # VGK's 1 missed by OCR: the 2 beside MTL must not be taken for it
        self.assertIsNone(parse_score([t for t in sn_mirrored if t[0] != "1"], "VGK", "MTL"))
        tnt = [["FLA", [150, 52, 192, 72]], ["1", [215, 50, 232, 78]], ["11", [250, 52, 268, 66]],
               ["BOS", [150, 82, 192, 102]], ["0", [214, 80, 233, 108]], ["9", [252, 84, 264, 98]]]
        self.assertEqual(parse_score(tnt, "FLA", "BOS"), (1, 0))  # not the smaller shot counts
        logo = [["EDM", [104, 42, 159, 70]], ["2", [180, 39, 203, 70]], ["32", [218, 43, 252, 69]],
                ["ANA", [270, 42, 327, 70]], ["0", [346, 41, 370, 69]]]
        self.assertEqual(parse_score(logo, "EDM", "ANA"), (2, 0))  # a logo read as 32 isn't a score


if __name__ == "__main__":
    unittest.main()
