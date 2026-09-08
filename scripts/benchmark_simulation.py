"""Record a real-time kinematic simulation baseline (not a hardware measurement)."""
import argparse
import json
from pathlib import Path
import platform
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.digitaltwin.commands import CommandService
from src.digitaltwin.palletizer_engine import PalletizerEngine
from src.digitaltwin.kinematics import joint_transforms

parser = argparse.ArgumentParser()
parser.add_argument('--output', default='artifacts/simulation-baseline.json')
args = parser.parse_args()
engine = PalletizerEngine(startup_mode='SIMULATION')
try:
    command = CommandService(engine).execute('pb1')
    started = time.monotonic()
    samples = []
    max_position_error = 0.0
    while engine.active_thread.is_alive() and time.monotonic()-started < 180:
        with engine.lock:
            q, p = list(engine.q), list(engine.p)
        samples.append(q)
        max_position_error = max(max_position_error, float(np.linalg.norm(joint_transforms(q)[-1][:3, 3]-p[:3])))
        engine.active_thread.join(timeout=0.02)
    record = next(c for c in engine.commands if c['command_id'] == command['command_id'])
    report = {
        'source': 'kinematic_simulation', 'python': platform.python_version(),
        'duration_seconds': round(time.monotonic() - started, 3),
        'parts_placed': engine.pallet_count, 'command_status': record['status'],
        'fault': engine.fault,
        'motion_samples': len(samples),
        'joint_range_degrees': np.ptp(samples, axis=0).round(3).tolist() if samples else [],
        'max_fk_position_error_mm': round(max_position_error, 5),
        'limitations': ['Single run, not a statistical performance study',
                       'Fixed motion durations; numerical inverse kinematics, no collision, dynamics, or hardware joint-limit validation',
                       'Not measured robot throughput'],
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    if record['status'] != 'completed' or engine.pallet_count != 8:
        raise SystemExit(1)
    if not samples or np.ptp(samples, axis=0).max() < 1 or max_position_error > 0.1:
        raise SystemExit('Simulation failed joint-motion / TCP consistency checks')
finally:
    engine.close()
