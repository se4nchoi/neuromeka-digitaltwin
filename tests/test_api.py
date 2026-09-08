"""Real HTTP/WebSocket integration tests without an additional test dependency."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.request import Request, urlopen

from websockets.sync.client import connect


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.process = subprocess.Popen(
            [sys.executable, 'run_digitaltwin.py'],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'DIGITALTWIN_MODE': 'SIMULATION', 'DIGITALTWIN_PORT': str(cls.port)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                cls.get('/api/health')
                return
            except OSError:
                time.sleep(.1)
        raise RuntimeError('Test server did not start')

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    @classmethod
    def get(cls, path):
        with urlopen(cls.base + path, timeout=3) as response:
            return json.load(response)

    def post(self, path, payload):
        request = Request(self.base + path, json.dumps(payload).encode(),
                          {'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_health_and_ui(self):
        self.assertEqual(self.get('/api/health')['mode'], 'SIMULATION')
        # 1. 3D Twin & Workcell page
        with urlopen(self.base, timeout=3) as response:
            self.assertIn('workcellDiagnostics', response.read().decode())
        # 2. Dedicated Production & Log Analytics page
        with urlopen(self.base + '/production', timeout=3) as response:
            self.assertEqual(response.status, 200)
            content = response.read().decode()
            self.assertIn('PRODUCTION &amp; LOG ANALYTICS', content)
        with urlopen(self.base + '/analytics', timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_rest_websocket_validation_parity_and_telemetry(self):
        rest = self.post('/api/jog/joint', {'joint_idx': 9, 'step_deg': 1})
        with connect(f'ws://127.0.0.1:{self.port}/ws/telemetry') as ws:
            ws.send(json.dumps({'cmd': 'jog_joint', 'joint_idx': 9, 'step_deg': 1}))
            telemetry = None
            result = None
            for _ in range(30):
                packet = json.loads(ws.recv(timeout=3))
                if packet.get('type') == 'command_result':
                    result = packet
                else:
                    telemetry = packet
                if result and telemetry:
                    break
            self.assertEqual(rest['status'], 'rejected')
            self.assertEqual(result['status'], rest['status'])
            self.assertEqual(telemetry['mode'], 'SIMULATION')
            ws.send('[]')
            for _ in range(30):
                packet = json.loads(ws.recv(timeout=3))
                if packet.get('type') == 'command_result':
                    self.assertEqual(packet['status'], 'rejected')
                    break
            else:
                self.fail('Missing malformed-command rejection')

    def test_command_lifecycle(self):
        result = self.post('/api/commands', {'cmd': 'home'})
        self.assertEqual(result['status'], 'accepted')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.get('/api/commands/' + result['command_id'])
            if record['status'] not in ('accepted', 'running'):
                break
            time.sleep(.05)
        self.assertEqual(record['status'], 'completed')

    def test_cartesian_websocket_publishes_changing_joints(self):
        samples = []
        with connect(f'ws://127.0.0.1:{self.port}/ws/telemetry') as ws:
            ws.send(json.dumps({'cmd': 'jog_task', 'axis': 'z', 'step_val': 5}))
            command_id = None
            for _ in range(100):
                packet = json.loads(ws.recv(timeout=3))
                if packet.get('type') == 'command_result':
                    self.assertEqual(packet['status'], 'accepted')
                    command_id = packet['command_id']
                else:
                    samples.append(tuple(packet['q']))
                    last = packet.get('last_command') or {}
                    if command_id and last.get('command_id') == command_id and last['status'] == 'completed':
                        break
            else:
                self.fail('Cartesian jog did not complete through WebSocket')
        self.assertGreater(len(set(samples)), 1, '3D viewer must receive changing joint angles')


if __name__ == '__main__':
    unittest.main()
