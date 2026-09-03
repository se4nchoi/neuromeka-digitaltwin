# Configuration and calibrated constants for Indy7 3D Digital Twin & Palletizer.

# Server configuration
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8088
TELEMETRY_HZ = 30  # WebSocket push frequency

# Hardware Defaults
DEFAULT_ROBOT_IP = "192.168.3.7"
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
