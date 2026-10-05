"""Apply or inspect database migrations (Postgres with DATABASE_URL, else the SQLite files).

Usage:
    python scripts/db/migrate.py            apply pending migrations to both databases
    python scripts/db/migrate.py status     list each migration and whether it is applied
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def main() -> None:
    status = len(sys.argv) > 1 and sys.argv[1] == "status"
    for name, path in db.DATABASES.items():
        where = f"Postgres schema {name}" if db.is_postgres() else str(path)
        con = db.open_connection(name)
        if status:
            done = db.applied_versions(con)
            print(f"{name} ({where}):")
            for f in db.migration_files(name):
                print(f"  [{'x' if int(f.name[:4]) in done else ' '}] {f.name}")
        else:
            applied = db.migrate(con, name)
            print(f"{name} ({where}): " + (", ".join(applied) if applied else "up to date"))
        con.close()


if __name__ == "__main__":
    main()
