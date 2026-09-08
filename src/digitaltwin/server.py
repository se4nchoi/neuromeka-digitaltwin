import os
import asyncio
import json
from typing import List, Optional
from contextlib import asynccontextmanager, suppress
from .commands import CommandService
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import SERVER_HOST, SERVER_PORT, TELEMETRY_HZ, DEFAULT_ROBOT_IP
from .palletizer_engine import PalletizerEngine

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(engine.start)
    task = asyncio.create_task(telemetry_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(engine.close)


app = FastAPI(lifespan=lifespan, title="Indy7 3D Digital Twin Workcell & Multi-Purpose Center", version="2.0.0")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

engine = PalletizerEngine()
commands = CommandService(engine)


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
    return await asyncio.to_thread(commands.execute, "connect", **payload.model_dump())


@app.post("/api/reconnect")
async def reconnect_robot():
    return await asyncio.to_thread(commands.execute, "reconnect")


@app.post("/api/disconnect")
async def disconnect_robot():
    return await asyncio.to_thread(commands.execute, "disconnect")


@app.post("/api/simulation")
async def switch_to_simulation():
    return await asyncio.to_thread(commands.execute, "simulation")


# --- PROGRAMS (PALLETIZING, HOME, ZERO, RECOVER) ---
@app.post("/api/pb1")
async def trigger_pb1():
    return await asyncio.to_thread(commands.execute, "pb1")


@app.post("/api/pb2")
async def trigger_pb2():
    return await asyncio.to_thread(commands.execute, "pb2")


@app.post("/api/robot/home")
async def trigger_home():
    return await asyncio.to_thread(commands.execute, "home")


@app.post("/api/robot/zero")
async def trigger_zero():
    return await asyncio.to_thread(commands.execute, "zero")


@app.post("/api/robot/recover")
async def trigger_recover():
    return await asyncio.to_thread(commands.execute, "recover")


@app.post("/api/robot/direct_teaching")
async def trigger_direct_teaching(payload: DirectTeachingPayload):
    return await asyncio.to_thread(commands.execute, "direct_teaching", **payload.model_dump())


@app.post("/api/robot/speed")
async def set_speed_ratio(payload: SpeedPayload):
    return await asyncio.to_thread(commands.execute, "speed", **payload.model_dump())


@app.post("/api/stop")
async def trigger_stop(payload: StopPayload):
    return await asyncio.to_thread(commands.execute, "stop", **payload.model_dump())


@app.post("/api/toggle_sensor")
async def toggle_sensor():
    return await asyncio.to_thread(commands.execute, "sensor")


@app.post("/api/reset_pallet")
async def reset_pallet():
    return await asyncio.to_thread(commands.execute, "reset_pallet")


# --- TEACH PENDANT CONTROLS (JOG & GRIPPER) ---
@app.post("/api/jog/joint")
async def jog_joint_endpoint(payload: JointJogPayload):
    return await asyncio.to_thread(commands.execute, "jog_joint", **payload.model_dump())


@app.post("/api/jog/task")
async def jog_task_endpoint(payload: TaskJogPayload):
    return await asyncio.to_thread(commands.execute, "jog_task", **payload.model_dump())


@app.post("/api/jog/stop")
async def jog_stop_endpoint():
    return await asyncio.to_thread(commands.execute, "jog_stop")


@app.post("/api/tool/gripper")
async def set_gripper_endpoint(payload: GripperPayload):
    return await asyncio.to_thread(commands.execute, "gripper", **payload.model_dump())


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
    return await asyncio.to_thread(commands.execute, "goto_wp", **payload.model_dump())


@app.post("/api/waypoints/delete")
async def delete_waypoint_endpoint(payload: WaypointIdPayload):
    with engine.lock:
        engine.waypoints = [w for w in engine.waypoints if w["id"] != payload.id]
        engine.save_waypoints(engine.waypoints)
    return {"success": True, "count": len(engine.waypoints)}


# --- SEQUENCE EXECUTION ---
@app.post("/api/sequence/start")
async def start_sequence_endpoint(payload: SequencePayload):
    return await asyncio.to_thread(commands.execute, "start_sequence", **payload.model_dump())


@app.post("/api/sequence/stop")
async def stop_sequence_endpoint():
    return await asyncio.to_thread(commands.execute, "stop_sequence")


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
                await asyncio.wait_for(ws.send_text(message), timeout=0.25)
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
                if not isinstance(msg, dict):
                    raise ValueError("Command must be a JSON object")
                cmd = msg.pop("cmd", "")
                result = await asyncio.to_thread(commands.execute, cmd, **msg)
                await websocket.send_json({"type": "command_result", **result})
            except (ValueError, TypeError):
                await websocket.send_json({"type": "command_result", "status": "rejected",
                                           "success": False, "message": "Invalid command JSON"})
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)


async def telemetry_loop():
    while True:
        packet = engine.get_telemetry_packet()
        await broadcaster.broadcast(json.dumps(packet))
        await asyncio.sleep(1.0 / TELEMETRY_HZ)


@app.post("/api/commands")
async def execute_command(payload: dict):
    payload = dict(payload)
    cmd = payload.pop("cmd", "")
    return await asyncio.to_thread(commands.execute, cmd, **payload)


@app.get("/api/commands/{command_id}")
async def get_command(command_id: str):
    with engine.lock:
        record = next((dict(c) for c in engine.commands if c["command_id"] == command_id), None)
    if record is None:
        run = await asyncio.to_thread(engine.storage.get_run, command_id)
        if run:
            record = {
                "command_id": run["run_id"],
                "cmd": run["command"],
                "status": run["status"],
                "success": run["status"] == "completed",
                "submitted_at": run["start_time"],
                "finished_at": run["end_time"],
                "message": run.get("error_message") or ("Run completed" if run["status"] == "completed" else run["status"]),
            }
    if record is None:
        raise HTTPException(404, "Command not found in memory history or persistent storage")
    return record


# --- PRODUCTION PERSISTENCE & KPI ENDPOINTS ---
@app.get("/api/production/runs")
async def list_production_runs(limit: int = 50, offset: int = 0, status: Optional[str] = None):
    return await asyncio.to_thread(engine.storage.list_runs, limit=limit, offset=offset, status=status)


@app.get("/api/production/runs/{run_id}")
async def get_production_run(run_id: str):
    run = await asyncio.to_thread(engine.storage.get_run, run_id)
    if run is None:
        raise HTTPException(404, f"Production run {run_id} not found")
    return run


@app.get("/api/production/events")
async def list_production_events(limit: int = 100):
    return await asyncio.to_thread(engine.storage.list_events, limit=limit)


@app.get("/api/production/kpi/summary")
async def get_kpi_summary():
    return await asyncio.to_thread(engine.storage.calculate_kpi_summary)


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": engine.mode, "workcell_state": engine.workcell_state}
