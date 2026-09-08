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

## Next milestone: production records and analysis

1. Add SQLite migrations for runs, cycles, process steps, transitions, alarms, and operator actions.
2. Emit events at controller transitions and step boundaries with monotonic durations and UTC timestamps.
3. Persist simulation/hardware origin and recipe version with every run.
4. Build production history, mean/p95 cycle time, step breakdown, and downtime views.
5. Test restart persistence and reconcile partial/interrupted runs.

## Then: production workflow

Add versioned recipes, work orders, part/pallet-slot traceability, pallet-change handling, and order summaries. Snapshot the recipe at order start.

## Then: validated optimization and AI

Improve simulator fidelity before claiming speed/clearance optimization. Gather repeated baseline/variant trials with documented constraints. Build cycle anomaly detection only after producing a labeled dataset, threshold baseline, and run/time-separated evaluation.

## Final showcase

Overhaul the UI at a later stage: replace the current color palette, simplify the console/HMI layout, increase text and control sizes, and verify readability at ordinary laptop viewport sizes. This redesign is deferred rather than part of the current simulation diagnosis.

Create a short demo video, Korean case study, architecture/data diagrams, and reproducible experiment report. Include measured hardware evidence only when equipment validation is available. No predictive-maintenance or industrial-accuracy claims from synthetic faults alone.
