"""The games table: one row per NHL game, from the NHL's public game API.

Shared by build_db.py and load_transcript.py, so a game gets a row as soon as either its
MoneyPuck data or its video's captions are loaded. The raw boxscore is kept in
data/raw/nhl/boxscore/<game_id>.json and re-fetched only while the game isn't final.
"""
import requests

import db
import paths
import ratelimit
import raw_store

NHL_API = "https://api-web.nhle.com/v1"
GAME_TYPES = {1: "preseason", 2: "regular", 3: "playoff"}
FINAL_STATES = {"OFF", "FINAL"}


def nhl_get(path: str) -> dict:
    url = f"{NHL_API}/{path}"
    with ratelimit.request("nhl", url):
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()


def nhl_get_cached(path: str, max_age: float = 86400) -> dict:
    """An NHL API response, kept in data/raw/nhl/ and re-fetched once older than max_age."""
    import time

    file = paths.NHL / (path.replace("/", "_") + ".json")
    if file.exists() and time.time() - file.stat().st_mtime < max_age:
        return raw_store.read_json(file)
    data = nhl_get(path)
    raw_store.write_json(file, data)
    return data


def boxscore_path(game_id: int):
    return paths.NHL / "boxscore" / f"{game_id}.json"


def fetch_boxscore(game_id: int) -> dict:
    path = boxscore_path(game_id)
    if path.exists():
        box = raw_store.read_json(path)
        if box.get("gameState") in FINAL_STATES:
            return box
    box = nhl_get(f"gamecenter/{game_id}/boxscore")
    raw_store.write_json(path, box)
    return box


def summarize(box: dict) -> dict:
    def team(side):
        t = box[side]
        return {"abbrev": t["abbrev"], "name": f"{t['placeName']['default']} {t['commonName']['default']}",
                "goals": t.get("score")}

    return {
        "season": box["season"],
        "game_type": GAME_TYPES.get(box["gameType"], str(box["gameType"])),
        "game_date": box["gameDate"],
        "venue": (box.get("venue") or {}).get("default"),
        "away": team("awayTeam"),
        "home": team("homeTeam"),
        "ended_in": (box.get("gameOutcome") or {}).get("lastPeriodType"),
    }


def upsert_game(con, game_id: int) -> None:
    """Insert or refresh a game's row from its boxscore. The MoneyPuck load columns are left alone."""
    g = summarize(fetch_boxscore(game_id))
    load_id = db.new_load(con, "games", game_id=game_id, raw_files=[boxscore_path(game_id)],
                          source_urls=[f"{NHL_API}/gamecenter/{game_id}/boxscore"])
    con.execute(
        """
        INSERT INTO games (game_id, season, game_type, game_date, venue, away_team, home_team,
                           away_name, home_name, away_goals, home_goals, ended_in, load_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id) DO UPDATE SET
            season = excluded.season, game_type = excluded.game_type,
            game_date = excluded.game_date, venue = excluded.venue,
            away_team = excluded.away_team, home_team = excluded.home_team,
            away_name = excluded.away_name, home_name = excluded.home_name,
            away_goals = excluded.away_goals, home_goals = excluded.home_goals,
            ended_in = excluded.ended_in, load_id = excluded.load_id
        """,
        (game_id, g["season"], g["game_type"], g["game_date"], g["venue"],
         g["away"]["abbrev"], g["home"]["abbrev"], g["away"]["name"], g["home"]["name"],
         g["away"]["goals"], g["home"]["goals"], g["ended_in"], load_id),
    )
