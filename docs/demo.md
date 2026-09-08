# Repeatable foundation demo

## Run

```sh
uv sync --frozen
uv run python run_digitaltwin.py
```

Open http://localhost:8088. The mode badge should say 3D SIMULATION. Expand WORKCELL at the top of the left column.

1. Start palletizing. Observe RUNNING and movement/part counts.
2. Try a second start. The command result should say rejected because the cell is busy.
3. Inject a simulated fault. Observe FAULTED, fault code, and interrupted step.
4. Wait for the program to stop. Reset simulated cell to reconcile inventory and restore the feeder.
5. Click Recover. Observe IDLE and no active fault. Recovery itself does not start motion.
6. Start palletizing again and allow all eight parts to complete.
7. Start put-back. Verify the pallet empties and the feeder returns to eight parts.

Injecting FEEDER_EMPTY while idle is a shorter demonstration: Recover rejects until the feeder sensor is restored (Reset simulated cell), then Recover returns to IDLE.

## Verification

```sh
uv run python -m unittest discover -s tests -v
uv run python scripts/benchmark_simulation.py
```

The benchmark runs an isolated in-process simulation, writes `artifacts/simulation-baseline.json`, and fails unless all eight parts finish. It takes roughly a minute on the measured workstation; actual wall time depends on scheduling. The result is not robot throughput.

API examples:

```json
{"cmd": "pb1"}
{"cmd": "inject_fault", "code": "MOTION_TIMEOUT"}
{"cmd": "reset_pallet"}
{"cmd": "recover"}
```

Send each object separately to POST `/api/commands`. Poll GET `/api/commands/{command_id}` to distinguish acceptance from completion. GET `/api/health` reports startup mode and cell state.
