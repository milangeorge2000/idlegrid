"""SQLite storage. One connection guarded by a lock; queries are plain SQL so the
schema can move to Postgres later without an ORM rewrite."""

import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  credits REAL NOT NULL DEFAULT 0, earnings REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  key TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS join_tokens (
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, secret_hash TEXT NOT NULL, name TEXT,
  platform TEXT, chip TEXT, memory_gb REAL, usable_mem_gb REAL, backends TEXT, benchmark TEXT,
  cached_models TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'probation',
  reputation REAL NOT NULL DEFAULT 0.5, canary_pass INTEGER NOT NULL DEFAULT 0,
  canary_fail INTEGER NOT NULL DEFAULT 0, verify_mismatch INTEGER NOT NULL DEFAULT 0,
  tasks_done INTEGER NOT NULL DEFAULT 0, tokens_served INTEGER NOT NULL DEFAULT 0,
  earnings REAL NOT NULL DEFAULT 0, last_seen REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, model TEXT NOT NULL, params TEXT NOT NULL,
  status TEXT NOT NULL, total INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, job_id TEXT, row_index INTEGER, kind TEXT NOT NULL, model TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL, node_id TEXT, lease_expires REAL,
  attempts INTEGER NOT NULL DEFAULT 0, result TEXT, tokens INTEGER NOT NULL DEFAULT 0,
  verify_of TEXT, excluded_nodes TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, model);
CREATE INDEX IF NOT EXISTS tasks_job ON tasks(job_id);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT, node_id TEXT,
  amount REAL NOT NULL, kind TEXT NOT NULL, ref TEXT
);
"""

JSON_COLS = {"backends", "benchmark", "cached_models", "params", "payload", "result", "excluded_nodes"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLS & d.keys():
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    @staticmethod
    def _encode(values: dict) -> dict:
        return {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in values.items()}

    def one(self, sql, *args):
        with self.lock:
            return self._decode(self.conn.execute(sql, args).fetchone())

    def all(self, sql, *args):
        with self.lock:
            return [self._decode(r) for r in self.conn.execute(sql, args).fetchall()]

    def run(self, sql, *args):
        with self.lock:
            return self.conn.execute(sql, args)

    def insert(self, table, **values):
        values = self._encode(values)
        cols = ", ".join(values)
        qs = ", ".join("?" for _ in values)
        self.run(f"INSERT INTO {table} ({cols}) VALUES ({qs})", *values.values())

    def update(self, table, key, **values):
        values = self._encode(values)
        sets = ", ".join(f"{k} = ?" for k in values)
        self.run(f"UPDATE {table} SET {sets} WHERE id = ?", *values.values(), key)

    def tx(self):
        """Context manager for an atomic block."""
        db = self

        class _Tx:
            def __enter__(self):
                db.lock.acquire()
                db.conn.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self, exc_type, *_):
                try:
                    db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db.lock.release()

        return _Tx()


def now() -> float:
    return time.time()
