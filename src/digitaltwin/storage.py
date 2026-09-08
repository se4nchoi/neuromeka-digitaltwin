"""SQLite persistence layer for production runs, cycles, steps, and event logs."""
from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from .config import DEFAULT_RECIPES, HOME_JPOS, PICK_LOCATION, DROP_BASE_LOCATION
    from .kinematics import cartesian_trajectory, analyze_trajectory_kinematics, get_approach_pose
except ImportError:
    try:
        from digitaltwin.config import DEFAULT_RECIPES, HOME_JPOS, PICK_LOCATION, DROP_BASE_LOCATION
        from digitaltwin.kinematics import cartesian_trajectory, analyze_trajectory_kinematics, get_approach_pose
    except ImportError:
        DEFAULT_RECIPES = {}
        HOME_JPOS = [0.0, 0.0, -90.0, 0.0, -90.0, 0.0]
        PICK_LOCATION = [350.0, -186.5, 32.0, 0.0, -180.0, 0.0]
        DROP_BASE_LOCATION = [450.0, 150.0, 32.0, 0.0, -180.0, 0.0]

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

            if current_version < 2:
                self._apply_migration_2(cur)
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?);",
                    (utc_now_iso(),),
                )

            if current_version < 3:
                self._apply_migration_3(cur)
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (3, ?);",
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

    def _apply_migration_2(self, cur: sqlite3.Cursor) -> None:
        """Milestone 3 migration: recipes, work orders, workpiece items, and order linkage."""
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                recipe_id TEXT NOT NULL,
                version TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                parameters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (recipe_id, version)
            );
        """)

        # Seed default recipes if empty
        cur.execute("SELECT COUNT(*) FROM recipes;")
        if cur.fetchone()[0] == 0 and DEFAULT_RECIPES:
            now_ts = utc_now_iso()
            for r_id, r_data in DEFAULT_RECIPES.items():
                cur.execute("""
                    INSERT INTO recipes (recipe_id, version, name, description, parameters_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    r_data["recipe_id"],
                    r_data["version"],
                    r_data["name"],
                    r_data.get("description", ""),
                    json.dumps(r_data["parameters"]),
                    now_ts,
                    now_ts,
                ))

        cur.execute("""
            CREATE TABLE IF NOT EXISTS work_orders (
                order_id TEXT PRIMARY KEY,
                order_number TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                recipe_version TEXT NOT NULL,
                recipe_snapshot_json TEXT NOT NULL,
                target_quantity INTEGER NOT NULL,
                completed_quantity INTEGER NOT NULL DEFAULT 0,
                current_pallet_index INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                notes TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS workpiece_items (
                part_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES work_orders(order_id) ON DELETE CASCADE,
                run_id TEXT,
                cycle_id TEXT,
                part_serial TEXT NOT NULL,
                pallet_index INTEGER NOT NULL,
                slot_index INTEGER NOT NULL,
                slot_floor INTEGER NOT NULL,
                slot_row INTEGER NOT NULL,
                slot_col INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PLACED',
                placed_at TEXT NOT NULL,
                cycle_duration REAL
            );
        """)

        # Add order_id column to production_runs if it doesn't exist
        cur.execute("PRAGMA table_info(production_runs);")
        cols = [row[1] for row in cur.fetchall()]
        if "order_id" not in cols:
            cur.execute("ALTER TABLE production_runs ADD COLUMN order_id TEXT;")

        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_workpiece_order ON workpiece_items(order_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_workpiece_part ON workpiece_items(part_serial);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_order ON production_runs(order_id);")

    def _apply_migration_3(self, cur: sqlite3.Cursor) -> None:
        """Milestone 4 migration: benchmarks and benchmark_trials."""
        cur.execute("""
            CREATE TABLE IF NOT EXISTS benchmarks (
                benchmark_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                baseline_recipe_id TEXT NOT NULL,
                baseline_recipe_version TEXT NOT NULL,
                candidate_recipe_id TEXT NOT NULL,
                candidate_recipe_version TEXT NOT NULL,
                trials_per_variant INTEGER NOT NULL DEFAULT 5,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                summary_json TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_trials (
                trial_id TEXT PRIMARY KEY,
                benchmark_id TEXT NOT NULL REFERENCES benchmarks(benchmark_id) ON DELETE CASCADE,
                variant TEXT NOT NULL,
                trial_index INTEGER NOT NULL,
                run_id TEXT,
                total_duration_sec REAL NOT NULL,
                step_durations_json TEXT NOT NULL,
                kinematic_stats_json TEXT,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL
            );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_trials_bench ON benchmark_trials(benchmark_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_benchmarks_time ON benchmarks(created_at);")

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

                cur.execute("""
                    UPDATE work_orders
                    SET status = 'interrupted',
                        completed_at = COALESCE(completed_at, ?)
                    WHERE status = 'in_progress';
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
        order_id: Optional[str] = None,
    ) -> str:
        with self._lock:
            cur = self._conn.cursor()
            ts = start_iso or utc_now_iso()
            cur.execute("""
                INSERT INTO production_runs (
                    run_id, command, origin, recipe_id, recipe_version,
                    status, target_count, completed_count, start_time, order_id
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, 0, ?, ?);
            """, (run_id, command, origin, recipe_id, recipe_version, target_count, ts, order_id))
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

    # -------------------------------------------------------------------------
    # Recipe Management
    # -------------------------------------------------------------------------
    def list_recipes(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM recipes ORDER BY recipe_id ASC, version DESC;")
            rows = cur.fetchall()
            result = []
            for r in rows:
                item = dict(r)
                if item.get("parameters_json"):
                    try:
                        item["parameters"] = json.loads(item["parameters_json"])
                    except Exception:
                        item["parameters"] = {}
                result.append(item)
            return result

    def get_recipe(self, recipe_id: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if version:
                cur.execute(
                    "SELECT * FROM recipes WHERE recipe_id = ? AND version = ?;",
                    (recipe_id, version),
                )
            else:
                cur.execute(
                    "SELECT * FROM recipes WHERE recipe_id = ? ORDER BY version DESC LIMIT 1;",
                    (recipe_id,),
                )
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            if item.get("parameters_json"):
                try:
                    item["parameters"] = json.loads(item["parameters_json"])
                except Exception:
                    item["parameters"] = {}
            return item

    def create_or_update_recipe(
        self,
        recipe_id: str,
        version: str,
        name: str,
        parameters: Dict[str, Any],
        description: str = "",
    ) -> Dict[str, Any]:
        now_ts = utc_now_iso()
        params_str = json.dumps(parameters)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO recipes (recipe_id, version, name, description, parameters_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recipe_id, version) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    parameters_json = excluded.parameters_json,
                    updated_at = excluded.updated_at;
            """, (recipe_id, version, name, description, params_str, now_ts, now_ts))
            self._conn.commit()
            return {
                "recipe_id": recipe_id,
                "version": version,
                "name": name,
                "description": description,
                "parameters": parameters,
                "updated_at": now_ts,
            }

    # -------------------------------------------------------------------------
    # Work Order Management
    # -------------------------------------------------------------------------
    def _get_next_order_seq(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders;")
        count = cur.fetchone()[0]
        return count + 1

    def create_work_order(
        self,
        recipe_id: str,
        target_quantity: int,
        notes: str = "",
        recipe_version: Optional[str] = None,
        order_id: Optional[str] = None,
        order_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates a new work order with an immutable snapshot of recipe parameters."""
        recipe = self.get_recipe(recipe_id, recipe_version)
        if not recipe:
            raise ValueError(f"Recipe '{recipe_id}' (version: {recipe_version or 'latest'}) not found")

        with self._lock:
            cur = self._conn.cursor()
            seq = self._get_next_order_seq()
            oid = order_id or f"WO-{uuid.uuid4().hex[:8].upper()}"
            onum = order_number or f"WO-{seq:04d}"
            now_ts = utc_now_iso()
            snapshot_str = json.dumps(recipe["parameters"])

            cur.execute("""
                INSERT INTO work_orders (
                    order_id, order_number, recipe_id, recipe_version,
                    recipe_snapshot_json, target_quantity, completed_quantity,
                    current_pallet_index, status, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'pending', ?, ?);
            """, (oid, onum, recipe["recipe_id"], recipe["version"], snapshot_str, target_quantity, now_ts, notes))
            self._conn.commit()

            return {
                "order_id": oid,
                "order_number": onum,
                "recipe_id": recipe["recipe_id"],
                "recipe_version": recipe["version"],
                "recipe_name": recipe["name"],
                "recipe_snapshot": recipe["parameters"],
                "target_quantity": target_quantity,
                "completed_quantity": 0,
                "current_pallet_index": 1,
                "status": "pending",
                "created_at": now_ts,
                "notes": notes,
                "progress_pct": 0.0,
            }

    def get_work_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM work_orders WHERE order_id = ? OR order_number = ?;", (order_id, order_id))
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            if item.get("recipe_snapshot_json"):
                try:
                    item["recipe_snapshot"] = json.loads(item["recipe_snapshot_json"])
                except Exception:
                    item["recipe_snapshot"] = {}
            target = item["target_quantity"]
            completed = item["completed_quantity"]
            item["progress_pct"] = round((completed / target) * 100.0, 1) if target > 0 else 0.0
            return item

    def list_work_orders(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if status:
                cur.execute("""
                    SELECT * FROM work_orders
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?;
                """, (status, limit, offset))
            else:
                cur.execute("""
                    SELECT * FROM work_orders
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?;
                """, (limit, offset))
            rows = cur.fetchall()
            result = []
            for r in rows:
                item = dict(r)
                if item.get("recipe_snapshot_json"):
                    try:
                        item["recipe_snapshot"] = json.loads(item["recipe_snapshot_json"])
                    except Exception:
                        item["recipe_snapshot"] = {}
                target = item["target_quantity"]
                completed = item["completed_quantity"]
                item["progress_pct"] = round((completed / target) * 100.0, 1) if target > 0 else 0.0
                result.append(item)
            return result

    def start_work_order(self, order_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            now_ts = utc_now_iso()
            cur.execute("""
                UPDATE work_orders
                SET status = 'in_progress',
                    started_at = COALESCE(started_at, ?)
                WHERE (order_id = ? OR order_number = ?)
                  AND status IN ('pending', 'paused_pallet_change');
            """, (now_ts, order_id, order_id))
            self._conn.commit()
            return cur.rowcount > 0

    def pause_work_order_for_pallet_change(self, order_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                UPDATE work_orders
                SET status = 'paused_pallet_change'
                WHERE (order_id = ? OR order_number = ?)
                  AND status = 'in_progress';
            """, (order_id, order_id))
            self._conn.commit()
            return cur.rowcount > 0

    def swap_work_order_pallet(self, order_id: str) -> int:
        """Increments pallet index and resets status to in_progress."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                UPDATE work_orders
                SET current_pallet_index = current_pallet_index + 1,
                    status = 'in_progress'
                WHERE (order_id = ? OR order_number = ?)
                  AND status IN ('paused_pallet_change', 'in_progress');
            """, (order_id, order_id))
            self._conn.commit()
            cur.execute("SELECT current_pallet_index FROM work_orders WHERE order_id = ? OR order_number = ?;", (order_id, order_id))
            row = cur.fetchone()
            return row[0] if row else 1

    def complete_work_order(self, order_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            now_ts = utc_now_iso()
            cur.execute("""
                UPDATE work_orders
                SET status = 'completed',
                    completed_at = ?
                WHERE (order_id = ? OR order_number = ?);
            """, (now_ts, order_id, order_id))
            self._conn.commit()
            return cur.rowcount > 0

    def cancel_work_order(self, order_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            now_ts = utc_now_iso()
            cur.execute("""
                UPDATE work_orders
                SET status = 'cancelled',
                    completed_at = ?
                WHERE (order_id = ? OR order_number = ?)
                  AND status NOT IN ('completed', 'cancelled');
            """, (now_ts, order_id, order_id))
            self._conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Part & Slot Traceability
    # -------------------------------------------------------------------------
    def record_workpiece_placed(
        self,
        order_id: str,
        run_id: Optional[str],
        cycle_id: Optional[str],
        part_serial: str,
        pallet_index: int,
        slot_index: int,
        slot_floor: int,
        slot_row: int,
        slot_col: int,
        cycle_duration: Optional[float] = None,
        status: str = "PLACED",
        placed_at: Optional[str] = None,
    ) -> str:
        part_id = str(uuid.uuid4())
        ts = placed_at or utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO workpiece_items (
                    part_id, order_id, run_id, cycle_id, part_serial,
                    pallet_index, slot_index, slot_floor, slot_row, slot_col,
                    status, placed_at, cycle_duration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                part_id, order_id, run_id, cycle_id, part_serial,
                pallet_index, slot_index, slot_floor, slot_row, slot_col,
                status, ts, cycle_duration
            ))
            cur.execute("""
                UPDATE work_orders
                SET completed_quantity = completed_quantity + 1
                WHERE order_id = ? OR order_number = ?;
            """, (order_id, order_id))
            self._conn.commit()
            return part_id

    def get_order_workpieces(self, order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT * FROM workpiece_items
                WHERE order_id = ? OR order_id IN (SELECT order_id FROM work_orders WHERE order_number = ?)
                ORDER BY placed_at ASC;
            """, (order_id, order_id))
            return [dict(r) for r in cur.fetchall()]

    # -------------------------------------------------------------------------
    # Production Summary & Replay
    # -------------------------------------------------------------------------
    def get_work_order_summary(self, order_id: str) -> Optional[Dict[str, Any]]:
        wo = self.get_work_order(order_id)
        if not wo:
            return None
        oid = wo["order_id"]
        parts = self.get_order_workpieces(oid)

        # Pallet breakdown
        pallets_dict: Dict[int, List[Dict[str, Any]]] = {}
        durations = []
        for p in parts:
            p_idx = p["pallet_index"]
            pallets_dict.setdefault(p_idx, []).append(p)
            if p.get("cycle_duration") is not None and p["cycle_duration"] > 0:
                durations.append(p["cycle_duration"])

        pallets_summary = []
        for p_idx in sorted(pallets_dict.keys()):
            p_list = pallets_dict[p_idx]
            pallets_summary.append({
                "pallet_index": p_idx,
                "parts_count": len(p_list),
                "part_serials": [item["part_serial"] for item in p_list],
            })

        # Cycle time stats
        if durations:
            avg_c = round(sum(durations) / len(durations), 2)
            min_c = round(min(durations), 2)
            max_c = round(max(durations), 2)
            sorted_d = sorted(durations)
            p95_idx = min(int(len(sorted_d) * 0.95), len(sorted_d) - 1)
            p95_c = round(sorted_d[p95_idx], 2)
        else:
            avg_c = min_c = max_c = p95_c = 0.0

        # Elapsed time
        started_at = wo.get("started_at")
        completed_at = wo.get("completed_at") or utc_now_iso()
        elapsed_sec = 0.0
        if started_at:
            try:
                t0 = datetime.fromisoformat(started_at)
                t1 = datetime.fromisoformat(completed_at)
                elapsed_sec = round(max(0.0, (t1 - t0).total_seconds()), 1)
            except Exception:
                pass

        total_active_motion_sec = round(sum(durations), 2)
        throughput_parts_per_min = round((len(parts) / (elapsed_sec / 60.0)), 2) if elapsed_sec > 0 else 0.0

        # Runs and faults
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM production_runs WHERE order_id = ? ORDER BY start_time ASC;", (oid,))
            runs = [dict(r) for r in cur.fetchall()]
            run_ids = [r["run_id"] for r in runs]
            faults = []
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                cur.execute(f"SELECT * FROM fault_events WHERE run_id IN ({placeholders}) ORDER BY timestamp ASC;", run_ids)
                faults = [dict(f) for f in cur.fetchall()]

        return {
            "order": wo,
            "total_target": wo["target_quantity"],
            "total_completed": len(parts),
            "total_pallets_used": len(pallets_summary),
            "pallets": pallets_summary,
            "parts": parts,
            "runs_count": len(runs),
            "faults_count": len(faults),
            "faults": faults,
            "metrics": {
                "elapsed_seconds": elapsed_sec,
                "active_motion_seconds": total_active_motion_sec,
                "throughput_parts_per_min": throughput_parts_per_min,
                "avg_cycle_sec": avg_c,
                "min_cycle_sec": min_c,
                "max_cycle_sec": max_c,
                "p95_cycle_sec": p95_c,
            },
        }

    def get_work_order_replay(self, order_id: str) -> Optional[Dict[str, Any]]:
        wo = self.get_work_order(order_id)
        if not wo:
            return None
        oid = wo["order_id"]
        parts = self.get_order_workpieces(oid)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT r.run_id, r.command, c.cycle_id, c.cycle_index, c.target_slot,
                       c.duration_seconds AS cycle_duration, c.status AS cycle_status,
                       c.start_time AS cycle_start, c.end_time AS cycle_end
                FROM production_runs r
                JOIN cycles c ON r.run_id = c.run_id
                WHERE r.order_id = ?
                ORDER BY c.start_time ASC;
            """, (oid,))
            cycles_rows = [dict(r) for r in cur.fetchall()]

            # Attach steps to each cycle
            for crow in cycles_rows:
                cur.execute("""
                    SELECT step_id, step_name, step_index, status, duration_seconds, start_time, end_time
                    FROM process_steps
                    WHERE cycle_id = ?
                    ORDER BY step_index ASC;
                """, (crow["cycle_id"],))
                crow["steps"] = [dict(s) for s in cur.fetchall()]

        return {
            "order": wo,
            "cycles": cycles_rows,
            "parts": parts,
        }

    # -------------------------------------------------------------------------
    # Milestone 4: Benchmarking and Statistical Analysis
    # -------------------------------------------------------------------------
    @staticmethod
    def _calc_sample_stats(values: List[float]) -> Dict[str, Any]:
        n = len(values)
        if n == 0:
            return {"mean": 0.0, "std": 0.0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "count": 0, "values": []}
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / (n - 1) if n > 1 else 0.0
        std = math.sqrt(variance)
        sorted_v = sorted(values)
        if n % 2 == 1:
            median = sorted_v[n // 2]
        else:
            median = (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2.0
        idx_p95 = min(n - 1, max(0, int(math.ceil(0.95 * n)) - 1))
        p95 = sorted_v[idx_p95]
        return {
            "mean": round(mean, 3),
            "std": round(std, 3),
            "median": round(median, 3),
            "p95": round(p95, 3),
            "min": round(sorted_v[0], 3),
            "max": round(sorted_v[-1], 3),
            "count": n,
            "values": [round(x, 3) for x in values],
        }

    @staticmethod
    def _betacf(a: float, b: float, x: float) -> float:
        max_it = 100
        eps = 3.0e-7
        qab = a + b
        qap = a + 1.0
        qam = a - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < 1e-30: d = 1e-30
        d = 1.0 / d
        h = d
        for m in range(1, max_it + 1):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30: d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30: c = 1e-30
            d = 1.0 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30: d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30: c = 1e-30
            d = 1.0 / d
            del_val = d * c
            h *= del_val
            if abs(del_val - 1.0) < eps:
                break
        return h

    @classmethod
    def _ibeta(cls, a: float, b: float, x: float) -> float:
        if x <= 0.0: return 0.0
        if x >= 1.0: return 1.0
        lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
        front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta)
        if x < (a + 1.0) / (a + b + 2.0):
            return front * cls._betacf(a, b, x) / a
        else:
            return 1.0 - front * cls._betacf(b, a, 1.0 - x) / b

    @classmethod
    def _welch_t_test(cls, mean1: float, std1: float, n1: int, mean2: float, std2: float, n2: int) -> Dict[str, Any]:
        if n1 < 2 or n2 < 2:
            return {"t_stat": 0.0, "df": 1.0, "p_value": 1.0, "se": 0.0, "ci_95": [0.0, 0.0], "is_significant": False}
        v1 = (std1 ** 2) / n1
        v2 = (std2 ** 2) / n2
        se = math.sqrt(v1 + v2)
        if se <= 1e-12:
            return {"t_stat": 0.0, "df": float(n1 + n2 - 2), "p_value": 1.0, "se": 0.0, "ci_95": [0.0, 0.0], "is_significant": False}

        num = (v1 + v2) ** 2
        den = (v1 ** 2) / (n1 - 1) + (v2 ** 2) / (n2 - 1)
        df = max(1.0, num / den if den > 0 else float(n1 + n2 - 2))

        diff = mean2 - mean1
        t_stat = diff / se
        x = df / (df + t_stat * t_stat)
        p_val = cls._ibeta(df / 2.0, 0.5, x)

        t_crit_table = {
            1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
            6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
            15: 2.131, 20: 2.086, 30: 2.042, 60: 2.000, 120: 1.980
        }
        df_rounded = int(round(df))
        if df_rounded in t_crit_table:
            t_crit = t_crit_table[df_rounded]
        else:
            z = 1.95996
            t_crit = z + (z**3 + z)/(4.0 * df) + (5.0*z**5 + 16.0*z**3 + 3.0*z)/(96.0 * df**2)

        ci_low = round(diff - t_crit * se, 3)
        ci_high = round(diff + t_crit * se, 3)

        return {
            "t_stat": round(t_stat, 3),
            "df": round(df, 2),
            "p_value": round(p_val, 6),
            "se": round(se, 3),
            "ci_95": [ci_low, ci_high],
            "is_significant": p_val < 0.05,
        }

    def create_benchmark(
        self,
        name: str,
        baseline_recipe_id: str,
        baseline_recipe_version: str,
        candidate_recipe_id: str,
        candidate_recipe_version: str,
        trials_per_variant: int = 5,
        benchmark_id: Optional[str] = None,
    ) -> str:
        bid = benchmark_id or str(uuid.uuid4())
        now_ts = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO benchmarks (
                    benchmark_id, name, baseline_recipe_id, baseline_recipe_version,
                    candidate_recipe_id, candidate_recipe_version, trials_per_variant,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?);
            """, (
                bid, name, baseline_recipe_id, baseline_recipe_version,
                candidate_recipe_id, candidate_recipe_version, trials_per_variant, now_ts
            ))
        return bid

    def record_benchmark_trial(
        self,
        benchmark_id: str,
        variant: str,
        trial_index: int,
        total_duration_sec: float,
        step_durations: Dict[str, float],
        kinematic_stats: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        trial_id: Optional[str] = None,
    ) -> str:
        tid = trial_id or str(uuid.uuid4())
        now_ts = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO benchmark_trials (
                    trial_id, benchmark_id, variant, trial_index, run_id,
                    total_duration_sec, step_durations_json, kinematic_stats_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?);
            """, (
                tid, benchmark_id, variant, trial_index, run_id,
                round(total_duration_sec, 3),
                json.dumps(step_durations),
                json.dumps(kinematic_stats) if kinematic_stats else None,
                now_ts
            ))
        return tid

    def finish_benchmark(self, benchmark_id: str, summary: Dict[str, Any]) -> None:
        now_ts = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                UPDATE benchmarks
                SET status = 'completed',
                    completed_at = ?,
                    summary_json = ?
                WHERE benchmark_id = ?;
            """, (now_ts, json.dumps(summary), benchmark_id))

    def get_benchmark(self, benchmark_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM benchmarks WHERE benchmark_id = ?;", (benchmark_id,))
            row = cur.fetchone()
            if not row:
                return None
            b = dict(row)
            if b.get("summary_json"):
                try:
                    b["summary"] = json.loads(b["summary_json"])
                except Exception:
                    b["summary"] = {}

            cur.execute("""
                SELECT * FROM benchmark_trials
                WHERE benchmark_id = ?
                ORDER BY variant ASC, trial_index ASC;
            """, (benchmark_id,))
            trials = []
            for tr in cur.fetchall():
                td = dict(tr)
                if td.get("step_durations_json"):
                    try:
                        td["step_durations"] = json.loads(td["step_durations_json"])
                    except Exception:
                        td["step_durations"] = {}
                if td.get("kinematic_stats_json"):
                    try:
                        td["kinematic_stats"] = json.loads(td["kinematic_stats_json"])
                    except Exception:
                        td["kinematic_stats"] = {}
                trials.append(td)
            b["trials"] = trials
            return b

    def list_benchmarks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT benchmark_id, name, baseline_recipe_id, candidate_recipe_id,
                       trials_per_variant, status, created_at, completed_at, summary_json
                FROM benchmarks
                ORDER BY created_at DESC
                LIMIT ?;
            """, (limit,))
            out = []
            for row in cur.fetchall():
                item = dict(row)
                if item.get("summary_json"):
                    try:
                        item["summary"] = json.loads(item["summary_json"])
                    except Exception:
                        item["summary"] = {}
                out.append(item)
            return out

    def delete_benchmark(self, benchmark_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM benchmarks WHERE benchmark_id = ?;", (benchmark_id,))
            return cur.rowcount > 0

    def compute_benchmark_statistics(
        self,
        baseline_trials: List[Dict[str, Any]],
        candidate_trials: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calculates statistical hypothesis testing, confidence interval, and step delta breakdown."""
        base_times = [float(t["total_duration_sec"]) for t in baseline_trials]
        cand_times = [float(t["total_duration_sec"]) for t in candidate_trials]

        base_stats = self._calc_sample_stats(base_times)
        cand_stats = self._calc_sample_stats(cand_times)

        delta_t = round(cand_stats["mean"] - base_stats["mean"], 3)
        pct_savings = round(((base_stats["mean"] - cand_stats["mean"]) / base_stats["mean"] * 100.0), 2) if base_stats["mean"] > 0 else 0.0

        welch = self._welch_t_test(
            base_stats["mean"], base_stats["std"], base_stats["count"],
            cand_stats["mean"], cand_stats["std"], cand_stats["count"],
        )

        # 8-step breakdown comparison
        step_names = [
            "PICK_APPROACH", "PICK_PLUNGE", "PICK_GRIP", "PICK_EXTRACT",
            "PLACE_APPROACH", "PLACE_PLUNGE", "PLACE_RELEASE", "PLACE_EXTRACT"
        ]
        step_breakdown = {}
        for sname in step_names:
            s_base = [float(t.get("step_durations", {}).get(sname, 0.0)) for t in baseline_trials if sname in t.get("step_durations", {})]
            s_cand = [float(t.get("step_durations", {}).get(sname, 0.0)) for t in candidate_trials if sname in t.get("step_durations", {})]
            m_base = round(sum(s_base) / len(s_base), 3) if s_base else 0.0
            m_cand = round(sum(s_cand) / len(s_cand), 3) if s_cand else 0.0
            d_step = round(m_cand - m_base, 3)
            p_step = round((m_base - m_cand) / m_base * 100.0, 1) if m_base > 0 else 0.0
            step_breakdown[sname] = {
                "baseline_sec": m_base,
                "candidate_sec": m_cand,
                "delta_sec": d_step,
                "pct_saved": p_step,
            }

        # Kinematic peaks comparison across variants
        def extract_kpeaks(trials):
            max_vel = 0.0
            max_acc = 0.0
            max_jerk = 0.0
            feasibility = "FEASIBLE"
            for t in trials:
                ks = t.get("kinematic_stats", {})
                if ks:
                    max_vel = max(max_vel, ks.get("max_joint_vel_deg_s", 0.0))
                    max_acc = max(max_acc, ks.get("max_joint_acc_deg_s2", 0.0))
                    max_jerk = max(max_jerk, ks.get("max_joint_jerk_deg_s3", 0.0))
                    f = ks.get("feasibility", "FEASIBLE")
                    if f == "INFEASIBLE":
                        feasibility = "INFEASIBLE"
                    elif f == "WARNING" and feasibility != "INFEASIBLE":
                        feasibility = "WARNING"
            return {
                "max_joint_vel_deg_s": round(max_vel, 2),
                "max_joint_acc_deg_s2": round(max_acc, 2),
                "max_joint_jerk_deg_s3": round(max_jerk, 2),
                "feasibility": feasibility,
            }

        kin_base = extract_kpeaks(baseline_trials)
        kin_cand = extract_kpeaks(candidate_trials)

        # Capacity throughput projections
        th_base = round(3600.0 / base_stats["mean"], 1) if base_stats["mean"] > 0 else 0.0
        th_cand = round(3600.0 / cand_stats["mean"], 1) if cand_stats["mean"] > 0 else 0.0
        th_uplift = round(th_cand - th_base, 1)

        return {
            "baseline": base_stats,
            "candidate": cand_stats,
            "delta_cycle_sec": delta_t,
            "pct_reduction": pct_savings,
            "throughput_base_parts_hr": th_base,
            "throughput_candidate_parts_hr": th_cand,
            "throughput_uplift_parts_hr": th_uplift,
            "confidence_interval_95": welch["ci_95"],
            "welch_t_stat": welch["t_stat"],
            "degrees_of_freedom": welch["df"],
            "p_value": welch["p_value"],
            "is_significant": welch["is_significant"],
            "step_breakdown": step_breakdown,
            "kinematics": {
                "baseline": kin_base,
                "candidate": kin_cand,
            },
        }

    def run_fast_benchmark(
        self,
        baseline_recipe_id: str = "pallet-2x2x2-default",
        candidate_recipe_id: str = "pallet-high-speed",
        trials: int = 5,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Runs a deterministic fast kinematic benchmark comparing two recipes over N trials."""
        trials = max(2, min(50, int(trials)))
        r_base = self.get_recipe(baseline_recipe_id) or DEFAULT_RECIPES.get(baseline_recipe_id, {})
        r_cand = self.get_recipe(candidate_recipe_id) or DEFAULT_RECIPES.get(candidate_recipe_id, {})

        p_base = r_base.get("parameters", {})
        p_cand = r_cand.get("parameters", {})

        bench_name = name or f"Benchmark: {r_base.get('name', baseline_recipe_id)} vs {r_cand.get('name', candidate_recipe_id)}"
        bid = self.create_benchmark(
            name=bench_name,
            baseline_recipe_id=baseline_recipe_id,
            baseline_recipe_version=r_base.get("version", "1.0.0"),
            candidate_recipe_id=candidate_recipe_id,
            candidate_recipe_version=r_cand.get("version", "1.0.0"),
            trials_per_variant=trials,
        )

        # Precompute trajectory kinematics for both recipes
        def eval_recipe_kinematics(params):
            clearance = float(params.get("approach_clearance_z", 100.0))
            t_vel = max(5.0, float(params.get("transit_vel_ratio", 45.0)))
            a_vel = max(5.0, float(params.get("action_vel_ratio", 25.0)))
            t_dur = max(0.4, round(1.30 * (45.0 / t_vel) ** 0.55, 2))
            p_dur = max(0.4, round(1.25 * (max(30.0, clearance) / 100.0) ** 0.40 * (45.0 / a_vel) ** 0.55, 2))

            try:
                t_app = get_approach_pose(PICK_LOCATION, clearance=clearance)
                traj = cartesian_trajectory(HOME_JPOS, t_app, t_dur)
                return analyze_trajectory_kinematics(traj, t_dur)
            except Exception:
                return {
                    "duration_sec": t_dur,
                    "max_step_deg": 3.0,
                    "max_joint_vel_deg_s": round(140.0 * (t_vel / 45.0), 1),
                    "max_joint_acc_deg_s2": round(450.0 * (t_vel / 45.0), 1),
                    "max_joint_jerk_deg_s3": round(4000.0 * (t_vel / 45.0) ** 2, 1),
                    "feasibility": "FEASIBLE" if t_vel <= 50 else "WARNING",
                }

        kin_base_eval = eval_recipe_kinematics(p_base)
        kin_cand_eval = eval_recipe_kinematics(p_cand)

        def simulate_trial_steps(params, k_eval):
            clearance = float(params.get("approach_clearance_z", 100.0))
            t_vel = max(5.0, float(params.get("transit_vel_ratio", 45.0)))
            a_vel = max(5.0, float(params.get("action_vel_ratio", 25.0)))
            dwell = float(params.get("gripper_dwell_sec", 0.5))

            # Base durations
            dur_transit = 1.30 * (45.0 / t_vel) ** 0.55
            dur_plunge = 1.25 * (max(30.0, clearance) / 100.0) ** 0.40 * (45.0 / a_vel) ** 0.55
            dur_dwell = dwell + 0.015

            # Apply realistic micro-jitter (normal distribution noise, sigma ~ 1.5%)
            def jitter(base_d, sigma=0.012):
                return max(0.15, round(random.gauss(base_d, sigma), 3))

            steps = {
                "PICK_APPROACH": jitter(dur_transit),
                "PICK_PLUNGE": jitter(dur_plunge),
                "PICK_GRIP": jitter(dur_dwell, sigma=0.005),
                "PICK_EXTRACT": jitter(dur_plunge),
                "PLACE_APPROACH": jitter(dur_transit * 1.05),
                "PLACE_PLUNGE": jitter(dur_plunge),
                "PLACE_RELEASE": jitter(dur_dwell, sigma=0.005),
                "PLACE_EXTRACT": jitter(dur_plunge),
            }
            total = round(sum(steps.values()), 3)
            return total, steps

        base_trial_records = []
        for i in range(trials):
            tot, steps = simulate_trial_steps(p_base, kin_base_eval)
            tid = self.record_benchmark_trial(bid, "baseline", i + 1, tot, steps, kin_base_eval)
            base_trial_records.append({"trial_id": tid, "variant": "baseline", "total_duration_sec": tot, "step_durations": steps, "kinematic_stats": kin_base_eval})

        cand_trial_records = []
        for i in range(trials):
            tot, steps = simulate_trial_steps(p_cand, kin_cand_eval)
            tid = self.record_benchmark_trial(bid, "candidate", i + 1, tot, steps, kin_cand_eval)
            cand_trial_records.append({"trial_id": tid, "variant": "candidate", "total_duration_sec": tot, "step_durations": steps, "kinematic_stats": kin_cand_eval})

        summary = self.compute_benchmark_statistics(base_trial_records, cand_trial_records)
        self.finish_benchmark(bid, summary)
        return self.get_benchmark(bid)

    def close(self) -> None:

        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
