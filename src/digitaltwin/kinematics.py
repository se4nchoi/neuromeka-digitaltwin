import math
import numpy as np
from typing import List, Tuple

# Exact MDH Parameters for Neuromeka Indy7
MDH_PARAMS = [
    {'d': 300.0, 'a': 0.0,   'alpha': 0.0,  'theta0': 0.0},
    {'d': 0.0,   'a': 0.0,   'alpha': 90.0, 'theta0': 90.0},
    {'d': 3.5,   'a': 450.0, 'alpha': 0.0,  'theta0': 90.0},
    {'d': 350.0, 'a': 0.0,   'alpha': 90.0, 'theta0': 180.0},
    {'d': 183.0, 'a': 0.0,   'alpha': 90.0, 'theta0': 180.0},
    {'d': 228.0, 'a': 0.0,   'alpha': 90.0, 'theta0': 180.0},
]

# Verified Calibrated Waypoint Joint Angles for Indy7 [deg]
KNOWN_JOINTS = {
    'home': [0.0, 0.0, -90.0, 0.0, -90.0, 0.0],
    'pick': [76.12, -37.44, -74.59, 19.62, -74.11, -20.29],
    'pick_approach': [74.50, -29.20, -78.10, 19.50, -73.80, -20.00],
    'mag_insert': [101.96, -23.45, -82.03, 20.36, -73.66, 6.71],
    'mag_insert_approach': [100.80, -14.50, -85.20, 20.20, -73.50, 6.50],
    'slot_0': [83.01, 3.74, -129.91, 3.85, -54.84, -9.23],
    'slot_1': [84.74, -5.57, -120.34, 3.88, -55.01, -7.50],
    'slot_2': [104.58, 13.25, -137.10, 3.96, -55.94, 12.38],
    'slot_3': [100.23, 0.22, -126.47, 4.08, -53.80, 7.83],
    'slot_4': [83.02, 6.60, -126.51, 3.60, -61.09, -8.74],
    'slot_5': [84.75, -3.02, -117.32, 3.65, -60.57, -7.06],
    'slot_6': [104.60, 16.27, -133.28, 3.69, -62.76, 12.92],
    'slot_7': [100.24, 2.99, -123.24, 3.81, -59.78, 8.33],
    'slot_0_approach': [82.80, -4.50, -120.50, 3.85, -54.80, -9.20],
    'slot_1_approach': [84.50, -13.80, -111.00, 3.88, -55.00, -7.50],
    'slot_2_approach': [104.20, 4.80, -127.50, 3.96, -55.90, 12.30],
    'slot_3_approach': [99.80, -8.00, -117.00, 4.08, -53.80, 7.80],
    'slot_4_approach': [82.80, -1.80, -117.00, 3.60, -61.00, -8.70],
    'slot_5_approach': [84.50, -11.20, -108.00, 3.65, -60.50, -7.00],
    'slot_6_approach': [104.20, 7.50, -124.00, 3.69, -62.70, 12.90],
    'slot_7_approach': [99.80, -5.20, -114.00, 3.81, -59.70, 8.30],
}

def get_approach_pose(target_pose: List[float], clearance: float = 100.0) -> List[float]:
    u = math.radians(target_pose[3])
    v = math.radians(target_pose[4])
    w = math.radians(target_pose[5])

    cu, su = math.cos(u), math.sin(u)
    cv, sv = math.cos(v), math.sin(v)
    cw, sw = math.cos(w), math.sin(w)

    zx = cw * sv * cu + sw * su
    zy = sw * sv * cu - cw * su
    zz = cv * cu

    return [
        target_pose[0] - clearance * zx,
        target_pose[1] - clearance * zy,
        target_pose[2] - clearance * zz,
        target_pose[3],
        target_pose[4],
        target_pose[5],
    ]

def quintic_interpolate(q_start: List[float], q_end: List[float], duration_sec: float, hz: int = 60) -> List[List[float]]:
    total_steps = max(int(duration_sec * hz), 2)
    trajectory = []
    
    q_s = np.array(q_start, dtype=float)
    q_e = np.array(q_end, dtype=float)
    delta = q_e - q_s

    for step in range(total_steps):
        tau = step / (total_steps - 1)
        s = 10.0 * (tau ** 3) - 15.0 * (tau ** 4) + 6.0 * (tau ** 5)
        q_current = q_s + s * delta
        trajectory.append([round(float(x), 3) for x in q_current])

    return trajectory

def forward_kinematics_craig(q: List[float]) -> List[float]:
    W = np.eye(4)
    for i in range(6):
        p = MDH_PARAMS[i]
        alpha = math.radians(p['alpha'])
        theta = math.radians(p['theta0'] + q[i])
        a = p['a']
        d = p['d']
        ca, sa = math.cos(alpha), math.sin(alpha)
        ct, st = math.cos(theta), math.sin(theta)
        T = np.array([
            [ct, -st, 0, a],
            [ca * st, ca * ct, -sa, -sa * d],
            [sa * st, sa * ct, ca, ca * d],
            [0, 0, 0, 1]
        ])
        W = W @ T

    x, y, z = W[0, 3], W[1, 3], W[2, 3]
    R = W[:3, :3]
    beta = math.atan2(-R[2, 0], math.sqrt(R[0, 0]**2 + R[1, 0]**2))
    if abs(math.cos(beta)) > 1e-6:
        alpha_ang = math.atan2(R[2, 1], R[2, 2])
        gamma = math.atan2(R[1, 0], R[0, 0])
    else:
        alpha_ang = 0.0
        gamma = math.atan2(-R[0, 1], R[1, 1])

    u = math.degrees(alpha_ang)
    v = math.degrees(beta)
    w = math.degrees(gamma)
    return [round(float(x), 2), round(float(y), 2), round(float(z), 2), round(float(u), 2), round(float(v), 2), round(float(w), 2)]
