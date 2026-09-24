"""Load a YouTube video's transcript into the MoneyPuck SQLite database.

Default: NHL full replay of Canadiens @ Sharks, Mar 3, 2026 (game 2025020969).

The captions are downloaded once and cached in data/raw/transcript_{video_id}.json;
later runs read the cache and do not contact YouTube.

Timestamps are seconds into the video, not game clock. Run scripts/align_video.py
afterwards to fill calibrated_game_time / in_play (reloading clears them).

Usage: python scripts/load_transcript.py [youtube_video_id] [nhl_game_id]
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "moneypuck.sqlite"
RAW_DIR = ROOT / "data" / "raw"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def fetch(video_id: str) -> dict:
    cache = RAW_DIR / f"transcript_{video_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())

    from youtube_transcript_api import YouTubeTranscriptApi

    url = f"https://www.youtube.com/watch?v={video_id}"
    meta = requests.get(
        "https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=30
    ).json()
    transcript = YouTubeTranscriptApi().list(video_id).find_transcript(["en"])
    data = {
        "video_id": video_id,
        "url": url,
        "title": meta.get("title"),
        "channel": meta.get("author_name"),
        "language_code": transcript.language_code,
        "is_generated": transcript.is_generated,
        "snippets": [
            {"start": s.start, "duration": s.duration, "text": s.text}
            for s in transcript.fetch().snippets
        ],
    }
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1))
    return data


def load(video_id: str, nhl_game_id: int) -> None:
    snippets = fetch(video_id)["snippets"]

    with sqlite3.connect(DB_PATH) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript (
                video_id             TEXT NOT NULL,
                game_id              INTEGER NOT NULL,
                seq                  INTEGER NOT NULL,
                start_sec            REAL NOT NULL,
                duration_sec         REAL NOT NULL,
                end_sec              REAL NOT NULL,
                text                 TEXT NOT NULL,
                calibrated_game_time REAL,
                in_play              INTEGER,
                PRIMARY KEY (video_id, seq)
            );
            CREATE INDEX IF NOT EXISTS ix_transcript_start ON transcript (video_id, start_sec);
            """
        )
        # Replace any previous load of this video so the script is re-runnable.
        con.execute("DELETE FROM transcript WHERE video_id = ?", (video_id,))
        con.executemany(
            "INSERT INTO transcript (video_id, game_id, seq, start_sec, duration_sec, end_sec, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (video_id, nhl_game_id, i, s["start"], s["duration"], s["start"] + s["duration"], clean(s["text"]))
                for i, s in enumerate(snippets, start=1)
            ],
        )

    print(f"Wrote {len(snippets)} transcript segments for video {video_id} to {DB_PATH}")


if __name__ == "__main__":
    load(
        sys.argv[1] if len(sys.argv) > 1 else "fqSsQgFJA4g",
        int(sys.argv[2]) if len(sys.argv) > 2 else 2025020969,
    )
