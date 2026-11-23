"""Compiler-side records for the isolated pilot's asynchronous builds."""

from contextlib import contextmanager
import json
import sqlite3
import time
import uuid

DB_PATH = "/build-state/jobs.sqlite3"


@contextmanager
def database():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init():
    with database() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                request_id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL UNIQUE,
                work TEXT NOT NULL,
                source_version TEXT NOT NULL,
                status TEXT NOT NULL,
                accepted_at_ns INTEGER NOT NULL,
                started_at_ns INTEGER,
                released INTEGER NOT NULL DEFAULT 0,
                receipt TEXT
            )
        """)


def read(db, request_id):
    row = db.execute("SELECT * FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["receipt"] is not None:
        result["receipt"] = json.loads(result["receipt"])
    return result


def lookup(request_id):
    with database() as db:
        return read(db, request_id)


def submit(request_id, work, version):
    with database() as db:
        created = db.execute("""
            INSERT INTO jobs (request_id, build_id, work, source_version, status, accepted_at_ns)
            VALUES (?, ?, ?, ?, 'accepted', ?) ON CONFLICT (request_id) DO NOTHING
        """, (request_id, str(uuid.uuid4()), work, version, time.time_ns())).rowcount == 1
        job = read(db, request_id)
    return job, created


def start(request_id):
    with database() as db:
        db.execute("UPDATE jobs SET status = 'running', started_at_ns = ? WHERE request_id = ?",
                   (time.time_ns(), request_id))
    return lookup(request_id)


def release(request_id):
    with database() as db:
        return db.execute("""
            UPDATE jobs SET released = 1 WHERE request_id = ? AND status = 'running' AND released = 0
        """, (request_id,)).rowcount == 1


def finish(request_id, receipt=None):
    with database() as db:
        db.execute("UPDATE jobs SET status = ?, receipt = ? WHERE request_id = ?",
                   ("completed" if receipt is not None else "failed", json.dumps(receipt), request_id))
