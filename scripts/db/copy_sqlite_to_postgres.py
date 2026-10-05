"""Copy everything in the SQLite files (data/db/*.sqlite) into Postgres (DATABASE_URL).

One-off, for the move to Postgres. Each database is copied in one transaction, parents before
children (foreign keys), with COPY. It refuses to write into tables that already have rows
unless --replace is given (then the Postgres tables are emptied first). Afterwards the identity
counters continue after the copied ids, and row counts are compared table by table.

Usage: python scripts/db/copy_sqlite_to_postgres.py [--replace]
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

ORDER = {
    "warehouse": ["loads", "games", "events", "shots", "transcript"],
    "ops": ["runs", "steps", "requests", "videos", "kv"],
}
IDENTITY = {"runs": "run_id", "steps": "step_id", "requests": "request_id"}


def coerce(value, pg_type):
    """SQLite keeps whatever was stored; Postgres columns are typed."""
    if value is None:
        return None
    if pg_type in ("integer", "bigint"):
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
            value = float(value)
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{value!r} in an integer column")
            return int(value)
        return value
    if pg_type == "double precision":
        if isinstance(value, str):
            return float(value) if value.strip() else None
        return value
    return value if isinstance(value, str) else str(value)


def main() -> None:
    if not db.is_postgres():
        raise SystemExit("DATABASE_URL is not set")
    replace = "--replace" in sys.argv
    for name, tables in ORDER.items():
        src = sqlite3.connect(db.DATABASES[name])
        pg = db.connect(name)  # also applies the Postgres migrations
        counts = {t: pg.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        if any(counts.values()) and not replace:
            raise SystemExit(f"{name}: Postgres tables already have rows {counts}; use --replace to overwrite")
        with pg:
            if replace:
                pg.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
            for t in tables:
                src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
                types = dict(pg.execute("SELECT column_name, data_type FROM information_schema.columns "
                                        "WHERE table_schema = ? AND table_name = ?", (name, t)).fetchall())
                cols = [c for c in src_cols if c.lower() in types]
                missing = sorted(set(types) - {c.lower() for c in cols})
                if missing:
                    print(f"  {name}.{t}: not in SQLite, left empty: {', '.join(missing)}")
                kinds = [types[c.lower()] for c in cols]
                n = 0
                with pg.raw.cursor() as cur:
                    with cur.copy(f"COPY {t} ({', '.join(c.lower() for c in cols)}) FROM STDIN") as cp:
                        for row in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):
                            cp.write_row([coerce(v, k) for v, k in zip(row, kinds)])
                            n += 1
                print(f"  {name}.{t}: {n} rows")
            for t, col in IDENTITY.items():
                if t in tables:
                    pg.execute(f"SELECT setval(pg_get_serial_sequence('{name}.{t}', '{col}'), "
                               f"COALESCE((SELECT MAX({col}) FROM {t}), 0) + 1, false)")
        # verify
        for t in tables:
            a = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            b = pg.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  check {name}.{t}: sqlite {a}, postgres {b} {'OK' if a == b else 'MISMATCH'}")
        src.close()


if __name__ == "__main__":
    main()
