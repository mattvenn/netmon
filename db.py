"""SQLite storage for probe samples."""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    probe TEXT NOT NULL,
    target TEXT NOT NULL,
    ok INTEGER NOT NULL,
    value REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_probe_ts ON samples(probe, ts);
"""


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert(conn, source, probe, target, ok, value=None, detail=None, ts=None):
    conn.execute(
        "INSERT INTO samples (ts, source, probe, target, ok, value, detail) VALUES (?,?,?,?,?,?,?)",
        (ts or time.time(), source, probe, target, 1 if ok else 0, value,
         json.dumps(detail) if detail else None))
    conn.commit()


def history(conn, since_ts, probes=None):
    q = "SELECT ts, source, probe, target, ok, value, detail FROM samples WHERE ts >= ?"
    args = [since_ts]
    if probes:
        q += " AND probe IN (%s)" % ",".join("?" * len(probes))
        args += probes
    q += " ORDER BY ts"
    rows = []
    for ts, source, probe, target, ok, value, detail in conn.execute(q, args):
        rows.append({"ts": ts, "source": source, "probe": probe, "target": target,
                     "ok": bool(ok), "value": value,
                     "detail": json.loads(detail) if detail else None})
    return rows


def latest(conn, window_s=180):
    """Most recent sample per (source, probe, target) within the window."""
    since = time.time() - window_s
    q = """SELECT ts, source, probe, target, ok, value, detail FROM samples
           WHERE ts >= ? ORDER BY ts"""
    out = {}
    for ts, source, probe, target, ok, value, detail in conn.execute(q, (since,)):
        out[(source, probe, target)] = {
            "ts": ts, "source": source, "probe": probe, "target": target,
            "ok": bool(ok), "value": value,
            "detail": json.loads(detail) if detail else None}
    return list(out.values())
