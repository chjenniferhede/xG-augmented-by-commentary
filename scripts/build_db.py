"""Pull MoneyPuck data for one NHL game and store it in SQLite.

Default: Canadiens @ Sharks, Mar 3, 2026 (NHL game id 2025020969).

Sources:
  - Play-by-play: https://moneypuck.com/moneypuck/gameData/{season}/{game_id}.csv
  - Shots:        https://peter-tanner.com/moneypuck/downloads/shots_{season_start}.zip

Usage: python scripts/build_db.py [nhl_game_id]
"""
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "moneypuck.sqlite"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"Downloading {url}")
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def load_events(nhl_game_id: int) -> pd.DataFrame:
    season_start = nhl_game_id // 1_000_000
    season = f"{season_start}{season_start + 1}"
    path = fetch(
        f"https://moneypuck.com/moneypuck/gameData/{season}/{nhl_game_id}.csv",
        RAW_DIR / f"{nhl_game_id}.csv",
    )
    events = pd.read_csv(path)
    events = events.rename(columns={"id": "event_id"})
    events.insert(0, "game_id", nhl_game_id)
    return events


def load_shots(nhl_game_id: int) -> pd.DataFrame:
    season_start = nhl_game_id // 1_000_000
    path = fetch(
        f"https://peter-tanner.com/moneypuck/downloads/shots_{season_start}.zip",
        RAW_DIR / f"shots_{season_start}.zip",
    )
    with zipfile.ZipFile(path) as zf:
        with zf.open(f"shots_{season_start}.csv") as f:
            shots = pd.read_csv(io.TextIOWrapper(f), low_memory=False)
    # MoneyPuck's game_id drops the season prefix (e.g. 20969 for 2025020969).
    shots = shots[(shots["season"] == season_start) & (shots["game_id"] == nhl_game_id % 1_000_000)].copy()
    shots = shots.rename(columns={"id": "event_id", "shotID": "moneypuck_shot_id"})
    shots["game_id"] = nhl_game_id
    return shots


def build(nhl_game_id: int) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    events = load_events(nhl_game_id)
    shots = load_shots(nhl_game_id)
    if shots.empty:
        raise SystemExit(f"No MoneyPuck shots found for game {nhl_game_id}")

    with sqlite3.connect(DB_PATH) as con:
        # Replace any previous load of this game so the script is re-runnable.
        for table in ("events", "shots"):
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                con.execute(f"DELETE FROM {table} WHERE game_id = ?", (nhl_game_id,))

        events.to_sql("events", con, if_exists="append", index=False)
        shots.to_sql("shots", con, if_exists="append", index=False)

        con.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_events_game_event ON events (game_id, event_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_shots_game_event ON shots (game_id, event_id);
            CREATE INDEX IF NOT EXISTS ix_shots_team ON shots (game_id, teamCode);
            CREATE INDEX IF NOT EXISTS ix_shots_shooter ON shots (shooterPlayerId);
            """
        )

    print(f"Wrote {len(events)} events and {len(shots)} shots for game {nhl_game_id} to {DB_PATH}")


if __name__ == "__main__":
    build(int(sys.argv[1]) if len(sys.argv) > 1 else 2025020969)
