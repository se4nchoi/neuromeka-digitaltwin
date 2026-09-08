"""Tests for Milestone 1: Production Event Data persistence and reporting."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from digitaltwin.commands import CommandService
from digitaltwin.palletizer_engine import PalletizerEngine
from digitaltwin.storage import ProductionStorage


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_production.db")
        self.storage = ProductionStorage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_schema_and_migrations_initialization(self):
        """Verify that all required tables and schema version exist."""
        cur = self.storage._conn.cursor()
        cur.execute("SELECT version FROM schema_migrations;")
        versions = [row[0] for row in cur.fetchall()]
        self.assertIn(1, versions)

        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        expected = {
            "schema_migrations",
            "production_runs",
            "cycles",
            "process_steps",
            "state_transitions",
            "fault_events",
            "operator_actions",
        }
        self.assertTrue(expected.issubset(tables))

    def test_reconcile_interrupted_runs_on_restart(self):
        """Verify that unclosed runs and cycles are marked as interrupted upon restart."""
        # Create an unclosed run
        run_id = self.storage.start_run(
            run_id="unclosed-run-123",
            command="pb1",
            origin="SIMULATION",
            recipe_id="pallet-2x2x2-default",
            recipe_version="1.0.0",
            target_count=8,
        )
        cid = self.storage.start_cycle(run_id, 0, 0)
        sid = self.storage.start_step(cid, "PICK_APPROACH", 1)

        # Simulate engine restart by creating a new storage instance on the same file
        new_storage = ProductionStorage(self.db_path)
        count = new_storage.reconcile_interrupted_runs()
        self.assertEqual(count, 1)

        run = new_storage.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "interrupted")
        self.assertIsNotNone(run["end_time"])
        self.assertEqual(run["cycles"][0]["status"], "interrupted")
        self.assertEqual(run["cycles"][0]["steps"][0]["status"], "interrupted")
        new_storage.close()

    def test_full_palletizing_persists_runs_cycles_and_steps(self):
        """Simulate a complete 8-slot palletizing routine and assert detailed durable storage."""
        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        engine.switch_to_simulation()

        # Mock fast motion and zero dwell for instantaneous test execution
        with patch.object(engine, "_execute_cartesian_move", return_value=True), \
             patch.object(engine, "_execute_move_home", return_value=True), \
             patch.object(engine, "_dwell", return_value=True), \
             patch.object(engine, "set_gripper", return_value=True):

            res = engine.trigger_pb1_palletize()
            self.assertIsInstance(res, dict)
            cmd_id = res["command_id"]

            if engine.active_thread:
                engine.active_thread.join(timeout=3)

            run = self.storage.get_run(cmd_id)
            self.assertIsNotNone(run)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["command"], "pb1")
            self.assertEqual(run["origin"], "SIMULATION")
            self.assertEqual(run["completed_count"], 8)
            self.assertGreaterEqual(run["duration_seconds"], 0)

            # Check cycles
            cycles = run["cycles"]
            self.assertEqual(len(cycles), 8)
            for idx, cycle in enumerate(cycles):
                self.assertEqual(cycle["cycle_index"], idx)
                self.assertEqual(cycle["target_slot"], idx)
                self.assertEqual(cycle["status"], "completed")
                self.assertGreaterEqual(cycle["duration_seconds"], 0)

                # Check process steps (8 steps per cycle)
                steps = cycle["steps"]
                self.assertEqual(len(steps), 8)
                step_names = [s["step_name"] for s in steps]
                expected_steps = [
                    "PICK_APPROACH", "PICK_PLUNGE", "PICK_GRIP", "PICK_EXTRACT",
                    "PLACE_APPROACH", "PLACE_PLUNGE", "PLACE_RELEASE", "PLACE_EXTRACT"
                ]
                self.assertEqual(step_names, expected_steps)
                for s in steps:
                    self.assertEqual(s["status"], "completed")
                    self.assertGreaterEqual(s["duration_seconds"], 0)

            # Check state transitions
            events = self.storage.list_events(limit=50)
            transitions = [e for e in events if e["event_type"] == "transition"]
            self.assertTrue(any("IDLE -> RUNNING" in t["summary"] for t in transitions))
            self.assertTrue(any("RUNNING -> IDLE" in t["summary"] for t in transitions))

        engine.close()

    def test_fault_and_recovery_persistence(self):
        """Verify fault injection records fault events with context, and recovery clears them."""
        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        engine.switch_to_simulation()

        # Inject feeder empty fault
        engine.inject_fault("FEEDER_EMPTY")
        self.assertEqual(engine.workcell_state, "FAULTED")

        # Verify fault record in DB
        events = self.storage.list_events(limit=10)
        fault_events = [e for e in events if e["event_type"] == "fault"]
        self.assertGreaterEqual(len(fault_events), 1)
        self.assertIn("FEEDER_EMPTY", fault_events[0]["summary"])

        # Recover cell (feeder restored)
        engine.mag_sensor = True
        success = engine.recover_robot()
        self.assertTrue(success)
        self.assertEqual(engine.workcell_state, "IDLE")

        # Verify resolution
        cur = self.storage._conn.cursor()
        cur.execute("SELECT resolved_at FROM fault_events WHERE code = 'FEEDER_EMPTY';")
        rows = cur.fetchall()
        self.assertTrue(all(r[0] is not None for r in rows))

        engine.close()

    def test_kpi_summary_calculation(self):
        """Verify KPI aggregation calculations (throughput, p95, step breakdown, fault Pareto)."""
        rid = self.storage.start_run("run-kpi-1", "pb1", "SIMULATION", "recipe-1", "1.0.0", 2)
        cid1 = self.storage.start_cycle(rid, 0, 0)
        sid1 = self.storage.start_step(cid1, "PICK_APPROACH", 1)
        self.storage.finish_step(sid1, "completed", 1.2)
        self.storage.finish_cycle(cid1, "completed", 5.0)

        cid2 = self.storage.start_cycle(rid, 1, 1)
        sid2 = self.storage.start_step(cid2, "PICK_APPROACH", 1)
        self.storage.finish_step(sid2, "completed", 1.4)
        self.storage.finish_cycle(cid2, "completed", 6.0)

        self.storage.finish_run(rid, "completed", 2, 12.0)
        self.storage.record_fault("SENSOR_TIMEOUT", "Sensor timed out")

        kpis = self.storage.calculate_kpi_summary()
        self.assertEqual(kpis["total_runs"], 1)
        self.assertEqual(kpis["completed_runs"], 1)
        self.assertEqual(kpis["total_parts_placed"], 2)
        self.assertEqual(kpis["completed_cycles_count"], 2)
        self.assertAlmostEqual(kpis["avg_cycle_time_sec"], 5.5, places=1)
        self.assertGreater(kpis["p95_cycle_time_sec"], 0)
        self.assertIn("PICK_APPROACH", kpis["step_time_breakdown"])
        self.assertEqual(kpis["fault_pareto"].get("SENSOR_TIMEOUT"), 1)

    def test_api_endpoints_for_production(self):
        """Verify REST API production endpoints directly via async route handlers."""
        from digitaltwin import server
        original_storage = server.engine.storage
        server.engine.storage = self.storage

        try:
            # Seed a run
            rid = self.storage.start_run("api-test-run", "pb1", "SIMULATION", "recipe-1", "1.0.0", 8)
            cid = self.storage.start_cycle(rid, 0, 0)
            sid = self.storage.start_step(cid, "PICK_APPROACH", 1)
            self.storage.finish_step(sid, "completed", 0.95)
            self.storage.finish_cycle(cid, "completed", 4.8)
            self.storage.finish_run(rid, "completed", 1, 5.5)

            # 1. GET /api/production/runs
            runs = asyncio.run(server.list_production_runs())
            self.assertIsInstance(runs, list)
            self.assertTrue(any(r["run_id"] == "api-test-run" for r in runs))

            # 2. GET /api/production/runs/{run_id}
            run_data = asyncio.run(server.get_production_run(rid))
            self.assertEqual(run_data["run_id"], "api-test-run")
            self.assertEqual(len(run_data["cycles"]), 1)
            self.assertEqual(len(run_data["cycles"][0]["steps"]), 1)

            # 3. GET /api/production/events
            events = asyncio.run(server.list_production_events())
            self.assertIsInstance(events, list)

            # 4. GET /api/production/kpi/summary
            kpi_data = asyncio.run(server.get_kpi_summary())
            self.assertEqual(kpi_data["completed_runs"], 1)

            # 5. GET /api/commands/{command_id} fallback to storage
            cmd_data = asyncio.run(server.get_command(rid))
            self.assertEqual(cmd_data["command_id"], "api-test-run")
            self.assertEqual(cmd_data["status"], "completed")

        finally:
            server.engine.storage = original_storage


if __name__ == "__main__":
    unittest.main()
