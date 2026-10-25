"""Metadata-only inventory of media in verified local-library directories."""
from __future__ import annotations

import sqlite3
import os
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_local_file(
  owner_path TEXT NOT NULL,absolute_path TEXT NOT NULL,size INTEGER NOT NULL,
  file_kind TEXT NOT NULL,mtime REAL NOT NULL,
  PRIMARY KEY(owner_path,absolute_path)
);
"""


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def replace_owner(db: sqlite3.Connection, owner: str, observed: Iterable[dict[str, Any]]) -> None:
    """Publish only after a complete, successful read; never claim media ownership."""
    rows = [(owner, str(row["path"]), int(row["bytes"]), str(row["kind"]), float(row["modifiedAt"]))
            for row in observed if row["kind"] in {"main_video", "bonus_video"}]
    db.execute("SAVEPOINT local_inventory")
    try:
        db.execute("DELETE FROM runtime_local_file WHERE owner_path=?", (owner,))
        db.executemany("INSERT INTO runtime_local_file VALUES(?,?,?,?,?)", rows)
        db.execute("RELEASE local_inventory")
    except BaseException:
        db.execute("ROLLBACK TO local_inventory")
        db.execute("RELEASE local_inventory")
        raise


def relative_kind_path(path: str, owner: Path) -> str:
    return Path(path).relative_to(owner).as_posix()


def owner_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))
