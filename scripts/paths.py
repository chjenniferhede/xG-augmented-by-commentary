"""Where the project's data lives.

Everything is under one data root: `data/` in the repo by default, or the folder in the
XG_DATA_DIR environment variable (a relative path is taken relative to the repo). Settings are
also read from the repo's .env file.

    raw/                          source data as downloaded
      moneypuck/                  season shot zips, games/<season>/<game_id>.csv
      nhl/                        boxscores, schedule responses
      youtube/captions/           <video_id>.json: caption track + video metadata
      youtube/frames/<video_id>/  scoreboard crops + index.json (OCR readings)
    db/moneypuck.sqlite           the warehouse: games, events, shots, transcript, loads
    db/pipeline.sqlite            operations: runs, steps, requests, videos queue
    logs/                         one log file per pipeline run
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

_data = Path(os.environ.get("XG_DATA_DIR", "data")).expanduser()
DATA = (_data if _data.is_absolute() else ROOT / _data).resolve()

RAW = DATA / "raw"
MONEYPUCK = RAW / "moneypuck"
NHL = RAW / "nhl"
CAPTIONS = RAW / "youtube" / "captions"
FRAMES = RAW / "youtube" / "frames"
DB_DIR = DATA / "db"
WAREHOUSE_DB = DB_DIR / "moneypuck.sqlite"
OPS_DB = DB_DIR / "pipeline.sqlite"
LOGS = DATA / "logs"
LOCK_FILE = DB_DIR / "pipeline.lock"
