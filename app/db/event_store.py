"""
Event Store — Persists all routing decisions and generation outcomes.
Uses SQLite for simplicity and deploy-friendliness.
"""

import time
import json
import sqlite3
import logging
import asyncio
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = "trypix.db"


class EventStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._memory_conn = None
        if db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS routing_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    selected_provider TEXT,
                    fallback_sequence TEXT,
                    reason TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS generation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL,
                    error_reason TEXT,
                    attempt INTEGER DEFAULT 1,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS failover_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    from_provider TEXT NOT NULL,
                    to_provider TEXT NOT NULL,
                    reason TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_gen_provider ON generation_events(provider_name);
                CREATE INDEX IF NOT EXISTS idx_gen_created ON generation_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_routing_job ON routing_decisions(job_id);
            """)
        logger.info(f"[eventstore] Initialized DB at {self.db_path}")

    @contextmanager
    def _connect(self):
        if self._memory_conn is not None:
            try:
                yield self._memory_conn
                self._memory_conn.commit()
            except Exception:
                self._memory_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def record_routing_decision(
        self,
        job_id: str,
        selected_provider: Optional[str],
        fallback_sequence: list,
        reason: str,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO routing_decisions
                       (job_id, selected_provider, fallback_sequence, reason, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (job_id, selected_provider, json.dumps(fallback_sequence), reason, time.time()),
                )

    async def record_generation(
        self,
        job_id: str,
        provider_name: str,
        success: bool,
        latency_ms: float,
        error_reason: Optional[str] = None,
        attempt: int = 1,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO generation_events
                       (job_id, provider_name, success, latency_ms, error_reason, attempt, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (job_id, provider_name, int(success), latency_ms, error_reason, attempt, time.time()),
                )

    async def record_failover(
        self,
        job_id: str,
        from_provider: str,
        to_provider: str,
        reason: str,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO failover_events
                       (job_id, from_provider, to_provider, reason, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (job_id, from_provider, to_provider, reason, time.time()),
                )

    def get_recent_generations(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_failures(self, limit: int = 20) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_events WHERE success = 0 ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_failovers(self, limit: int = 20) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM failover_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_success_rate_over_time(self, provider_name: str, bucket_minutes: int = 5, num_buckets: int = 12) -> list:
        now = time.time()
        bucket_seconds = bucket_minutes * 60
        results = []
        with self._connect() as conn:
            for i in range(num_buckets - 1, -1, -1):
                bucket_end = now - i * bucket_seconds
                bucket_start = bucket_end - bucket_seconds
                row = conn.execute(
                    """SELECT COUNT(*) as total, SUM(success) as successes, AVG(latency_ms) as avg_latency
                       FROM generation_events
                       WHERE provider_name = ? AND created_at >= ? AND created_at < ?""",
                    (provider_name, bucket_start, bucket_end),
                ).fetchone()
                total = row["total"] or 0
                successes = row["successes"] or 0
                results.append({
                    "bucket_start": bucket_start,
                    "bucket_end": bucket_end,
                    "total": total,
                    "successes": successes,
                    "success_rate": round(successes / total, 4) if total > 0 else None,
                    "avg_latency_ms": round(row["avg_latency"], 1) if row["avg_latency"] else None,
                })
        return results

    def get_stats_summary(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as total_jobs, SUM(success) as total_successes
                   FROM generation_events"""
            ).fetchone()
            failover_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM failover_events"
            ).fetchone()["cnt"]
        total = row["total_jobs"] or 0
        successes = row["total_successes"] or 0
        return {
            "total_jobs": total,
            "total_successes": successes,
            "overall_success_rate": round(successes / total, 4) if total > 0 else None,
            "total_failovers": failover_count,
        }
