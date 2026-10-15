"""Rate limiting for every outbound request, so scraping stays slow and polite.

Wrap each request:

    with ratelimit.request("yt_frame", target=url) as r:
        ...                       # do the request; set r["status"] = "blocked" if refused

- Spacing: requests in the same group wait a minimum interval (with jitter) after the last
  one, and an hourly cap applies. Spacing is read from the requests table, so it holds across
  processes (a scheduled run and a dashboard-triggered run can't stack).
- Budget: YouTube scraping requests per process (one pipeline run) are capped.
- Circuit breaker: when YouTube refuses us (IP block, HTTP 429/403, bot check), all YouTube
  requests stop and a cooldown starts: 24 h, doubling on each repeat block, up to 7 days.
  Blocks more than 14 days apart start again at 24 h.

These rules apply to scheduled runs. Runs started by hand (dashboard, make, CLI) set
`enforce = False`: no spacing, hourly cap, budget or cooldown, and a refusal fails only the request
it hit. Every request is still recorded, and a refusal still starts the cooldown (once per process),
so the next scheduled run backs off.
"""
import os
import random
import threading
import time
from contextlib import contextmanager

import ops

YOUTUBE_KINDS = {"yt_page", "yt_caption", "yt_frame"}  # scraping (yt_api is the official API)

# kind -> spacing group; requests in a group are spaced against each other
GROUPS = {"yt_page": "yt_web", "yt_caption": "yt_web", "yt_frame": "yt_frame",
          "yt_api": "yt_api", "nhl": "nhl", "moneypuck": "moneypuck"}
# group -> (minimum interval s, jitter fraction, max per hour or None)
SPACING = {
    "yt_web": (10.0, 0.3, 60),
    "yt_frame": (float(os.environ.get("XG_FRAME_INTERVAL", "2.0")), 0.3,
                 int(os.environ.get("XG_FRAME_PER_HOUR", "600"))),
    "yt_api": (0.2, 0.0, None),
    "nhl": (1.0, 0.2, None),
    "moneypuck": (1.0, 0.2, None),
}
RUN_BUDGET = int(os.environ.get("XG_YOUTUBE_BUDGET", "800"))  # YouTube scraping requests per run
COOLDOWN_HOURS, MAX_COOLDOWN_HOURS, BLOCK_MEMORY_DAYS = 24, 168, 14

# Swappable for tests.
clock = time.time
sleep = time.sleep
rng = random.Random()

offline = os.environ.get("XG_OFFLINE") == "1"  # refuse all network access (rebuilds)
enforce = True  # False for runs started by hand: only scheduled runs are limited
youtube_count = 0  # YouTube scraping requests made by this process
_tripped = False
_locks = {g: threading.Lock() for g in SPACING}
_count_lock = threading.Lock()


class Blocked(Exception):
    """YouTube refused us, or a cooldown from an earlier block is still running."""


class BudgetExhausted(Exception):
    """This run has used its YouTube request budget."""


class Offline(Exception):
    """Network access is disabled (XG_OFFLINE=1)."""


def cooldown_until() -> float:
    return float(ops.kv_get("cooldown_until", 0) or 0)


def youtube_available() -> bool:
    if not enforce:
        return True
    return not _tripped and cooldown_until() <= clock() and youtube_count < RUN_BUDGET


def gate(kind: str) -> None:
    """Wait until a request of this kind may go out, or raise if it may not."""
    global youtube_count
    if offline:
        raise Offline(f"network disabled; refusing {kind} request")
    if not enforce:
        if kind in YOUTUBE_KINDS:
            with _count_lock:
                youtube_count += 1
        con = ops.connect()
        with con:
            return con.execute("INSERT INTO requests (ts, run_id, kind, status) VALUES (?, ?, ?, 'sent') "
                               "RETURNING request_id", (clock(), ops.current_run_id, kind)).fetchone()[0]
    if kind in YOUTUBE_KINDS:
        if _tripped:
            raise Blocked("YouTube refused a request earlier in this run")
        until = cooldown_until()
        if until > clock():
            raise Blocked(f"cooling down until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}")
        with _count_lock:
            if youtube_count >= RUN_BUDGET:
                raise BudgetExhausted(f"used this run's budget of {RUN_BUDGET} YouTube requests")
            youtube_count += 1

    group = GROUPS[kind]
    interval, jitter, per_hour = SPACING[group]
    kinds = [k for k, g in GROUPS.items() if g == group]
    marks = ",".join("?" * len(kinds))
    with _locks[group]:
        con = ops.connect()
        last = con.execute(f"SELECT MAX(ts) FROM requests WHERE kind IN ({marks})", kinds).fetchone()[0] or 0
        wait = last + interval * (1 + rng.uniform(-jitter, jitter)) - clock()
        if per_hour:
            recent = con.execute(
                f"SELECT ts FROM requests WHERE kind IN ({marks}) AND ts > ? ORDER BY ts",
                (*kinds, clock() - 3600),
            ).fetchall()
            if len(recent) >= per_hour:
                wait = max(wait, recent[len(recent) - per_hour][0] + 3600 - clock())
        if wait > 0:
            if wait > 30:
                ops.log.info("rate limit: waiting %.0f s before next %s request", wait, kind)
            sleep(wait)
        # Reserve the slot before releasing the lock, so the next caller spaces after us.
        with con:
            return con.execute(
                "INSERT INTO requests (ts, run_id, kind, status) VALUES (?, ?, ?, 'sent') RETURNING request_id",
                (clock(), ops.current_run_id, kind),
            ).fetchone()[0]


def is_block(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    if name in ("IpBlocked", "RequestBlocked", "TooManyRequests"):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 429):
        return True
    return any(s in text for s in ("HTTP Error 429", "HTTP Error 403", "confirm you're not a bot",
                                    "Sign in to confirm"))


def trip(reason: str) -> None:
    """Open the circuit breaker: stop YouTube requests and start (or extend) the cooldown."""
    global _tripped
    _tripped = True
    now = clock()
    last = float(ops.kv_get("last_block_ts", 0) or 0)
    count = int(ops.kv_get("block_count", 0) or 0)
    if now - last > BLOCK_MEMORY_DAYS * 86400:
        count = 0
    count += 1
    hours = min(COOLDOWN_HOURS * 2 ** (count - 1), MAX_COOLDOWN_HOURS)
    ops.kv_set("block_count", count)
    ops.kv_set("last_block_ts", now)
    ops.kv_set("last_block_reason", reason[:500])
    ops.kv_set("cooldown_until", now + hours * 3600)
    ops.log.warning("YouTube blocked us (%s); pausing YouTube requests for %d h", reason[:200], hours)


@contextmanager
def request(kind: str, target: str = ""):
    request_id = gate(kind)
    rec = {"status": "ok"}
    start = clock()
    error = None
    try:
        yield rec
    except BaseException as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        if rec["status"] == "ok":  # the caller may already have marked it
            blocked = kind in YOUTUBE_KINDS and (isinstance(e, Blocked) or is_block(e))
            rec["status"] = "blocked" if blocked else "error"
        raise
    finally:
        con = ops.connect()
        with con:
            con.execute(
                "UPDATE requests SET status = ?, target = ?, latency_ms = ? WHERE request_id = ?",
                (rec["status"], target[:300], int((clock() - start) * 1000), request_id),
            )
        # A run started by hand keeps going after a refusal, so record the block only once.
        if rec["status"] == "blocked" and (enforce or not _tripped):
            trip(f"{kind} {target[:120]} ({error or 'refused'})")
