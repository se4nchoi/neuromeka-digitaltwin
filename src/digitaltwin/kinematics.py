import math
import numpy as np
from typing import List, Tuple


class UnreachablePose(ValueError):
    """The configured kinematic model cannot track the requested pose/path."""


def joint_transforms(q):
    """Unrounded base-to-link transforms for the same Craig chain as the viewer."""
    transform = np.eye(4)
    frames = []
    for angle, p in zip(q, MDH_PARAMS):
        alpha, theta = np.radians([p['alpha'], p['theta0'] + angle])
        ca, sa, ct, st = np.cos(alpha), np.sin(alpha), np.cos(theta), np.sin(theta)
        transform = transform @ np.array([
            [ct, -st, 0, p['a']],
            [ca * st, ca * ct, -sa, -sa * p['d']],
            [sa * st, sa * ct, ca, ca * p['d']],
            [0, 0, 0, 1],
        ])
        frames.append(transform)
    return frames


def pose_transform(pose):
    """TCP [mm, mm, mm, roll°, pitch°, yaw°], R = Rz(yaw) Ry(pitch) Rx(roll)."""
    pose = np.asarray(pose, dtype=float)
    if pose.shape != (6,) or not np.isfinite(pose).all():
        raise UnreachablePose('TCP pose must contain six finite values')
    u, v, w = np.radians(pose[3:])
    cu, su, cv, sv, cw, sw = np.cos(u), np.sin(u), np.cos(v), np.sin(v), np.cos(w), np.sin(w)
    transform = np.eye(4)
    transform[:3, :3] = [
        [cw*cv, cw*sv*su-sw*cu, cw*sv*cu+sw*su],
        [sw*cv, sw*sv*su+cw*cu, sw*sv*cu-cw*su],
        [-sv, cv*su, cv*cu],
    ]
    transform[:3, 3] = pose[:3]
    return transform


def rotation_vector(rotation):
    """Shortest rotation logarithm, including the 180-degree case."""
    cosine = np.clip((np.trace(rotation) - 1) / 2, -1, 1)
    angle = np.arccos(cosine)
    skew = np.array([rotation[2, 1]-rotation[1, 2],
                     rotation[0, 2]-rotation[2, 0], rotation[1, 0]-rotation[0, 1]])
    if angle < 1e-7:
        return skew / 2
    if np.pi - angle < 1e-5:
        _, vectors = np.linalg.eigh((rotation + rotation.T) / 2)
        axis = vectors[:, -1]
        if np.dot(axis, skew) < 0:
            axis = -axis
        return angle * axis
    return angle * skew / (2 * np.sin(angle))


def rotation_exp(vector):
    angle = np.linalg.norm(vector)
    if angle < 1e-12:
        return np.eye(3)
    x, y, z = vector / angle
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle)*skew + (1-np.cos(angle))*(skew @ skew)


def inverse_kinematics(target, seed, max_iterations=100):
    """Seeded damped least squares; returns continuous joint angles in degrees.

    Target is a homogeneous transform. Tolerances are model-space, not hardware
    accuracy claims. No robot joint-limit or collision model is applied here.
    """
    q = np.asarray(seed, dtype=float).copy()
    if q.shape != (6,) or not np.isfinite(q).all():
        raise UnreachablePose('Joint seed must contain six finite values')
    scale = 300.0  # Normalize mm against angular error in radians.

    def error(transform):
        return np.r_[(target[:3, 3]-transform[:3, 3])/scale,
                     rotation_vector(target[:3, :3] @ transform[:3, :3].T)]

    for _ in range(max_iterations):
        frames = joint_transforms(q)
        current = frames[-1]
        residual = error(current)
        if np.linalg.norm(residual[:3])*scale < 0.02 and np.linalg.norm(residual[3:]) < 1e-4:
            return q.tolist()
        jacobian = np.column_stack([
            np.r_[np.cross(frame[:3, 2], current[:3, 3]-frame[:3, 3])/scale, frame[:3, 2]]
            for frame in frames
        ])
        delta = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T + 0.001**2*np.eye(6), residual)
        delta *= min(1.0, 0.2 / max(np.max(np.abs(delta)), 1e-12))
        # Backtracking avoids a large update making a near-singular pose worse.
        for factor in (1.0, 0.5, 0.25, 0.125, 0.0625):
            candidate = q + np.degrees(delta)*factor
            if np.linalg.norm(error(joint_transforms(candidate)[-1])) < np.linalg.norm(residual):
                q = candidate
                break
        else:
            break
    raise UnreachablePose('Cartesian path is unreachable or singular in the configured robot model')


def cartesian_trajectory(q_start, pose_target, duration, hz=60, cancelled=lambda: False):
    """Preflight a straight TCP path with shortest-rotation interpolation.

    Each sample uses the preceding solution as its seed. Fail before playback if
    the path cannot be followed continuously; never substitute a joint-space arc.
    """
    start = joint_transforms(q_start)[-1]
    end = pose_transform(pose_target)
    rotation = rotation_vector(end[:3, :3] @ start[:3, :3].T)
    seed = list(q_start)
    trajectory = []
    for tau in np.linspace(0, 1, max(int(duration * hz), 2)):
        if cancelled():
            raise InterruptedError('Cartesian planning stopped')
        blend = 10*tau**3 - 15*tau**4 + 6*tau**5
        target = np.eye(4)
        target[:3, 3] = start[:3, 3] + blend*(end[:3, 3]-start[:3, 3])
        target[:3, :3] = rotation_exp(blend*rotation) @ start[:3, :3]
        q = inverse_kinematics(target, seed)
        if np.max(np.abs(np.asarray(q)-seed)) > 15:
            raise UnreachablePose('Cartesian path requires a discontinuous joint step')
        trajectory.append(q)
        seed = q
    return trajectory

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
