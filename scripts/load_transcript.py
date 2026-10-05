"""Load a YouTube game replay's transcript into the warehouse.

Give it the video's link (or ID). It downloads the captions once and keeps them in
data/raw/youtube/captions/<video_id>.json; later runs read that file and do not contact YouTube.

The NHL game ID is worked out from the video title (teams, plus the date or the season),
using the NHL's public schedule API, and saved with the captions. Pass --game-id to set it
yourself, or when the title is ambiguous (the script lists the candidates).

Timestamps are seconds into the video, not game clock. Run scripts/align_video.py
afterwards to fill calibrated_game_time / in_play (a reload keeps them).

Usage: python scripts/load_transcript.py <youtube link or id> [--game-id NHL_GAME_ID]
"""
import argparse
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

import db
import ops
import paths
import ratelimit
import raw_store
from games import nhl_get_cached, upsert_game

TEAMS = {
    "Ducks": "ANA", "Coyotes": "ARI", "Bruins": "BOS", "Sabres": "BUF", "Flames": "CGY",
    "Hurricanes": "CAR", "Blackhawks": "CHI", "Avalanche": "COL", "Blue Jackets": "CBJ",
    "Stars": "DAL", "Red Wings": "DET", "Oilers": "EDM", "Panthers": "FLA", "Kings": "LAK",
    "Wild": "MIN", "Canadiens": "MTL", "Predators": "NSH", "Devils": "NJD", "Islanders": "NYI",
    "Rangers": "NYR", "Senators": "OTT", "Flyers": "PHI", "Penguins": "PIT", "Sharks": "SJS",
    "Kraken": "SEA", "Blues": "STL", "Lightning": "TBL", "Maple Leafs": "TOR",
    "Utah Hockey Club": "UTA", "Mammoth": "UTA", "Canucks": "VAN", "Golden Knights": "VGK",
    "Capitals": "WSH", "Jets": "WPG",
}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
LANGUAGES = ["en", "en-US", "en-CA", "en-GB"]


class NoCaptions(Exception):
    """The video has no English caption track (or is unavailable)."""


class GameUnresolved(Exception):
    """The video title doesn't pin down one NHL game."""


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def video_id_from(link: str) -> str:
    """Accepts watch?v=, youtu.be/, /shorts/, /embed/, /live/ links or a bare 11-character ID."""
    if re.fullmatch(r"[\w-]{11}", link):
        return link
    url = urlparse(link if "//" in link else "https://" + link)
    if "v" in parse_qs(url.query):
        return parse_qs(url.query)["v"][0]
    m = re.search(r"(?:youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", link)
    if m:
        return m.group(1)
    raise SystemExit(f"Could not find a YouTube video ID in {link!r}")


def captions_path(video_id: str):
    return paths.CAPTIONS / f"{video_id}.json"


def fetch(video_id: str) -> dict:
    """The video's captions and metadata, from data/raw if present, else from YouTube."""
    path = captions_path(video_id)
    if path.exists():
        return raw_store.read_json(path)

    import youtube_transcript_api as yta

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with ratelimit.request("yt_page", "oembed " + url):
            resp = requests.get("https://www.youtube.com/oembed",
                                params={"url": url, "format": "json"}, timeout=30)
            if resp.status_code in (401, 404):
                raise NoCaptions(f"video {video_id} is unavailable ({resp.status_code})")
            resp.raise_for_status()
            meta = resp.json()
        api = yta.YouTubeTranscriptApi()
        with ratelimit.request("yt_page", "transcript list " + url):
            transcript = api.list(video_id).find_transcript(LANGUAGES)
        with ratelimit.request("yt_caption", "transcript " + url):
            snippets = transcript.fetch().snippets
    except (yta.NoTranscriptFound, yta.TranscriptsDisabled, yta.VideoUnavailable,
            yta.VideoUnplayable, yta.AgeRestricted) as e:
        raise NoCaptions(f"{type(e).__name__} for {video_id}") from e
    except Exception as e:
        if ratelimit.is_block(e):
            raise ratelimit.Blocked(f"{type(e).__name__}: {str(e)[:200]}") from e
        raise

    data = {
        "video_id": video_id,
        "url": url,
        "title": meta.get("title"),
        "channel": meta.get("author_name"),
        "language_code": transcript.language_code,
        "is_generated": transcript.is_generated,
        "snippets": [{"start": s.start, "duration": s.duration, "text": s.text} for s in snippets],
    }
    raw_store.write_json(path, data)
    return data


def save(data: dict) -> None:
    raw_store.write_json(captions_path(data["video_id"]), data)


def game_number_hint(data: dict):
    """'Game 4' / 'Game four' in the title or the opening captions -> 4."""
    opening = " ".join(s["text"] for s in data["snippets"][:20])
    for text in (data["title"], opening):
        m = re.search(r"\bgame (\d|one|two|three|four|five|six|seven)\b", text, re.I)
        if m:
            word = m.group(1).lower()
            return int(word) if word.isdigit() else NUMBERS[word]
    return None


def resolve_game_id(data: dict) -> int:
    """Find the NHL game from the video title: teams, then the date or the season."""
    title = data["title"] or ""
    teams = sorted({abbrev for name, abbrev in TEAMS.items() if re.search(rf"\b{name}\b", title)})
    if len(teams) != 2:
        raise GameUnresolved(f"Could not find two NHL teams in the title {title!r}; pass --game-id.")

    date = re.search(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})\b", title)
    if date:
        month, day, year = date.groups()
        try:
            day_str = datetime.strptime(f"{month[:3]} {day} {year}", "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            day_str = None
        if day_str:
            games = nhl_get_cached(f"score/{day_str}").get("games", [])
            matches = [g for g in games
                       if sorted([g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]]) == teams]
            if len(matches) == 1:
                return matches[0]["id"]

    # No usable date: the two teams' games in the season the title names.
    playoffs = bool(re.search(r"playoff|stanley cup|conference final|round|game [1-7]\b", title, re.I))
    season = re.search(r"\b((?:19|20)\d\d)-(\d\d)\b", title)  # "2019-20"
    year = re.search(r"\b((?:19|20)\d\d)\b", title)
    if season:
        y = int(season.group(1))
        seasons = [f"{y}{y + 1}"]
    elif year:
        y = int(year.group(1))
        # A playoff year is the season's second year; otherwise the year could be either half.
        seasons = [f"{y - 1}{y}"] if playoffs else [f"{y - 1}{y}", f"{y}{y + 1}"]
    else:
        raise GameUnresolved(f"No date or year in the title {title!r}; pass --game-id.")
    candidates = []
    for season in seasons:
        schedule = nhl_get_cached(f"club-schedule-season/{teams[0]}/{season}")
        for g in schedule.get("games", []):
            both = sorted([g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]]) == teams
            if both and g["gameType"] in ((3,) if playoffs else (2, 3)):
                candidates.append(g)

    number = game_number_hint(data)
    if len(candidates) > 1 and playoffs and number:
        # A playoff game ID ends in its game number within the series.
        candidates = [g for g in candidates if g["id"] % 10 == number]
    if len(candidates) == 1:
        return candidates[0]["id"]

    listing = "\n".join(
        f"  {g['id']}  {g['gameDate']}  {g['awayTeam']['abbrev']} @ {g['homeTeam']['abbrev']}" for g in candidates
    ) or "  (none found)"
    raise GameUnresolved(f"Could not pin down the game for {title!r}. Candidates:\n{listing}\nPass --game-id.")


def game_id_for(video_id: str, game_id: int = None) -> int:
    """The video's NHL game: given, saved with the captions, or resolved from the title (then saved)."""
    data = fetch(video_id)
    game_id = game_id or data.get("game_id") or resolve_game_id(data)
    if data.get("game_id") != game_id:
        data["game_id"] = game_id
        save(data)
    return game_id


def load(video_id: str, nhl_game_id: int) -> None:
    data = fetch(video_id)
    snippets = data["snippets"]

    con = db.connect("warehouse")
    with con:
        # a video re-pinned to another game: unlink it from the old one
        con.execute("UPDATE games SET video_id = NULL, video_title = NULL WHERE video_id = ? AND game_id != ?",
                    (video_id, nhl_game_id))
        upsert_game(con, nhl_game_id, video_id=video_id, video_title=data["title"])
        # Replace any previous load of this video, keeping its alignment: calibrated times
        # depend only on the video, which has not changed.
        aligned = con.execute(
            "SELECT calibrated_game_time, in_play, alignment_load_id, video_id, seq FROM transcript "
            "WHERE video_id = ? AND alignment_load_id IS NOT NULL",
            (video_id,),
        ).fetchall()
        con.execute("DELETE FROM transcript WHERE video_id = ?", (video_id,))
        load_id = db.new_load(con, "transcript", game_id=nhl_game_id, video_id=video_id,
                              raw_files=[captions_path(video_id)], source_urls=[data["url"]])
        con.executemany(
            "INSERT INTO transcript (video_id, game_id, seq, start_sec, duration_sec, end_sec, text, load_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (video_id, nhl_game_id, i, s["start"], s["duration"], s["start"] + s["duration"],
                 clean(s["text"]), load_id)
                for i, s in enumerate(snippets, start=1)
            ],
        )
        con.executemany(
            "UPDATE transcript SET calibrated_game_time = ?, in_play = ?, alignment_load_id = ? "
            "WHERE video_id = ? AND seq = ?",
            aligned,
        )
    ops.log.info("video %s (game %s): loaded %d transcript lines", video_id, nhl_game_id, len(snippets))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", help="YouTube link or video ID")
    parser.add_argument("--game-id", type=int, help="NHL game ID; found from the title when omitted")
    args = parser.parse_args()

    ops.setup_logging()
    ratelimit.enforce = False  # run by hand: only scheduled runs are rate-limited
    video_id = video_id_from(args.video)
    try:
        game_id = game_id_for(video_id, args.game_id)
    except (NoCaptions, GameUnresolved, ratelimit.Blocked) as e:
        raise SystemExit(str(e))
    print(f"{fetch(video_id)['title']} -> NHL game {game_id}")
    load(video_id, game_id)


if __name__ == "__main__":
    main()
