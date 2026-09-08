import threading
import unittest
from unittest.mock import patch

from src.digitaltwin.commands import CommandService
from src.digitaltwin.palletizer_engine import PalletizerEngine


class WorkcellTests(unittest.TestCase):
    def setUp(self):
        self.engine = PalletizerEngine(startup_mode="SIMULATION")
        self.commands = CommandService(self.engine)

    def tearDown(self):
        self.engine.close()

    def wait(self):
        self.engine.active_thread.join(3)
        self.assertFalse(self.engine.active_thread.is_alive())

    def test_construction_has_no_io_or_threads(self):
        with patch.object(PalletizerEngine, 'connect_hardware') as connect:
            engine = PalletizerEngine()
            self.assertEqual(engine.mode, "SIMULATION")
            self.assertIsNone(engine.poll_thread)
            connect.assert_not_called()

    def test_simulation_start_does_not_connect(self):
        with patch.object(self.engine, 'connect_hardware') as connect:
            self.engine.start()
            connect.assert_not_called()
            self.assertTrue(self.engine.poll_thread.is_alive())

    def test_invalid_commands_rejected(self):
        for cmd, payload in [('unknown', {}), ('jog_joint', {'joint_idx': 6}),
                             ('jog_joint', {'step_deg': float('nan')}),
                             ('speed', {'speed_ratio': 101}),
                             ('jog_task', {'axis': 'invalid'})]:
            with self.subTest(cmd=cmd, payload=payload):
                self.assertEqual(self.commands.execute(cmd, **payload)['status'], 'rejected')

    def test_duplicate_start_reserves_before_thread_runs(self):
        gate = threading.Event()
        entered = threading.Event()
        def motion(*args, **kwargs):
            entered.set()
            gate.wait(2)
            return True
        with patch.object(self.engine, '_sim_move', side_effect=motion):
            first = self.commands.execute('home')
            self.assertEqual(first['status'], 'accepted')
            entered.wait(1)
            for cmd in ('home', 'pb1', 'zero', 'simulation', 'reset_pallet', 'jog_joint'):
                self.assertEqual(self.commands.execute(cmd)['status'], 'rejected', cmd)
            gate.set()
            self.wait()
        record = next(c for c in self.engine.commands if c['command_id'] == first['command_id'])
        self.assertEqual(record['status'], 'completed')

    def test_stop_latches_until_recovery(self):
        self.commands.execute('stop')
        self.commands.execute('stop', active=False)
        self.assertEqual(self.commands.execute('pb1')['status'], 'rejected')
        self.assertEqual(self.engine.workcell_state, 'STOPPED')
        self.assertTrue(self.commands.execute('recover')['success'])
        self.assertEqual(self.engine.workcell_state, 'IDLE')

    def test_feeder_fault_requires_cause_resolution(self):
        self.commands.execute('inject_fault', code='FEEDER_EMPTY')
        self.assertEqual(self.engine.workcell_state, 'FAULTED')
        self.assertFalse(self.commands.execute('recover')['success'])
        self.commands.execute('sensor')
        self.assertTrue(self.commands.execute('recover')['success'])
        self.assertIsNone(self.engine.fault)

    def test_fault_stops_active_motion_and_retains_context(self):
        first = self.commands.execute('home')
        self.commands.execute('inject_fault', code='MOTION_TIMEOUT')
        self.wait()
        record = next(c for c in self.engine.commands if c['command_id'] == first['command_id'])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(self.engine.fault['code'], 'MOTION_TIMEOUT')
        self.assertIn('step', self.engine.fault)
        self.assertFalse(self.engine.is_moving)

    def test_no_simulation_mutation_in_live_mode(self):
        self.engine.mode = 'HARDWARE_LIVE'
        for cmd in ('inject_fault', 'sensor', 'reset_pallet'):
            self.assertEqual(self.commands.execute(cmd)['status'], 'rejected')

    def test_disconnected_motion_never_falls_back_to_simulation(self):
        self.engine.mode = 'DISCONNECTED'
        with patch.object(self.engine, '_sim_cartesian_move') as simulate:
            self.assertFalse(self.engine._execute_cartesian_move([0]*6))
            simulate.assert_not_called()
        self.assertEqual(self.engine.fault['code'], 'CONNECTION_LOST')

    def test_full_palletize_and_putback_inventory(self):
        # Exercise the real sequences with motion/dwell sped up, not real-time timing.
        with patch.object(self.engine, '_sim_cartesian_move', return_value=True), \
             patch.object(self.engine, '_sim_move', return_value=True), \
             patch.object(self.engine, '_dwell', return_value=True):
            self.assertEqual(self.commands.execute('pb1')['status'], 'accepted')
            self.wait()
            self.assertEqual(self.engine.pallet_count, 8)
            self.assertEqual(self.engine.magazine_count, 0)
            self.assertTrue(all(slot['placed'] for slot in self.engine.slots))
            self.assertEqual(self.engine.commands[-1]['status'], 'completed')
            self.assertEqual(self.commands.execute('pb2')['status'], 'accepted')
            self.wait()
            self.assertEqual(self.engine.pallet_count, 0)
            self.assertEqual(self.engine.magazine_count, 8)
            self.assertFalse(any(slot['placed'] for slot in self.engine.slots))

    def test_worker_exception_faults_and_releases_ownership(self):
        with patch.object(self.engine, '_sim_move', side_effect=RuntimeError('test failure')):
            self.commands.execute('home')
            self.wait()
        self.assertEqual(self.engine.workcell_state, 'FAULTED')
        self.assertEqual(self.engine.commands[-1]['status'], 'failed')
        self.assertFalse(self.engine.sequence_running)

    def test_held_part_requires_reconciliation(self):
        self.engine.held_workpiece = True
        self.commands.execute('stop')
        self.assertFalse(self.commands.execute('recover')['success'])
        self.commands.execute('reset_pallet')
        self.assertTrue(self.commands.execute('recover')['success'])

    def test_cancelled_program_is_not_completed(self):
        command = self.commands.execute('home')
        self.commands.execute('stop')
        self.wait()
        record = next(c for c in self.engine.commands if c['command_id'] == command['command_id'])
        self.assertEqual(record['status'], 'cancelled')
        self.assertTrue(self.commands.execute('recover')['success'])
        self.assertEqual(self.engine.op_state, 5)

    def test_failed_motion_does_not_place_parts(self):
        with patch.object(self.engine, '_sim_cartesian_move', return_value=False):
            self.commands.execute('pb1')
            self.wait()
        self.assertEqual(self.engine.pallet_count, 0)
        self.assertEqual(self.engine.magazine_count, 8)
        self.assertEqual(self.engine.commands[-1]['status'], 'failed')

    def test_stop_during_gripper_dwell_does_not_advance_inventory(self):
        def stop_during_dwell(seconds):
            self.engine.set_stop(True)
            return False
        with patch.object(self.engine, '_sim_cartesian_move', return_value=True), \
             patch.object(self.engine, '_dwell', side_effect=stop_during_dwell):
            self.commands.execute('pb1')
            self.wait()
        self.assertEqual(self.engine.magazine_count, 8)
        self.assertEqual(self.engine.pallet_count, 0)
        self.assertEqual(self.engine.commands[-1]['status'], 'cancelled')

    def test_cartesian_waypoint_uses_cartesian_adapter(self):
        self.engine.waypoints = [{'id': 'cartesian', 'name': 'Test',
                                  'move_type': 'MoveL', 'p': [1, 2, 3, 0, 0, 0]}]
        with patch.object(self.engine, '_sim_cartesian_move', return_value=True) as move:
            self.commands.execute('goto_wp', id='cartesian')
            self.wait()
            self.assertEqual(move.call_args.args[0], [1, 2, 3, 0, 0, 0])

    def test_recovery_cannot_release_an_active_worker(self):
        gate = threading.Event()
        entered = threading.Event()
        def motion(*args):
            entered.set()
            gate.wait(2)
            return False
        with patch.object(self.engine, '_sim_move', side_effect=motion):
            self.commands.execute('home')
            self.assertTrue(entered.wait(1))
            self.commands.execute('stop')
            self.assertEqual(self.commands.execute('recover')['status'], 'rejected')
            gate.set()
            self.wait()


if __name__ == '__main__':
    unittest.main()
