from __future__ import annotations

import sqlite3
from pathlib import Path


def run_migrations(connection: sqlite3.Connection, migrations_dir: Path) -> None:
    for path in sorted(migrations_dir.glob("*.sql")):
        script = path.read_text(encoding="utf-8")
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise
            statement = ""
    connection.commit()
