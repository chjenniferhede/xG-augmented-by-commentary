"""The warehouse schema on SQLite: migrations apply from scratch, the MoneyPuck loader maps
camelCase columns and 0/1 flags, and shot_commentary joins shots to the commentary.

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

import pandas as pd  # noqa: E402

import build_db  # noqa: E402
import db  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
