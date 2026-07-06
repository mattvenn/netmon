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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
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


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))
    conn.commit()


def rows_since(conn, last_id, limit=2000):
    """Rows with id > last_id (oldest first), for shipping to a hub. Returns
    (max_id_seen, [row_dicts]); detail is parsed back to an object so the hub can
    re-store it the same way a local insert would."""
    out, max_id = [], last_id
    for id_, ts, source, probe, target, ok, value, detail in conn.execute(
            "SELECT id, ts, source, probe, target, ok, value, detail FROM samples "
            "WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)):
        out.append({"ts": ts, "source": source, "probe": probe, "target": target,
                    "ok": bool(ok), "value": value,
                    "detail": json.loads(detail) if detail else None})
        max_id = id_
    return max_id, out


def insert_if_absent(conn, source, probe, target, ok, value, detail, ts):
    """Idempotent insert keyed on (ts, source, probe, target) — for hub ingest, so a
    retried push can't duplicate rows. Does not commit; caller commits the batch.
    Returns True if a row was inserted."""
    if conn.execute("SELECT 1 FROM samples WHERE ts = ? AND source = ? AND probe = ? "
                    "AND target = ? LIMIT 1", (ts, source, probe, target)).fetchone():
        return False
    conn.execute(
        "INSERT INTO samples (ts, source, probe, target, ok, value, detail) VALUES (?,?,?,?,?,?,?)",
        (ts, source, probe, target, 1 if ok else 0, value,
         json.dumps(detail) if detail else None))
    return True


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


def client_traffic_baseline(conn, before_ts):
    """Latest client_traffic snapshot per source strictly before before_ts.

    client_traffic values are cumulative router counters, so the dashboard
    needs the snapshot just before a window to diff against and show
    per-device usage *during* the selected period rather than lifetime totals.
    """
    rows = []
    sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM samples WHERE probe = 'client_traffic'")]
    for source in sources:
        row = conn.execute(
            """SELECT ts, detail FROM samples
               WHERE probe = 'client_traffic' AND source = ? AND ts < ?
               ORDER BY ts DESC LIMIT 1""", (source, before_ts)).fetchone()
        if row:
            ts, detail = row
            rows.append({"ts": ts, "source": source, "probe": "client_traffic",
                         "target": "", "ok": True, "value": None,
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
