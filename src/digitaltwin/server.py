import os
import asyncio
import json
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import SERVER_HOST, SERVER_PORT, TELEMETRY_HZ, DEFAULT_ROBOT_IP
from .palletizer_engine import PalletizerEngine

app = FastAPI(title="Indy7 3D Digital Twin Workcell & Multi-Purpose Center", version="2.0.0")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

engine = PalletizerEngine()


# --- REQUEST MODELS ---
class ConnectionPayload(BaseModel):
    ip: str = DEFAULT_ROBOT_IP


class StopPayload(BaseModel):
    active: bool = True


class JointJogPayload(BaseModel):
    joint_idx: int
    step_deg: float
    vel_ratio: Optional[int] = None


class TaskJogPayload(BaseModel):
    axis: str
    step_val: float
    vel_ratio: Optional[int] = None


class DirectTeachingPayload(BaseModel):
    enable: bool


class GripperPayload(BaseModel):
    close: bool


class SpeedPayload(BaseModel):
    speed_ratio: int


class AcquireWaypointPayload(BaseModel):
    name: str = ""
    move_type: str = "MoveJ"
    speed: int = 25
    gripper: str = "keep"
    dwell: float = 0.5


class WaypointsListPayload(BaseModel):
    waypoints: List[dict]


class WaypointIdPayload(BaseModel):
    id: str


class SequencePayload(BaseModel):
    repeat_count: int = 1


# --- HTTP ENDPOINTS ---
@app.get("/")
async def get_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/state")
async def get_state():
    return engine.get_telemetry_packet()


@app.post("/api/connect")
async def connect_robot(payload: ConnectionPayload):
    success = engine.connect_hardware(payload.ip)
    return {"success": success, "mode": engine.mode, "msg": engine.status_msg}


@app.post("/api/reconnect")
async def reconnect_robot():
    success = engine.connect_hardware(engine.robot_ip)
    return {"success": success, "mode": engine.mode, "msg": engine.status_msg}


@app.post("/api/disconnect")
async def disconnect_robot():
    engine.disconnect_hardware()
    return {"success": True, "mode": engine.mode, "msg": engine.status_msg}


@app.post("/api/simulation")
async def switch_to_simulation():
    engine.switch_to_simulation()
    return {"success": True, "mode": engine.mode, "msg": engine.status_msg}


# --- PROGRAMS (PALLETIZING, HOME, ZERO, RECOVER) ---
@app.post("/api/pb1")
async def trigger_pb1():
    engine.trigger_pb1_palletize()
    return {"success": True, "status": engine.status_msg}


@app.post("/api/pb2")
async def trigger_pb2():
    engine.trigger_pb2_put_back()
    return {"success": True, "status": engine.status_msg}


@app.post("/api/robot/home")
async def trigger_home():
    engine.move_home()
    return {"success": True, "status": engine.status_msg}


@app.post("/api/robot/zero")
async def trigger_zero():
    engine.move_zero()
    return {"success": True, "status": engine.status_msg}


@app.post("/api/robot/recover")
async def trigger_recover():
    success = engine.recover_robot()
    return {"success": success, "status": engine.status_msg}


@app.post("/api/robot/direct_teaching")
async def trigger_direct_teaching(payload: DirectTeachingPayload):
    success = engine.set_direct_teaching(payload.enable)
    return {"success": success, "direct_teaching": engine.direct_teaching, "status": engine.status_msg}


@app.post("/api/robot/speed")
async def set_speed_ratio(payload: SpeedPayload):
    with engine.lock:
        engine.speed_ratio = max(1, min(100, payload.speed_ratio))
    return {"success": True, "speed_ratio": engine.speed_ratio}


@app.post("/api/stop")
async def trigger_stop(payload: StopPayload):
    engine.set_stop(payload.active)
    return {"success": True, "stop_active": engine.stop_active, "msg": engine.status_msg}


@app.post("/api/toggle_sensor")
async def toggle_sensor():
    engine.mag_sensor = not engine.mag_sensor
    return {"success": True, "mag_sensor": engine.mag_sensor}


@app.post("/api/reset_pallet")
async def reset_pallet():
    with engine.lock:
        engine.pallet_count = 0
        engine.magazine_count = 8
        engine.held_workpiece = False
        engine.gripper_closed = False
        engine.slots = engine._init_slots()
        engine.status_msg = "Pallet & Feeder Stack Reset"
    return {"success": True, "pallet_count": 0, "magazine_count": 8}


# --- TEACH PENDANT CONTROLS (JOG & GRIPPER) ---
@app.post("/api/jog/joint")
async def jog_joint_endpoint(payload: JointJogPayload):
    success = engine.jog_joint(payload.joint_idx, payload.step_deg, payload.vel_ratio)
    return {"success": success, "status": engine.status_msg, "q": engine.q}


@app.post("/api/jog/task")
async def jog_task_endpoint(payload: TaskJogPayload):
    success = engine.jog_task(payload.axis, payload.step_val, payload.vel_ratio)
    return {"success": success, "status": engine.status_msg, "p": engine.p}


@app.post("/api/jog/stop")
async def jog_stop_endpoint():
    engine.stop_jog()
    return {"success": True}


@app.post("/api/tool/gripper")
async def set_gripper_endpoint(payload: GripperPayload):
    success = engine.set_gripper(payload.close)
    return {"success": success, "gripper_closed": engine.gripper_closed}


# --- WAYPOINTS MANAGEMENT ---
@app.get("/api/waypoints")
async def get_waypoints_endpoint():
    return engine.waypoints


@app.post("/api/waypoints")
async def save_waypoints_endpoint(payload: WaypointsListPayload):
    success = engine.save_waypoints(payload.waypoints)
    return {"success": success, "count": len(payload.waypoints)}


@app.post("/api/waypoints/acquire")
async def acquire_waypoint_endpoint(payload: AcquireWaypointPayload):
    wp = engine.acquire_current_waypoint(
        name=payload.name,
        move_type=payload.move_type,
        speed=payload.speed,
        gripper=payload.gripper,
        dwell=payload.dwell
    )
    return {"success": True, "waypoint": wp}


@app.post("/api/waypoints/goto")
async def goto_waypoint_endpoint(payload: WaypointIdPayload):
    engine.move_to_waypoint(payload.id)
    return {"success": True, "status": engine.status_msg}


@app.post("/api/waypoints/delete")
async def delete_waypoint_endpoint(payload: WaypointIdPayload):
    with engine.lock:
        engine.waypoints = [w for w in engine.waypoints if w["id"] != payload.id]
        engine.save_waypoints(engine.waypoints)
    return {"success": True, "count": len(engine.waypoints)}


# --- SEQUENCE EXECUTION ---
@app.post("/api/sequence/start")
async def start_sequence_endpoint(payload: SequencePayload):
    engine.start_waypoint_sequence(payload.repeat_count)
    return {"success": True, "status": engine.status_msg}


@app.post("/api/sequence/stop")
async def stop_sequence_endpoint():
    engine.abort_requested = True
    return {"success": True, "status": "Aborting Sequence..."}


# --- WEBSOCKET BROADCASTER (30 HZ) ---
class TelemetryBroadcaster:
    def __init__(self):
        self.active_sockets: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_sockets.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_sockets:
            self.active_sockets.remove(ws)

    async def broadcast(self, message: str):
        for ws in list(self.active_sockets):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(ws)


broadcaster = TelemetryBroadcaster()


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await broadcaster.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                cmd = msg.get("cmd")
                if cmd == "pb1":
                    engine.trigger_pb1_palletize()
                elif cmd == "pb2":
                    engine.trigger_pb2_put_back()
                elif cmd == "home":
                    engine.move_home()
                elif cmd == "zero":
                    engine.move_zero()
                elif cmd == "recover":
                    engine.recover_robot()
                elif cmd == "stop":
                    engine.set_stop(msg.get("active", True))
                elif cmd == "sensor":
                    engine.mag_sensor = not engine.mag_sensor
                elif cmd == "reconnect":
                    engine.connect_hardware(engine.robot_ip)
                elif cmd == "simulation":
                    engine.switch_to_simulation()
                elif cmd == "jog_joint":
                    engine.jog_joint(msg.get("joint_idx", 0), msg.get("step_deg", 0.0), msg.get("vel_ratio"))
                elif cmd == "jog_task":
                    engine.jog_task(msg.get("axis", "z"), msg.get("step_val", 0.0), msg.get("vel_ratio"))
                elif cmd == "jog_stop":
                    engine.stop_jog()
                elif cmd == "direct_teaching":
                    engine.set_direct_teaching(msg.get("enable", False))
                elif cmd == "gripper":
                    engine.set_gripper(msg.get("close", False))
                elif cmd == "goto_wp":
                    engine.move_to_waypoint(msg.get("id"))
                elif cmd == "start_sequence":
                    engine.start_waypoint_sequence(msg.get("repeat_count", 1))
                elif cmd == "stop_sequence":
                    engine.abort_requested = True
            except Exception:
                pass
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)


@app.on_event("startup")
async def start_telemetry_loop():
    engine.connect_hardware(DEFAULT_ROBOT_IP)

    async def loop():
        while True:
            packet = engine.get_telemetry_packet()
            await broadcaster.broadcast(json.dumps(packet))
            await asyncio.sleep(1.0 / TELEMETRY_HZ)

    asyncio.create_task(loop())
