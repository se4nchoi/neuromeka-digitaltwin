"""Automated tests for Milestone 3: Work-Order and Recipe Workflow."""
import os
import tempfile
import unittest
from unittest.mock import patch

from digitaltwin.palletizer_engine import PalletizerEngine
from digitaltwin.storage import ProductionStorage


class WorkOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_orders.db")
        self.storage = ProductionStorage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_recipe_immutability(self):
        """Verify that modifying a recipe in the database NEVER changes an active order's snapshot."""
        # 1. Check default recipes seeded
        recipes = self.storage.list_recipes()
        self.assertGreaterEqual(len(recipes), 3)

        # 2. Create custom recipe
        original_params = {
            "grid_x": 2,
            "grid_y": 2,
            "num_floors": 2,
            "offset_x": 75.0,
            "offset_y": 75.0,
            "layer_height": 25.0,
            "approach_clearance_z": 90.0,
            "transit_vel_ratio": 50,
            "transit_acc_ratio": 50,
            "action_vel_ratio": 30,
            "action_acc_ratio": 30,
            "gripper_dwell_sec": 0.4,
            "slots_per_floor": 4,
            "total_slots": 8,
        }
        self.storage.create_or_update_recipe(
            recipe_id="custom-pallet-v1",
            version="1.0.0",
            name="Custom Precision Pallet",
            parameters=original_params,
            description="Custom precision layout",
        )

        # 3. Create work order referencing custom-pallet-v1
        order = self.storage.create_work_order(
            recipe_id="custom-pallet-v1",
            target_quantity=8,
            notes="Order A",
            recipe_version="1.0.0",
        )
        order_id = order["order_id"]
        self.assertEqual(order["recipe_snapshot"]["offset_x"], 75.0)

        # 4. Modify custom recipe in DB (change offset_x to 999.0 and vel to 10)
        modified_params = dict(original_params, offset_x=999.0, transit_vel_ratio=10)
        self.storage.create_or_update_recipe(
            recipe_id="custom-pallet-v1",
            version="1.0.0",
            name="Custom Modified",
            parameters=modified_params,
        )

        # 5. Assert work order still has original frozen snapshot
        fetched_order = self.storage.get_work_order(order_id)
        self.assertIsNotNone(fetched_order)
        self.assertEqual(fetched_order["recipe_snapshot"]["offset_x"], 75.0)
        self.assertEqual(fetched_order["recipe_snapshot"]["transit_vel_ratio"], 50)
        self.assertNotEqual(fetched_order["recipe_snapshot"]["offset_x"], 999.0)

    def test_single_pallet_work_order_execution_and_traceability(self):
        """Execute a full 8-part work order and verify part-by-part slot traceability."""
        order = self.storage.create_work_order(
            recipe_id="pallet-2x2x2-default",
            target_quantity=8,
            notes="Single Pallet Test",
        )
        order_id = order["order_id"]
        self.assertEqual(order["status"], "pending")

        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        engine.switch_to_simulation()

        with patch.object(engine, "_execute_cartesian_move", return_value=True), \
             patch.object(engine, "_execute_move_home", return_value=True), \
             patch.object(engine, "_dwell", return_value=True), \
             patch.object(engine, "set_gripper", return_value=True):

            res = engine.start_work_order(order_id)
            self.assertTrue(res["success"])

            if engine.active_thread:
                engine.active_thread.join(timeout=5)

            # Assert order reached completed status
            wo = self.storage.get_work_order(order_id)
            self.assertEqual(wo["status"], "completed")
            self.assertEqual(wo["completed_quantity"], 8)
            self.assertEqual(wo["progress_pct"], 100.0)

            # Assert all 8 parts are recorded with slot coordinates
            parts = self.storage.get_order_workpieces(order_id)
            self.assertEqual(len(parts), 8)
            for idx, part in enumerate(parts):
                self.assertEqual(part["slot_index"], idx)
                self.assertEqual(part["pallet_index"], 1)
                self.assertEqual(part["slot_floor"], idx // 4)
                self.assertEqual(part["slot_row"], (idx % 4) // 2)
                self.assertEqual(part["slot_col"], (idx % 4) % 2)
                self.assertTrue(part["part_serial"].startswith(f"{wo['order_number']}-P1-S"))
                self.assertEqual(part["status"], "PLACED")

    def test_multi_pallet_change_workflow(self):
        """Execute a 12-part order on an 8-slot pallet: pauses at 8, swaps pallet, places remaining 4."""
        order = self.storage.create_work_order(
            recipe_id="pallet-2x2x2-default",
            target_quantity=12,
            notes="Multi Pallet 12-Billet Batch",
        )
        order_id = order["order_id"]

        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        engine.switch_to_simulation()

        with patch.object(engine, "_execute_cartesian_move", return_value=True), \
             patch.object(engine, "_execute_move_home", return_value=True), \
             patch.object(engine, "_dwell", return_value=True), \
             patch.object(engine, "set_gripper", return_value=True):

            # Start order (Pallet 1)
            res = engine.start_work_order(order_id)
            self.assertTrue(res["success"])

            if engine.active_thread:
                engine.active_thread.join(timeout=5)

            # 1. Assert order paused for pallet change after 8 parts
            self.assertTrue(engine.pallet_change_required)
            self.assertEqual(engine.pallet_count, 8)
            self.assertEqual(engine.order_completed_quantity, 8)

            wo_paused = self.storage.get_work_order(order_id)
            self.assertEqual(wo_paused["status"], "paused_pallet_change")
            self.assertEqual(wo_paused["completed_quantity"], 8)

            # 2. Trigger Pallet Swap
            swap_res = engine.confirm_pallet_swap()
            self.assertTrue(swap_res["success"])
            self.assertEqual(engine.current_pallet_index, 2)
            self.assertEqual(engine.pallet_count, 0)
            self.assertFalse(engine.pallet_change_required)

            if engine.active_thread:
                engine.active_thread.join(timeout=5)

            # 3. Assert order finished all 12 parts
            wo_final = self.storage.get_work_order(order_id)
            self.assertEqual(wo_final["status"], "completed")
            self.assertEqual(wo_final["completed_quantity"], 12)
            self.assertEqual(wo_final["progress_pct"], 100.0)

            # 4. Assert part breakdown across pallets
            parts = self.storage.get_order_workpieces(order_id)
            self.assertEqual(len(parts), 12)
            pallet1_parts = [p for p in parts if p["pallet_index"] == 1]
            pallet2_parts = [p for p in parts if p["pallet_index"] == 2]
            self.assertEqual(len(pallet1_parts), 8)
            self.assertEqual(len(pallet2_parts), 4)

            # 5. Verify Production Summary Report
            summary = self.storage.get_work_order_summary(order_id)
            self.assertIsNotNone(summary)
            self.assertEqual(summary["total_target"], 12)
            self.assertEqual(summary["total_completed"], 12)
            self.assertEqual(summary["total_pallets_used"], 2)
            self.assertEqual(len(summary["pallets"]), 2)
            self.assertEqual(summary["pallets"][0]["parts_count"], 8)
            self.assertEqual(summary["pallets"][1]["parts_count"], 4)

            # 6. Verify Replay Data
            replay = self.storage.get_work_order_replay(order_id)
            self.assertIsNotNone(replay)
            self.assertEqual(len(replay["cycles"]), 12)
            self.assertEqual(len(replay["parts"]), 12)

    def test_cancel_work_order(self):
        """Verify that cancelling an order updates its status and stops execution."""
        order = self.storage.create_work_order(
            recipe_id="pallet-2x2x2-default",
            target_quantity=8,
        )
        order_id = order["order_id"]

        engine = PalletizerEngine(startup_mode="SIMULATION", storage=self.storage)
        engine.switch_to_simulation()
        engine.active_order_id = order_id

        res = engine.cancel_active_work_order()
        self.assertTrue(res["success"])

        wo = self.storage.get_work_order(order_id)
        self.assertEqual(wo["status"], "cancelled")
        self.assertIsNotNone(wo["completed_at"])

    def test_recipe_and_work_order_api_routes(self):
        """Verify REST API routes for recipes and work orders."""
        import asyncio
        import digitaltwin.server as server
        from digitaltwin.server import CreateRecipePayload, CreateWorkOrderPayload

        original_storage = server.engine.storage
        server.engine.storage = self.storage
        try:
            # 1. GET /api/recipes
            recipes = asyncio.run(server.list_recipes_endpoint())
            self.assertGreaterEqual(len(recipes), 3)

            # 2. POST /api/recipes
            new_recipe = asyncio.run(server.create_recipe_endpoint(CreateRecipePayload(
                recipe_id="test-recipe-api",
                version="1.0.0",
                name="API Test Recipe",
                parameters={"grid_x": 2, "grid_y": 2, "num_floors": 1, "total_slots": 4},
                description="Testing via API",
            )))
            self.assertEqual(new_recipe["recipe_id"], "test-recipe-api")

            # 3. GET /api/recipes/{recipe_id}
            fetched_recipe = asyncio.run(server.get_recipe_endpoint("test-recipe-api"))
            self.assertEqual(fetched_recipe["name"], "API Test Recipe")

            # 4. POST /api/work-orders
            wo = asyncio.run(server.create_work_order_endpoint(CreateWorkOrderPayload(
                recipe_id="test-recipe-api",
                target_quantity=4,
                notes="API Order 1",
            )))
            self.assertEqual(wo["recipe_id"], "test-recipe-api")
            self.assertEqual(wo["target_quantity"], 4)
            wo_id = wo["order_id"]

            # 5. GET /api/work-orders
            orders = asyncio.run(server.list_work_orders_endpoint())
            self.assertTrue(any(o["order_id"] == wo_id for o in orders))

            # 6. GET /api/work-orders/{order_id}
            order_data = asyncio.run(server.get_work_order_endpoint(wo_id))
            self.assertEqual(order_data["order_id"], wo_id)

            # 7. GET /api/work-orders/{order_id}/summary & replay
            summary = asyncio.run(server.get_work_order_summary_endpoint(wo_id))
            self.assertEqual(summary["total_target"], 4)
            replay = asyncio.run(server.get_work_order_replay_endpoint(wo_id))
            self.assertIn("cycles", replay)

            # 8. POST /api/pallet/swap
            swap_res = asyncio.run(server.swap_current_pallet_endpoint())
            self.assertIn("success", swap_res)
        finally:
            server.engine.storage = original_storage


if __name__ == "__main__":
    unittest.main()

