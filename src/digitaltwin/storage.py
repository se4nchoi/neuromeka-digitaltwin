"""SQLite persistence layer for production runs, cycles, steps, and event logs."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Returns the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class ProductionStorage:
    """Thread-safe SQLite storage for workcell production and event telemetry."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=10.0,
            autocommit=True,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL;")
            cur.execute("PRAGMA synchronous = NORMAL;")
            cur.execute("PRAGMA foreign_keys = ON;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

            cur.execute("SELECT MAX(version) FROM schema_migrations;")
            row = cur.fetchone()
            current_version = row[0] if row and row[0] is not None else 0

            if current_version < 1:
                self._apply_migration_1(cur)
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?);",
                    (utc_now_iso(),),
                )

    def _apply_migration_1(self, cur: sqlite3.Cursor) -> None:
        """Initial schema migration: runs, cycles, steps, transitions, faults, operator actions."""
        cur.execute("""
            CREATE TABLE IF NOT EXISTS production_runs (
                run_id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                origin TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                recipe_version TEXT NOT NULL,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL DEFAULT 8,
                completed_count INTEGER NOT NULL DEFAULT 0,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_seconds REAL,
                error_message TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES production_runs(run_id) ON DELETE CASCADE,
                cycle_index INTEGER NOT NULL,
                target_slot INTEGER NOT NULL,
                status TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_seconds REAL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS process_steps (
                step_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
                step_name TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_seconds REAL,
                details_json TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS state_transitions (
                transition_id TEXT PRIMARY KEY,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                trigger TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                run_id TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fault_events (
                fault_id TEXT PRIMARY KEY,
                run_id TEXT,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                interrupted_step TEXT,
                recovery_requirement TEXT,
                timestamp TEXT NOT NULL,
                resolved_at TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS operator_actions (
                action_id TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                parameters_json TEXT,
                timestamp TEXT NOT NULL
            );
        """)

        # Indexes for query performance
        cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON production_runs(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cycles_run ON cycles(run_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_steps_cycle ON process_steps(cycle_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transitions_time ON state_transitions(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faults_time ON fault_events(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_time ON operator_actions(timestamp);")

    # -------------------------------------------------------------------------
    # Reconcile on Startup
    # -------------------------------------------------------------------------
    def reconcile_interrupted_runs(self) -> int:
        """Marks any runs and cycles left in 'running' status as 'interrupted'."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT run_id, start_time FROM production_runs WHERE status = 'running';
            """)
            interrupted = cur.fetchall()
            now_iso = utc_now_iso()
            count = len(interrupted)

            if count > 0:
                cur.execute("""
                    UPDATE production_runs
                    SET status = 'interrupted',
                        end_time = ?,
                        error_message = COALESCE(error_message, 'Interrupted by server restart')
                    WHERE status = 'running';
                """, (now_iso,))

                cur.execute("""
                    UPDATE cycles
                    SET status = 'interrupted',
                        end_time = ?
                    WHERE status = 'running';
                """, (now_iso,))

                cur.execute("""
                    UPDATE process_steps
                    SET status = 'interrupted',
                        end_time = ?
                    WHERE status = 'running';
                """, (now_iso,))

                self._conn.commit()
                logger.info("Reconciled %d interrupted production runs on startup", count)

            return count

    # -------------------------------------------------------------------------
    # Production Runs
    # -------------------------------------------------------------------------
    def start_run(
        self,
        run_id: str,
        command: str,
        origin: str,
        recipe_id: str,
        recipe_version: str,
        target_count: int = 8,
        start_iso: Optional[str] = None,
    ) -> str:
        with self._lock:
            cur = self._conn.cursor()
            ts = start_iso or utc_now_iso()
            cur.execute("""
                INSERT INTO production_runs (
                    run_id, command, origin, recipe_id, recipe_version,
                    status, target_count, completed_count, start_time
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, 0, ?);
            """, (run_id, command, origin, recipe_id, recipe_version, target_count, ts))
            self._conn.commit()
            return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        completed_count: int,
        duration_seconds: Optional[float] = None,
        error_message: Optional[str] = None,
        end_iso: Optional[str] = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            ts = end_iso or utc_now_iso()
            cur.execute("""
                UPDATE production_runs
                SET status = ?,
                    completed_count = ?,
                    duration_seconds = ?,
                    error_message = ?,
                    end_time = ?
                WHERE run_id = ?;
            """, (status, completed_count, duration_seconds, error_message, ts, run_id))
            self._conn.commit()

    # -------------------------------------------------------------------------
    # Cycles
    # -------------------------------------------------------------------------
    def start_cycle(
        self,
        run_id: str,
        cycle_index: int,
        target_slot: int,
        cycle_id: Optional[str] = None,
        start_iso: Optional[str] = None,
    ) -> str:
        cid = cycle_id or str(uuid.uuid4())
        ts = start_iso or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO cycles (
                    cycle_id, run_id, cycle_index, target_slot, status, start_time
                ) VALUES (?, ?, ?, ?, 'running', ?);
            """, (cid, run_id, cycle_index, target_slot, ts))
            self._conn.commit()
            return cid

    def finish_cycle(
        self,
        cycle_id: str,
        status: str,
        duration_seconds: Optional[float] = None,
        end_iso: Optional[str] = None,
    ) -> None:
        ts = end_iso or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                UPDATE cycles
                SET status = ?,
                    duration_seconds = ?,
                    end_time = ?
                WHERE cycle_id = ?;
            """, (status, duration_seconds, ts, cycle_id))
            self._conn.commit()

    # -------------------------------------------------------------------------
    # Process Steps
    # -------------------------------------------------------------------------
    def start_step(
        self,
        cycle_id: str,
        step_name: str,
        step_index: int,
        details: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
        start_iso: Optional[str] = None,
    ) -> str:
        sid = step_id or str(uuid.uuid4())
        ts = start_iso or utc_now_iso()
        details_str = json.dumps(details) if details else None
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO process_steps (
                    step_id, cycle_id, step_name, step_index, status, start_time, details_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?);
            """, (sid, cycle_id, step_name, step_index, ts, details_str))
            self._conn.commit()
            return sid

    def finish_step(
        self,
        step_id: str,
        status: str,
        duration_seconds: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
        end_iso: Optional[str] = None,
    ) -> None:
        ts = end_iso or utc_now_iso()
        details_str = json.dumps(details) if details else None
        with self._lock:
            cur = self._conn.cursor()
            if details_str:
                cur.execute("""
                    UPDATE process_steps
                    SET status = ?,
                        duration_seconds = ?,
                        end_time = ?,
                        details_json = ?
                    WHERE step_id = ?;
                """, (status, duration_seconds, ts, details_str, step_id))
            else:
                cur.execute("""
                    UPDATE process_steps
                    SET status = ?,
                        duration_seconds = ?,
                        end_time = ?
                    WHERE step_id = ?;
                """, (status, duration_seconds, ts, step_id))
            self._conn.commit()

    # -------------------------------------------------------------------------
    # State Transitions & Faults
    # -------------------------------------------------------------------------
    def record_state_transition(
        self,
        from_state: str,
        to_state: str,
        trigger: str,
        run_id: Optional[str] = None,
        timestamp_iso: Optional[str] = None,
    ) -> str:
        tid = str(uuid.uuid4())
        ts = timestamp_iso or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO state_transitions (
                    transition_id, from_state, to_state, trigger, timestamp, run_id
                ) VALUES (?, ?, ?, ?, ?, ?);
            """, (tid, from_state, to_state, trigger, ts, run_id))
            self._conn.commit()
            return tid

    def record_fault(
        self,
        code: str,
        message: str,
        interrupted_step: str = "",
        recovery_requirement: str = "",
        run_id: Optional[str] = None,
        timestamp_iso: Optional[str] = None,
    ) -> str:
        fid = str(uuid.uuid4())
        ts = timestamp_iso or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO fault_events (
                    fault_id, run_id, code, message, interrupted_step,
                    recovery_requirement, timestamp, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL);
            """, (fid, run_id, code, message, interrupted_step, recovery_requirement, ts))
            self._conn.commit()
            return fid

    def resolve_faults(self, resolved_iso: Optional[str] = None) -> int:
        ts = resolved_iso or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                UPDATE fault_events
                SET resolved_at = ?
                WHERE resolved_at IS NULL;
            """, (ts,))
            count = cur.rowcount
            self._conn.commit()
            return count

    def record_operator_action(
        self,
        action_type: str,
        parameters: Optional[Dict[str, Any]] = None,
        timestamp_iso: Optional[str] = None,
    ) -> str:
        aid = str(uuid.uuid4())
        ts = timestamp_iso or utc_now_iso()
        p_str = json.dumps(parameters) if parameters else None
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO operator_actions (
                    action_id, action_type, parameters_json, timestamp
                ) VALUES (?, ?, ?, ?);
            """, (aid, action_type, p_str, ts))
            self._conn.commit()
            return aid

    # -------------------------------------------------------------------------
    # Queries & Reporting
    # -------------------------------------------------------------------------
    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if status:
                cur.execute("""
                    SELECT * FROM production_runs
                    WHERE status = ?
                    ORDER BY start_time DESC
                    LIMIT ? OFFSET ?;
                """, (status, limit, offset))
            else:
                cur.execute("""
                    SELECT * FROM production_runs
                    ORDER BY start_time DESC
                    LIMIT ? OFFSET ?;
                """, (limit, offset))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM production_runs WHERE run_id = ?;", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                return None
            run_dict = dict(run_row)

            # Fetch cycles
            cur.execute("""
                SELECT * FROM cycles WHERE run_id = ? ORDER BY cycle_index ASC;
            """, (run_id,))
            cycles_rows = cur.fetchall()
            cycles_list = []

            for crow in cycles_rows:
                cdict = dict(crow)
                cur.execute("""
                    SELECT * FROM process_steps WHERE cycle_id = ? ORDER BY step_index ASC;
                """, (cdict["cycle_id"],))
                srows = cur.fetchall()
                cdict["steps"] = [dict(s) for s in srows]
                for s in cdict["steps"]:
                    if s.get("details_json"):
                        try:
                            s["details"] = json.loads(s["details_json"])
                        except Exception:
                            pass
                cycles_list.append(cdict)

            run_dict["cycles"] = cycles_list

            # Fetch faults for this run
            cur.execute("SELECT * FROM fault_events WHERE run_id = ? ORDER BY timestamp ASC;", (run_id,))
            frows = cur.fetchall()
            run_dict["faults"] = [dict(f) for f in frows]

            return run_dict

    def list_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns a combined chronological stream of state transitions, faults, and operator actions."""
        with self._lock:
            cur = self._conn.cursor()
            # Union of state transitions, faults, operator actions
            cur.execute("""
                SELECT 'transition' AS event_type, transition_id AS id,
                       from_state || ' -> ' || to_state AS summary,
                       trigger AS detail, timestamp
                FROM state_transitions
                UNION ALL
                SELECT 'fault' AS event_type, fault_id AS id,
                       code || ': ' || message AS summary,
                       COALESCE(interrupted_step, '') AS detail, timestamp
                FROM fault_events
                UNION ALL
                SELECT 'action' AS event_type, action_id AS id,
                       action_type AS summary,
                       COALESCE(parameters_json, '') AS detail, timestamp
                FROM operator_actions
                ORDER BY timestamp DESC
                LIMIT ?;
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def calculate_kpi_summary(self) -> Dict[str, Any]:
        """Calculates production performance KPIs (throughput, cycle stats, step breakdown, downtime)."""
        with self._lock:
            cur = self._conn.cursor()

            # Total & completed runs
            cur.execute("SELECT COUNT(*), SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) FROM production_runs;")
            r_total, r_completed = cur.fetchone()
            r_total = r_total or 0
            r_completed = r_completed or 0

            # Total parts placed
            cur.execute("SELECT SUM(completed_count) FROM production_runs;")
            parts_row = cur.fetchone()
            total_parts = parts_row[0] if parts_row and parts_row[0] is not None else 0

            # Cycle durations (completed only)
            cur.execute("""
                SELECT duration_seconds FROM cycles
                WHERE status = 'completed' AND duration_seconds IS NOT NULL
                ORDER BY duration_seconds ASC;
            """)
            cycle_durs = [row[0] for row in cur.fetchall()]
            avg_cycle = round(sum(cycle_durs) / len(cycle_durs), 2) if cycle_durs else 0.0

            if cycle_durs:
                # 95th percentile
                p95_idx = min(len(cycle_durs) - 1, int(math.ceil(0.95 * len(cycle_durs))) - 1)
                p95_cycle = round(cycle_durs[max(0, p95_idx)], 2)
            else:
                p95_cycle = 0.0

            # Step-time breakdown (average per step name)
            cur.execute("""
                SELECT step_name, AVG(duration_seconds), COUNT(*)
                FROM process_steps
                WHERE status = 'completed' AND duration_seconds IS NOT NULL
                GROUP BY step_name
                ORDER BY AVG(duration_seconds) DESC;
            """)
            step_breakdown = {
                row[0]: {"avg_duration_sec": round(row[1], 3), "count": row[2]}
                for row in cur.fetchall()
            }

            # Fault counts & Pareto
            cur.execute("""
                SELECT code, COUNT(*) FROM fault_events
                GROUP BY code
                ORDER BY COUNT(*) DESC;
            """)
            fault_pareto = {row[0]: row[1] for row in cur.fetchall()}
            total_faults = sum(fault_pareto.values())

            return {
                "total_runs": r_total,
                "completed_runs": r_completed,
                "total_parts_placed": total_parts,
                "completed_cycles_count": len(cycle_durs),
                "avg_cycle_time_sec": avg_cycle,
                "p95_cycle_time_sec": p95_cycle,
                "step_time_breakdown": step_breakdown,
                "total_faults": total_faults,
                "fault_pareto": fault_pareto,
            }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
