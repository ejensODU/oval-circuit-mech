"""Short-horizon steering for mechfight/1. All distances are arena metres."""
import math


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(x, y):
    size = math.hypot(x, y)
    return (x / size, y / size) if size > 0.001 else (0.0, 0.0)


def future_ring(zone, t, seconds):
    remaining = zone["shrink_end"] - max(t, zone["shrink_start"])
    active = max(0, t + seconds - max(t, zone["shrink_start"]))
    fraction = min(1, active / remaining) if remaining > 0 else 1
    center = tuple(a + (b - a) * fraction for a, b in zip(zone["center"], zone["next_center"]))
    return center, zone["radius"] + (zone["next_radius"] - zone["radius"]) * fraction


def inside_goal(pos, center, radius):
    dx, dy = pos[0] - center[0], pos[1] - center[1]
    scale = min(1, max(0.0, radius) / max(0.001, math.hypot(dx, dy)))
    return center[0] + dx * scale, center[1] + dy * scale


class Footwork:
    def __init__(self, hello):
        self.phase = (hello["seed"] % 1709) / 1709 * math.tau
        self.mortar = hello["config"]["weapons"]["mortar"]
        self.jet_until = -1
        self.boosting = False
        self.last_sample = None
        self.stuck_since = -1000

    def orbit(self, pos, enemy, me, t, arena=None):
        # A wide strafe across the other mech's view works with all loadouts.
        # Tether gently toward nearby building cover so reload/laser-duck cover stays within 1 stride!
        axis = unit(enemy["pos"][0] - pos[0], enemy["pos"][1] - pos[1])
        kinds = {w["kind"]: w["stats"] for w in me["weapons"] if w}
        distance_now = distance(pos, enemy["pos"])
        ideal = 125 if "laser" in kinds else 115
        if "lightning" in kinds:
            ideal = kinds["lightning"]["range"] * 0.72
        elif "lightning" in enemy.get("weapons", []):
            ideal = 117
        side = 1 if math.sin(t * 1.05 + self.phase + 0.55 * math.sin(t * 0.7)) >= 0 else -1
        if arena is not None and "lightning" not in kinds:
            lx, ly = -axis[1], axis[0]
            cl_pos = arena.ground_clearance(pos[0] + lx * 8.0, pos[1] + ly * 8.0)
            cl_neg = arena.ground_clearance(pos[0] - lx * 8.0, pos[1] - ly * 8.0)
            if side == 1 and cl_pos > 16.0 and 2.8 <= cl_neg <= 11.0 and arena.open_step(pos, (-lx, -ly), 6.0):
                side = -1
            elif side == -1 and cl_neg > 16.0 and 2.8 <= cl_pos <= 11.0 and arena.open_step(pos, (lx, ly), 6.0):
                side = 1
        radial = max(-1.65, min(1.35, (distance_now - ideal) / 24))
        return unit(radial * axis[0] - side * axis[1], radial * axis[1] + side * axis[0])

    def near_projectiles(self, obs, arena):
        me = obs["self"]
        pos, vel = me["pos"], me["vel"]
        height = me["stats"]["height"]
        bullets, shells = [], []
        for shot in obs["projectiles"]:
            p, v = shot["pos"], shot["vel"]
            if shot["kind"] == "mortar":
                # Approximate first local ground impact. The regular strafe is
                # still used where the map makes this prediction uncertain.
                floor = pos[2] if distance(p, pos) < 15 else arena.ground(*p[:2])
                vz = v[2]
                grav = self.mortar["gravity"]
                time = (vz + math.sqrt(max(0, vz * vz + 2 * grav * max(0, p[2] - floor)))) / grav
                if 0 <= time <= 1.05:
                    impact = p[0] + v[0] * time, p[1] + v[1] * time
                    if distance(impact, pos) < 28:
                        shells.append((time, *impact, 0.0, 0.0))
                continue
            rx, ry = p[0] - pos[0], p[1] - pos[1]
            vx, vy = v[0] - vel[0], v[1] - vel[1]
            time = -(rx * vx + ry * vy) / max(1, vx * vx + vy * vy)
            if not 0.025 < time < 0.85:
                continue
            z = p[2] + v[2] * time - pos[2] - vel[2] * time
            if -2.5 < z < height + 2.5 and math.hypot(rx + vx*time, ry + vy*time) < 16:
                normal = unit(-vy, vx)
                bullets.append((time, p[0] + v[0]*time, p[1] + v[1]*time, *normal))
        return sorted(bullets)[:7], sorted(shells)[:4]

    def move(self, obs, arena, goal, mode, enemy, climbing, anchor_radius=0):
        me, t = obs["self"], obs["t"]
        pos, vel = me["pos"], me["vel"]
        stats, zone = me["stats"], obs["zone"]
        target_dist = distance(pos, goal)
        face_ahead = False
        if mode == "duel" and enemy is not None:
            desired = self.orbit(pos, enemy, me, t, arena)
        elif climbing and target_dist < 12:
            desired = unit(goal[0] - pos[0], goal[1] - pos[1])
        else:
            desired = arena.steer(pos, goal, t)
            face_ahead = arena.needs_jet
        ground = me["grounded"]
        moving = target_dist > max(1.0, anchor_radius)
        if mode == "duel":
            moving = True
        if not moving:
            desired = (0, 0)
        bullets, shells = self.near_projectiles(obs, arena)
        danger = bool(bullets or shells)
        if self.last_sample is None or t - self.last_sample[0] > 0.95:
            if self.last_sample is not None:
                then, old, was_moving = self.last_sample
                if t - then < 2 and was_moving and moving and target_dist > 8 and distance(old, pos) < 1.5:
                    self.stuck_since = t
            self.last_sample = (t, pos[:2], moving)

        stuck = t - self.stuck_since < 0.95
        surface = arena.roof(*goal[:2]) if climbing else 0
        altitude = goal[2] if climbing and len(goal) > 2 else surface
        needs_lift = climbing and altitude + 1.4 > pos[2]
        if needs_lift and (face_ahead or target_dist < 13 + min(12, max(0, altitude-pos[2]) * 0.27)):
            if not ground or me["energy"] > min(0.89 * me["max_energy"], 8 + (altitude - pos[2]) * 1.9):
                self.jet_until = t + 0.23
                desired = unit(goal[0] - pos[0], goal[1] - pos[1])
        elif stuck and moving and ground and me["energy"] > 26:
            self.jet_until = t + 0.58

        active_jet = t < self.jet_until
        mobile = mode in ("duel", "retreat", "rotate") or (moving and target_dist > 16)
        reserve = 24 if mode == "duel" else 13
        if climbing:
            reserve = min(me["max_energy"] * 0.93, max(39, 15 + (altitude - pos[2]) * 2.1))
        if me["energy"] < reserve:
            self.boosting = False
        if me["energy"] > reserve + 28:
            self.boosting = True
        boost = active_jet or (ground and mobile and self.boosting and moving)
        # A leap changes vertical velocity in time for slower, distant rounds.
        jump = active_jet and ground
        if ground and not anchor_radius and not climbing and any(b[0] > 0.33 for b in bullets):
            jump = True
        if jump and not active_jet:
            boost = False

        if not active_jet and (danger or mode == "duel"):
            desired = self.avoid(obs, arena, desired, bullets, shells, boost, goal, anchor_radius)
        if not moving and anchor_radius and (distance(pos, goal) + math.hypot(*vel[:2]) * 0.23 > anchor_radius + 0.7):
            desired = unit(goal[0] - pos[0], goal[1] - pos[1])
        # A vanishing ring punishes lateral drift more than a standing duel.
        if zone["radius"] < 9:
            cx, cy = zone["center"]
            deviation = math.hypot(pos[0] - cx, pos[1] - cy)
            if deviation > max(0.18, zone["radius"] - 1.3) or mode == "last":
                if mode != "last":
                    desired = arena.steer(pos, (cx, cy), t)
                if deviation < 3 and not climbing:
                    desired = ((cx - pos[0]) * 0.65 - vel[0] * 0.10,
                               (cy - pos[1]) * 0.65 - vel[1] * 0.10)
                    speed = math.hypot(*desired)
                    if speed > 1:
                        desired = unit(*desired)
                    boost = jump = False
        return {"move": list(desired), "boost": bool(boost), "jump": bool(jump)}

    def avoid(self, obs, arena, desired, bullets, shells, boost, goal, anchor_radius):
        me, pos = obs["self"], obs["self"]["pos"]
        speed = me["stats"]["boost_speed" if boost else "walk_speed"]
        if any(w and w.get("charge", 0) > 0 for w in me["weapons"]):
            speed *= me["stats"]["charge_move_mult"]
        accel = me["stats"]["ground_accel" if me["grounded"] else "air_accel"]
        center, radius = future_ring(obs["zone"], obs["t"], 0.7)
        circle_error = max(0, distance(pos, center) - radius + 1.2)
        if desired == (0, 0) and not bullets and not shells:
            return desired
        yaw = math.atan2(desired[1], desired[0])
        choices = [desired]
        for angle in (0.62, -0.62, 1.35, -1.35, 2.30, -2.30, math.pi):
            choices.append((math.cos(yaw + angle), math.sin(yaw + angle)))
        best, best_value = desired, -math.inf
        vx, vy = me["vel"][:2]
        has_light = any(w and w["kind"] == "lightning" for w in me["weapons"])
        light_foes = ([] if has_light else
                      [m["pos"] for m in obs["mechs"] if m.get("kind") == "mech"
                       and "lightning" in m.get("weapons", ()) and distance(pos, m["pos"]) < 72.0])
        for ux, uy in choices:
            value = 2.5 * (desired[0] * ux + desired[1] * uy)
            if not arena.open_step(pos, (ux, uy), 5.2):
                value -= 9
            dx, dy = ux * speed - vx, uy * speed - vy
            change = math.hypot(dx, dy)
            # Accelerate towards the chosen velocity, then coast.
            transition = change / max(1, accel)
            for hazards, size, penalty in ((bullets, me["stats"]["radius"] + 0.8, 7),
                                            (shells, self.mortar["splash_radius"] + 2, 11)):
                for time, x, y, nx, ny in hazards:
                    f = min(time, transition)
                    ratio = (time - f + f * f / (2 * max(0.001, transition)))
                    rx = pos[0] + vx*time + dx*ratio - x
                    ry = pos[1] + vy*time + dy*ratio - y
                    # Sideways clearance avoids the swept bullet. Moving down
                    # its path merely changes the instant of contact.
                    d = abs(rx*nx + ry*ny) if nx or ny else math.hypot(rx, ry)
                    value -= penalty * max(0, 1.3 - d / max(1, size))
            end = pos[0] + ux * speed * 0.55, pos[1] + uy * speed * 0.55
            value -= 1.1 * max(0, distance(end, center) - radius + 1.2 - circle_error)
            for lpos in light_foes:
                ld = distance(end, lpos)
                if ld < 49.0:
                    value -= 16.0 * (1.0 - ld / 49.0)
            if anchor_radius:
                value -= 2 * max(0, distance(end, goal) - anchor_radius)
            if value > best_value:
                best, best_value = (ux, uy), value
        return best
