"""Short-horizon steering, silent movement, projectile & laser-charge dodging."""
import math


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(x, y):
    size = math.hypot(x, y)
    return (x / size, y / size) if size > 0.001 else (0.0, 0.0)


def angle_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def future_ring(zone, t, seconds):
    remaining = zone["shrink_end"] - max(t, zone["shrink_start"])
    active = max(0.0, t + seconds - max(t, zone["shrink_start"]))
    fraction = min(1.0, active / remaining) if remaining > 0 else 1.0
    center = tuple(a + (b - a) * fraction for a, b in zip(zone["center"], zone["next_center"]))
    return center, zone["radius"] + (zone["next_radius"] - zone["radius"]) * fraction


def inside_goal(pos, center, radius):
    dx, dy = pos[0] - center[0], pos[1] - center[1]
    scale = min(1.0, max(0.0, radius) / max(0.001, math.hypot(dx, dy)))
    return center[0] + dx * scale, center[1] + dy * scale


class Footwork:
    def __init__(self, hello):
        cfg = hello["config"]
        self.mortar = cfg["weapons"]["mortar"]
        self.step_speed = float(cfg.get("sound", {}).get("step_speed", 5.0))
        self.jet_until = -1.0
        self.boosting = False
        self.last_sample = None
        self.stuck_since = -1000.0

    def near_projectiles(self, obs, arena):
        me = obs["self"]
        pos, vel = me["pos"], me["vel"]
        height = me["stats"]["height"]
        bullets, shells = [], []
        for shot in obs.get("projectiles", []):
            p, v = shot["pos"], shot["vel"]
            if shot.get("kind") == "mortar":
                floor = pos[2] if distance(p, pos) < 15.0 else arena.ground(p[0], p[1])
                vz = v[2]
                grav = self.mortar["gravity"]
                t_hit = (vz + math.sqrt(max(0.0, vz * vz + 2.0 * grav * max(0.0, p[2] - floor)))) / grav
                if 0.0 <= t_hit <= 1.2:
                    impact = (p[0] + v[0] * t_hit, p[1] + v[1] * t_hit)
                    if distance(impact, pos) < 28.0:
                        shells.append((t_hit, impact[0], impact[1], 0.0, 0.0))
                continue
            rx, ry = p[0] - pos[0], p[1] - pos[1]
            vx, vy = v[0] - vel[0], v[1] - vel[1]
            t_hit = -(rx * vx + ry * vy) / max(1.0, vx * vx + vy * vy)
            if not (0.025 < t_hit < 0.85):
                continue
            z = p[2] + v[2] * t_hit - pos[2] - vel[2] * t_hit
            if -2.5 < z < height + 2.5 and math.hypot(rx + vx * t_hit, ry + vy * t_hit) < 16.0:
                nx, ny = unit(-vy, vx)
                bullets.append((t_hit, p[0] + v[0] * t_hit, p[1] + v[1] * t_hit, nx, ny))
        return sorted(bullets)[:7], sorted(shells)[:4]

    def laser_beams_charging(self, obs, arena):
        """Return list of (ex, ey, dx, dy) for enemies currently charging a laser near our line."""
        me = obs["self"]
        pos = me["pos"]
        my_mid = (pos[0], pos[1], pos[2] + me["stats"]["height"] * 0.5)
        hazards = []
        for m in obs.get("mechs", []):
            if m.get("kind") != "mech" or not m.get("charging"):
                continue
            ep = m["pos"]
            dx, dy = pos[0] - ep[0], pos[1] - ep[1]
            d = math.hypot(dx, dy)
            if d < 1.0 or d > 285.0:
                continue
            to_me = math.atan2(dy, dx)
            if abs(angle_delta(m["aim"][0], to_me)) < 0.45:
                e_muzzle = (ep[0], ep[1], ep[2] + 7.0 * (1.0 + 0.05 * m.get("cubes", 0)))
                if arena.clear(e_muzzle, my_mid):
                    ux, uy = math.cos(m["aim"][0]), math.sin(m["aim"][0])
                    hazards.append((ep[0], ep[1], ux, uy))
        return hazards

    def move(self, obs, arena, goal, mode, climbing=False, anchor_radius=0.0, silent=False, urgent_boost=False):
        me, t = obs["self"], obs["t"]
        pos, vel = me["pos"], me["vel"]
        stats, zone = me["stats"], obs["zone"]
        target_dist = distance(pos, goal)
        face_ahead = False

        if climbing and target_dist < 12.0:
            desired = unit(goal[0] - pos[0], goal[1] - pos[1])
        else:
            desired = arena.steer(pos, goal, t)
            face_ahead = arena.needs_jet

        ground = me["grounded"]
        moving = target_dist > max(1.0, anchor_radius)
        if not moving:
            desired = (0.0, 0.0)

        bullets, shells = self.near_projectiles(obs, arena)
        lasers = self.laser_beams_charging(obs, arena)
        danger = bool(bullets or shells or lasers)
        if danger:
            silent = False

        # Stuck detection (skip when intentionally holding or walking slowly)
        if self.last_sample is None or t - self.last_sample[0] > 1.1:
            if self.last_sample is not None:
                then, old, was_moving, was_silent = self.last_sample
                thresh = 0.8 if was_silent else 1.5
                if t - then < 2.2 and was_moving and moving and target_dist > 8.0 and distance(old, pos) < thresh:
                    self.stuck_since = t
            self.last_sample = (t, pos[:2], moving, silent)

        stuck = t - self.stuck_since < 0.95
        surface = arena.roof(goal[0], goal[1]) if climbing else 0.0
        altitude = goal[2] if climbing and len(goal) > 2 else surface
        needs_lift = climbing and altitude + 1.4 > pos[2]
        if needs_lift and (face_ahead or target_dist < 13.0 + min(12.0, max(0.0, altitude - pos[2]) * 0.27)):
            if not ground or me["energy"] > min(0.89 * me["max_energy"], 8.0 + (altitude - pos[2]) * 1.9):
                self.jet_until = t + 0.25
                desired = unit(goal[0] - pos[0], goal[1] - pos[1])
                silent = False
        elif stuck and moving and ground and me["energy"] > 26.0:
            self.jet_until = t + 0.58
            silent = False

        active_jet = t < self.jet_until
        reserve = 12.0 if (urgent_boost or lasers) else 25.0
        if climbing:
            reserve = min(me["max_energy"] * 0.93, max(39.0, 15.0 + (altitude - pos[2]) * 2.1))
        if me["energy"] < reserve:
            self.boosting = False
        if me["energy"] > reserve + (12.0 if (urgent_boost or lasers) else 28.0):
            self.boosting = True

        # Only boost when escaping, dodging, jetting, or urgently beating the zone!
        allow_boost = urgent_boost or bool(lasers) or bool(shells) or (mode == "rotate" and not silent)
        boost = active_jet or (ground and allow_boost and self.boosting and moving and not silent)
        jump = active_jet and ground
        if ground and not anchor_radius and not climbing and not silent and any(b[0] > 0.33 for b in bullets):
            jump = True
        if jump and not active_jet:
            boost = False

        if not active_jet and danger:
            desired = self.avoid(obs, arena, desired, bullets, shells, lasers, boost, goal, anchor_radius)

        if not moving and anchor_radius and (distance(pos, goal) + math.hypot(vel[0], vel[1]) * 0.23 > anchor_radius + 0.7):
            desired = unit(goal[0] - pos[0], goal[1] - pos[1])

        # Exact centering inside tiny final ring
        if zone["radius"] < 9.0:
            cx, cy = zone["center"]
            deviation = math.hypot(pos[0] - cx, pos[1] - cy)
            if deviation > max(0.18, zone["radius"] - 1.3) or mode == "last":
                if mode != "last":
                    desired = arena.steer(pos, (cx, cy), t)
                if deviation < 3.0 and not climbing:
                    desired = ((cx - pos[0]) * 0.65 - vel[0] * 0.10,
                               (cy - pos[1]) * 0.65 - vel[1] * 0.10)
                    spd = math.hypot(desired[0], desired[1])
                    if spd > 1.0:
                        desired = unit(desired[0], desired[1])
                    boost = jump = False
                    silent = False

        # Silent movement cap (< step_speed 5.0 m/s emits ZERO step sound!)
        if silent and moving and not active_jet:
            walk = max(1.0, float(stats.get("walk_speed", 11.7)))
            scale = min(1.0, max(0.12, (self.step_speed - 0.35) / walk))
            desired = (desired[0] * scale, desired[1] * scale)
            boost = False
            jump = False

        return {"move": [float(desired[0]), float(desired[1])], "boost": bool(boost), "jump": bool(jump)}

    def avoid(self, obs, arena, desired, bullets, shells, lasers, boost, goal, anchor_radius):
        me, pos = obs["self"], obs["self"]["pos"]
        speed = me["stats"]["boost_speed" if boost else "walk_speed"]
        accel = me["stats"]["ground_accel" if me["grounded"] else "air_accel"]
        center, radius = future_ring(obs["zone"], obs["t"], 0.7)
        circle_error = max(0.0, distance(pos, center) - radius + 1.2)
        if desired == (0.0, 0.0) and not bullets and not shells and not lasers:
            return desired
        yaw = math.atan2(desired[1], desired[0]) if desired != (0.0, 0.0) else 0.0
        choices = [desired] if desired != (0.0, 0.0) else []
        for angle in (0.0, 0.55, -0.55, 1.15, -1.15, 1.75, -1.75, 2.4, -2.4, math.pi):
            choices.append((math.cos(yaw + angle), math.sin(yaw + angle)))
        best, best_value = desired, -math.inf
        vx, vy = me["vel"][0], me["vel"][1]
        my_rad = me["stats"]["radius"]
        for ux, uy in choices:
            value = 2.6 * (desired[0] * ux + desired[1] * uy)
            if not arena.open_step(pos, (ux, uy), 5.2):
                value -= 9.5
            dx, dy = ux * speed - vx, uy * speed - vy
            change = math.hypot(dx, dy)
            transition = change / max(1.0, accel)
            for hazards, size, penalty in ((bullets, my_rad + 0.8, 7.5),
                                           (shells, self.mortar["splash_radius"] + 2.0, 12.0)):
                for t_hit, x, y, nx, ny in hazards:
                    f = min(t_hit, transition)
                    ratio = t_hit - f + f * f / (2.0 * max(0.001, transition))
                    rx = pos[0] + vx * t_hit + dx * ratio - x
                    ry = pos[1] + vy * t_hit + dy * ratio - y
                    d = abs(rx * nx + ry * ny) if (nx or ny) else math.hypot(rx, ry)
                    value -= penalty * max(0.0, 1.3 - d / max(1.0, size))
            # Charging laser dodge: strongly reward stepping behind cover or moving perpendicular to beam!
            end_x = pos[0] + ux * speed * 0.45
            end_y = pos[1] + uy * speed * 0.45
            end_z = arena.ground(end_x, end_y) + me["stats"]["height"] * 0.5
            for ex, ey, bx, by in lasers:
                # Perpendicular distance from beam line through (ex, ey) along (bx, by)
                perp = abs((end_x - ex) * (-by) + (end_y - ey) * bx)
                value += 1.6 * min(10.0, perp)
                if not arena.clear((ex, ey, pos[2] + 7.0), (end_x, end_y, end_z)):
                    value += 14.0
            end = (pos[0] + ux * speed * 0.55, pos[1] + uy * speed * 0.55)
            value -= 1.2 * max(0.0, distance(end, center) - radius + 1.2 - circle_error)
            if anchor_radius:
                value -= 2.0 * max(0.0, distance(end, goal) - anchor_radius)
            if value > best_value:
                best, best_value = (ux, uy), value
        return best
