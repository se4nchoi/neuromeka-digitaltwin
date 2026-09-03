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
import numpy as np
from typing import Dict, List, Optional
from .config import (
    DEFAULT_ROBOT_IP,
    PICK_LOCATION,
    DROP_BASE_LOCATION,
    MAGAZINE_INSERT_LOCATION,
    HOME_JPOS,
    GRID_X,
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
    def __init__(self):
        self.lock = threading.RLock()
        self.robot_ip = DEFAULT_ROBOT_IP
        self.indy: Optional[IndyDCP3] = None
        self.hardware_connected = False
        self.mode = "DISCONNECTED"  # "HARDWARE_LIVE", "DISCONNECTED", "SIMULATION"
        self.auto_reconnect = True

        # Robot Kinematic State
        self.q = list(HOME_JPOS)
        self.p = [350.0, -186.5, 522.0, 0.0, -180.0, 0.0]
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

        # Telemetry Hz diagnostics
        self.telemetry_hz_actual = 0.0
        self.last_telemetry_ts = 0.0

        # Connect hardware on startup
        self.connect_hardware(self.robot_ip)

        # Background worker for non-blocking 30Hz polling
        self.poll_thread = threading.Thread(target=self._telemetry_worker, daemon=True)
        self.poll_thread.start()

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
                                threading.Thread(target=self.trigger_pb1_palletize, daemon=True).start()
                        if self.pb2 and not prev_pb2:
                            if not self.sequence_running and not self.is_moving:
                                threading.Thread(target=self.trigger_pb2_put_back, daemon=True).start()
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
                        self.status_msg = f"Lost connection to Indy7: {e}"

            time.sleep(0.033)

    # =========================================================================
    # TEACH PENDANT CONTROLS (JOGGING & TOOLS)
    # =========================================================================
    def jog_joint(self, joint_idx: int, step_deg: float, vel_ratio: Optional[int] = None) -> bool:
        """Jogs a single joint incrementally (+/- step_deg)."""
        vel = vel_ratio or self.speed_ratio
        if not (0 <= joint_idx <= 5):
            return False

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
                    return True
                except Exception as e:
                    self.status_msg = f"Jog J{joint_idx+1} Error: {e}"
                    return False
            else:
                # Simulation Jog
                self.q[joint_idx] = round(self.q[joint_idx] + step_deg, 2)
                self.p = forward_kinematics_craig(self.q)
                self.status_msg = f"[SIM] Jog J{joint_idx+1} -> {self.q[joint_idx]}°"
                return True

    def jog_task(self, axis: str, step_val: float, vel_ratio: Optional[int] = None) -> bool:
        """Jogs the TCP in Cartesian coordinates along axis ('x', 'y', 'z', 'u', 'v', 'w')."""
        axis_map = {"x": 0, "y": 1, "z": 2, "u": 3, "v": 4, "w": 5}
        ax = axis.lower()
        if ax not in axis_map:
            return False
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
                    return True
                except Exception as e:
                    self.status_msg = f"Jog {ax.upper()} Error: {e}"
                    return False
            else:
                # Simulation Cartesian jog
                self.p[idx] = round(self.p[idx] + step_val, 2)
                unit = "mm" if idx < 3 else "°"
                self.status_msg = f"[SIM] Jog {ax.upper()} -> {self.p[idx]}{unit}"
                return True

    def stop_jog(self):
        """Immediately halts jogging."""
        with self.lock:
            if self.hardware_connected and self.indy:
                try:
                    self.indy.stop_motion(StopCategory.CAT0)
                except Exception:
                    pass
            self.status_msg = "Jog Stopped"

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
        def worker():
            with self.lock:
                self.sequence_running = True
                self.active_program_name = "Move Home"
            if self.hardware_connected and self.indy:
                try:
                    self.status_msg = "Moving to Home Position..."
                    self.indy.move_home()
                    self._wait_hardware_motion()
                    self.status_msg = "Reached Home Position"
                except Exception as e:
                    self.status_msg = f"Move Home Error: {e}"
            else:
                self._sim_move(HOME_JPOS, 1.4, "Returning to Home Position")

            with self.lock:
                self.sequence_running = False
                self.active_program_name = "Idle"

        threading.Thread(target=worker, daemon=True).start()

    def move_zero(self):
        def worker():
            with self.lock:
                self.sequence_running = True
                self.active_program_name = "Move Zero"
            zero_q = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            if self.hardware_connected and self.indy:
                try:
                    self.status_msg = "Moving to Zero Position..."
                    self.indy.movej(zero_q, vel_ratio=20, acc_ratio=20)
                    self._wait_hardware_motion()
                    self.status_msg = "Reached Zero Position"
                except Exception as e:
                    self.status_msg = f"Move Zero Error: {e}"
            else:
                self._sim_move(zero_q, 1.6, "Moving to Calibration Zero Position")

            with self.lock:
                self.sequence_running = False
                self.active_program_name = "Idle"

        threading.Thread(target=worker, daemon=True).start()

    def recover_robot(self) -> bool:
        """Clears faults, violations, and emergency stop latch."""
        with self.lock:
            if self.hardware_connected and self.indy:
                try:
                    self.indy.recover()
                    self.stop_active = False
                    self.abort_requested = False
                    self.status_msg = "Robot Recovered & Fault Cleared"
                    return True
                except Exception as e:
                    self.status_msg = f"Recover Error: {e}"
                    return False
            else:
                self.stop_active = False
                self.abort_requested = False
                self.status_msg = "[SIM] System Reset / Ready"
                return True

    def move_to_waypoint(self, wp_id: str):
        """Commands robot to navigate directly to a specified waypoint."""
        wp = next((w for w in self.waypoints if w["id"] == wp_id), None)
        if not wp:
            self.status_msg = f"Waypoint '{wp_id}' not found"
            return

        def worker():
            with self.lock:
                self.sequence_running = True
                self.active_program_name = f"GoTo: {wp['name']}"

            target_q = wp.get("q", HOME_JPOS)
            target_p = wp.get("p")
            move_type = wp.get("move_type", "MoveJ")
            speed = wp.get("speed", 25)

            if self.hardware_connected and self.indy:
                try:
                    self.status_msg = f"Moving to '{wp['name']}' ({move_type})..."
                    if move_type == "MoveL" and target_p:
                        self.indy.movel(target_p, vel_ratio=speed, acc_ratio=speed)
                    else:
                        self.indy.movej(target_q, vel_ratio=speed, acc_ratio=speed)
                    self._wait_hardware_motion()
                    
                    # Tool action
                    grip_act = wp.get("gripper", "keep")
                    if grip_act == "open":
                        self.set_gripper(False)
                    elif grip_act == "close":
                        self.set_gripper(True)

                    dwell = wp.get("dwell", 0.0)
                    if dwell > 0:
                        time.sleep(dwell)

                    self.status_msg = f"Reached '{wp['name']}'"
                except Exception as e:
                    self.status_msg = f"Move Error: {e}"
            else:
                # Simulation move
                self._sim_move(target_q, 1.2, f"Moving to '{wp['name']}'")
                grip_act = wp.get("gripper", "keep")
                if grip_act == "open":
                    self.set_gripper(False)
                elif grip_act == "close":
                    self.set_gripper(True)
                dwell = wp.get("dwell", 0.0)
                if dwell > 0:
                    time.sleep(dwell)

            with self.lock:
                self.sequence_running = False
                self.active_program_name = "Idle"

        threading.Thread(target=worker, daemon=True).start()

    def start_waypoint_sequence(self, repeat_count: int = 1):
        """Executes all saved waypoints in sequence."""
        if not self.waypoints:
            self.status_msg = "No waypoints saved to execute!"
            return

        def worker():
            self.cycle_start_time = time.time()
            with self.lock:
                self.sequence_running = True
                self.abort_requested = False
                self.total_steps = len(self.waypoints) * max(1, repeat_count)
                self.current_step_idx = 0

            cycles = range(repeat_count) if repeat_count > 0 else iter(int, 1) # 0 = infinite

            for cycle in cycles:
                if self.abort_requested or self.stop_active:
                    break

                for i, wp in enumerate(self.waypoints):
                    if self.abort_requested or self.stop_active:
                        break

                    with self.lock:
                        self.current_step_idx += 1
                        self.active_program_name = f"Seq [{cycle+1}] {wp['name']}"

                    target_q = wp.get("q", HOME_JPOS)
                    target_p = wp.get("p")
                    move_type = wp.get("move_type", "MoveJ")
                    speed = wp.get("speed", 25)

                    if self.hardware_connected and self.indy:
                        try:
                            self.status_msg = f"Step {self.current_step_idx}: '{wp['name']}'"
                            if move_type == "MoveL" and target_p:
                                self.indy.movel(target_p, vel_ratio=speed, acc_ratio=speed)
                            else:
                                self.indy.movej(target_q, vel_ratio=speed, acc_ratio=speed)
                            self._wait_hardware_motion()

                            grip_act = wp.get("gripper", "keep")
                            if grip_act == "open":
                                self.set_gripper(False)
                            elif grip_act == "close":
                                self.set_gripper(True)

                            dwell = wp.get("dwell", 0.0)
                            if dwell > 0:
                                time.sleep(dwell)
                        except Exception as e:
                            self.status_msg = f"Sequence Error: {e}"
                            break
                    else:
                        if not self._sim_move(target_q, 1.2, f"Step {self.current_step_idx}: '{wp['name']}'"):
                            break
                        grip_act = wp.get("gripper", "keep")
                        if grip_act == "open":
                            self.set_gripper(False)
                        elif grip_act == "close":
                            self.set_gripper(True)
                        dwell = wp.get("dwell", 0.0)
                        if dwell > 0:
                            time.sleep(dwell)

            self.last_cycle_tact = round(time.time() - self.cycle_start_time, 2)
            with self.lock:
                self.sequence_running = False
                self.active_program_name = "Idle"
                self.status_msg = f"Sequence Finished! Tact: {self.last_cycle_tact}s"

        self.active_thread = threading.Thread(target=worker, daemon=True)
        self.active_thread.start()

    def _wait_hardware_motion(self, timeout: float = 30.0) -> bool:
        """Polls until physical robot has finished moving using 2-phase verification."""
        if not self.indy:
            return True
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
                    self.status_msg = f"Safety stop triggered! OpState={op_state}"
                    return False

                if not is_in_motion and op_state == 5:
                    return True
            except Exception:
                pass
            time.sleep(0.04)

        return False

    def get_pallet_slot_pose(self, index: int) -> List[float]:
        """Computes target pose for item `index` (0 to 7) matching palletizing_with_plc.py."""
        layer = index // SLOTS_PER_FLOOR
        slot_in_layer = index % SLOTS_PER_FLOOR
        row = slot_in_layer // GRID_X
        col = slot_in_layer % GRID_X

        base_x, base_y, base_z = DROP_BASE_LOCATION[0], DROP_BASE_LOCATION[1], DROP_BASE_LOCATION[2]
        rot = DROP_BASE_LOCATION[3:]

        x = base_x - (row * OFFSET_X)
        y = base_y + (col * OFFSET_Y)
        z = base_z + (layer * LAYER_HEIGHT)

        return [round(x, 2), round(y, 2), round(z, 2), *rot]

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

    def _execute_cartesian_move(self, pose: List[float], vel_ratio: Optional[int] = None, acc_ratio: Optional[int] = None, step_name: str = "") -> bool:
        """Executes a Cartesian straight-line linear move (MoveL) collinear with tool angle."""
        if step_name:
            self.status_msg = step_name
        self.is_moving = True
        self.op_state = 6
        self.op_state_name = "OP_MOVING (6)"

        vel = vel_ratio or self.speed_ratio
        acc = acc_ratio or self.speed_ratio

        if self.hardware_connected and self.indy:
            try:
                with self.lock:
                    self.indy.movel(
                        ttarget=list(pose),
                        base_type=TaskBaseType.ABSOLUTE,
                        vel_ratio=vel,
                        acc_ratio=acc,
                    )
                success = self._wait_hardware_motion()
                self.is_moving = False
                return success
            except Exception as e:
                self.status_msg = f"Hardware MoveL Error: {e}"
                self.is_moving = False
                return False
        else:
            return self._sim_cartesian_move(pose, 1.2, step_name)

    def _execute_move_home(self) -> bool:
        """Executes calibrated Move Home matching palletizing_with_plc.py."""
        self.status_msg = "Returning to HOME position..."
        self.is_moving = True
        self.op_state = 6
        self.op_state_name = "OP_MOVING (6)"

        if self.hardware_connected and self.indy:
            try:
                with self.lock:
                    self.indy.move_home()
                success = self._wait_hardware_motion()
                self.is_moving = False
                return success
            except Exception as e:
                self.status_msg = f"Move Home Error: {e}"
                self.is_moving = False
                return False
        else:
            return self._sim_move(HOME_JPOS, 1.5, "Returning to HOME Position")

    def _execute_joint_move(self, q_target: List[float], vel_ratio: Optional[int] = None, step_name: str = "") -> bool:
        """Executes a joint move on physical hardware if connected, or runs 3D simulation."""
        if step_name:
            self.status_msg = step_name
        self.is_moving = True
        self.op_state = 6
        self.op_state_name = "OP_MOVING (6)"

        vel = vel_ratio or self.speed_ratio

        if self.hardware_connected and self.indy:
            try:
                with self.lock:
                    self.indy.movej(list(q_target), vel_ratio=vel, acc_ratio=vel)
                
                success = self._wait_hardware_motion()
                self.is_moving = False
                return success
            except Exception as e:
                self.status_msg = f"Hardware Motion Error: {e}"
                self.is_moving = False
                return False
        else:
            return self._sim_move(q_target, 1.2, step_name)

    def _sim_cartesian_move(self, p_target: List[float], duration: float, step_name: str) -> bool:
        self.status_msg = step_name
        self.is_moving = True
        self.op_state = 6
        self.op_state_name = "OP_MOVING (6)"

        steps = max(int(duration * 60), 2)
        p_start = np.array(self.p, dtype=float)
        p_end = np.array(p_target, dtype=float)

        for step in range(steps):
            if self.abort_requested or self.stop_active:
                self.is_moving = False
                self.op_state = 8
                self.op_state_name = "OP_STOP (X107)"
                return False

            tau = step / (steps - 1)
            s = 10.0 * (tau ** 3) - 15.0 * (tau ** 4) + 6.0 * (tau ** 5)
            cur_p = p_start + s * (p_end - p_start)
            with self.lock:
                self.p = [round(float(v), 2) for v in cur_p]
            time.sleep(1.0 / 60.0)

        with self.lock:
            self.p = list(p_target)
            self.is_moving = False
            self.op_state = 5
            self.op_state_name = "OP_IDLE (5)"
        return True

    def set_stop(self, active: bool = True):
        with self.lock:
            self.stop_active = active
            if active:
                self.abort_requested = True
                self.status_msg = "EMERGENCY STOP TRIGGERED!"
                if self.hardware_connected and self.indy:
                    try:
                        self.indy.stop_motion(StopCategory.CAT2)
                    except Exception:
                        pass
            else:
                self.abort_requested = False
                self.status_msg = "Stop Cleared / Ready"

    def trigger_pb1_palletize(self):
        with self.lock:
            if self.is_moving or self.sequence_running:
                return
            if self.pallet_count >= TOTAL_MAX_ITEMS:
                self.status_msg = "Pallet is already full (8/8)!"
                return
            if self.magazine_count <= 0 or not self.mag_sensor:
                self.status_msg = "Magazine is empty! Cannot pick."
                return

            self.abort_requested = False
            self.active_thread = threading.Thread(target=self._run_palletize_sequence, daemon=True)
            self.active_thread.start()

    def trigger_pb2_put_back(self):
        with self.lock:
            if self.is_moving or self.sequence_running:
                return
            if self.pallet_count <= 0:
                self.status_msg = "Pallet is empty! Nothing to put back."
                return

            self.abort_requested = False
            self.active_thread = threading.Thread(target=self._run_put_back_sequence, daemon=True)
            self.active_thread.start()

    def _sim_move(self, q_target: List[float], duration: float, step_name: str) -> bool:
        self.status_msg = step_name
        self.is_moving = True
        self.op_state = 6
        self.op_state_name = "OP_MOVING (6)"

        traj = quintic_interpolate(self.q, q_target, duration, hz=60)
        for point in traj:
            if self.abort_requested or self.stop_active:
                self.is_moving = False
                self.op_state = 8
                self.op_state_name = "OP_STOP (X107)"
                return False

            with self.lock:
                self.q = point
                self.p = forward_kinematics_craig(self.q)
            time.sleep(1.0 / 60.0)

        with self.lock:
            self.q = list(q_target)
            self.p = forward_kinematics_craig(self.q)
            self.is_moving = False
            self.op_state = 5
            self.op_state_name = "OP_IDLE (5)"
        return True

    def _run_palletize_sequence(self):
        """Executes exact single-pallet 8-slot palletizing loop using MoveL matching palletizing_with_plc.py."""
        self.cycle_start_time = time.time()
        start_count = self.pallet_count
        with self.lock:
            self.sequence_running = True
            self.active_program_name = "Palletizing Loop (8 Slots)"

        # Precompute feeder pick approach pose with 100mm clearance backed off along TCP Z-axis
        pick_approach = self.get_approach_pose(PICK_LOCATION, clearance=APPROACH_CLEARANCE_Z)

        for i in range(start_count, TOTAL_MAX_ITEMS):
            if self.abort_requested or self.stop_active:
                break
            if self.magazine_count <= 0 or not self.mag_sensor:
                self.status_msg = f"Magazine Sensor OFF (Empty). Placed {self.pallet_count}/8 items."
                break

            slot_pose = self.get_pallet_slot_pose(i)
            drop_approach = self.get_approach_pose(slot_pose, clearance=APPROACH_CLEARANCE_Z)
            floor = i // SLOTS_PER_FLOOR

            # -------------------------------------------------------------
            # STEP 1: Feeder Pick Approach (MoveL with -19.52° Tool Angle)
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Approach (Tilt: {PICK_LOCATION[3]}°)"}
            self.set_gripper(False)
            if not self._execute_cartesian_move(pick_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Feeder Approach MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {PICK_LOCATION[3]}°)"):
                break

            # -------------------------------------------------------------
            # STEP 2: Feeder Pick Plunge (MoveL Collinear along -19.52° Angle)
            # -------------------------------------------------------------
            self.motion_phase = "PLUNGE"
            self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Plunge (Collinear @ {PICK_LOCATION[3]}°)"}
            if not self._execute_cartesian_move(PICK_LOCATION, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Feeder Pick Plunge MoveL (Collinear @ {PICK_LOCATION[3]}°)"):
                break

            # -------------------------------------------------------------
            # STEP 3: Grip Billet
            # -------------------------------------------------------------
            self.motion_phase = "GRIP"
            self.status_msg = f"[Item {i+1}] Gripping Billet (DO1=ON)"
            self.set_gripper(True)
            time.sleep(GRIPPER_DWELL_SEC)
            self.held_workpiece = True
            self.magazine_count = max(0, self.magazine_count - 1)
            if self.magazine_count == 0:
                self.mag_sensor = False

            # -------------------------------------------------------------
            # STEP 4: Feeder Pick Extract (MoveL Collinear Retract out of Feeder)
            # -------------------------------------------------------------
            self.motion_phase = "EXTRACT"
            self.motion_angle = {"u": PICK_LOCATION[3], "v": PICK_LOCATION[4], "w": PICK_LOCATION[5], "desc": f"Feeder Pick Extract MoveL (Collinear @ {PICK_LOCATION[3]}°)"}
            if not self._execute_cartesian_move(pick_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Feeder Pick Extract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {PICK_LOCATION[3]}°)"):
                break

            # -------------------------------------------------------------
            # STEP 5: Pallet Slot Place Approach (MoveL with -3.24° Tool Angle)
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Approach (Floor {floor}) @ {slot_pose[3]}°"}
            if not self._execute_cartesian_move(drop_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Pallet Slot {i+1} Approach MoveL (Floor {floor}, Clearance {APPROACH_CLEARANCE_Z}mm)"):
                break

            # -------------------------------------------------------------
            # STEP 6: Pallet Slot Place Plunge (MoveL Lower into Slot)
            # -------------------------------------------------------------
            self.motion_phase = "PLUNGE"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Place Plunge (Z={slot_pose[2]}mm)"}
            if not self._execute_cartesian_move(slot_pose, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Lowering to Slot {i+1} MoveL (Z={slot_pose[2]}mm)"):
                break

            # -------------------------------------------------------------
            # STEP 7: Release Billet
            # -------------------------------------------------------------
            self.motion_phase = "RELEASE"
            self.status_msg = f"[Item {i+1}] Releasing into Slot {i+1} (DO0=ON)"
            self.set_gripper(False)
            time.sleep(GRIPPER_DWELL_SEC)
            self.held_workpiece = False
            self.slots[i]["placed"] = True
            self.pallet_count += 1

            # -------------------------------------------------------------
            # STEP 8: Pallet Slot Place Extract (MoveL Vertical Retract)
            # -------------------------------------------------------------
            self.motion_phase = "EXTRACT"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Extract Retract MoveL @ {slot_pose[3]}°"}
            if not self._execute_cartesian_move(drop_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Item {i+1}] Slot {i+1} Extract Retract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm)"):
                break

        if not self.abort_requested and not self.stop_active:
            self.motion_phase = "IDLE"
            self._execute_move_home()
            self.last_cycle_tact = round(time.time() - self.cycle_start_time, 2)
            self.status_msg = f"Palletizing Completed! Placed: {self.pallet_count}/8 | Tact: {self.last_cycle_tact}s"

        with self.lock:
            self.sequence_running = False
            self.active_program_name = "Idle"
            self.motion_phase = "IDLE"

    def _run_put_back_sequence(self):
        """Executes exact smart LIFO de-palletizing put-back loop using MoveL matching palletizing_with_plc.py."""
        self.cycle_start_time = time.time()
        start_count = self.pallet_count
        with self.lock:
            self.sequence_running = True
            self.active_program_name = "Put-Back LIFO (Depalletize)"

        mag_insert_approach = self.get_approach_pose(MAGAZINE_INSERT_LOCATION, clearance=APPROACH_CLEARANCE_Z)

        for i in range(start_count - 1, -1, -1):
            if self.abort_requested or self.stop_active:
                break

            slot_pose = self.get_pallet_slot_pose(i)
            slot_approach = self.get_approach_pose(slot_pose, clearance=APPROACH_CLEARANCE_Z)
            floor = i // SLOTS_PER_FLOOR

            # -------------------------------------------------------------
            # STEP 1: Pallet Slot Pick Approach (MoveL to Slot Clearance)
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Return Approach (Floor {floor})"}
            self.set_gripper(False)
            if not self._execute_cartesian_move(slot_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Slot {i+1} Approach MoveL (Clearance {APPROACH_CLEARANCE_Z}mm)"):
                break

            # -------------------------------------------------------------
            # STEP 2: Pallet Slot Pick Plunge (MoveL Grasp Height)
            # -------------------------------------------------------------
            self.motion_phase = "PLUNGE"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Plunge to Grasp (Z={slot_pose[2]}mm)"}
            if not self._execute_cartesian_move(slot_pose, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Plunging to Slot {i+1} MoveL (Z={slot_pose[2]}mm)"):
                break

            # -------------------------------------------------------------
            # STEP 3: Grip Billet
            # -------------------------------------------------------------
            self.motion_phase = "GRIP"
            self.status_msg = f"[Return {i+1}] Gripping Billet (DO1=ON)"
            self.set_gripper(True)
            time.sleep(GRIPPER_DWELL_SEC)
            self.held_workpiece = True
            self.slots[i]["placed"] = False
            self.pallet_count -= 1

            # -------------------------------------------------------------
            # STEP 4: Pallet Slot Pick Extract (MoveL Retract Clearance)
            # -------------------------------------------------------------
            self.motion_phase = "EXTRACT"
            self.motion_angle = {"u": slot_pose[3], "v": slot_pose[4], "w": slot_pose[5], "desc": f"Slot {i+1} Extract Retract MoveL"}
            if not self._execute_cartesian_move(slot_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Slot {i+1} Extract Retract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm)"):
                break

            # -------------------------------------------------------------
            # STEP 5: Feeder Top Insert Approach (MoveL to Feeder Top Clearance)
            # -------------------------------------------------------------
            self.motion_phase = "APPROACH"
            self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Top Approach @ {MAGAZINE_INSERT_LOCATION[3]}°"}
            if not self._execute_cartesian_move(mag_insert_approach, vel_ratio=TRANSIT_VEL_RATIO, acc_ratio=TRANSIT_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Feeder Top Approach MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {MAGAZINE_INSERT_LOCATION[3]}°)"):
                break

            # -------------------------------------------------------------
            # STEP 6: Feeder Top Insert Plunge (MoveL Collinear Insert)
            # -------------------------------------------------------------
            self.motion_phase = "PLUNGE"
            self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Insert Plunge (Collinear @ {MAGAZINE_INSERT_LOCATION[3]}°)"}
            if not self._execute_cartesian_move(MAGAZINE_INSERT_LOCATION, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Inserting into Feeder MoveL (Collinear @ {MAGAZINE_INSERT_LOCATION[3]}°)"):
                break

            # -------------------------------------------------------------
            # STEP 7: Release Billet
            # -------------------------------------------------------------
            self.motion_phase = "RELEASE"
            self.status_msg = f"[Return {i+1}] Releasing into Feeder (DO0=ON)"
            self.set_gripper(False)
            time.sleep(GRIPPER_DWELL_SEC)
            self.held_workpiece = False
            self.magazine_count = min(8, self.magazine_count + 1)
            self.mag_sensor = True

            # -------------------------------------------------------------
            # STEP 8: Feeder Top Insert Extract (MoveL Collinear Retract)
            # -------------------------------------------------------------
            self.motion_phase = "EXTRACT"
            self.motion_angle = {"u": MAGAZINE_INSERT_LOCATION[3], "v": MAGAZINE_INSERT_LOCATION[4], "w": MAGAZINE_INSERT_LOCATION[5], "desc": f"Feeder Extract Retract MoveL @ {MAGAZINE_INSERT_LOCATION[3]}°"}
            if not self._execute_cartesian_move(mag_insert_approach, vel_ratio=ACTION_VEL_RATIO, acc_ratio=ACTION_ACC_RATIO,
                                               step_name=f"[Return {i+1}] Feeder Extract Retract MoveL (Clearance {APPROACH_CLEARANCE_Z}mm @ {MAGAZINE_INSERT_LOCATION[3]}°)"):
                break

        if not self.abort_requested and not self.stop_active:
            self.motion_phase = "IDLE"
            self._execute_move_home()
            self.last_cycle_tact = round(time.time() - self.cycle_start_time, 2)
            self.status_msg = f"Put-Back Finished! Pallet: {self.pallet_count}/8 | Tact: {self.last_cycle_tact}s"

        with self.lock:
            self.sequence_running = False
            self.active_program_name = "Idle"
            self.motion_phase = "IDLE"

    def get_telemetry_packet(self) -> Dict:
        with self.lock:
            return {
                "mode": self.mode,
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
                "timestamp": time.time()
            }
