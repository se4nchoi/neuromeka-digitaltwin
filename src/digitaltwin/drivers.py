"""Simulation interpolation and Neuromeka motion adapters.

The controller owns cell state and program sequencing. Adapters execute one motion.
Cartesian simulation solves joint angles along a straight TCP path.
"""
import math
import time
from typing import List, Optional, Protocol
from .kinematics import quintic_interpolate, cartesian_trajectory, UnreachablePose


class MotionDriver(Protocol):
    def move_cartesian(self, target, velocity, acceleration, step): ...
    def move_joint(self, target, velocity, step): ...
    def home(self): ...


class HardwareDriver:
    def __init__(self, cell, absolute_base):
        self.cell = cell
        self.absolute_base = absolute_base

    def move_cartesian(self, target, velocity, acceleration, step):
        self.cell.indy.movel(ttarget=list(target), base_type=self.absolute_base,
                             vel_ratio=velocity, acc_ratio=acceleration)
        return self.cell._wait_hardware_motion()

    def move_joint(self, target, velocity, step):
        self.cell.indy.movej(list(target), vel_ratio=velocity, acc_ratio=velocity)
        return self.cell._wait_hardware_motion()

    def home(self):
        self.cell.indy.move_home()
        return self.cell._wait_hardware_motion()


class SimulationDriver:
    def __init__(self, cell, fk):
        self.cell = cell
        self.fk = fk

    def _calc_cartesian_duration(self, target: List[float], velocity: Optional[float] = None, acceleration: Optional[float] = None) -> float:
        """Calculates realistic kinematic motion duration based on distance, velocity, and acceleration ratios."""
        try:
            cur_p = getattr(self.cell, "p", [0.0, 0.0, 0.0])
            dx = float(target[0]) - float(cur_p[0])
            dy = float(target[1]) - float(cur_p[1])
            dz = float(target[2]) - float(cur_p[2])
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        except Exception:
            dist = 300.0

        vel = max(5.0, float(velocity or 45.0))
        v_factor = 45.0 / vel
        if dist > 200.0:
            base = 1.30 * (dist / 600.0) ** 0.35
        else:
            base = 1.25 * (max(30.0, dist) / 100.0) ** 0.40

        dur = round(base * (v_factor ** 0.55), 2)
        return max(0.40, dur)

    def _calc_joint_duration(self, target: List[float], velocity: Optional[float] = None) -> float:
        """Calculates joint motion duration based on maximum joint displacement."""
        try:
            cur_q = getattr(self.cell, "q", [0.0] * 6)
            max_delta = max(abs(float(t) - float(c)) for t, c in zip(target, cur_q))
        except Exception:
            max_delta = 90.0

        vel = max(5.0, float(velocity or 45.0))
        nominal = max_delta / (150.0 * (vel / 100.0)) + 0.3
        return max(0.40, round(nominal, 2))

    def move_cartesian(self, target, velocity, acceleration, step):
        duration = self._calc_cartesian_duration(target, velocity, acceleration)
        return self.cell._sim_cartesian_move(target, duration, step)

    def move_joint(self, target, velocity, step):
        duration = self._calc_joint_duration(target, velocity)
        return self.cell._sim_move(target, duration, step)

    def home(self):
        from .config import HOME_JPOS
        duration = self._calc_joint_duration(HOME_JPOS, 25.0)
        return self.cell._sim_move(HOME_JPOS, max(1.2, duration), "Returning to HOME Position")

    def _sim_cartesian_move(self, p_target: List[float], duration: float, step_name: str) -> bool:
        self.cell.status_msg = step_name
        self.cell.is_moving = True
        self.cell.op_state = 6
        self.cell.op_state_name = "OP_MOVING (6)"

        is_jog = step_name.startswith("[SIM] Jog") or getattr(self.cell, "is_jogging", False)

        try:
            trajectory = cartesian_trajectory(
                self.cell.q, p_target, duration,
                cancelled=lambda: self.cell.abort_requested or self.cell.stop_active or (is_jog and not getattr(self.cell, "is_jogging", False)))
        except InterruptedError:
            self.cell.is_moving = False
            return False
        except UnreachablePose as exc:
            self.cell.is_moving = False
            if is_jog:
                self.cell.status_msg = f"Jog limit reached: {exc}"
                self.cell.op_state = 5
                self.cell.op_state_name = "OP_IDLE (5)"
                return False
            self.cell.raise_fault("UNREACHABLE_POSE", str(exc))
            return False

        for q in trajectory:
            if self.cell.abort_requested or self.cell.stop_active:
                self.cell.is_moving = False
                self.cell.op_state = 8
                self.cell.op_state_name = "OP_STOP (X107)"
                return False
            if is_jog and not getattr(self.cell, "is_jogging", False):
                self.cell.is_moving = False
                self.cell.op_state = 5
                self.cell.op_state_name = "OP_IDLE (5)"
                return False

            with self.cell.lock:
                self.cell.q = list(q)
                self.cell.p = self.fk(q)
            time.sleep(1.0 / 60.0)

        with self.cell.lock:
            self.cell.is_moving = False
            self.cell.op_state = 5
            self.cell.op_state_name = "OP_IDLE (5)"
        return True


    def _sim_move(self, q_target: List[float], duration: float, step_name: str) -> bool:
        self.cell.status_msg = step_name
        self.cell.is_moving = True
        self.cell.op_state = 6
        self.cell.op_state_name = "OP_MOVING (6)"

        is_jog = step_name.startswith("[SIM] Jog") or getattr(self.cell, "is_jogging", False)

        traj = quintic_interpolate(self.cell.q, q_target, duration, hz=60)
        for point in traj:
            if self.cell.abort_requested or self.cell.stop_active:
                self.cell.is_moving = False
                self.cell.op_state = 8
                self.cell.op_state_name = "OP_STOP (X107)"
                return False
            if is_jog and not getattr(self.cell, "is_jogging", False):
                self.cell.is_moving = False
                self.cell.op_state = 5
                self.cell.op_state_name = "OP_IDLE (5)"
                return False

            with self.cell.lock:
                self.cell.q = point
                self.cell.p = self.fk(self.cell.q)
            time.sleep(1.0 / 60.0)

        with self.cell.lock:
            self.cell.q = list(q_target)
            self.cell.p = self.fk(self.cell.q)
            self.cell.is_moving = False
            self.cell.op_state = 5
            self.cell.op_state_name = "OP_IDLE (5)"
        return True

