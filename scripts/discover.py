"""Find NHL full-game replays on YouTube with the official YouTube Data API.

Lists the NHL channel's uploads playlist (1 quota unit per 50 videos) and queues every video
whose title starts with "FULL REPLAY". The first scan reads as far back as the API allows (the
newest 20,000 uploads), resuming where it stopped if interrupted; later scans stop at the first
replay already queued. Older replays are found by a slow backfill using search (100 units per
page, a few pages per run), one year-long window at a time.
Needs YOUTUBE_API_KEY (in .env).

Usage: python scripts/discover.py
"""
import html
import os
import re
from datetime import datetime, timedelta, timezone

import requests

import ops
import ratelimit

API = "https://www.googleapis.com/youtube/v3"
CHANNEL_HANDLE = "@NHL"
TITLE_RE = re.compile(r"^\s*FULL (GAME )?REPLAY\b", re.I)
MAX_INCREMENTAL_PAGES = 20
BACKFILL_PAGES = 5            # search pages per run (500 of the 10,000 free daily units)
BACKFILL_UNTIL = "2015-01-01"  # stop searching back past this


def api_get(endpoint: str, **params) -> dict:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not set (add it to .env)")
    with ratelimit.request("yt_api", endpoint):
        resp = requests.get(f"{API}/{endpoint}", params={**params, "key": key}, timeout=30)
        resp.raise_for_status()
        return resp.json()


def uploads_playlist() -> str:
    playlist = ops.kv_get("nhl_uploads_playlist")
    if not playlist:
        items = api_get("channels", part="contentDetails", forHandle=CHANNEL_HANDLE).get("items", [])
        if not items:
            raise RuntimeError(f"YouTube channel {CHANNEL_HANDLE} not found")
        playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ops.kv_set("nhl_uploads_playlist", playlist)
        ops.kv_set("nhl_channel_id", items[0]["id"])
    return playlist


def backfill(max_pages: int = BACKFILL_PAGES) -> dict:
    """Older replays, via search (100 quota units per page), one year-long window at a time.

    The uploads playlist only returns the newest 20,000 uploads, so replays published before
    that need search. The cursor walks back a year per window and is kept between runs.
    """
    if ops.kv_get("backfill_done") == "1":
        return {"pages": 0, "new_videos": 0, "done": True}
    uploads_playlist()
    channel = ops.kv_get("nhl_channel_id")
    if not channel:
        channel = api_get("channels", part="id", forHandle=CHANNEL_HANDLE)["items"][0]["id"]
        ops.kv_set("nhl_channel_id", channel)
    before = ops.kv_get("backfill_before") or _oldest_scanned()
    token = ops.kv_get("backfill_token")
    pages = added = 0
    while pages < max_pages:
        after = _year_before(before)
        # The quoted phrase, by relevance: sorting by date makes search ignore the phrase and
        # return every upload in the window.
        params = {"part": "snippet", "channelId": channel, "q": '"full replay"', "type": "video",
                  "order": "relevance", "maxResults": 50, "publishedBefore": before, "publishedAfter": after}
        if token:
            params["pageToken"] = token
        page = api_get("search", **params)
        pages += 1
        for item in page.get("items", []):
            title = html.unescape(item["snippet"]["title"])
            if TITLE_RE.match(title) and ops.add_video(item["id"]["videoId"], title, item["snippet"]["publishedAt"]):
                added += 1
        token = page.get("nextPageToken")
        if not token:  # window finished: step back a year
            before = after
            if before < BACKFILL_UNTIL:
                ops.kv_set("backfill_done", "1")
                break
        ops.kv_set("backfill_before", before)
        ops.kv_set("backfill_token", token)
    result = {"pages": pages, "quota_units": pages * 100, "new_videos": added, "searched_back_to": before}
    ops.log.info("discovery backfill: %s", result)
    return result


def _oldest_scanned() -> str:
    row = ops.connect().execute("SELECT MIN(published_at) FROM videos WHERE published_at > ''").fetchone()
    return row[0] or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _year_before(iso: str) -> str:
    t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    return (t - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover() -> dict:
    playlist = uploads_playlist()
    full_scan_done = ops.kv_get("discover_full_scan_done") == "1"
    token = None if full_scan_done else ops.kv_get("discover_resume_token")
    pages = added = seen_replays = 0
    while True:
        params = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = api_get("playlistItems", **params)
        pages += 1
        hit_known = False
        for item in page.get("items", []):
            title = item["snippet"]["title"]
            if not TITLE_RE.match(title):
                continue
            seen_replays += 1
            video_id = item["contentDetails"]["videoId"]
            published = item["contentDetails"].get("videoPublishedAt") or item["snippet"]["publishedAt"]
            if ops.add_video(video_id, title, published):
                added += 1
            else:
                hit_known = True
        token = page.get("nextPageToken")
        if not full_scan_done:
            ops.kv_set("discover_resume_token", token)
            if not token:
                ops.kv_set("discover_full_scan_done", "1")
                break
        elif hit_known or not token or pages >= MAX_INCREMENTAL_PAGES:
            break
    result = {"pages": pages, "quota_units": pages, "replays_seen": seen_replays, "new_videos": added,
              "full_scan": not full_scan_done}
    ops.log.info("discovery: %s", result)
    return result


if __name__ == "__main__":
    ops.setup_logging()
    print(discover())
    print(backfill())
