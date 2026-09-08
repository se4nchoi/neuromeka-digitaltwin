"""Shared validated command boundary for HTTP, WebSocket, and physical starts."""
import logging
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cmd: Literal["pb1", "pb2", "home", "zero", "recover", "stop", "sensor",
                 "reconnect", "connect", "disconnect", "simulation", "jog_joint",
                 "jog_task", "jog_stop", "direct_teaching", "gripper", "goto_wp",
                 "start_sequence", "stop_sequence", "speed", "reset_pallet", "inject_fault"]
    active: bool = True
    joint_idx: int = Field(default=0, ge=0, le=5)
    step_deg: float = Field(default=0, ge=-180, le=180)
    axis: Literal["x", "y", "z", "u", "v", "w"] = "z"
    step_val: float = Field(default=0, ge=-1000, le=1000)
    vel_ratio: int | None = Field(default=None, ge=1, le=100)
    enable: bool = False
    close: bool = False
    id: str = ""
    repeat_count: int = Field(default=1, ge=0, le=1000)
    speed_ratio: int = Field(default=25, ge=1, le=100)
    ip: str | None = None
    code: Literal["FEEDER_EMPTY", "SENSOR_TIMEOUT", "MOTION_TIMEOUT", "CONNECTION_LOST"] = "FEEDER_EMPTY"


class CommandService:
    def __init__(self, engine):
        self.engine = engine
        engine.command_service = self

    def execute(self, cmd, **payload):
        e = self.engine
        record = {"command_id": str(uuid.uuid4()), "cmd": cmd, "submitted_at": time.time()}
        try:
            p = Command(cmd=cmd, **payload)
            with e.lock:
                busy = e.sequence_running or e.is_moving or (e.active_thread and e.active_thread.is_alive())
                interrupt = cmd in {"stop", "stop_sequence", "jog_stop", "inject_fault"}
                if busy and not interrupt:
                    raise ValueError("Cell is busy; stop the active operation first")
                resolution = {"recover", "sensor", "reset_pallet", "connect", "reconnect", "disconnect", "simulation"}
                if (e.fault or e.stop_active or e.abort_requested) and not interrupt and cmd not in resolution:
                    raise ValueError("Cell requires recovery before another command")
                if e.mode == "DISCONNECTED" and cmd not in resolution and not interrupt:
                    raise ValueError("Connect equipment or select simulation first")
                if cmd in {"sensor", "reset_pallet", "inject_fault"} and e.mode != "SIMULATION":
                    raise ValueError("This operation is simulation-only")
                handlers = {
                    "pb1": e.trigger_pb1_palletize, "pb2": e.trigger_pb2_put_back,
                    "home": e.move_home, "zero": e.move_zero, "recover": e.recover_robot,
                    "stop": lambda: e.set_stop(p.active), "jog_stop": e.stop_jog,
                    "connect": lambda: e.connect_hardware(p.ip or e.robot_ip),
                    "reconnect": lambda: e.connect_hardware(e.robot_ip),
                    "disconnect": e.disconnect_hardware, "simulation": e.switch_to_simulation,
                    "jog_joint": lambda: e.jog_joint(p.joint_idx, p.step_deg, p.vel_ratio),
                    "jog_task": lambda: e.jog_task(p.axis, p.step_val, p.vel_ratio),
                    "direct_teaching": lambda: e.set_direct_teaching(p.enable),
                    "gripper": lambda: e.set_gripper(p.close),
                    "goto_wp": lambda: e.move_to_waypoint(p.id),
                    "start_sequence": lambda: e.start_waypoint_sequence(p.repeat_count),
                    "stop_sequence": lambda: e.set_stop(True),
                    "speed": lambda: setattr(e, "speed_ratio", p.speed_ratio),
                    "sensor": lambda: setattr(e, "mag_sensor", not e.mag_sensor),
                    "inject_fault": lambda: e.inject_fault(p.code),
                    "reset_pallet": self.reset_cell,
                }
                result = handlers[cmd]()
                if isinstance(result, dict) and "command_id" in result:
                    e.commands[-1]["cmd"] = cmd
                    return {**result, "cmd": cmd}
                record.update(status="completed" if result is not False else "rejected",
                              success=result is not False, message=e.status_msg)
        except (ValidationError, ValueError) as exc:
            record.update(status="rejected", success=False, message=str(exc))
        except Exception:
            logging.getLogger(__name__).exception("Command %s failed", cmd)
            record.update(status="failed", success=False, message="Command failed; inspect server log")
        record["finished_at"] = time.time()
        with e.lock:
            e.commands.append(record)
            e.commands[:] = e.commands[-100:]
        return record

    def reset_cell(self):
        e = self.engine
        e.pallet_count = 0
        e.magazine_count = len(e.slots)
        e.held_workpiece = False
        e.gripper_closed = False
        e.do_gripper_open = True
        e.do_gripper_close = False
        e.mag_sensor = True
        e.slots = e._init_slots()
        e.status_msg = "Simulation inventory reset; recover separately if stopped or faulted"
