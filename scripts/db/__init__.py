"""Database connections, versioned migrations, and load provenance.

Two databases, warehouse (the data) and ops (runs, steps, requests, the video queue). With
DATABASE_URL set they are the schemas `warehouse` and `ops` of one Postgres database (Supabase);
otherwise SQLite files in data/db/ (tests, offline work).

Each database has a folder of numbered SQL migrations in scripts/db/migrations/<name>/. A file
named NNNN_x.postgres.sql replaces NNNN_x.sql on Postgres. Connecting applies any that are
pending, so the scripts never create or alter tables themselves. The applied versions are
recorded in each database's schema_version table.

The code writes SQL once, SQLite style: `?` placeholders and `with con:` for a transaction. On
Postgres a small adapter translates the placeholders; statements outside `with con:` commit
on their own.
"""
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paths
import raw_store

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
DATABASES = {"warehouse": paths.WAREHOUSE_DB, "ops": paths.OPS_DB}
DATABASE_URL = os.environ.get("DATABASE_URL") or None  # empty = SQLite

class _ThreadConnections(dict):
    """A thread's connections, closed when the thread ends: a worker thread's thread-local data is
    freed as it exits, so its connections don't stay open until garbage collection (the database
    login has a connection limit)."""

    def __del__(self):
        for con in self.values():
            try:
                con.close()
            except Exception:
                pass


_local = threading.local()  # .conns: one connection per database per thread


def _conns() -> _ThreadConnections:
    if not hasattr(_local, "conns"):
        _local.conns = _ThreadConnections()
    return _local.conns


def is_postgres() -> bool:
    return DATABASE_URL is not None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migration_files(name: str):
    """The migration files for this backend, in order."""
    by_version = {}
    for p in sorted((MIGRATIONS / name).glob("*.sql")):
        if not p.name[:4].isdigit():
            continue
        pg = p.name.endswith(".postgres.sql")
        if pg and not is_postgres():
            continue
        version = int(p.name[:4])
        if pg or version not in by_version:  # a Postgres variant replaces the shared file
            by_version[version] = p
    return [by_version[v] for v in sorted(by_version)]


def applied_versions(con) -> set:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return {row[0] for row in con.execute("SELECT version FROM schema_version")}


def migrate(con, name: str) -> list:
    """Apply pending migrations in order; returns the file names applied."""
    done = applied_versions(con)
    applied = []
    for path in migration_files(name):
        version = int(path.name[:4])
        if version in done:
            continue
        # One transaction per file: its statements and its version row land together or not at all.
        if isinstance(con, PgConnection):
            with con:
                con.execute_script(path.read_text())
                con.execute("INSERT INTO schema_version VALUES (?, ?, ?)", (version, path.name, now_iso()))
        else:
            con.executescript(
                "BEGIN;\n" + path.read_text()
                + f"\nINSERT INTO schema_version VALUES ({version}, '{path.name}', '{now_iso()}');\nCOMMIT;"
            )
        applied.append(path.name)
    return applied


class PgConnection:
    """A psycopg connection that takes the SQLite-style SQL the code is written in."""

    def __init__(self, url: str, schema: str):
        self.url, self.schema = url, schema
        self.row_factory = None  # accepted and ignored (rows are always tuples)
        self._tx = []
        self._open()

    def _open(self):
        import psycopg

        self._con = psycopg.connect(self.url, autocommit=True, connect_timeout=20,
                                    keepalives=1, keepalives_idle=60, keepalives_interval=15, keepalives_count=4)
        self._con.execute(f"SET search_path TO {self.schema}")
        # This database sends floats rounded to 15 digits (extra_float_digits = 0); ask for exact
        # values, or reading a number and writing it back (as reloads do) would lose precision.
        self._con.execute("SET extra_float_digits = 3")

    @staticmethod
    def _sql(query: str) -> str:
        return query.replace("%", "%%").replace("?", "%s")

    def _run(self, fn):
        import psycopg

        try:
            return fn()
        except psycopg.OperationalError:
            if self._tx or not (self._con.closed or self._con.broken):
                raise
            self._open()  # the connection dropped between statements (idle for a long run): retry once
            return fn()

    def execute(self, query: str, params=None):
        if params is None:
            return self._run(lambda: self._con.execute(query))
        return self._run(lambda: self._con.execute(self._sql(query), tuple(params)))

    def executemany(self, query: str, seq):
        rows = [tuple(r) for r in seq]

        def go():
            cur = self._con.cursor()
            if rows:
                cur.executemany(self._sql(query), rows)
            return cur
        return self._run(go)

    def execute_script(self, script: str):
        """Several statements, no parameters (migrations)."""
        self._con.execute(script)

    def __enter__(self):
        tx = self._con.transaction()
        tx.__enter__()
        self._tx.append(tx)
        return self

    def __exit__(self, *exc):
        return self._tx.pop().__exit__(*exc)

    def close(self):
        self._con.close()

    @property
    def raw(self):
        """The underlying psycopg connection (COPY, advisory locks)."""
        return self._con


def connect(name: str):
    """The calling thread's connection to a database (warehouse or ops), migrated to the latest version."""
    conns = _conns()
    if name in conns:
        return conns[name]
    con = open_connection(name)
    migrate(con, name)
    conns[name] = con
    return con


def open_connection(name: str):
    """A new connection, without migrating (migrate.py's status)."""
    if is_postgres():
        return PgConnection(DATABASE_URL, name)
    path = DATABASES[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")  # the dashboard reads while a run writes
    con.execute("PRAGMA foreign_keys=ON")
    return con


def close_thread_connections() -> None:
    """Close this thread's connections (the dashboard does this after each request)."""
    conns = _conns()
    for con in conns.values():
        try:
            con.close()
        except Exception:
            pass
    conns.clear()


def column_types(con, table: str) -> dict:
    """{column: type name, lowercase} in table order (the SQLite migrations use the same type names)."""
    if isinstance(con, PgConnection):
        rows = con.execute("SELECT column_name, data_type FROM information_schema.columns "
                           "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                           (con.schema, table)).fetchall()
        return {name: typ.lower() for name, typ in rows}
    return {row[1]: row[2].lower() for row in con.execute(f"PRAGMA table_info({table})")}


def reset(name: str) -> None:
    """Empty a database completely (a full rebuild): the SQLite file is moved aside as a backup;
    on Postgres every table and view in the schema is dropped. The next connect re-creates them."""
    close_thread_connections()
    if is_postgres():
        con = PgConnection(DATABASE_URL, name)
        with con:
            for kind, obj in con.execute(
                    "SELECT 'VIEW', table_name FROM information_schema.views WHERE table_schema = ? "
                    "UNION ALL SELECT 'TABLE', tablename FROM pg_tables WHERE schemaname = ?", (name, name)).fetchall():
                con.execute(f'DROP {kind} IF EXISTS {name}."{obj}" CASCADE')
        con.close()
        return
    path = DATABASES[name]
    if path.exists():
        backup = path.with_suffix(".sqlite.bak")
        for suffix in ("", "-wal", "-shm"):
            src = path.with_name(path.name + suffix)
            if src.exists():
                os.replace(src, backup.with_name(backup.name + suffix))


def code_version() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=paths.ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "scripts"], cwd=paths.ROOT,
                               capture_output=True, text=True).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def file_records(raw_files) -> str:
    """JSON list of {path (under data/), sha256} for the raw files a batch was made from."""
    files = [Path(p) for p in raw_files if p]
    return json.dumps([{"path": str(p.relative_to(paths.DATA)), "sha256": raw_store.sha256(p)} for p in files])


def new_load(con, kind: str, *, game_id=None, video_id=None, raw_files=(), source_urls=()) -> str:
    """Record one batch of loaded data (kind: games, events, shots or captions); returns its load_id."""
    import ops  # late import: ops imports db

    files = [Path(p) for p in raw_files if p]
    fetched = max((p.stat().st_mtime for p in files), default=None)
    load_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO loads (load_id, kind, game_id, video_id, raw_files, source_urls, fetched_at, "
        "loaded_at, code_version, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            load_id, kind, game_id, video_id, file_records(files), json.dumps(list(source_urls)),
            datetime.fromtimestamp(fetched, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if fetched else None,
            now_iso(), code_version(), ops.current_run_id,
        ),
    )
    return load_id
