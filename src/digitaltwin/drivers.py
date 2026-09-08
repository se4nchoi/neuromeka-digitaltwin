"""Simulation interpolation and Neuromeka motion adapters.

The controller owns cell state and program sequencing. Adapters execute one motion.
Cartesian simulation solves joint angles along a straight TCP path.
"""
import time
from typing import List, Protocol
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

    def move_cartesian(self, target, velocity, acceleration, step):
        return self.cell._sim_cartesian_move(target, 1.2, step)

    def move_joint(self, target, velocity, step):
        return self.cell._sim_move(target, 1.2, step)

    def home(self):
        from .config import HOME_JPOS
        return self.cell._sim_move(HOME_JPOS, 1.5, "Returning to HOME Position")

    def _sim_cartesian_move(self, p_target: List[float], duration: float, step_name: str) -> bool:
        self.cell.status_msg = step_name
        self.cell.is_moving = True
        self.cell.op_state = 6
        self.cell.op_state_name = "OP_MOVING (6)"

        try:
            trajectory = cartesian_trajectory(
                self.cell.q, p_target, duration,
                cancelled=lambda: self.cell.abort_requested or self.cell.stop_active)
        except InterruptedError:
            self.cell.is_moving = False
            return False
        except UnreachablePose as exc:
            self.cell.is_moving = False
            self.cell.raise_fault("UNREACHABLE_POSE", str(exc))
            return False

        for q in trajectory:
            if self.cell.abort_requested or self.cell.stop_active:
                self.cell.is_moving = False
                self.cell.op_state = 8
                self.cell.op_state_name = "OP_STOP (X107)"
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

        traj = quintic_interpolate(self.cell.q, q_target, duration, hz=60)
        for point in traj:
            if self.cell.abort_requested or self.cell.stop_active:
                self.cell.is_moving = False
                self.cell.op_state = 8
                self.cell.op_state_name = "OP_STOP (X107)"
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

