"""
Indy7 Multi-Purpose Center Engine:
- Dual-mode Workcell & Kinematic Simulation / Live Hardware Controller
- Joint & Task Jogging (Web Pendant)
- Direct Teaching (Zero-G Hand Guiding)
- Action/Program Center (Palletizing, Put-Back, Zero, Home, Custom Sequences)
- Waypoint Management & Persistence (Acquire, Save, Load, Execute)
- Real-time 30Hz Telemetry & Hardware I/O Tracking
"""

import os
import json
import time
import math
import threading
import logging
import uuid
from .drivers import HardwareDriver, SimulationDriver
from .storage import ProductionStorage
import numpy as np
from typing import Any, Dict, List, Optional
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_RECIPE_ID,
    DEFAULT_RECIPE_VERSION,
    DEFAULT_ROBOT_IP,
    STARTUP_MODE,
    PICK_LOCATION,
    DROP_BASE_LOCATION,
    MAGAZINE_INSERT_LOCATION,
    HOME_JPOS,
    GRID_X,
    GRID_Y,
    SLOTS_PER_FLOOR,
    NUM_FLOORS,
    TOTAL_MAX_ITEMS,
    OFFSET_X,
    OFFSET_Y,
    LAYER_HEIGHT,
    APPROACH_CLEARANCE_Z,
    TRANSIT_VEL_RATIO,
    TRANSIT_ACC_RATIO,
    ACTION_VEL_RATIO,
    ACTION_ACC_RATIO,
    GRIPPER_DWELL_SEC,
    DI_MAGAZINE_SENSOR,
    DI_PB1,
    DI_PB2,
    DI_STOP,
)
from .kinematics import KNOWN_JOINTS, MDH_PARAMS, quintic_interpolate, forward_kinematics_craig

try:
    from neuromeka import IndyDCP3, StopCategory, TaskBaseType, JointBaseType
    HAS_NEUROMEKA = True
except ImportError:
    HAS_NEUROMEKA = False

WAYPOINTS_FILE = os.path.join(os.path.dirname(__file__), "waypoints.json")

OP_STATE_NAMES = {
    0: "SYSTEM_OFF",
    1: "SYSTEM_ON",
    2: "VIOLATION",
    3: "RECOVER_HARD",
    4: "RECOVER_SOFT",
    5: "OP_IDLE (5)",
    6: "OP_MOVING (6)",
    7: "DIRECT_TEACHING (7)",
    8: "COLLISION (8)",
    9: "STOP_AND_OFF",
    10: "COMPLIANCE",
    11: "BRAKE_CONTROL",
    12: "SYSTEM_RESET",
    13: "SYSTEM_SWITCH",
    15: "VIOLATE_HARD",
    16: "MANUAL_RECOVER",
    17: "TELE_OP",
}


def analytic_ti(alpha_deg, a, theta_deg, d):
    alpha = np.radians(alpha_deg)
    theta = np.radians(theta_deg)
    ca, sa = np.cos(alpha), np.sin(alpha)
    ct, st = np.cos(theta), np.sin(theta)
    return np.array([
        [ct, -st, 0, a],
        [ca * st, ca * ct, -sa, -sa * d],
        [sa * st, sa * ct, ca, ca * d],
        [0, 0, 0, 1]
    ])


def forward_kinematics_craig(q_deg: List[float]) -> List[float]:
    """Computes Cartesian pose [X, Y, Z, U, V, W] from joint angles using Indy7 Craig MDH."""
    W = np.eye(4)
    for i in range(6):
        p = MDH_PARAMS[i]
        Ti = analytic_ti(p["alpha"], p["a"], p["theta0"] + q_deg[i], p["d"])
        W = W @ Ti

    x, y, z = W[0, 3], W[1, 3], W[2, 3]

    # Extract Euler angles (Indy convention Z-Y-X / Tait-Bryan)
    r11, r12, r13 = W[0, 0], W[0, 1], W[0, 2]
    r21, r22, r23 = W[1, 0], W[1, 1], W[1, 2]
    r31, r32, r33 = W[2, 0], W[2, 1], W[2, 2]

    # Singularities check
    sy = math.sqrt(r11 * r11 + r21 * r21)
    singular = sy < 1e-6

    if not singular:
        u = math.atan2(r32, r33)
        v = math.atan2(-r31, sy)
        w = math.atan2(r21, r11)
    else:
        u = math.atan2(-r23, r22)
        v = math.atan2(-r31, sy)
        w = 0.0

    return [
        round(float(x), 2),
        round(float(y), 2),
        round(float(z), 2),
        round(math.degrees(u), 2),
        round(math.degrees(v), 2),
        round(math.degrees(w), 2),
    ]


class PalletizerEngine:
    def __init__(self, startup_mode=None, storage: Optional[ProductionStorage] = None):
        self.lock = threading.RLock()
        self.storage = storage if storage is not None else ProductionStorage(DEFAULT_DB_PATH)
        self.recipe_id = DEFAULT_RECIPE_ID
        self.recipe_version = DEFAULT_RECIPE_VERSION
        self.active_run_id: Optional[str] = None
        self.active_cycle_id: Optional[str] = None
        self.current_command_id: Optional[str] = None

        # Work Order & Recipe Workflow
        self.active_order_id: Optional[str] = None
        self.active_order_number: Optional[str] = None
        self.active_order_snapshot: Optional[Dict[str, Any]] = None
        self.order_target_quantity: int = TOTAL_MAX_ITEMS
        self.order_completed_quantity: int = 0
        self.current_pallet_index: int = 1
        self.pallet_change_required: bool = False


        self.robot_ip = DEFAULT_ROBOT_IP
        self.indy: Optional[IndyDCP3] = None
        self.hardware_connected = False
        self.mode = "DISCONNECTED"  # "HARDWARE_LIVE", "DISCONNECTED", "SIMULATION"
        self.auto_reconnect = False

        # Robot Kinematic State
        self.q = list(HOME_JPOS)
        self.p = forward_kinematics_craig(self.q)
        self.op_state = 5  # IDLE
        self.op_state_name = "OP_IDLE (5)"
        self.is_moving = False
        self.status_msg = f"Connecting to Indy7 ({DEFAULT_ROBOT_IP})..."
        self.direct_teaching = False
        self.speed_ratio = 25

        # Workcell Pallet & Feeder State
        self.pallet_count = 0
        self.magazine_count = 8
        self.gripper_closed = False
        self.held_workpiece = False
        self.slots = self._init_slots()

        # Real / Virtual PLC I/O Registers
        self.pb1 = False
        self.pb2 = False
        self.stop_active = False
        self.mag_sensor = True
        self.do_gripper_open = True
        self.do_gripper_close = False

        # Raw signals
        self.raw_di: List[Dict] = []
        self.raw_do: List[Dict] = []

        # Waypoints storage
        self.waypoints: List[Dict] = self.load_waypoints()

        # Sequence execution tracking
        self.sequence_running = False
        self.active_program_name = "Idle"
        self.motion_phase = "IDLE"  # "APPROACH", "PLUNGE", "GRIP", "RELEASE", "EXTRACT"
        self.motion_angle = {"u": 0.0, "v": -180.0, "w": 0.0, "desc": "Neutral"}
        self.current_step_idx = 0
        self.total_steps = 0
        self.cycle_start_time = 0.0
        self.last_cycle_tact = 0.0

        # Worker thread controls
        self.active_thread: Optional[threading.Thread] = None
        self.abort_requested = False
        self.running = True

        # Jogging controls
        self.is_jogging: bool = False

        # Telemetry Hz diagnostics
        self.telemetry_hz_actual = 0.0
        self.last_telemetry_ts = 0.0

        self.startup_mode = startup_mode or STARTUP_MODE
        self.poll_thread = None
        self.fault = None
        self.events = []
        self.command_service = None
        self.commands = []
        self.simulation_driver = SimulationDriver(self, forward_kinematics_craig)
        self.switch_to_simulation()

    def start(self):
        """Start I/O only during application lifespan, never on import."""
        try:
            self.storage.reconcile_interrupted_runs()
        except Exception:
            logging.getLogger(__name__).exception("Failed to reconcile interrupted runs")
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self.running = True
        if self.startup_mode == "HARDWARE_LIVE":
            self.connect_hardware(self.robot_ip)
        self.poll_thread = threading.Thread(target=self._telemetry_worker, daemon=True)
        self.poll_thread.start()

    def close(self):
        self.running = False
        self.auto_reconnect = False
        self.set_stop(True)
        for thread in (self.active_thread, self.poll_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=3)
        try:
            self.storage.close()
        except Exception:
            pass

    @property
    def workcell_state(self):
        if self.fault:
            return "FAULTED"
        if self.stop_active or self.abort_requested:
            return "STOPPED"
        if self.mode == "DISCONNECTED":
            return "DISCONNECTED"
        if self.sequence_running or self.is_moving:
            return "RUNNING"
        return "IDLE"

    def raise_fault(self, code, message):
        with self.lock:
            if self.fault:
                return
            prev_state = self.workcell_state
            recovery_req = "Resolve cause, wait for motion to stop, then recover. Interrupted programs restart; they do not resume."
            self.fault = {"code": code, "message": message,
                          "timestamp": time.time(), "step": self.status_msg,
                          "recovery": recovery_req}
            self.events.append(dict(self.fault))
            self.events[:] = self.events[-100:]
            self.abort_requested = True
            self.status_msg = message
            logging.getLogger(__name__).warning("Workcell fault %s: %s", code, message)
            try:
                self.storage.record_fault(
                    code=code,
                    message=message,
                    interrupted_step=self.status_msg,
                    recovery_requirement=recovery_req,
                    run_id=self.active_run_id,
                )
                self.storage.record_state_transition(
                    from_state=prev_state,
                    to_state="FAULTED",
                    trigger=f"fault:{code}",
                    run_id=self.active_run_id,
                )
            except Exception:
                logging.getLogger(__name__).exception("Failed to record fault in storage")

    def inject_fault(self, code):
        with self.lock:
            if self.mode != "SIMULATION":
                raise ValueError("Fault injection is available only in simulation")
            if code not in {"FEEDER_EMPTY", "SENSOR_TIMEOUT", "MOTION_TIMEOUT", "CONNECTION_LOST"}:
                raise ValueError("Unknown fault code")
            if code == "FEEDER_EMPTY":
                self.mag_sensor = False
            self.raise_fault(code, f"Simulated {code.replace('_', ' ').lower()}")

    def _dwell(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.abort_requested or self.stop_active:
                return False
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        return True

    def _exec_step(self, cycle_id: Optional[str], step_name: str, step_idx: int, action_fn, details: Optional[Dict] = None) -> bool:
        step_id = None
        t0 = time.monotonic()
        if cycle_id:
            try:
                step_id = self.storage.start_step(cycle_id, step_name, step_idx, details)
            except Exception:
                pass
        outcome = False
        try:
            outcome = bool(action_fn())
        except Exception:
            logging.getLogger(__name__).exception("Step %s execution failed", step_name)
            outcome = False
        dur = round(time.monotonic() - t0, 3)
        status = "completed" if outcome else ("cancelled" if (self.stop_active or self.abort_requested) else "failed")
        if cycle_id and step_id:
            try:
                self.storage.finish_step(step_id, status=status, duration_seconds=dur, details=details)
            except Exception:
                pass
        return outcome

    def _launch_program(self, worker):
        with self.lock:
            if (self.sequence_running or self.is_moving or self.stop_active or self.fault
                    or (self.active_thread and self.active_thread.is_alive())):
                self.status_msg = "Program rejected: cell is busy or requires recovery"
                return False
            if self.mode == "DISCONNECTED" or self.abort_requested:
                self.status_msg = "Program rejected: reconnect or recover first"
                return False
            self.sequence_running = True
            cmd_id = self.current_command_id or str(uuid.uuid4())
            self.current_command_id = None
            self.active_run_id = cmd_id

            command = {"command_id": cmd_id, "status": "accepted",
                       "success": True, "submitted_at": time.time()}
            self.commands.append(command)
            self.commands[:] = self.commands[-100:]
            response = dict(command)

            def run():
                self.active_run_id = cmd_id
                with self.lock:
                    command.update(status="running", started_at=time.time())
                try:
                    outcome = worker()
                    if outcome is False and not (self.fault or self.stop_active or self.abort_requested):
                        self.raise_fault("PROGRAM_FAILED", "Program did not complete")
                except Exception:
                    logging.getLogger(__name__).exception("Program failed")
                    self.raise_fault("PROGRAM_FAILED", "Unexpected program failure; inspect server log")
                finally:
                    with self.lock:
                        status = "failed" if self.fault else "cancelled" if self.abort_requested or self.stop_active else "completed"
                        command.update(status=status, success=status == "completed",
                                       finished_at=time.time(), message=self.status_msg)
                        self.sequence_running = False
                        self.is_moving = False
                        self.active_program_name = "Idle"
                        self.motion_phase = "IDLE"

            self.active_thread = threading.Thread(target=run, daemon=True)
            self.active_thread.start()
            return response

    def _launch_jog(self, worker, step_name: str = ""):
        """Executes a jog step smoothly without latching program failures or blocking rapid hold ticks."""
        with self.lock:
            if self.fault or self.stop_active:
                self.status_msg = "Jog rejected: cell is faulted or stopped"
                return {"status": "rejected", "success": False, "message": self.status_msg}
            if self.sequence_running:
                self.status_msg = "Jog rejected: automated sequence is running"
                return {"status": "rejected", "success": False, "message": self.status_msg}

        # If previous jog step is active, wait briefly for it to settle smoothly
        t0 = time.time()
        while self.is_jogging and self.active_thread and self.active_thread.is_alive():
            if time.time() - t0 > 0.12:
                break
            time.sleep(0.01)

        cmd_id = self.current_command_id or str(uuid.uuid4())
        self.current_command_id = None

        command = {"command_id": cmd_id, "status": "accepted",
                   "success": True, "submitted_at": time.time()}
        with self.lock:
            self.is_jogging = True
            self.is_moving = True
            self.status_msg = step_name
            self.op_state = 6
            self.op_state_name = "OP_MOVING (6)"
            self.commands.append(command)
            self.commands[:] = self.commands[-100:]
        response = dict(command)

        def run():
            with self.lock:
                command.update(status="running", started_at=time.time())
            try:
                worker()
            except Exception as e:
                logging.getLogger(__name__).warning("Jog error: %s", e)
            finally:
                with self.lock:
                    status = "failed" if self.fault else "cancelled" if self.stop_active else "completed"
                    command.update(status=status, success=(status == "completed"),
                                   finished_at=time.time(), message=self.status_msg)
                    self.is_moving = False
                    self.is_jogging = False
                    if not (self.fault or self.stop_active):
                        self.op_state = 5
                        self.op_state_name = "OP_IDLE (5)"

        self.active_thread = threading.Thread(target=run, daemon=True)
        self.active_thread.start()
        return response

    def _init_slots(self) -> List[Dict]:
        from .kinematics import get_approach_pose
        slots = []
        for i in range(TOTAL_MAX_ITEMS):
            layer = i // SLOTS_PER_FLOOR
            slot_in_layer = i % SLOTS_PER_FLOOR
            r = slot_in_layer // GRID_X
            c = slot_in_layer % GRID_X

            x = DROP_BASE_LOCATION[0] - r * OFFSET_X
            y = DROP_BASE_LOCATION[1] + c * OFFSET_Y
            z = DROP_BASE_LOCATION[2] + layer * LAYER_HEIGHT
            target_pose = [x, y, z, *DROP_BASE_LOCATION[3:]]
            app_pose = get_approach_pose(target_pose, clearance=80.0)

            slots.append({
                "index": i,
                "floor": layer,
                "row": r,
                "col": c,
                "placed": False,
                "target_pose": target_pose,
                "approach_pose": [round(v, 2) for v in app_pose],
                "extract_pose": [round(v, 2) for v in app_pose],
                "angle": {
                    "u": DROP_BASE_LOCATION[3],
                    "v": DROP_BASE_LOCATION[4],
                    "w": DROP_BASE_LOCATION[5],
                    "tilt_deg": round(DROP_BASE_LOCATION[3], 2)
                }
            })
        return slots

    def load_waypoints(self) -> List[Dict]:
        if os.path.exists(WAYPOINTS_FILE):
            try:
                with open(WAYPOINTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return [
            {
                "id": "wp_home",
                "name": "Home Position",
                "move_type": "MoveJ",
                "q": [0.0, 0.0, -90.0, 0.0, -90.0, 0.0],
                "p": [350.0, -186.5, 522.0, 0.0, -180.0, 0.0],
                "speed": 25,
                "gripper": "keep",
                "dwell": 0.5
            }
        ]

    def save_waypoints(self, waypoints: List[Dict]) -> bool:
        try:
            with open(WAYPOINTS_FILE, "w", encoding="utf-8") as f:
                json.dump(waypoints, f, indent=2)
            with self.lock:
                self.waypoints = waypoints
            return True
        except Exception as e:
            print(f"[PalletizerEngine] Save waypoints error: {e}")
            return False

    def acquire_current_waypoint(self, name: str, move_type: str = "MoveJ", speed: int = 25, gripper: str = "keep", dwell: float = 0.5) -> Dict:
        with self.lock:
            q_cur = list(self.q)
            p_cur = list(self.p)
            wp_id = f"wp_{int(time.time() * 1000) % 1000000}"
            wp = {
                "id": wp_id,
                "name": name or f"Waypoint {len(self.waypoints) + 1}",
                "move_type": move_type,
                "q": q_cur,
                "p": p_cur,
                "speed": speed,
                "gripper": gripper,
                "dwell": dwell
            }
            self.waypoints.append(wp)
            self.save_waypoints(self.waypoints)
            self.status_msg = f"Acquired Waypoint: '{wp['name']}'"
            return wp

    def connect_hardware(self, ip: str = DEFAULT_ROBOT_IP) -> bool:
        if not HAS_NEUROMEKA:
            with self.lock:
                self.hardware_connected = False
                self.mode = "DISCONNECTED"
                self.status_msg = "Neuromeka library not installed"
            return False

        try:
            client = IndyDCP3(ip)
            r_data = client.get_robot_data()
            with self.lock:
                self.indy = client
                self.robot_ip = ip
                self.hardware_connected = True
                self.mode = "HARDWARE_LIVE"
                self.auto_reconnect = True
                if "q" in r_data and r_data["q"]:
                    self.q = [round(x, 2) for x in r_data["q"]]
                if "p" in r_data and r_data["p"]:
                    self.p = [round(x, 2) for x in r_data["p"]]
                self.op_state = r_data.get("op_state", 5)
                self.op_state_name = OP_STATE_NAMES.get(self.op_state, f"STATE_{self.op_state}")
                self.status_msg = f"Connected to Hardware Indy at {ip}"
            return True
        except Exception as e:
            with self.lock:
                self.indy = None
                self.hardware_connected = False
                if self.mode != "SIMULATION":
                    self.mode = "DISCONNECTED"
                self.status_msg = f"NO CONNECTION to Indy7 at {ip} ({e})"
            return False

    def disconnect_hardware(self):
        with self.lock:
            self.auto_reconnect = False
            self.indy = None
            self.hardware_connected = False
            self.mode = "DISCONNECTED"
            self.status_msg = f"Disconnected from robot ({self.robot_ip})"

    def switch_to_simulation(self):
        with self.lock:
            self.auto_reconnect = False
            self.indy = None
            self.hardware_connected = False
            self.mode = "SIMULATION"
            self.status_msg = "Switched to Autonomous Simulation Mode"

    def _telemetry_worker(self):
        """High-frequency background worker to poll robot hardware without blocking FastAPI."""
        last_time = time.time()
        poll_count = 0
        reconnect_interval = 2.0
        last_reconnect_attempt = 0.0

        while self.running:
            now = time.time()

            # Attempt auto-reconnect if needed
            if not self.hardware_connected and self.auto_reconnect and (self.mode == "DISCONNECTED"):
                if now - last_reconnect_attempt >= reconnect_interval:
                    last_reconnect_attempt = now
                    self.connect_hardware(self.robot_ip)

            # Poll hardware if connected
            if self.hardware_connected and self.indy:
                try:
                    r_data = self.indy.get_robot_data()
                    m_data = self.indy.get_motion_data()
                    di_data = self.indy.get_di()
                    do_data = self.indy.get_do()

                    with self.lock:
                        if "q" in r_data and r_data["q"]:
                            self.q = [round(x, 2) for x in r_data["q"]]
                        if "p" in r_data and r_data["p"]:
                            self.p = [round(x, 2) for x in r_data["p"]]

                        self.op_state = r_data.get("op_state", 5)
                        self.op_state_name = OP_STATE_NAMES.get(self.op_state, f"OP_{self.op_state}")
                        self.is_moving = m_data.get("is_in_motion", False)

                        # DI signals
                        di_signals = di_data.get("signals", []) if isinstance(di_data, dict) else di_data
                        self.raw_di = di_signals
                        di_dict = {s["address"]: s["state"] for s in di_signals if isinstance(s, dict) and "address" in s}

                        prev_pb1 = self.pb1
                        prev_pb2 = self.pb2
                        prev_stop = self.stop_active

                        if 3 in di_dict:
                            self.mag_sensor = (di_dict[3] == 1)
                        if 8 in di_dict:
                            self.pb1 = (di_dict[8] == 1)
                        if 9 in di_dict:
                            self.pb2 = (di_dict[9] == 1)
                        if 15 in di_dict:
                            self.stop_active = (di_dict[15] == 1)

                        # Physical PLC push button rising edge triggers
                        if self.pb1 and not prev_pb1:
                            if not self.sequence_running and not self.is_moving:
                                self.command_service.execute("pb1") if self.command_service else self.trigger_pb1_palletize()
                        if self.pb2 and not prev_pb2:
                            if not self.sequence_running and not self.is_moving:
                                self.command_service.execute("pb2") if self.command_service else self.trigger_pb2_put_back()
                        if self.stop_active and not prev_stop:
                            self.set_stop(True)

                        # DO signals
                        do_signals = do_data.get("signals", []) if isinstance(do_data, dict) else do_data
                        self.raw_do = do_signals
                        do_dict = {s["address"]: s["state"] for s in do_signals if isinstance(s, dict) and "address" in s}

                        if 0 in do_dict:
                            self.do_gripper_open = (do_dict[0] == 1)
                        if 1 in do_dict:
                            self.do_gripper_close = (do_dict[1] == 1)

                        self.gripper_closed = self.do_gripper_close or (not self.do_gripper_open)

                        poll_count += 1
                        self.last_telemetry_ts = now
                        if now - last_time >= 1.0:
                            self.telemetry_hz_actual = round(poll_count / (now - last_time), 1)
                            poll_count = 0
                            last_time = now

                except Exception as e:
                    with self.lock:
                        self.hardware_connected = False
                        self.indy = None
                        if self.mode == "HARDWARE_LIVE":
                            self.mode = "DISCONNECTED"
                        self.auto_reconnect = False
                        self.raise_fault("CONNECTION_LOST", f"Lost connection to Indy7: {e}")

            time.sleep(0.033)

    # =========================================================================
    # TEACH PENDANT CONTROLS (JOGGING & TOOLS)
    # =========================================================================
    def jog_joint(self, joint_idx: int, step_deg: float, vel_ratio: Optional[int] = None) -> Any:
        """Jogs a single joint incrementally (+/- step_deg) with limit clamping and smooth interpolation."""
        vel = vel_ratio or self.speed_ratio
        if not (0 <= joint_idx <= 5):
            return {"status": "rejected", "success": False, "message": "Invalid joint index"}

        joint_limits = [
            (-175.0, 175.0),
            (-175.0, 175.0),
            (-175.0, 175.0),
            (-175.0, 175.0),
            (-175.0, 175.0),
            (-215.0, 215.0),
        ]
        min_lim, max_lim = joint_limits[joint_idx]

        with self.lock:
            if self.hardware_connected and self.indy:
                offset = [0.0] * 6
                offset[joint_idx] = step_deg
                try:
                    self.indy.movej(
                        jtarget=offset,
                        base_type=JointBaseType.RELATIVE,
                        vel_ratio=vel,
                        acc_ratio=vel,
                        teaching_mode=True
                    )
                    self.status_msg = f"Jog J{joint_idx+1}: {step_deg:+.1f}°"
                    return {"status": "accepted", "success": True, "message": self.status_msg}
                except Exception as e:
                    self.status_msg = f"Jog J{joint_idx+1} Error: {e}"
                    return {"status": "rejected", "success": False, "message": self.status_msg}
            else:
                # Simulation Jog with limit protection and smooth interpolation
                cur_val = self.q[joint_idx]
                target_val = round(cur_val + step_deg, 2)
                if target_val < min_lim or target_val > max_lim:
                    target_val = max(min_lim, min(max_lim, target_val))
                    if abs(target_val - cur_val) < 0.01:
                        self.status_msg = f"Jog J{joint_idx+1} limit reached ({target_val:+.1f}°)"
                        return {"status": "rejected", "success": False, "message": self.status_msg}

                target_q = list(self.q)
                target_q[joint_idx] = target_val
                step_name = f"[SIM] Jog J{joint_idx+1} -> {target_val:+.1f}°"

        return self._launch_jog(lambda: self._sim_move(target_q, 0.10, step_name), step_name)

    def jog_task(self, axis: str, step_val: float, vel_ratio: Optional[int] = None) -> Any:
        """Jogs the TCP in Cartesian coordinates along axis ('x', 'y', 'z', 'u', 'v', 'w')."""
        axis_map = {"x": 0, "y": 1, "z": 2, "u": 3, "v": 4, "w": 5}
        ax = axis.lower()
        if ax not in axis_map:
            return {"status": "rejected", "success": False, "message": f"Invalid axis: {axis}"}
        idx = axis_map[ax]
        vel = vel_ratio or self.speed_ratio

        with self.lock:
            if self.hardware_connected and self.indy:
                offset = [0.0] * 6
                offset[idx] = step_val
                try:
                    self.indy.movel(
                        ttarget=offset,
                        base_type=TaskBaseType.RELATIVE,
                        vel_ratio=vel,
                        acc_ratio=vel,
                        teaching_mode=True
                    )
                    unit = "mm" if idx < 3 else "°"
                    self.status_msg = f"Jog {ax.upper()}: {step_val:+.1f}{unit}"
                    return {"status": "accepted", "success": True, "message": self.status_msg}
                except Exception as e:
                    self.status_msg = f"Jog {ax.upper()} Error: {e}"
                    return {"status": "rejected", "success": False, "message": self.status_msg}
            else:
                target = list(self.p)
                target[idx] = round(target[idx] + step_val, 2)
                unit = "mm" if idx < 3 else "°"
                step_name = f"[SIM] Jog {ax.upper()}: {step_val:+.1f}{unit}"

        return self._launch_jog(lambda: self._sim_cartesian_move(
            target, 0.10, step_name), step_name)

    def stop_jog(self):
        """Immediately halts jogging without latching an emergency fault or requiring recovery."""
        with self.lock:
            self.is_jogging = False
            self.is_moving = False
            if not (self.fault or self.stop_active):
                self.abort_requested = False
                self.op_state = 5
                self.op_state_name = "OP_IDLE (5)"
            if self.hardware_connected and self.indy:
                try:
                    self.indy.stop_motion(StopCategory.CAT0)
                except Exception:
                    pass
            self.status_msg = "Jog Stopped"

        if self.active_thread and self.active_thread.is_alive():
            self.active_thread.join(timeout=0.20)

        return {"status": "completed", "success": True, "message": "Jog Stopped"}

    def set_direct_teaching(self, enable: bool) -> bool:
        """Enables/disables physical Zero-G direct teaching."""
        with self.lock:
            self.direct_teaching = enable
            if self.hardware_connected and self.indy:
                try:
                    self.indy.set_direct_teaching(enable)
                    state_str = "ENABLED (Free-Drive)" if enable else "LOCKED (Motor Brake)"
                    self.status_msg = f"Direct Teaching {state_str}"
                    return True
                except Exception as e:
                    self.status_msg = f"Direct Teaching Error: {e}"
                    return False
            else:
                state_str = "ENABLED (Simulated)" if enable else "DISABLED"
                self.status_msg = f"Direct Teaching {state_str}"
                return True

    def set_gripper(self, close: bool) -> bool:
        """Actuates the pneumatic gripper."""
        with self.lock:
            self.gripper_closed = close
            if close:
                self.do_gripper_open = False
                self.do_gripper_close = True
            else:
                self.do_gripper_open = True
                self.do_gripper_close = False

            if self.hardware_connected and self.indy:
                try:
                    # DO0: Open, DO1: Close
                    if close:
                        self.indy.set_do([{"address": 0, "state": 0}, {"address": 1, "state": 1}])
                    else:
                        self.indy.set_do([{"address": 0, "state": 1}, {"address": 1, "state": 0}])
                    self.status_msg = f"Gripper {'CLOSED (DO1)' if close else 'OPEN (DO0)'}"
                    return True
                except Exception as e:
                    self.status_msg = f"Gripper Error: {e}"
                    return False
            else:
                self.status_msg = f"[SIM] Gripper {'CLOSED' if close else 'OPEN'}"
                return True

    # =========================================================================
    # MULTI-PURPOSE PROGRAMS (HOME, ZERO, RECOVER, WAYPOINTS)
    # =========================================================================
    def move_home(self):
        return self._launch_program(self._execute_move_home)

    def move_zero(self):
        return self._launch_program(lambda: self._execute_joint_move([0.0] * 6, 20, "Moving to Zero"))

    def recover_robot(self) -> bool:
        """Acknowledge a resolved fault; never resume an interrupted sequence."""
        with self.lock:
            if self.sequence_running or self.is_moving or (self.active_thread and self.active_thread.is_alive()):
                self.status_msg = "Wait for the active program to finish stopping"
                return False
            if self.held_workpiece:
                self.status_msg = "Reconcile the held workpiece before recovery (reset the simulated cell)"
                return False
            if self.mode == "SIMULATION" and self.fault and self.fault["code"] == "FEEDER_EMPTY" and not self.mag_sensor:
                self.status_msg = "Restore feeder sensor before recovery (click 'Refill Feeder' or 'Reset Cell')"
                return False
            if self.mode == "DISCONNECTED":
                self.status_msg = "Reconnect equipment before recovery"
                return False
            prev_state = self.workcell_state
            success = False
            if self.hardware_connected and self.indy:
                try:
                    self.indy.recover()
                    self.stop_active = False
                    self.abort_requested = False
                    self.fault = None
                    self.status_msg = "Robot Recovered & Fault Cleared"
                    success = True
                except Exception as e:
                    self.status_msg = f"Recover Error: {e}"
                    return False
            else:
                self.stop_active = False
                self.abort_requested = False
                self.fault = None
                self.op_state = 5
                self.op_state_name = "OP_IDLE (5)"
                self.status_msg = "[SIM] System Reset / Ready"
                success = True

            if success:
                try:
                    self.storage.resolve_faults()
                    self.storage.record_state_transition(
                        from_state=prev_state,
                        to_state=self.workcell_state,
                        trigger="recover",
                        run_id=self.active_run_id,
                    )
                    self.storage.record_operator_action("RECOVER")
                except Exception:
                    logging.getLogger(__name__).exception("Failed to record recovery in storage")
            return success

    def move_to_waypoint(self, wp_id):
        from copy import deepcopy
        waypoint = next((deepcopy(w) for w in self.waypoints if w["id"] == wp_id), None)
        if waypoint is None:
            self.status_msg = "Waypoint not found"
            return False
        return self._launch_program(lambda: self._execute_waypoint(waypoint))

    def _execute_waypoint(self, waypoint):
        if self.abort_requested or self.stop_active:
            return False
        name = waypoint.get("name", "Waypoint")
        speed = waypoint.get("speed", 25)
        self.active_program_name = name
        if waypoint.get("move_type") == "MoveL" and waypoint.get("p"):
            success = self._execute_cartesian_move(waypoint["p"], speed, speed, name)
        else:
            success = self._execute_joint_move(waypoint.get("q", HOME_JPOS), speed, name)
        if not success or self.abort_requested or self.stop_active:
            return False
        action = waypoint.get("gripper", "keep")
        if action in ("open", "close") and not self.set_gripper(action == "close"):
            return False
        return self._dwell(waypoint.get("dwell", 0))

    def start_waypoint_sequence(self, repeat_count=1):
        from copy import deepcopy
        waypoints = deepcopy(self.waypoints)
        if not waypoints:
            self.status_msg = "No waypoints saved to execute"
            return False

        def worker():
            self.cycle_start_time = time.time()
            self.total_steps = len(waypoints) * repeat_count
            self.current_step_idx = 0
            cycle = 0
            while repeat_count == 0 or cycle < repeat_count:
                for waypoint in waypoints:
                    self.current_step_idx += 1
                    if not self._execute_waypoint(waypoint):
                        return False
                cycle += 1
            self.last_cycle_tact = round(time.time() - self.cycle_start_time, 2)
            self.status_msg = f"Sequence finished in {self.last_cycle_tact}s"
            return True

        return self._launch_program(worker)

    def _wait_hardware_motion(self, timeout: float = 30.0) -> bool:
        """Polls until physical robot has finished moving using 2-phase verification."""
        if not self.indy:
            self.raise_fault("CONNECTION_LOST", "Robot connection unavailable")
            return False
        start_time = time.time()

        # Phase 1: Wait for motion to register and start (up to 0.5s)
        while time.time() - start_time < 0.5:
            if self.abort_requested or self.stop_active:
                try:
                    self.indy.stop_motion(StopCategory.CAT1)
                except Exception:
                    pass
                return False
            try:
                m_data = self.indy.get_motion_data()
                if m_data.get("is_in_motion", False):
                    break
            except Exception:
                pass
            time.sleep(0.02)

        # Phase 2: Wait for motion to finish and settle in IDLE
        while time.time() - start_time < timeout:
            if self.abort_requested or self.stop_active:
                try:
                    self.indy.stop_motion(StopCategory.CAT1)
                except Exception:
                    pass
                return False
            try:
                m_data = self.indy.get_motion_data()
                r_data = self.indy.get_robot_data()
                is_in_motion = m_data.get("is_in_motion", False)
                op_state = r_data.get("op_state", 5)

                if op_state in [2, 3, 4, 8, 9, 15]:
                    self.raise_fault("ROBOT_STOP", f"Robot stop reported: OpState={op_state}")
                    return False

                if not is_in_motion and op_state == 5:
                    return True
            except Exception:
                pass
            time.sleep(0.04)

        self.raise_fault("MOTION_TIMEOUT", "Robot motion did not complete before timeout")
        return False

    def get_pallet_slot_pose(self, index: int, recipe_params: Optional[Dict[str, Any]] = None) -> List[float]:
        """Computes target pose for item `index` matching pallet recipe geometry."""
        params = recipe_params or self.active_order_snapshot or {}
        grid_x = params.get("grid_x", GRID_X)
        grid_y = params.get("grid_y", GRID_Y)
        slots_per_floor = params.get("slots_per_floor", grid_x * grid_y)
        offset_x = params.get("offset_x", OFFSET_X)
        offset_y = params.get("offset_y", OFFSET_Y)
        layer_height = params.get("layer_height", LAYER_HEIGHT)

        layer = index // slots_per_floor
        slot_in_layer = index % slots_per_floor
        row = slot_in_layer // grid_x
        col = slot_in_layer % grid_x

        base_x, base_y, base_z = DROP_BASE_LOCATION[0], DROP_BASE_LOCATION[1], DROP_BASE_LOCATION[2]
        rot = DROP_BASE_LOCATION[3:]

        x = base_x - (row * offset_x)
        y = base_y + (col * offset_y)
        z = base_z + (layer * layer_height)

        return [round(x, 2), round(y, 2), round(z, 2), *rot]

    def _reconfigure_slots_from_recipe(self, params: Optional[Dict[str, Any]] = None) -> None:
        """Reconfigures virtual pallet slots to match the specified recipe parameters."""
        if not params:
            return
        from .kinematics import get_approach_pose
        grid_x = params.get("grid_x", GRID_X)
        grid_y = params.get("grid_y", GRID_Y)
        num_floors = params.get("num_floors", NUM_FLOORS)
        slots_per_floor = grid_x * grid_y
        total_slots = params.get("total_slots", slots_per_floor * num_floors)
        offset_x = params.get("offset_x", OFFSET_X)
        offset_y = params.get("offset_y", OFFSET_Y)
        layer_height = params.get("layer_height", LAYER_HEIGHT)
        clearance = params.get("approach_clearance_z", APPROACH_CLEARANCE_Z)

        slots = []
        for i in range(total_slots):
            layer = i // slots_per_floor
            slot_in_layer = i % slots_per_floor
            r = slot_in_layer // grid_x
            c = slot_in_layer % grid_x

            x = DROP_BASE_LOCATION[0] - r * offset_x
            y = DROP_BASE_LOCATION[1] + c * offset_y
            z = DROP_BASE_LOCATION[2] + layer * layer_height
            target_pose = [round(x, 2), round(y, 2), round(z, 2), *DROP_BASE_LOCATION[3:]]
            app_pose = get_approach_pose(target_pose, clearance=clearance)

            placed = i < self.pallet_count

            slots.append({
                "index": i,
                "floor": layer,
                "row": r,
                "col": c,
                "placed": placed,
                "target_pose": target_pose,
                "approach_pose": [round(v, 2) for v in app_pose],
                "extract_pose": [round(v, 2) for v in app_pose],
                "angle": {
                    "u": DROP_BASE_LOCATION[3],
                    "v": DROP_BASE_LOCATION[4],
                    "w": DROP_BASE_LOCATION[5],
                    "tilt_deg": round(DROP_BASE_LOCATION[3], 2)
                }
            })
        self.slots = slots


    def get_approach_pose(self, target_pose: List[float], clearance: float = APPROACH_CLEARANCE_Z) -> List[float]:
        """Computes collinear approach/extract pose backed off along tool TCP Z-axis vector."""
        if self.hardware_connected and self.indy:
            try:
                res = self.indy.calculate_current_pose_rel(
                    current_pos=list(target_pose),
                    relative_pos=[0.0, 0.0, -clearance, 0.0, 0.0, 0.0],
                    base_type=TaskBaseType.TCP,
                )
                if isinstance(res, dict) and "calculated_pos" in res:
                    return [round(v, 2) for v in res["calculated_pos"]]
            except Exception:
                pass

        # Direct kinematic calculation:
        u = math.radians(target_pose[3])
        v = math.radians(target_pose[4])
        w = math.radians(target_pose[5])

        cu, su = math.cos(u), math.sin(u)
        cv, sv = math.cos(v), math.sin(v)
        cw, sw = math.cos(w), math.sin(w)

        zx = cw * sv * cu + sw * su
        zy = sw * sv * cu - cw * sw
        zz = cv * cu

        return [
            round(target_pose[0] - clearance * zx, 2),
            round(target_pose[1] - clearance * zy, 2),
            round(target_pose[2] - clearance * zz, 2),
            target_pose[3],
            target_pose[4],
            target_pose[5],
        ]

    def _execute_cartesian_move(self, pose, vel_ratio=None, acc_ratio=None, step_name=""):
        return self._driver_motion("move_cartesian", pose, vel_ratio or self.speed_ratio,
                                   acc_ratio or self.speed_ratio, step_name)

    def _execute_move_home(self):
        return self._driver_motion("home")

    def _driver_motion(self, operation, *args):
        if self.abort_requested or self.stop_active or self.fault:
            return False
        if self.mode == "SIMULATION":
            driver = self.simulation_driver
        elif self.hardware_connected and self.indy:
            driver = HardwareDriver(self, TaskBaseType.ABSOLUTE)
        else:
            self.raise_fault("CONNECTION_LOST", "Robot connection unavailable")
            return False
        self.is_moving = True
        try:
            result = getattr(driver, operation)(*args)
            if not result and not (self.fault or self.abort_requested or self.stop_active):
                self.raise_fault("MOTION_FAILED", f"{operation} did not complete")
            return result
        except Exception:
            logging.getLogger(__name__).exception("Motion failed: %s", operation)
            self.raise_fault("MOTION_FAILED", f"{operation} failed; inspect server log")
            return False
        finally:
            self.is_moving = False

    def _execute_joint_move(self, q_target, vel_ratio=None, step_name=""):
        return self._driver_motion("move_joint", q_target, vel_ratio or self.speed_ratio, step_name)

    def _sim_cartesian_move(self, p_target, duration, step_name):
        return self.simulation_driver._sim_cartesian_move(p_target, duration, step_name)

    def set_stop(self, active: bool = True):
        with self.lock:
            prev_state = self.workcell_state
            self.stop_active = active
            if active:
                self.abort_requested = True
                self.status_msg = "Application stop requested"
                if self.hardware_connected and self.indy:
                    try:
                        self.indy.stop_motion(StopCategory.CAT2)
                    except Exception:
                        pass
            else:
                self.status_msg = "Stop released; use Recover before restarting"
            try:
                self.storage.record_operator_action("STOP", {"active": active})
                new_state = self.workcell_state
                if new_state != prev_state:
                    self.storage.record_state_transition(
                        from_state=prev_state,
                        to_state=new_state,
                        trigger="stop:active" if active else "stop:release",
                        run_id=self.active_run_id,
                    )
            except Exception:
                logging.getLogger(__name__).exception("Failed to record stop in storage")

    def trigger_pb1_palletize(self):
        with self.lock:
            if self.is_moving or self.sequence_running or self.stop_active or self.fault:
                return False
            if self.pallet_count >= TOTAL_MAX_ITEMS:
                self.status_msg = "Pallet is already full (8/8)!"
                return False
            if self.magazine_count <= 0 or not self.mag_sensor:
                self.raise_fault("FEEDER_EMPTY", "Magazine is empty! Cannot pick.")
                return False

            return self._launch_program(self._run_palletize_sequence)

    def trigger_pb2_put_back(self):
        with self.lock:
            if self.is_moving or self.sequence_running or self.stop_active or self.fault:
                return False
            if self.pallet_count <= 0:
                self.status_msg = "Pallet is empty! Nothing to put back."
                return False

            return self._launch_program(self._run_put_back_sequence)

    def start_work_order(self, order_id: str) -> Dict[str, Any]:
        """Starts executing a persisted work order using its immutable recipe snapshot."""
        with self.lock:
            if self.is_moving or self.sequence_running or self.stop_active or self.fault:
                return {"success": False, "message": "Cell is busy or requires recovery"}
            wo = self.storage.get_work_order(order_id)
            if not wo:
                return {"success": False, "message": f"Work order {order_id} not found"}
            if wo["status"] in ("completed", "cancelled"):
                return {"success": False, "message": f"Work order is already {wo['status']}"}

            self.active_order_id = wo["order_id"]
            self.active_order_number = wo["order_number"]
            self.active_order_snapshot = wo.get("recipe_snapshot", {})
            self.order_target_quantity = wo["target_quantity"]
            self.order_completed_quantity = wo["completed_quantity"]
            self.current_pallet_index = wo["current_pallet_index"]
            self.pallet_change_required = False

            # Set recipe version for reporting
            self.recipe_id = wo["recipe_id"]
            self.recipe_version = wo["recipe_version"]

            # Reconfigure virtual slots geometry to match recipe parameters
            self._reconfigure_slots_from_recipe(self.active_order_snapshot)

            self.storage.start_work_order(self.active_order_id)
            self.storage.record_operator_action("START_WORK_ORDER", {
                "order_id": self.active_order_id,
                "order_number": self.active_order_number,
                "target_quantity": self.order_target_quantity,
            })

            # Check if current pallet is full and requires swap first
            total_slots = len(self.slots) or TOTAL_MAX_ITEMS
            if self.pallet_count >= total_slots and self.order_completed_quantity < self.order_target_quantity:
                self.pallet_change_required = True
                self.motion_phase = "WAITING_PALLET_CHANGE"
                self.status_msg = f"Pallet #{self.current_pallet_index} is FULL. Please swap pallet to continue."
                return {"success": True, "message": "Pallet swap required", "pallet_change_required": True}

            # In simulation, ensure feeder has parts
            if self.mode == "SIMULATION" and (self.magazine_count <= 0 or not self.mag_sensor):
                self.magazine_count = max(8, len(self.slots))
                self.mag_sensor = True

            res = self._launch_program(self._run_palletize_sequence)
            cmd_id = res.get("command_id") if isinstance(res, dict) else None
            return {"success": res is not False, "order_id": self.active_order_id, "command_id": cmd_id}

    def confirm_pallet_swap(self) -> Dict[str, Any]:
        """Operator action: clears the full pallet, refills feeder in simulation, and resumes work order."""
        with self.lock:
            # Clear virtual pallet
            self.pallet_count = 0
            for s in self.slots:
                s["placed"] = False
            self.held_workpiece = False

            # In simulation, refill feeder so production continues
            if self.mode == "SIMULATION":
                self.magazine_count = max(8, len(self.slots))
                self.mag_sensor = True

            order_id = self.active_order_id
            if order_id:
                new_pallet_idx = self.storage.swap_work_order_pallet(order_id)
                self.current_pallet_index = new_pallet_idx
            else:
                self.current_pallet_index += 1

            self.pallet_change_required = False
            self.status_msg = f"Pallet swapped! Ready for Pallet #{self.current_pallet_index}."
            self.storage.record_operator_action("CONFIRM_PALLET_SWAP", {
                "order_id": order_id,
                "new_pallet_index": self.current_pallet_index,
            })

            # If work order has remaining parts, automatically resume palletizing
            if order_id and self.order_completed_quantity < self.order_target_quantity:
                self.status_msg = f"Resuming Work Order for Pallet #{self.current_pallet_index}..."
                res = self._launch_program(self._run_palletize_sequence)
                cmd_id = res.get("command_id") if isinstance(res, dict) else None
                return {"success": True, "current_pallet_index": self.current_pallet_index, "resumed": True, "command_id": cmd_id}

            return {"success": True, "current_pallet_index": self.current_pallet_index, "resumed": False}

    def cancel_active_work_order(self) -> Dict[str, Any]:
        """Operator action: cancels active work order and halts motion safely."""
        with self.lock:
            if not self.active_order_id:
                return {"success": False, "message": "No active work order"}
            order_id = self.active_order_id
            self.storage.cancel_work_order(order_id)
            self.storage.record_operator_action("CANCEL_WORK_ORDER", {"order_id": order_id})
            self.active_order_id = None
            self.pallet_change_required = False
            self.set_stop(True)
            self.status_msg = f"Work Order {order_id} cancelled by operator"
            return {"success": True, "order_id": order_id}

    def run_benchmark_experiment(
        self,
        name: Optional[str] = None,
        baseline_recipe_id: str = "pallet-2x2x2-default",
        candidate_recipe_id: str = "pallet-high-speed",
        trials: int = 5,
        fast_mode: bool = True,
    ) -> Dict[str, Any]:
        """Executes a cycle time and kinetic benchmark experiment between two recipes."""
        trials = max(2, min(50, int(trials)))
        bench = self.storage.run_fast_benchmark(
            baseline_recipe_id=baseline_recipe_id,
            candidate_recipe_id=candidate_recipe_id,
            trials=trials,
            name=name,
        )
        self.storage.record_operator_action("RUN_BENCHMARK", {
            "benchmark_id": bench["benchmark_id"],
            "baseline": baseline_recipe_id,
            "candidate": candidate_recipe_id,
            "trials": trials,
            "fast_mode": fast_mode,
        })
        return bench

    def _sim_move(self, q_target, duration, step_name):
        return self.simulation_driver._sim_move(q_target, duration, step_name)

    def _run_palletize_sequence(self):
        """Executes palletizing routine using calibrated/recipe parameters and records step events."""
        self.cycle_start_time = time.time()
        t_run_start = time.monotonic()
        run_id = self.active_run_id or str(uuid.uuid4())
        self.active_run_id = run_id

        # Determine if running under a Work Order or standard manual PB1
        order_id = self.active_order_id
        recipe_params = self.active_order_snapshot if order_id else {}
        slots_per_floor = recipe_params.get("slots_per_floor", SLOTS_PER_FLOOR)
        total_slots = recipe_params.get("total_slots", len(self.slots) or TOTAL_MAX_ITEMS)
        transit_vel = recipe_params.get("transit_vel_ratio", TRANSIT_VEL_RATIO)
        transit_acc = recipe_params.get("transit_acc_ratio", TRANSIT_ACC_RATIO)
        action_vel = recipe_params.get("action_vel_ratio", ACTION_VEL_RATIO)
        action_acc = recipe_params.get("action_acc_ratio", ACTION_ACC_RATIO)
        clearance = recipe_params.get("approach_clearance_z", APPROACH_CLEARANCE_Z)
        dwell = recipe_params.get("gripper_dwell_sec", GRIPPER_DWELL_SEC)
        cmd_name = "work_order" if order_id else "pb1"

        try:
            self.storage.start_run(
                run_id=run_id,
                command=cmd_name,
                origin=self.mode,
                recipe_id=self.recipe_id,
                recipe_version=self.recipe_version,
                target_count=total_slots,
                order_id=order_id,
            )
            self.storage.record_state_transition("IDLE", "RUNNING", cmd_name, run_id=run_id)
        except Exception:
            logging.getLogger(__name__).exception("Failed to start run in storage")

        with self.lock:
            self.sequence_running = True
            prog_name = f"Work Order {self.active_order_number or order_id}" if order_id else "Palletizing Loop (8 Slots)"
            self.active_program_name = prog_name

        pick_approach = self.get_approach_pose(PICK_LOCATION, clearance=clearance)

        cycle_aborted = False
        pallet_swap_paused = False

        while True:
            if self.abort_requested or self.stop_active:
                cycle_aborted = True
                break

            # If work order active, check if target quantity has already been reached
            if order_id and self.order_completed_quantity >= self.order_target_quantity:
                break

            # Check if current pallet is full
            if self.pallet_count >= total_slots:
                if order_id and self.order_completed_quantity < self.order_target_quantity:
                    pallet_swap_paused = True
                    break
                else:
                    break

            if self.magazine_count <= 0 or not self.mag_sensor:
                self.raise_fault("FEEDER_EMPTY", f"Feeder empty after {self.pallet_count} items")
                cycle_aborted = True
                break

            i = self.pallet_count
            slot_pose = self.get_pallet_slot_pose(i, recipe_params)
            drop_approach = self.get_approach_pose(slot_pose, clearance=clearance)
            floor = i // slots_per_floor

            cycle_id = None
            t_cycle_start = time.monotonic()
            try:
                cycle_id = self.storage.start_cycle(run_id=run_id, cycle_index=i, target_slot=i)
                self.active_cycle_id = cycle_id
            except Exception:
                pass

            cycle_failed = False

            # -------------------------------------------------------------
            # STEP 1: Feeder Pick Approach
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Approach (Tilt: {PICK_LOCATION[3]}°)"}
            if not self._exec_step(cycle_id, "PICK_APPROACH", 1, lambda: (
                self.set_gripper(False) and
                self._execute_cartesian_move(pick_approach, vel_ratio=transit_vel, acc_ratio=transit_acc,
                                             step_name=f"[Item {i+1}] Feeder Approach MoveL (Clearance {clearance}mm @ {PICK_LOCATION[3]}°)")
            )):
                cycle_failed = True

            # -------------------------------------------------------------
            # STEP 2: Feeder Pick Plunge
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "PLUNGE"
                self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Plunge (Collinear @ {PICK_LOCATION[3]}°)"}
                if not self._exec_step(cycle_id, "PICK_PLUNGE", 2, lambda: (
                    self._execute_cartesian_move(PICK_LOCATION, vel_ratio=action_vel, acc_ratio=action_acc,
                                                 step_name=f"[Item {i+1}] Feeder Pick Plunge MoveL (Collinear @ {PICK_LOCATION[3]}°)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 3: Grip Billet
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "GRIP"
                self.status_msg = f"[Item {i+1}] Gripping Billet (DO1=ON)"
                def do_grip():
                    if not self.set_gripper(True):
                        return False
                    self._dwell(dwell)
                    if self.abort_requested or self.stop_active:
                        return False
                    self.held_workpiece = True
                    self.magazine_count = max(0, self.magazine_count - 1)
                    if self.magazine_count == 0:
                        self.mag_sensor = False
                    return True
                if not self._exec_step(cycle_id, "PICK_GRIP", 3, do_grip):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 4: Feeder Pick Extract
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "EXTRACT"
                self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Extract MoveL (Collinear @ {PICK_LOCATION[3]}°)"}
                if not self._exec_step(cycle_id, "PICK_EXTRACT", 4, lambda: (
                    self._execute_cartesian_move(pick_approach, vel_ratio=action_vel, acc_ratio=action_acc,
                                                 step_name=f"[Item {i+1}] Feeder Pick Extract MoveL (Clearance {clearance}mm @ {PICK_LOCATION[3]}°)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 5: Pallet Slot Place Approach
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "APPROACH"
                self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Approach (Floor {floor}) @ {slot_pose[3]}°"}
                if not self._exec_step(cycle_id, "PLACE_APPROACH", 5, lambda: (
                    self._execute_cartesian_move(drop_approach, vel_ratio=transit_vel, acc_ratio=transit_acc,
                                                 step_name=f"[Item {i+1}] Pallet Slot {i+1} Approach MoveL (Floor {floor}, Clearance {clearance}mm)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 6: Pallet Slot Place Plunge
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "PLUNGE"
                self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Place Plunge (Z={slot_pose[2]}mm)"}
                if not self._exec_step(cycle_id, "PLACE_PLUNGE", 6, lambda: (
                    self._execute_cartesian_move(slot_pose, vel_ratio=action_vel, acc_ratio=action_acc,
                                                 step_name=f"[Item {i+1}] Lowering to Slot {i+1} MoveL (Z={slot_pose[2]}mm)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 7: Release Billet
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "RELEASE"
                self.status_msg = f"[Item {i+1}] Releasing into Slot {i+1} (DO0=ON)"
                def do_release():
                    if not self.set_gripper(False):
                        return False
                    self._dwell(dwell)
                    if self.abort_requested or self.stop_active:
                        return False
                    self.held_workpiece = False
                    if i < len(self.slots):
                        self.slots[i]["placed"] = True
                    self.pallet_count += 1
                    return True
                if not self._exec_step(cycle_id, "PLACE_RELEASE", 7, do_release):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 8: Pallet Slot Place Extract
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "EXTRACT"
                self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Extract Retract MoveL @ {slot_pose[3]}°"}
                if not self._exec_step(cycle_id, "PLACE_EXTRACT", 8, lambda: (
                    self._execute_cartesian_move(drop_approach, vel_ratio=action_vel, acc_ratio=action_acc,
                                                 step_name=f"[Item {i+1}] Slot {i+1} Extract Retract MoveL (Clearance {clearance}mm)")
                )):
                    cycle_failed = True

            cycle_dur = round(time.monotonic() - t_cycle_start, 3)
            c_status = "completed" if not cycle_failed else ("cancelled" if (self.stop_active or self.abort_requested) else "failed")
            if cycle_id:
                try:
                    self.storage.finish_cycle(cycle_id, status=c_status, duration_seconds=cycle_dur)
                except Exception:
                    pass

            if not cycle_failed:
                # Record part traceability
                grid_x = recipe_params.get("grid_x", GRID_X)
                slot_floor = floor
                slot_row = (i % slots_per_floor) // grid_x
                slot_col = (i % slots_per_floor) % grid_x
                if order_id:
                    onum = self.active_order_number or order_id
                    part_serial = f"{onum}-P{self.current_pallet_index}-S{i+1:02d}"
                    try:
                        self.storage.record_workpiece_placed(
                            order_id=order_id,
                            run_id=run_id,
                            cycle_id=cycle_id,
                            part_serial=part_serial,
                            pallet_index=self.current_pallet_index,
                            slot_index=i,
                            slot_floor=slot_floor,
                            slot_row=slot_row,
                            slot_col=slot_col,
                            cycle_duration=cycle_dur,
                        )
                    except Exception:
                        pass
                    self.order_completed_quantity += 1
                else:
                    self.order_completed_quantity += 1

            if cycle_failed or self.abort_requested or self.stop_active:
                cycle_aborted = True
                break

        run_dur = round(time.monotonic() - t_run_start, 3)

        if pallet_swap_paused:
            # Pallet is full, more parts remain in order -> park at HOME and wait for swap
            self.motion_phase = "WAITING_PALLET_CHANGE"
            self.pallet_change_required = True
            self._execute_move_home()
            self.status_msg = (
                f"Pallet #{self.current_pallet_index} FULL ({self.pallet_count}/{total_slots}). "
                f"Pallet swap required to continue Work Order ({self.order_completed_quantity}/{self.order_target_quantity})."
            )
            try:
                self.storage.pause_work_order_for_pallet_change(order_id)
                self.storage.finish_run(run_id, status="completed", completed_count=self.pallet_count, duration_seconds=run_dur)
                self.storage.record_state_transition("RUNNING", "WAITING", "PALLET_FULL_NEED_SWAP", run_id=run_id)
            except Exception:
                pass
            with self.lock:
                self.sequence_running = False
                self.is_moving = False
                self.active_run_id = None
                self.active_cycle_id = None
            return True

        if not cycle_aborted and not self.abort_requested and not self.stop_active:
            self.motion_phase = "IDLE"
            if not self._execute_move_home():
                try:
                    self.storage.finish_run(run_id, status="failed", completed_count=self.pallet_count, duration_seconds=run_dur, error_message=self.status_msg)
                except Exception:
                    pass
                return False
            self.last_cycle_tact = run_dur
            if order_id and self.order_completed_quantity >= self.order_target_quantity:
                self.status_msg = f"Work Order {self.active_order_number or order_id} COMPLETED ({self.order_completed_quantity}/{self.order_target_quantity} parts)!"
                try:
                    self.storage.complete_work_order(order_id)
                except Exception:
                    pass
                self.active_order_id = None
            else:
                self.status_msg = f"Palletizing Completed! Placed: {self.pallet_count}/{total_slots} | Tact: {self.last_cycle_tact}s"

            try:
                self.storage.finish_run(run_id, status="completed", completed_count=self.pallet_count, duration_seconds=run_dur)
                self.storage.record_state_transition("RUNNING", "IDLE", "completed", run_id=run_id)
            except Exception:
                pass
        else:
            final_status = "cancelled" if (self.stop_active or self.abort_requested and not self.fault) else "failed"
            try:
                self.storage.finish_run(run_id, status=final_status, completed_count=self.pallet_count, duration_seconds=run_dur, error_message=self.status_msg)
                self.storage.record_state_transition("RUNNING", self.workcell_state, final_status, run_id=run_id)
            except Exception:
                pass

        with self.lock:
            self.sequence_running = False
            self.is_moving = False
            self.active_run_id = None
            self.active_cycle_id = None
            self.active_program_name = "Idle"
            self.motion_phase = "IDLE"
        return not cycle_aborted and not cycle_failed


    def _run_put_back_sequence(self):
        """Executes exact smart LIFO de-palletizing put-back loop using MoveL matching palletizing_with_plc.py."""
        self.cycle_start_time = time.time()
        t_run_start = time.monotonic()
        start_count = self.pallet_count
        run_id = self.active_run_id or str(uuid.uuid4())
        self.active_run_id = run_id

        try:
            self.storage.start_run(
                run_id=run_id,
                command="pb2",
                origin=self.mode,
                recipe_id=self.recipe_id,
                recipe_version=self.recipe_version,
                target_count=start_count,
            )
            self.storage.record_state_transition("IDLE", "RUNNING", "pb2", run_id=run_id)
        except Exception:
            logging.getLogger(__name__).exception("Failed to start put-back run in storage")

        with self.lock:
            self.sequence_running = True
            self.active_program_name = "Put-Back LIFO (Depalletize)"

        mag_insert_approach = self.get_approach_pose(MAGAZINE_INSERT_LOCATION, clearance=APPROACH_CLEARANCE_Z)

        cycle_aborted = False
        for i in range(start_count - 1, -1, -1):
            if self.abort_requested or self.stop_active:
                cycle_aborted = True
                break

            slot_pose = self.get_pallet_slot_pose(i)
            slot_approach = self.get_approach_pose(slot_pose, clearance=APPROACH_CLEARANCE_Z)
            floor = i // SLOTS_PER_FLOOR

            cycle_id = None
            t_cycle_start = time.monotonic()
            try:
                cycle_id = self.storage.start_cycle(run_id=run_id, cycle_index=i, target_slot=i)
                self.active_cycle_id = cycle_id
            except Exception:
                pass

            cycle_failed = False

            # -------------------------------------------------------------
            # STEP 1: Pallet Slot Pick Approach (MoveL to Slot Clearance)
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Return Approach (Floor {floor})"}
            if not self._exec_step(cycle_id, "SLOT_PICK_APPROACH", 1, lambda: (
                self.set_gripper(False) and
                self._execute_cartesian_move(slot_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                             step_name=f"[Return {i+1}] Slot {i+1} Approach MoveL (Clearance {APPROACH_CLEARANCE_Z}mm)")
            )):
                cycle_failed = True

            # -------------------------------------------------------------
            # STEP 2: Pallet Slot Pick Plunge (MoveL Grasp Height)
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "PLUNGE"
                self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Plunge to Grasp (Z={slot_pose[2]}mm)"}
                if not self._exec_step(cycle_id, "SLOT_PICK_PLUNGE", 2, lambda: (
                    self._execute_cartesian_move(slot_pose, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                                 step_name=f"[Return {i+1}] Plunging to Slot {i+1} MoveL (Z={slot_pose[2]}mm)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 3: Grip Billet
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "GRIP"
                self.status_msg = f"[Return {i+1}] Gripping Billet (DO1=ON)"
                def do_grip():
                    if not self.set_gripper(True):
                        return False
                    self._dwell(GRIPPER_DWELL_SEC)
                    if self.abort_requested or self.stop_active:
                        return False
                    self.held_workpiece = True
                    self.slots[i]["placed"] = False
                    self.pallet_count -= 1
                    return True
                if not self._exec_step(cycle_id, "SLOT_PICK_GRIP", 3, do_grip):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 4: Pallet Slot Pick Extract (MoveL Retract Clearance)
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "EXTRACT"
                self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Extract Retract MoveL"}
                if not self._exec_step(cycle_id, "SLOT_PICK_EXTRACT", 4, lambda: (
                    self._execute_cartesian_move(slot_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                                 step_name=f"[Return {i+1}] Slot {i+1} Extract Retract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 5: Feeder Top Insert Approach (MoveL to Feeder Top Clearance)
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "APPROACH"
                self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Top Approach @ {MAGAZINE_INSERT_LOCATION[3]}°"}
                if not self._exec_step(cycle_id, "MAGAZINE_PLACE_APPROACH", 5, lambda: (
                    self._execute_cartesian_move(mag_insert_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                                 step_name=f"[Return {i+1}] Feeder Top Approach MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {MAGAZINE_INSERT_LOCATION[3]}°)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 6: Feeder Top Insert Plunge (MoveL Collinear Insert)
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "PLUNGE"
                self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Insert Plunge (Collinear @ {MAGAZINE_INSERT_LOCATION[3]}°)"}
                if not self._exec_step(cycle_id, "MAGAZINE_PLACE_PLUNGE", 6, lambda: (
                    self._execute_cartesian_move(MAGAZINE_INSERT_LOCATION, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                                 step_name=f"[Return {i+1}] Inserting into Feeder MoveL (Collinear @ {MAGAZINE_INSERT_LOCATION[3]}°)")
                )):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 7: Release Billet
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "RELEASE"
                self.status_msg = f"[Return {i+1}] Releasing into Feeder (DO0=ON)"
                def do_release():
                    if not self.set_gripper(False):
                        return False
                    self._dwell(GRIPPER_DWELL_SEC)
                    if self.abort_requested or self.stop_active:
                        return False
                    self.held_workpiece = False
                    self.magazine_count = min(8, self.magazine_count + 1)
                    self.mag_sensor = True
                    return True
                if not self._exec_step(cycle_id, "MAGAZINE_PLACE_RELEASE", 7, do_release):
                    cycle_failed = True

            # -------------------------------------------------------------
            # STEP 8: Feeder Top Insert Extract (MoveL Collinear Retract)
            # -------------------------------------------------------------
            if not cycle_failed:
                self.motion_phase = "EXTRACT"
                self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Extract Retract MoveL @ {MAGAZINE_INSERT_LOCATION[3]} deg"}
                if not self._exec_step(cycle_id, "MAGAZINE_PLACE_EXTRACT", 8, lambda: (
                    self._execute_cartesian_move(mag_insert_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                                 step_name=f"[Return {i+1}] Feeder Extract Retract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {MAGAZINE_INSERT_LOCATION[3]} deg)")
                )):
                    cycle_failed = True

            cycle_dur = round(time.monotonic() - t_cycle_start, 3)
            c_status = "completed" if not cycle_failed else ("cancelled" if (self.stop_active or self.abort_requested) else "failed")
            if cycle_id:
                try:
                    self.storage.finish_cycle(cycle_id, status=c_status, duration_seconds=cycle_dur)
                except Exception:
                    pass

            if cycle_failed or self.abort_requested or self.stop_active:
                cycle_aborted = True
                break

        run_dur = round(time.monotonic() - t_run_start, 3)
        if not cycle_aborted and not self.abort_requested and not self.stop_active:
            self.motion_phase = "IDLE"
            if not self._execute_move_home():
                try:
                    self.storage.finish_run(run_id, status="failed", completed_count=start_count - self.pallet_count, duration_seconds=run_dur, error_message=self.status_msg)
                except Exception:
                    pass
                return False
            self.last_cycle_tact = run_dur
            self.status_msg = f"Put-Back Finished! Pallet: {self.pallet_count}/8 | Tact: {self.last_cycle_tact}s"
            try:
                self.storage.finish_run(run_id, status="completed", completed_count=start_count - self.pallet_count, duration_seconds=run_dur)
                self.storage.record_state_transition("RUNNING", "IDLE", "completed", run_id=run_id)
            except Exception:
                pass
        else:
            final_status = "cancelled" if (self.stop_active or self.abort_requested and not self.fault) else "failed"
            try:
                self.storage.finish_run(run_id, status=final_status, completed_count=start_count - self.pallet_count, duration_seconds=run_dur, error_message=self.status_msg)
                self.storage.record_state_transition("RUNNING", self.workcell_state, final_status, run_id=run_id)
            except Exception:
                pass

        with self.lock:
            self.sequence_running = False
            self.active_program_name = "Idle"
            self.motion_phase = "IDLE"

    def get_telemetry_packet(self) -> Dict:
        with self.lock:
            return {
                "mode": self.mode,
                "workcell_state": self.workcell_state,
                "fault": self.fault,
                "recent_events": list(self.events[-10:]),
                "last_command": dict(self.commands[-1]) if self.commands else None,
                "robot_ip": self.robot_ip,
                "hardware_connected": self.hardware_connected,
                "q": [round(x, 2) for x in self.q],
                "p": [round(x, 2) for x in self.p],
                "op_state": self.op_state,
                "op_state_name": self.op_state_name,
                "is_moving": self.is_moving,
                "status_msg": self.status_msg,
                "motion_phase": {
                    "phase": self.motion_phase,
                    "angle": self.motion_angle
                },
                "pallet_count": self.pallet_count,
                "max_items": TOTAL_MAX_ITEMS,
                "magazine_count": self.magazine_count,
                "gripper_closed": self.gripper_closed,
                "held_workpiece": self.held_workpiece,
                "slots": self.slots,
                "plc_io": {
                    "pb1": self.pb1,
                    "pb2": self.pb2,
                    "stop": self.stop_active,
                    "mag_sensor": self.mag_sensor,
                    "do0_open": self.do_gripper_open,
                    "do1_close": self.do_gripper_close,
                },
                "tact_time": self.last_cycle_tact,
                "telemetry_hz": self.telemetry_hz_actual,
                "direct_teaching": self.direct_teaching,
                "speed_ratio": self.speed_ratio,
                "sequence": {
                    "running": self.sequence_running,
                    "program": self.active_program_name,
                    "step": self.current_step_idx,
                    "total": self.total_steps,
                },
                "waypoints": self.waypoints,
                "work_order": {
                    "active_order_id": self.active_order_id,
                    "active_order_number": self.active_order_number,
                    "target_quantity": self.order_target_quantity,
                    "completed_quantity": self.order_completed_quantity,
                    "current_pallet_index": self.current_pallet_index,
                    "pallet_change_required": self.pallet_change_required,
                    "progress_pct": round((self.order_completed_quantity / self.order_target_quantity) * 100.0, 1) if self.order_target_quantity > 0 else 0.0,
                } if self.active_order_id else None,
                "pallet_change_required": self.pallet_change_required,
                "current_pallet_index": self.current_pallet_index,
                "timestamp": time.time()
            }

