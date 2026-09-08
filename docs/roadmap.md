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

## Implemented: Milestone 3 — Work-order and recipe workflow

- Versioned recipes system with parameter snapshots (`storage.py`, `config.py`, `server.py`):
  - Pre-seeded recipes: `pallet-2x2x2-default`, `pallet-2x2x1-single`, `pallet-high-speed`.
  - Recipe parameters govern grid layout (floors, rows, cols), slot spacing, approach clearance, transit velocities, and dwell times.
  - Snapshot immutability: active orders snapshot recipe parameters in `recipe_snapshot_json` at launch; future recipe edits never mutate historical or in-flight production runs.
- Work-order lifecycle and multi-pallet orchestration (`palletizer_engine.py`, `storage.py`):
  - States: `pending` -> `in_progress` -> `paused_pallet_change` -> `completed` / `cancelled` / `interrupted`.
  - Multi-pallet handling: orders exceeding single pallet capacity (e.g. 12 parts on an 8-slot pallet) pause safely at `HOME` in `WAITING_PALLET_CHANGE`, alert the operator, prompt pallet swap confirmation (`confirm_pallet_swap()` / `swap_pallet` command), and resume on Pallet #2.
- Granular billet/part traceability:
  - Each placed item persists to `workpiece_items` with serial number (`{order_number}-P{pallet}-S{slot}`), slot level/row/col, cycle ID, run ID, duration, and UTC timestamp.
- Production summary and replay APIs:
  - `GET /api/work-orders/{order_id}/summary`: aggregated KPIs, pallet breakdown, and full traceability registry.
  - `GET /api/work-orders/{order_id}/replay`: step-by-step cycle and motion replay payload.
- Unified Operator UI:
  - Integrated into `/production` (Active Order card, Pallet swap pulsating banner, versioned recipe creator with presets, recent work orders table, and work order summary modal with CSV/JSON exports).
  - 3D Twin HUD synchronization (`/`): displays active work order tag and real-time pallet swap alert button.
- Comprehensive test coverage in `tests/test_work_orders.py` (immutability, single-pallet, multi-pallet swap, cancellation, and API routes).

## Implemented: Milestone 4 — Cycle time optimization & variant benchmarking

- Dynamic kinematic velocity scaling in `drivers.py`:
  - Motion durations dynamically scale with Cartesian distance, transit velocity ratio (`transit_vel_ratio`), and action velocity ratio (`action_vel_ratio`).
  - Clearance reduction (`approach_clearance_z`: 100mm vs 70mm) directly reduces vertical plunge/extract travel times.
- Kinetic envelope, jerk, and singularity analysis in `kinematics.py`:
  - `analyze_trajectory_kinematics()` calculates peak joint velocities ($\dot{q}$), peak accelerations ($\ddot{q}$), and peak jerk ($\dddot{q}$) along minimum-jerk quintic polynomials.
  - Feasibility validation (`FEASIBLE`, `WARNING`, `INFEASIBLE`) asserts step continuity ($\max |\Delta q| \le 15^\circ$), motor limits ($\le 180^\circ/\text{s}$), and Yoshikawa manipulability margin ($\sqrt{\det(J J^T)}$).
- Statistical benchmarking & hypothesis testing engine in `storage.py`:
  - Schema Migration 3: durable `benchmarks` and `benchmark_trials` tables.
  - Multi-trial statistical comparison: sample mean, sample standard deviation ($\sigma$), median, P95 tail latency, net cycle time delta ($\Delta \bar{T} = \bar{T}_{\text{candidate}} - \bar{T}_{\text{baseline}}$), and percentage cycle time reduction.
  - Welch-Satterthwaite effective degrees of freedom and two-tailed Welch's $t$-test $p$-value ($p < 0.05$ significance threshold).
  - 95% Confidence Interval for $\Delta T$ with regularized incomplete beta / Student-$t$ distribution.
  - Granular step-level breakdown delta across all 8 process phases (`PICK_APPROACH`, `PICK_PLUNGE`, `PICK_GRIP`, `PICK_EXTRACT`, `PLACE_APPROACH`, `PLACE_PLUNGE`, `PLACE_RELEASE`, `PLACE_EXTRACT`).
- REST APIs in `server.py`:
  - `POST /api/benchmarks/run`: executes multi-trial benchmark experiments.
  - `GET /api/benchmarks`: lists historical benchmarks with summary metrics.
  - `GET /api/benchmarks/{id}`: detailed trial records and kinematic envelope profiles.
  - `GET /api/benchmarks/compare`: on-the-fly analytical comparison between recipes.
- Glassmorphic operator console workspace in `production.html`, `production.css`, `dashboard.js`:
  - Experiment setup controls (baseline vs candidate recipe selectors, trial count).
  - 4 Executive Comparison Cards ($\Delta T$, % savings, 95% CI, $p$-value, Throughput Uplift, Feasibility Verdict).
  - Step-by-step delta horizontal stacked bars isolating where seconds are saved (speed vs clearance vs dwell).
  - Kinematic limits trade-off table comparing peak velocity, acceleration, jerk, and safety margins.
  - Historical benchmarks log with one-click trial inspection.
- Complete automated test suite in `tests/test_benchmarks.py` (all 44 tests pass).

## Next milestone: Milestone 5 — AI Anomaly Detection & Operational Quality Baseline

1. Build cycle anomaly detection with labeled datasets, threshold baselines, and run/time-separated evaluation.
2. Gather repeated baseline/variant trials with documented operating constraints.
3. Quantify cycle-level deviations and early failure predictors without synthetic-only accuracy claims.

## Then: validated optimization and AI

Improve simulator fidelity before claiming speed/clearance optimization. Gather repeated baseline/variant trials with documented constraints. Build cycle anomaly detection only after producing a labeled dataset, threshold baseline, and run/time-separated evaluation.

## Final showcase

Overhaul the UI at a later stage: replace the current color palette, simplify the console/HMI layout, increase text and control sizes, and verify readability at ordinary laptop viewport sizes. This redesign is deferred rather than part of the current simulation diagnosis.

Create a short demo video, Korean case study, architecture/data diagrams, and reproducible experiment report. Include measured hardware evidence only when equipment validation is available. No predictive-maintenance or industrial-accuracy claims from synthetic faults alone.
