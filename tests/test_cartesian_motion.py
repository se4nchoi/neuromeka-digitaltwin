import unittest
from unittest.mock import patch
import numpy as np

from src.digitaltwin.commands import CommandService
from src.digitaltwin.config import HOME_JPOS, PICK_LOCATION
from src.digitaltwin.kinematics import (
    cartesian_trajectory, joint_transforms, pose_transform,
    get_approach_pose, rotation_vector, rotation_exp, UnreachablePose,
)
from src.digitaltwin.palletizer_engine import PalletizerEngine


class CartesianMotionTests(unittest.TestCase):
    def test_home_to_feeder_follows_straight_line_and_orientation(self):
        target = get_approach_pose(PICK_LOCATION)
        trajectory = cartesian_trajectory(HOME_JPOS, target, 1.2)
        start = joint_transforms(HOME_JPOS)[-1]
        end = pose_transform(target)
        delta_rotation = rotation_vector(end[:3, :3] @ start[:3, :3].T)
        self.assertGreater(np.max(np.abs(np.array(trajectory[-1])-HOME_JPOS)), 10)
        for q, tau in zip(trajectory, np.linspace(0, 1, len(trajectory))):
            blend = 10*tau**3 - 15*tau**4 + 6*tau**5
            actual = joint_transforms(q)[-1]
            expected_position = start[:3, 3] + blend*(end[:3, 3]-start[:3, 3])
            self.assertLess(np.linalg.norm(actual[:3, 3]-expected_position), 0.03)
            expected_rotation = rotation_exp(blend*delta_rotation) @ start[:3, :3]
            self.assertLess(np.linalg.norm(rotation_vector(expected_rotation @ actual[:3, :3].T)), 0.0002)
        self.assertLess(np.max(np.abs(np.diff(trajectory, axis=0))), 15)

    def test_unreachable_target_does_not_publish_fake_pose(self):
        engine = PalletizerEngine(startup_mode='SIMULATION')
        before_q, before_p = list(engine.q), list(engine.p)
        self.assertFalse(engine._sim_cartesian_move([10000, 0, 0, 0, 0, 0], 1.2, 'unreachable'))
        self.assertEqual(engine.q, before_q)
        self.assertEqual(engine.p, before_p)
        self.assertEqual(engine.fault['code'], 'UNREACHABLE_POSE')
        engine.close()

    def test_rotation_wrap_uses_short_path(self):
        a = pose_transform([0, 0, 0, 0, 0, 179])[:3, :3]
        b = pose_transform([0, 0, 0, 0, 0, -179])[:3, :3]
        self.assertAlmostEqual(np.linalg.norm(rotation_vector(b @ a.T)), np.radians(2))
        vector = rotation_vector(pose_transform([0, 0, 0, 180, 0, 0])[:3, :3])
        np.testing.assert_allclose(rotation_exp(vector), pose_transform([0, 0, 0, 180, 0, 0])[:3, :3], atol=1e-8)

    def test_planning_obeys_stop(self):
        with self.assertRaises(InterruptedError):
            cartesian_trajectory(HOME_JPOS, PICK_LOCATION, 1.2, cancelled=lambda: True)

    def test_full_pick_place_and_putback_update_joints_and_matching_tcp(self):
        engine = PalletizerEngine(startup_mode='SIMULATION')
        commands = CommandService(engine)
        frames = []
        def capture(_):
            frames.append((list(engine.q), list(engine.p)))
        try:
            # Only wall-clock waits are replaced; IK and all motion samples run.
            with patch('src.digitaltwin.drivers.time.sleep', side_effect=capture), \
                 patch.object(engine, '_dwell', return_value=True):
                for cmd, expected in [('pb1', 8), ('pb2', 0)]:
                    result = commands.execute(cmd)
                    self.assertEqual(result['status'], 'accepted')
                    engine.active_thread.join(timeout=30)
                    self.assertFalse(engine.active_thread.is_alive())
                    self.assertIsNone(engine.fault)
                    self.assertEqual(engine.commands[-1]['status'], 'completed')
                    self.assertEqual(engine.pallet_count, expected)
            self.assertGreater(len(frames), 6000)
            self.assertGreater(np.ptp(np.array([q for q, _ in frames]), axis=0).max(), 60)
            for q, p in frames[::20]:
                np.testing.assert_allclose(joint_transforms(q)[-1][:3, 3], p[:3], atol=0.02)
                error = rotation_vector(pose_transform(p)[:3, :3] @ joint_transforms(q)[-1][:3, :3].T)
                self.assertLess(np.linalg.norm(error), 0.001)
        finally:
            engine.close()

    def test_cartesian_jog_updates_joint_angles(self):
        engine = PalletizerEngine(startup_mode='SIMULATION')
        before_q, before_p = list(engine.q), list(engine.p)
        try:
            with patch('src.digitaltwin.drivers.time.sleep'):
                self.assertEqual(CommandService(engine).execute('jog_task', axis='z', step_val=5)['status'], 'accepted')
                engine.active_thread.join(3)
            self.assertIsNone(engine.fault)
            self.assertNotEqual(engine.q, before_q)
            self.assertAlmostEqual(engine.p[2], before_p[2]+5, delta=0.03)
        finally:
            engine.close()


if __name__ == '__main__':
    unittest.main()
