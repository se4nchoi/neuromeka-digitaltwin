"""Tests for Milestone 4: Cycle time optimization, kinematic envelope, and variant benchmarking."""
import asyncio
import json
import os
import tempfile
import unittest

from digitaltwin.config import DEFAULT_RECIPES, HOME_JPOS, PICK_LOCATION
from digitaltwin.kinematics import (
    analyze_trajectory_kinematics,
    cartesian_trajectory,
    compute_manipulability,
    eval_feasibility,
    get_approach_pose,
)
from digitaltwin.palletizer_engine import PalletizerEngine
from digitaltwin.server import (
    compare_recipes_endpoint,
    delete_benchmark_endpoint,
    get_benchmark_endpoint,
    list_benchmarks_endpoint,
    run_benchmark_endpoint,
    RunBenchmarkPayload,
)
from digitaltwin.storage import ProductionStorage


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_benchmarks.db")
        self.storage = ProductionStorage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_schema_migration_3_tables_and_indexes(self):
        """Verify that schema migration 3 creates benchmarks and benchmark_trials tables."""
        cur = self.storage._conn.cursor()
        cur.execute("SELECT version FROM schema_migrations;")
        versions = [row[0] for row in cur.fetchall()]
        self.assertIn(3, versions)

        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        self.assertIn("benchmarks", tables)
        self.assertIn("benchmark_trials", tables)

        # Verify columns of benchmarks
        cur.execute("PRAGMA table_info(benchmarks);")
        col_names = {row[1] for row in cur.fetchall()}
        expected_cols = {
            "benchmark_id", "name", "baseline_recipe_id", "candidate_recipe_id",
            "trials_per_variant", "status", "created_at", "summary_json"
        }
        self.assertTrue(expected_cols.issubset(col_names))

    def test_statistical_engine_calculations(self):
        """Verify statistical sample metrics, Welch's t-test, and confidence intervals."""
        # Baseline trials around 8.2s with small variance
        base_trials = [
            {"total_duration_sec": 8.15, "step_durations": {"PICK_APPROACH": 1.30, "PICK_PLUNGE": 1.55}},
            {"total_duration_sec": 8.25, "step_durations": {"PICK_APPROACH": 1.32, "PICK_PLUNGE": 1.57}},
            {"total_duration_sec": 8.20, "step_durations": {"PICK_APPROACH": 1.31, "PICK_PLUNGE": 1.56}},
            {"total_duration_sec": 8.18, "step_durations": {"PICK_APPROACH": 1.29, "PICK_PLUNGE": 1.54}},
            {"total_duration_sec": 8.22, "step_durations": {"PICK_APPROACH": 1.33, "PICK_PLUNGE": 1.58}},
        ]
        # Candidate trials around 5.1s with small variance
        cand_trials = [
            {"total_duration_sec": 5.05, "step_durations": {"PICK_APPROACH": 0.88, "PICK_PLUNGE": 1.10}},
            {"total_duration_sec": 5.12, "step_durations": {"PICK_APPROACH": 0.90, "PICK_PLUNGE": 1.12}},
            {"total_duration_sec": 5.08, "step_durations": {"PICK_APPROACH": 0.89, "PICK_PLUNGE": 1.11}},
            {"total_duration_sec": 5.15, "step_durations": {"PICK_APPROACH": 0.91, "PICK_PLUNGE": 1.13}},
            {"total_duration_sec": 5.10, "step_durations": {"PICK_APPROACH": 0.87, "PICK_PLUNGE": 1.09}},
        ]

        stats = self.storage.compute_benchmark_statistics(base_trials, cand_trials)

        self.assertAlmostEqual(stats["baseline"]["mean"], 8.20, delta=0.05)
        self.assertAlmostEqual(stats["candidate"]["mean"], 5.10, delta=0.05)
        self.assertLess(stats["delta_cycle_sec"], -3.0)
        self.assertGreater(stats["pct_reduction"], 35.0)

        # Welch's t-test and p-value
        self.assertLess(stats["p_value"], 0.001)
        self.assertTrue(stats["is_significant"])

        # 95% Confidence Interval for delta T
        ci = stats["confidence_interval_95"]
        self.assertEqual(len(ci), 2)
        self.assertLess(ci[0], ci[1])
        self.assertLess(ci[1], -2.9)

        # Step breakdown
        self.assertIn("PICK_APPROACH", stats["step_breakdown"])
        self.assertIn("PICK_PLUNGE", stats["step_breakdown"])
        app_delta = stats["step_breakdown"]["PICK_APPROACH"]["delta_sec"]
        self.assertLess(app_delta, -0.3)

    def test_kinematic_jerk_and_envelope_analysis(self):
        """Verify peak joint velocity, acceleration, jerk, and continuity margins."""
        target = get_approach_pose(PICK_LOCATION, clearance=100.0)
        traj = cartesian_trajectory(HOME_JPOS, target, 1.3)
        stats = analyze_trajectory_kinematics(traj, 1.3)

        self.assertGreater(stats["max_step_deg"], 0.0)
        self.assertLessEqual(stats["max_step_deg"], 15.0)
        self.assertGreater(stats["max_joint_vel_deg_s"], 0.0)
        self.assertGreater(stats["max_joint_acc_deg_s2"], 0.0)
        self.assertGreater(stats["max_joint_jerk_deg_s3"], 0.0)
        self.assertIn(stats["feasibility"], {"FEASIBLE", "WARNING"})

        # Manipulability index
        mu = compute_manipulability(HOME_JPOS)
        self.assertGreater(mu, 0.01)

        # Infeasible case check
        infeasible_stats = {"max_step_deg": 18.5, "max_joint_vel_deg_s": 50.0, "max_joint_acc_deg_s2": 100.0, "manipulability_min": 0.05}
        self.assertEqual(eval_feasibility(infeasible_stats), "INFEASIBLE")

    def test_fast_benchmark_execution_and_storage(self):
        """Execute a 5-trial fast benchmark comparing default vs high-speed recipes."""
        bench = self.storage.run_fast_benchmark(
            baseline_recipe_id="pallet-2x2x2-default",
            candidate_recipe_id="pallet-high-speed",
            trials=5,
            name="Unit Test Benchmark",
        )

        self.assertIsNotNone(bench)
        self.assertEqual(bench["status"], "completed")
        self.assertEqual(bench["name"], "Unit Test Benchmark")

        summary = bench["summary"]
        self.assertLess(summary["delta_cycle_sec"], 0)
        self.assertGreater(summary["pct_reduction"], 25.0)
        self.assertLess(summary["p_value"], 0.05)
        self.assertTrue(summary["is_significant"])

        # Check all 8 steps in step breakdown
        expected_steps = [
            "PICK_APPROACH", "PICK_PLUNGE", "PICK_GRIP", "PICK_EXTRACT",
            "PLACE_APPROACH", "PLACE_PLUNGE", "PLACE_RELEASE", "PLACE_EXTRACT"
        ]
        for step in expected_steps:
            self.assertIn(step, summary["step_breakdown"])
            self.assertIn("delta_sec", summary["step_breakdown"][step])

        # Verify trials in database
        self.assertEqual(len(bench["trials"]), 10)
        baseline_trials = [t for t in bench["trials"] if t["variant"] == "baseline"]
        candidate_trials = [t for t in bench["trials"] if t["variant"] == "candidate"]
        self.assertEqual(len(baseline_trials), 5)
        self.assertEqual(len(candidate_trials), 5)

        # Verify listing
        benchmarks_list = self.storage.list_benchmarks()
        self.assertGreaterEqual(len(benchmarks_list), 1)
        self.assertEqual(benchmarks_list[0]["benchmark_id"], bench["benchmark_id"])

        # Verify deletion
        del_result = self.storage.delete_benchmark(bench["benchmark_id"])
        self.assertTrue(del_result)
        self.assertIsNone(self.storage.get_benchmark(bench["benchmark_id"]))

    def test_palletizer_engine_benchmark_experiment(self):
        """Verify PalletizerWorkcell.run_benchmark_experiment integration."""
        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        try:
            res = engine.run_benchmark_experiment(
                name="Engine Benchmark Test",
                baseline_recipe_id="pallet-2x2x2-default",
                candidate_recipe_id="pallet-high-speed",
                trials=3,
                fast_mode=True,
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["name"], "Engine Benchmark Test")
            self.assertEqual(res["status"], "completed")
            self.assertEqual(len(res["trials"]), 6)
        finally:
            engine.close()

    def test_benchmark_api_endpoints(self):
        """Verify REST API endpoint functions for benchmarks."""
        async def run_tests():
            # 1. POST /api/benchmarks/run
            payload = RunBenchmarkPayload(
                name="API Test Benchmark",
                baseline_recipe_id="pallet-2x2x2-default",
                candidate_recipe_id="pallet-high-speed",
                trials=3,
                fast_mode=True,
            )
            res = await run_benchmark_endpoint(payload)
            self.assertIsNotNone(res)
            bid = res["benchmark_id"]

            # 2. GET /api/benchmarks
            blist = await list_benchmarks_endpoint(limit=10)
            self.assertGreaterEqual(len(blist), 1)

            # 3. GET /api/benchmarks/{id}
            bdetail = await get_benchmark_endpoint(bid)
            self.assertEqual(bdetail["benchmark_id"], bid)
            self.assertEqual(len(bdetail["trials"]), 6)

            # 4. GET /api/benchmarks/compare
            cmp_res = await compare_recipes_endpoint(
                base="pallet-2x2x2-default",
                cand="pallet-high-speed",
                trials=2,
            )
            self.assertIsNotNone(cmp_res)
            self.assertIn("summary", cmp_res)

            # 5. DELETE /api/benchmarks/{id}
            del_res = await delete_benchmark_endpoint(bid)
            self.assertTrue(del_res["success"])

        asyncio.run(run_tests())


if __name__ == "__main__":
    unittest.main()
