#!/usr/bin/env python3
"""Merge WAL sidecars into webhooks.db and switch to DELETE journal."""
import sqlite3
from pathlib import Path

import db as dbmod

db = Path(__file__).resolve().parent / "webhooks.db"
dbmod.prepare_database_file(db)
c = sqlite3.connect(db)
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("PRAGMA journal_mode=DELETE")
n = c.execute("SELECT MAX(id) FROM events").fetchone()[0]
c.close()
for suffix in ("-wal", "-shm"):
    p = db.parent / f"{db.name}{suffix}"
    if p.exists():
        p.unlink()
print("merged; max event id:", n)
