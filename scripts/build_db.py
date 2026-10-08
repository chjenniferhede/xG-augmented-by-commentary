"""Load MoneyPuck's events and shots for one NHL game into the warehouse.

Sources (kept in data/raw/moneypuck/):
  - Play-by-play: https://moneypuck.com/moneypuck/gameData/{season}/{game_id}.csv
  - Shots:        https://peter-tanner.com/moneypuck/downloads/shots_{season_start}.zip

A game's play-by-play file is downloaded once. The season shot zip grows during the season,
so it is re-downloaded (overwritten) when the game isn't in it and the copy is over a day old.

Usage: python scripts/build_db.py <nhl_game_id>
"""
import io
import re
import sys
import time
import zipfile

import pandas as pd
import requests

import db
import ops
import paths
import ratelimit
import raw_store
from games import upsert_game

HEADERS = {"User-Agent": "xG-augmented-by-commentary (research)"}
SHOTS_REFRESH_AFTER = 86400  # seconds


class GameDataMissing(Exception):
    """MoneyPuck has no data for this game (yet)."""


def snake(name: str) -> str:
    """MoneyPuck's camelCase column name -> the warehouse's snake_case one.

    'homeTeamExpectedGoalsEVNonRebound' -> 'home_team_expected_goals_ev_non_rebound',
    'lastEventxCord_adjusted' -> 'last_event_x_cord_adjusted', 'xGoal' -> 'x_goal'.
    Migration 0002 was generated with this function, so the two always agree.
    """
    s = re.sub(r"([a-z])([xy])(Cord)", r"\1_\2\3", name)   # 'lastEventxCord': x starts a word
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"_+", "_", s).lower()


def download(url: str, path) -> None:
    with ratelimit.request("moneypuck", url):
        resp = requests.get(url, headers=HEADERS, timeout=180)
        if resp.status_code == 404:
            raise GameDataMissing(f"MoneyPuck has no file at {url}")
        resp.raise_for_status()
    raw_store.write_bytes(path, resp.content)
    ops.log.info("downloaded %s (%d KB)", url, len(resp.content) // 1024)


def events_source(game_id: int):
    season_start = game_id // 1_000_000
    url = f"https://moneypuck.com/moneypuck/gameData/{season_start}{season_start + 1}/{game_id}.csv"
    return url, paths.MONEYPUCK / "games" / f"{season_start}{season_start + 1}" / f"{game_id}.csv"


def shots_source(game_id: int):
    season_start = game_id // 1_000_000
    url = f"https://peter-tanner.com/moneypuck/downloads/shots_{season_start}.zip"
    return url, paths.MONEYPUCK / f"shots_{season_start}.zip"


def load_events(game_id: int) -> pd.DataFrame:
    url, path = events_source(game_id)
    if not path.exists():
        download(url, path)
    events = pd.read_csv(path)
    events = events.rename(columns={"id": "event_id"})
    return pd.concat([pd.Series(game_id, index=events.index, name="game_id"), events], axis=1)


def read_shots(path, game_id: int) -> pd.DataFrame:
    season_start = game_id // 1_000_000
    with zipfile.ZipFile(path) as zf:
        with zf.open(f"shots_{season_start}.csv") as f:
            shots = pd.read_csv(io.TextIOWrapper(f), low_memory=False)
    # MoneyPuck's game_id drops the season prefix (e.g. 20969 for 2025020969).
    return shots[(shots["season"] == season_start) & (shots["game_id"] == game_id % 1_000_000)].copy()


def load_shots(game_id: int) -> pd.DataFrame:
    url, path = shots_source(game_id)
    if not path.exists():
        download(url, path)
    shots = read_shots(path, game_id)
    if shots.empty and time.time() - path.stat().st_mtime > SHOTS_REFRESH_AFTER:
        ops.log.info("game %s not in cached %s; refreshing it", game_id, path.name)
        download(url, path)
        shots = read_shots(path, game_id)
    if shots.empty:
        raise GameDataMissing(f"No MoneyPuck shots for game {game_id} in {path.name}")
    shots = shots.rename(columns={"id": "event_id", "shotID": "moneypuck_shot_id"}).drop(columns="game_id")
    return pd.concat([pd.Series(game_id, index=shots.index, name="game_id"), shots], axis=1)


def insert_frame(con, table: str, df: pd.DataFrame) -> None:
    """Insert a MoneyPuck table: every column the schema defines (MoneyPuck's names in snake_case);
    report any column MoneyPuck adds that the schema doesn't have yet."""
    types = {c: t for c, t in db.column_types(con, table).items() if c != "video_sec"}
    df = df.rename(columns={c: snake(c) for c in df.columns})
    extra = sorted(set(df.columns) - set(types))
    if extra:
        ops.log.info("%s: ignoring %d columns not in the schema: %s", table, len(extra), ", ".join(extra[:10]))
    df = df.reindex(columns=list(types))

    # Plain Python values (neither driver binds numpy scalars); NaN becomes NULL; MoneyPuck's 0/1
    # flags become True/False for the boolean columns.
    def value(v, typ):
        if pd.isna(v):
            return None
        return bool(v) if typ == "boolean" else v

    values = [[value(v, t) for v in df[c].tolist()] for c, t in types.items()]
    con.executemany(
        f"INSERT INTO {table} ({', '.join(types)}) VALUES ({', '.join('?' * len(types))})",
        list(zip(*values)),
    )


def build(game_id: int) -> None:
    events = load_events(game_id)
    shots = load_shots(game_id)
    (events_url, events_path), (shots_url, shots_path) = events_source(game_id), shots_source(game_id)

    con = db.connect("warehouse")
    with con:
        # Reloading keeps the events' alignment to the video (it depends only on the video).
        aligned = con.execute(
            "SELECT video_sec, game_id, event_id FROM events WHERE game_id = ? AND video_sec IS NOT NULL",
            (game_id,),
        ).fetchall()
        con.execute("DELETE FROM shots WHERE game_id = ?", (game_id,))  # shots reference events
        con.execute("DELETE FROM events WHERE game_id = ?", (game_id,))

        upsert_game(con, game_id)
        insert_frame(con, "events", events)
        insert_frame(con, "shots", shots)
        con.execute(
            "UPDATE games SET events_load_id = ?, shots_load_id = ? WHERE game_id = ?",
            (db.new_load(con, "events", game_id=game_id, raw_files=[events_path], source_urls=[events_url]),
             db.new_load(con, "shots", game_id=game_id, raw_files=[shots_path], source_urls=[shots_url]),
             game_id),
        )
        con.executemany("UPDATE events SET video_sec = ? WHERE game_id = ? AND event_id = ?", aligned)
    ops.log.info("game %s: loaded %d events and %d shots", game_id, len(events), len(shots))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    ops.setup_logging()
    build(int(sys.argv[1]))
