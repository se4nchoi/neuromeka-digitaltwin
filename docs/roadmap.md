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

## Implemented: Milestone 2 — KPI and production dashboard

- Production analytics and KPI calculation engine (`storage.py`, `server.py`).
  - Total parts placed and real-time throughput rate (parts/hr and parts/min).
  - Cycle time metrics: Average, P95 tail latency, Minimum, and Maximum durations.
  - Step breakdown with percentage contribution and automatic bottleneck identification.
  - Workcell operational availability percentage ($T_{\text{active}} / (T_{\text{active}} + T_{\text{downtime}}) \times 100$) and total downtime tracking.
  - Downtime Pareto chart attributing occurrences and cumulative downtime to fault codes.
  - Equipment state timeline (`GET /api/production/timeline`) calculating exact state transition durations (`RUNNING`, `IDLE`, `FAULTED`, `STOPPED`).
- Rich, glassmorphic client-side dashboard in `src/digitaltwin/static/dashboard.js`, `index.html`, and `style.css`:
  - Quick KPI status badge in top navigation bar (`#kpiQuickBadge`) with click-to-open.
  - `📊 PRODUCTION & KPIS` workspace in Multi-Purpose Center drawer with `⛶ EXPAND VIEW` toggle.
  - 6 executive metric cards with top accent gradients and glowing hover animations.
  - 8-phase process step segmented stacked bar with interactive tooltip and bottleneck badge.
  - Downtime Pareto chart with occurrence counts, downtime seconds, and cumulative line.
  - Horizontal equipment state ribbon with hover tooltips for state, trigger, and duration.
  - Historical production runs table with status pills, durations, and UTC timestamps.
  - Live auto-refresh engine (1s/3s/10s/paused) and instant refresh on telemetry/commands.
  - One-click export to CSV and JSON.

## Next milestone: Milestone 3 — Work-order and recipe workflow

1. Add versioned recipes (e.g. `pallet-2x2x2-standard`, `pallet-1x2x2-half`, speeds, approach clearances).
2. Implement work-order lifecycle (`PENDING` -> `RUNNING` -> `COMPLETED` / `CANCELLED`).
3. Associate each production run with a specific work order, snapshotting recipe parameters at run start.
4. Part and pallet-slot traceability: link each placed billet to its cycle, slot, and order.
5. Pallet-change workflow: operator signal for pallet swap upon full capacity.

## Then: validated optimization and AI

Improve simulator fidelity before claiming speed/clearance optimization. Gather repeated baseline/variant trials with documented constraints. Build cycle anomaly detection only after producing a labeled dataset, threshold baseline, and run/time-separated evaluation.

## Final showcase

Overhaul the UI at a later stage: replace the current color palette, simplify the console/HMI layout, increase text and control sizes, and verify readability at ordinary laptop viewport sizes. This redesign is deferred rather than part of the current simulation diagnosis.

Create a short demo video, Korean case study, architecture/data diagrams, and reproducible experiment report. Include measured hardware evidence only when equipment validation is available. No predictive-maintenance or industrial-accuracy claims from synthetic faults alone.
