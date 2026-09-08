import time
import unittest
from unittest.mock import patch

from src.digitaltwin.palletizer_engine import PalletizerEngine
from src.digitaltwin.commands import CommandService


class TestPendantJog(unittest.TestCase):
    def setUp(self):
        self.engine = PalletizerEngine(startup_mode="SIMULATION")
        self.commands = CommandService(self.engine)

    def tearDown(self):
        self.engine.close()

    def test_single_joint_jog_smooth(self):
        initial_q = list(self.engine.q)
        res = self.commands.execute("jog_joint", joint_idx=0, step_deg=5.0)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(self.engine.active_thread)
        self.engine.active_thread.join(timeout=2.0)

        self.assertAlmostEqual(self.engine.q[0], initial_q[0] + 5.0, places=1)
        self.assertFalse(self.engine.is_jogging)
        self.assertFalse(self.engine.is_moving)
        self.assertFalse(self.engine.abort_requested)
        self.assertIsNone(self.engine.fault)
        self.assertEqual(self.engine.op_state, 5)

    def test_joint_jog_respects_limits(self):
        # Move J1 near its limit of 175.0
        self.engine.q[0] = 173.0
        res = self.commands.execute("jog_joint", joint_idx=0, step_deg=10.0)
        self.assertEqual(res["status"], "accepted")
        if self.engine.active_thread:
            self.engine.active_thread.join(timeout=2.0)

        self.assertAlmostEqual(self.engine.q[0], 175.0, places=1)
        self.assertIsNone(self.engine.fault)

        # Attempting to jog past 175.0 should be rejected cleanly without faulting
        res2 = self.commands.execute("jog_joint", joint_idx=0, step_deg=5.0)
        self.assertEqual(res2["status"], "rejected")
        self.assertIn("limit reached", res2.get("message", ""))
        self.assertIsNone(self.engine.fault)
        self.assertFalse(self.engine.abort_requested)

    def test_cartesian_jog_smooth_and_idle_return(self):
        initial_z = self.engine.p[2]
        res = self.commands.execute("jog_task", axis="z", step_val=10.0)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(self.engine.active_thread)
        self.engine.active_thread.join(timeout=2.0)

        self.assertAlmostEqual(self.engine.p[2], initial_z + 10.0, places=1)
        self.assertFalse(self.engine.is_jogging)
        self.assertFalse(self.engine.is_moving)
        self.assertEqual(self.engine.op_state, 5)
        self.assertIsNone(self.engine.fault)

    def test_stop_jog_does_not_latch_fault_or_abort(self):
        # Start a jog step
        self.commands.execute("jog_joint", joint_idx=1, step_deg=10.0)
        self.assertTrue(self.engine.is_jogging or self.engine.is_moving)

        # Stop jog
        res = self.commands.execute("jog_stop")
        self.assertEqual(res["status"], "completed")
        self.assertFalse(self.engine.is_jogging)
        self.assertFalse(self.engine.is_moving)
        self.assertFalse(self.engine.abort_requested)
        self.assertIsNone(self.engine.fault)
        self.assertEqual(self.engine.op_state, 5)

        # Cell must immediately accept subsequent commands without recovery!
        res_next = self.commands.execute("jog_joint", joint_idx=1, step_deg=-5.0)
        self.assertEqual(res_next["status"], "accepted")
        if self.engine.active_thread:
            self.engine.active_thread.join(timeout=2.0)
        self.assertIsNone(self.engine.fault)

    def test_streaming_jog_commands_do_not_throw_busy(self):
        # When user holds a jog button, commands arrive rapidly
        res1 = self.commands.execute("jog_joint", joint_idx=2, step_deg=2.0)
        self.assertEqual(res1["status"], "accepted")

        # Second jog arriving while is_jogging is active should not raise "Cell is busy"
        res2 = self.commands.execute("jog_joint", joint_idx=2, step_deg=2.0)
        self.assertEqual(res2["status"], "accepted")

        if self.engine.active_thread:
            self.engine.active_thread.join(timeout=2.0)
        self.assertIsNone(self.engine.fault)

    def test_cartesian_jog_boundary_does_not_raise_workcell_fault(self):
        # Jogging far beyond reach should gracefully stop, not crash workcell with UNREACHABLE_POSE fault
        res = self.commands.execute("jog_task", axis="z", step_val=2000.0)
        if self.engine.active_thread:
            self.engine.active_thread.join(timeout=2.0)

        # Engine fault MUST remain None
        self.assertIsNone(self.engine.fault)
        self.assertFalse(self.engine.abort_requested)
        self.assertEqual(self.engine.op_state, 5)


if __name__ == "__main__":
    unittest.main()
