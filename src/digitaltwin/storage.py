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

    def get_state_timeline(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Returns chronological state transitions with calculated durations for timeline rendering."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT transition_id, from_state, to_state, trigger, timestamp, run_id
                FROM state_transitions
                ORDER BY timestamp DESC
                LIMIT ?;
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]

        # Reverse so earliest transition is first
        rows.reverse()
        now_dt = datetime.now(timezone.utc)

        for i, item in enumerate(rows):
            try:
                cur_dt = datetime.fromisoformat(item["timestamp"])
                if i + 1 < len(rows):
                    next_dt = datetime.fromisoformat(rows[i + 1]["timestamp"])
                    duration = max(0.0, (next_dt - cur_dt).total_seconds())
                else:
                    duration = max(0.0, (now_dt - cur_dt).total_seconds())
                item["duration_seconds"] = round(duration, 2)
            except Exception:
                item["duration_seconds"] = 0.0

        return rows

    def calculate_kpi_summary(self) -> Dict[str, Any]:
        """Calculates comprehensive production performance KPIs (throughput, cycle stats, step breakdown, downtime)."""
        with self._lock:
            cur = self._conn.cursor()

            # Total & completed runs, plus active production duration
            cur.execute("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END),
                    SUM(COALESCE(duration_seconds, 0.0))
                FROM production_runs;
            """)
            r_row = cur.fetchone()
            r_total = r_row[0] or 0
            r_completed = r_row[1] or 0
            r_failed = r_row[2] or 0
            r_interrupted = r_row[3] or 0
            r_cancelled = r_row[4] or 0
            active_prod_sec = round(float(r_row[5] or 0.0), 2)

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
            min_cycle = round(cycle_durs[0], 2) if cycle_durs else 0.0
            max_cycle = round(cycle_durs[-1], 2) if cycle_durs else 0.0

            if cycle_durs:
                # 95th percentile
                p95_idx = min(len(cycle_durs) - 1, int(math.ceil(0.95 * len(cycle_durs))) - 1)
                p95_cycle = round(cycle_durs[max(0, p95_idx)], 2)
            else:
                p95_cycle = 0.0

            # Throughput calculation
            if active_prod_sec > 0 and total_parts > 0:
                throughput_per_hour = round((total_parts / active_prod_sec) * 3600.0, 1)
            elif avg_cycle > 0:
                throughput_per_hour = round(3600.0 / avg_cycle, 1)
            else:
                throughput_per_hour = 0.0

            throughput_per_min = round(throughput_per_hour / 60.0, 2)

            # Step-time breakdown (average per step name)
            cur.execute("""
                SELECT step_name, AVG(duration_seconds), COUNT(*), MIN(duration_seconds), MAX(duration_seconds)
                FROM process_steps
                WHERE status = 'completed' AND duration_seconds IS NOT NULL
                GROUP BY step_name
                ORDER BY AVG(duration_seconds) DESC;
            """)
            step_rows = cur.fetchall()
            total_step_time = sum(float(r[1] or 0.0) for r in step_rows)

            step_breakdown = {}
            bottleneck_step = None
            max_step_dur = -1.0

            for row in step_rows:
                s_name = row[0]
                s_avg = round(float(row[1] or 0.0), 3)
                s_count = row[2]
                s_min = round(float(row[3] or 0.0), 3)
                s_max = round(float(row[4] or 0.0), 3)
                s_pct = round((s_avg / total_step_time) * 100.0, 1) if total_step_time > 0 else 0.0

                step_breakdown[s_name] = {
                    "avg_duration_sec": s_avg,
                    "min_duration_sec": s_min,
                    "max_duration_sec": s_max,
                    "count": s_count,
                    "pct_of_cycle": s_pct,
                }
                if s_avg > max_step_dur:
                    max_step_dur = s_avg
                    bottleneck_step = s_name

            # Fault counts, downtime & Pareto
            cur.execute("SELECT code, timestamp, resolved_at FROM fault_events;")
            fault_rows = cur.fetchall()
            now_dt = datetime.now(timezone.utc)

            total_downtime_sec = 0.0
            fault_counts: Dict[str, int] = {}
            fault_downtimes: Dict[str, float] = {}

            for fcode, fts, fres in fault_rows:
                fault_counts[fcode] = fault_counts.get(fcode, 0) + 1
                try:
                    start_d = datetime.fromisoformat(fts)
                    end_d = datetime.fromisoformat(fres) if fres else now_dt
                    dt_sec = max(0.0, (end_d - start_d).total_seconds())
                except Exception:
                    dt_sec = 0.0
                total_downtime_sec += dt_sec
                fault_downtimes[fcode] = fault_downtimes.get(fcode, 0.0) + dt_sec

            total_downtime_sec = round(total_downtime_sec, 2)
            total_faults = len(fault_rows)

            # Sort fault pareto by count descending
            sorted_codes = sorted(fault_counts.keys(), key=lambda c: fault_counts[c], reverse=True)
            fault_pareto = {c: fault_counts[c] for c in sorted_codes}

            fault_stats = {}
            for c in sorted_codes:
                cnt = fault_counts[c]
                dtime = round(fault_downtimes.get(c, 0.0), 2)
                pct = round((cnt / total_faults) * 100.0, 1) if total_faults > 0 else 0.0
                fault_stats[c] = {
                    "count": cnt,
                    "downtime_seconds": dtime,
                    "pct_of_faults": pct,
                }

            # Availability %
            if (active_prod_sec + total_downtime_sec) > 0:
                availability_pct = round((active_prod_sec / (active_prod_sec + total_downtime_sec)) * 100.0, 1)
            else:
                availability_pct = 100.0

            return {
                "total_runs": r_total,
                "completed_runs": r_completed,
                "failed_runs": r_failed,
                "interrupted_runs": r_interrupted,
                "cancelled_runs": r_cancelled,
                "total_parts_placed": total_parts,
                "completed_cycles_count": len(cycle_durs),
                "avg_cycle_time_sec": avg_cycle,
                "min_cycle_time_sec": min_cycle,
                "max_cycle_time_sec": max_cycle,
                "p95_cycle_time_sec": p95_cycle,
                "active_production_time_sec": active_prod_sec,
                "throughput_parts_per_hour": throughput_per_hour,
                "throughput_parts_per_minute": throughput_per_min,
                "step_time_breakdown": step_breakdown,
                "bottleneck_step": bottleneck_step,
                "total_faults": total_faults,
                "total_downtime_seconds": total_downtime_sec,
                "operational_availability_pct": availability_pct,
                "fault_pareto": fault_pareto,
                "fault_stats": fault_stats,
            }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
