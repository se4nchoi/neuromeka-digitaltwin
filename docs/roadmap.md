# Portfolio implementation status

## Implemented foundation

- Simulation-first localhost startup and environment configuration.
- Shared action validation across HTTP, WebSocket, and physical program starts.
- Atomic program ownership, command IDs, and terminal outcomes.
- Separate simulation/hardware motion adapters for automated programs.
- Fault context, stop latching, controlled recovery, and simulation injection UI.
- Behavior tests, live API tests, CI definition, and a measured simulation baseline.

## Fixed: Cartesian simulation now animates robot joints

Cartesian simulation previously changed TCP pose (`p`) without updating joint angles (`q`), leaving the rendered arm stationary. Numerical inverse kinematics now updates both consistently for palletizing, put-back, Cartesian waypoints, and Cartesian jogging. Unreachable or discontinuous paths fault before motion playback.

Regression coverage executes real motion samples and checks joint/TCP consistency, path geometry, orientation wrapping, stop handling, and unreachable targets. The benchmark also records joint ranges and FK position error. Collision, dynamics, hardware joint limits, and detailed gripper/workpiece contact calibration remain outside the kinematic simulation.

## Implemented: Milestone 1 — Production event data

- Durable SQLite persistence in `src/digitaltwin/storage.py` with WAL mode and automated schema migrations.
- Tables: `production_runs`, `cycles`, `process_steps`, `state_transitions`, `fault_events`, `operator_actions`.
- Granular step-level duration tracking with monotonic timing (`time.monotonic()`) across all 8 palletize and put-back motion/dwell phases.
- Safe server restart recovery: unclosed runs and cycles reconcile to `interrupted`.
- REST endpoints for querying runs (`/api/production/runs`), run details with nested cycles/steps (`/api/production/runs/{run_id}`), chronological event timeline (`/api/production/events`), and aggregated KPI summaries (`/api/production/kpi/summary`).
- Complete automated test suite in `tests/test_persistence.py`.

## Next milestone: Milestone 2 — KPI and production dashboard

1. Build production history and analytics dashboard views (completed parts, throughput).
2. Display average and p95 cycle time metrics calculated from durable cycle records.
3. Visualize step-time breakdown (approach, plunge, grip, extract, release).
4. Render equipment state timeline and downtime Pareto charts from persisted events.

## Then: production workflow

Add versioned recipes, work orders, part/pallet-slot traceability, pallet-change handling, and order summaries. Snapshot the recipe at order start.

## Then: validated optimization and AI

Improve simulator fidelity before claiming speed/clearance optimization. Gather repeated baseline/variant trials with documented constraints. Build cycle anomaly detection only after producing a labeled dataset, threshold baseline, and run/time-separated evaluation.

## Final showcase

Overhaul the UI at a later stage: replace the current color palette, simplify the console/HMI layout, increase text and control sizes, and verify readability at ordinary laptop viewport sizes. This redesign is deferred rather than part of the current simulation diagnosis.

Create a short demo video, Korean case study, architecture/data diagrams, and reproducible experiment report. Include measured hardware evidence only when equipment validation is available. No predictive-maintenance or industrial-accuracy claims from synthetic faults alone.
