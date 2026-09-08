# Configuration and calibrated constants for Indy7 3D Digital Twin & Palletizer.
import os

# Server configuration
SERVER_HOST = os.getenv("DIGITALTWIN_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("DIGITALTWIN_PORT", "8088"))
STARTUP_MODE = os.getenv("DIGITALTWIN_MODE", "SIMULATION").upper()
if STARTUP_MODE not in {"SIMULATION", "HARDWARE_LIVE"}:
    raise ValueError("DIGITALTWIN_MODE must be SIMULATION or HARDWARE_LIVE")
TELEMETRY_HZ = 30  # WebSocket push frequency

# Database & Storage
DEFAULT_DB_PATH = os.getenv("DIGITALTWIN_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "..", "data", "production.db"))
DEFAULT_RECIPE_ID = os.getenv("DIGITALTWIN_RECIPE_ID", "pallet-2x2x2-default")
DEFAULT_RECIPE_VERSION = os.getenv("DIGITALTWIN_RECIPE_VERSION", "1.0.0")

# Hardware Defaults
DEFAULT_ROBOT_IP = os.getenv("DIGITALTWIN_ROBOT_IP", "192.168.3.7")
DEFAULT_ROBOT_INDEX = 0

# Calibrated Workcell Task Coordinates [X, Y, Z (mm), U, V, W (deg)]
PICK_LOCATION            = [232.49, 514.57, 254.19, -19.52, -179.64, 90.03]
DROP_BASE_LOCATION       = [201.75, 219.29, 304.94, -3.24, -179.44, 90.01]
MAGAZINE_INSERT_LOCATION = [-8.15, 515.98, 343.32, -19.46, -177.65, 90.01]
HOME_JPOS                = [0.0, 0.0, -90.0, 0.0, -90.0, 0.0]

# Pallet Geometry
GRID_X = 2
GRID_Y = 2
SLOTS_PER_FLOOR = GRID_X * GRID_Y  # 4 slots
NUM_FLOORS = 2
TOTAL_MAX_ITEMS = SLOTS_PER_FLOOR * NUM_FLOORS  # 8 items

OFFSET_X = 80.0    # Row spacing (mm)
OFFSET_Y = 80.0    # Column spacing (mm)
LAYER_HEIGHT = 30.0  # Height between Floor 0 and Floor 1 (mm)

APPROACH_CLEARANCE_Z = 100.0  # Backoff along tool approach angle (mm)

# PLC Input Pin Mappings
DI_MAGAZINE_SENSOR = 3   # Magazine part presence sensor
DI_PB1             = 8   # PLC X103 -> PB1 (Palletize Loop)
DI_PB2             = 9   # PLC X104 -> PB2 (Put-Back LIFO)
DI_STOP            = 15  # PLC X107 -> Y167 -> Stop

# Gripper Output Channels
DO_GRIPPER_OPEN    = 0
DO_GRIPPER_CLOSE   = 1
GRIPPER_DWELL_SEC  = 0.5

# Velocities & Accelerations
TRANSIT_VEL_RATIO = 45
TRANSIT_ACC_RATIO = 45
ACTION_VEL_RATIO  = 25
ACTION_ACC_RATIO  = 25

# Pre-calibrated Default Recipes
DEFAULT_RECIPES = {
    "pallet-2x2x2-default": {
        "recipe_id": "pallet-2x2x2-default",
        "version": "1.0.0",
        "name": "Standard Dual-Layer Pallet (2x2x2)",
        "description": "Standard 8-slot palletizing across 2 layers (4 slots/floor) with calibrated approach.",
        "parameters": {
            "grid_x": 2,
            "grid_y": 2,
            "num_floors": 2,
            "slots_per_floor": 4,
            "total_slots": 8,
            "offset_x": 80.0,
            "offset_y": 80.0,
            "layer_height": 30.0,
            "approach_clearance_z": 100.0,
            "transit_vel_ratio": 45,
            "transit_acc_ratio": 45,
            "action_vel_ratio": 25,
            "action_acc_ratio": 25,
            "gripper_dwell_sec": 0.5,
        },
    },
    "pallet-2x2x1-single": {
        "recipe_id": "pallet-2x2x1-single",
        "version": "1.0.0",
        "name": "Single-Layer Flat Pallet (2x2x1)",
        "description": "Single-layer 4-slot layout for short production runs and quick batch validation.",
        "parameters": {
            "grid_x": 2,
            "grid_y": 2,
            "num_floors": 1,
            "slots_per_floor": 4,
            "total_slots": 4,
            "offset_x": 80.0,
            "offset_y": 80.0,
            "layer_height": 30.0,
            "approach_clearance_z": 100.0,
            "transit_vel_ratio": 45,
            "transit_acc_ratio": 45,
            "action_vel_ratio": 25,
            "action_acc_ratio": 25,
            "gripper_dwell_sec": 0.5,
        },
    },
    "pallet-high-speed": {
        "recipe_id": "pallet-high-speed",
        "version": "1.0.0",
        "name": "High-Speed Dual-Layer (2x2x2)",
        "description": "Accelerated motion profiles (vel: 70%, clearance: 70mm, dwell: 0.3s) for high throughput.",
        "parameters": {
            "grid_x": 2,
            "grid_y": 2,
            "num_floors": 2,
            "slots_per_floor": 4,
            "total_slots": 8,
            "offset_x": 80.0,
            "offset_y": 80.0,
            "layer_height": 30.0,
            "approach_clearance_z": 70.0,
            "transit_vel_ratio": 70,
            "transit_acc_ratio": 70,
            "action_vel_ratio": 35,
            "action_acc_ratio": 35,
            "gripper_dwell_sec": 0.3,
        },
    },
}

