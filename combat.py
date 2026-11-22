"""Target selection and trigger control for the Mech Royale game protocol."""
import math


def angle_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def bearing(a, b):
    dx, dy, dz = (b[i] - a[i] for i in range(3))
    return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))


class Combat:
    def __init__(self, hello):
        self.config = hello["config"]
        self.tick_s = 1 / self.config["match"]["tick_hz"]
        self.target = None
        self.attacks = {}
        self.last_bearing = None
        self.bearing_until = -1
        self.scan_sign = 1 if hello["seed"] % 2 else -1
        self.sight = {}
        self.blocked_since = {}
        self.arc = {}
        self.light_hold = False
        self.rear_bearing = None
        self.rear_until = -1.0

    def _observe(self, obs):
        t = obs["t"]
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.last_bearing = e["bearing"]
                self.bearing_until = t + 1.1
                self.attacks[e.get("by")] = t
                if e.get("rear"):
                    self.rear_bearing = e["bearing"]
                    self.rear_until = t + 0.90
        if self.bearing_until < t:
            cues = [s for s in obs["sounds"] if s["kind"] in
                    ("laser_charge", "gun", "laser", "mortar", "boost", "step", "lightning")]
            if cues:
                cue = max(cues, key=lambda s: ({"near": 3, "mid": 2, "far": 0}.get(s["dist"], 0)
                                               + (s["kind"] == "laser_charge"), -s["age"]))
                self.last_bearing = cue["bearing"]
                self.bearing_until = t + (0.45 if cue["dist"] != "far" else 0.2)

    def _body(self, target):
        kind = target.get("kind", "crate")
        section = self.config["mech" if kind == "mech" else "npc" if kind == "npc" else "crates"]
        scale = 1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0) if kind == "mech" else 1
        return scale * section["height"], scale * section["radius"]

    def _visible_height(self, arena, origin, target, now):
        pos = target["pos"]
        height, _ = self._body(target)
        cached = self.sight.get(target["id"])
        if cached and now - cached[0] < 0.18 and math.dist(origin, cached[1]) < 1.4 and math.dist(pos, cached[2]) < 1.4:
            return cached[3]
        chosen = None
        for frac in (0.52, 0.85, 0.22):
            if arena.clear(origin, (pos[0], pos[1], pos[2] + height * frac)):
                chosen = height * frac
                break
        if chosen is None:
            self.blocked_since.setdefault(target["id"], now)
        else:
            self.blocked_since.pop(target["id"], None)
        self.sight[target["id"]] = now, origin, tuple(pos), chosen
        return chosen

    def _select(self, obs, arena, origin, focus, weapons, quiet):
        me, now = obs["self"], obs["t"]
        last = self.target["id"] if self.target else None
        focus_id = focus.get("id") if focus else None
        candidates = []
        reach = max((w["stats"].get("range", w["stats"].get("speed", 0)**2 /
                                    max(1, w["stats"].get("gravity", 1))) for w in weapons.values()), default=0)
        for obj in (*obs["mechs"], *obs["crates"]):
            pos = obj["pos"]
            kind = obj.get("kind", "crate")
            d = math.dist(pos, me["pos"])
            if d > reach + 12 and (kind != "mech" or d > reach + 70):
                continue
            yaw = math.atan2(pos[1] - origin[1], pos[0] - origin[0])
            if kind == "mech":
                hp = obj.get("hp", self.config["mech"]["hp"])
                enemy_aim = obj.get("aim", (0, 0))[0]
                looking = abs(angle_delta(enemy_aim, yaw + math.pi)) < 0.35
                rate = (110 - 0.28 * d + 32 * max(0, 1 - hp / self.config["mech"]["hp"])
                        + 27 * (hp < 265) + 16 * (d < 48) + 13 * looking
                        + 14 * (now - self.attacks.get(obj["id"], -100) < 3))
                if obj.get("charging"):
                    rate += 14
                if "laser" in weapons and "mortar" in obj.get("weapons", ()):
                    rate += 11
                if d > reach:
                    rate -= 60
            else:
                if quiet or d > (110 if focus_id == obj["id"] else 70):
                    continue
                rate = 26 - 0.38 * d + 8 * (kind == "npc" and d < self.config["npc"]["aggro_range"])
                if focus_id == obj["id"]:
                    rate += 28
            rate += 9 * (last == obj["id"]) + 7 * (focus_id == obj["id"])
            rate -= 3 * abs(angle_delta(yaw, me["aim"][0]))
            cached = self.sight.get(obj["id"])
            if cached and cached[3] is None and now - cached[0] < 0.33:
                rate -= 32
            candidates.append((rate, obj["id"], obj))
        candidates.sort(key=lambda x: -x[0])
        # LOS checks are the expensive part. Limit the work even if everyone
        # converges behind a wall in the last circle.
        sheltered = None
        for _, _, obj in candidates[:5]:
            height = self._visible_height(arena, origin, obj, now)
            if height is not None:
                return obj, height, True
            if sheltered is None and obj.get("kind") == "mech":
                sheltered = obj
        if sheltered is not None and (now - self.blocked_since[sheltered["id"]] < 0.65 or
                                      (not quiet and "mortar" in weapons and weapons["mortar"]["cooldown"] < 0.24)):
            return sheltered, self._body(sheltered)[0] * 0.5, False
        return None, 0, False

    def _point(self, obj, height, flight):
        x, y, z = obj["pos"]
        vx, vy, vz = obj.get("vel", (0, 0, 0))
        # Ground movement extrapolates well over a bullet's flight. On long
        # shell flights the observed heading becomes progressively less useful.
        lead = min(flight, 1.05) + max(0, flight - 1.05) * 0.52
        zlead = min(0.53, flight)
        return x + vx * lead, y + vy * lead, z + height + vz * zlead

    def _gun_aim(self, origin, obj, height, speed, barrel):
        flight = 0
        for _ in range(3):
            point = self._point(obj, height, flight)
            flight = max(0, math.dist(point, origin) - barrel) / speed + self.tick_s * 0.35
        return bearing(origin, self._point(obj, height, flight))

    def _mortar_aim(self, arena, me, target, weapon, now, visible):
        stats = weapon["stats"]
        ox, oy, feet = me["pos"]
        oz = feet + me["stats"]["height"] + 0.5
        speed, gravity = stats["speed"], stats["gravity"]
        raw = target["pos"]
        if math.dist(raw, me["pos"]) < stats["splash_radius"] * 1.65:
            return None
        cached = self.arc.get(target["id"])
        if cached and now - cached[0] < 0.15 and math.dist(me["pos"], cached[1]) < 1.3 and math.dist(raw, cached[2]) < 1.4:
            return cached[3]
        result = None
        height = self._body(target)[0] * 0.37
        flight = max(0.01, (math.dist(me["pos"], raw) - 3) / speed)
        for high in ([False] if visible else [False, True]):
            valid = True
            for _ in range(4):
                tx, ty, tz = self._point(target, height, flight)
                dx, dy, dz = tx - ox, ty - oy, tz - oz
                distance = math.hypot(dx, dy)
                if distance < 1:
                    valid = False
                    break
                # Start the shell a barrel-length along the launch direction.
                # First solve the tangent for a conservative three-metre
                # shorter shot; then refine that offset at the computed pitch.
                pitch_guess = math.atan2(dz, distance) if _ == 0 else pitch
                barrel = me["stats"]["radius"] + 0.5
                run = max(0.5, distance - barrel * math.cos(pitch_guess))
                rise = dz - barrel * math.sin(pitch_guess)
                det = speed**4 - gravity * (gravity * run**2 + 2 * rise * speed**2)
                if det < 0:
                    valid = False
                    break
                root = math.sqrt(det)
                pitch = math.atan2(speed**2 + (root if high else -root), gravity * run)
                flight = run / max(1, speed * math.cos(pitch))
            if not valid or not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"] or flight > stats["ttl"]:
                continue
            yaw = math.atan2(dy, dx)
            direction = math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)
            start = (ox, oy, oz)
            barrel = me["stats"]["radius"] + 0.5
            # Use short chords near takeoff/impact so a parapet between the
            # muzzle and the moving target can invalidate the shot.
            segments = max(3, min(10, math.ceil(flight / 0.37)))
            for i in range(1, segments + 1):
                dt = flight * i / segments
                path = barrel + speed * dt
                end = (ox + path * direction[0], oy + path * direction[1],
                       oz + path * direction[2] - 0.5 * gravity * dt**2)
                if not arena.clear(start, end):
                    break
                start = end
            else:
                result = yaw, pitch
                break
        self.arc[target["id"]] = now, tuple(me["pos"]), tuple(raw), result
        return result

    def _aligned(self, wish, actual, command, me, radius, height, distance):
        if wish is None:
            return False
        stats = me["stats"]
        yaw = actual[0] + max(-stats["turn_rate"] * self.tick_s,
                              min(stats["turn_rate"] * self.tick_s, angle_delta(command[0], actual[0])))
        pitch = actual[1] + max(-stats["pitch_rate"] * self.tick_s,
                                min(stats["pitch_rate"] * self.tick_s, command[1] - actual[1]))
        return (abs(angle_delta(wish[0], yaw)) < math.atan2(radius, max(1, distance)) + 0.003
                and abs(wish[1] - pitch) < math.atan2(height, max(1, distance)) + 0.003)

    def _scan_yaw(self, now, actual):
        if self.bearing_until < now:
            return angle_delta(actual + self.scan_sign * 0.50, 0)
        # Search across a bearing, especially when repeated shots come from
        # behind a building. Keep the source in the armored front hemisphere.
        return angle_delta(self.last_bearing + 0.6 * math.sin(now * 1.7), 0)

    def act(self, obs, arena, focus=None, quiet=False):
        self._observe(obs)
        me, now = obs["self"], obs["t"]
        sx, sy, sz = me["pos"]
        actual = me["aim"]
        origin = (sx, sy, sz + me["stats"]["muzzle_height"])
        weapons = {w["kind"]: w for w in me["weapons"] if w}
        obj, height, visible = self._select(obs, arena, origin, focus, weapons, quiet)
        self.target = obj
        trigger = [False, False]
        gun = weapons.get("gun")
        if obj is None:
            return {"aim": [self._scan_yaw(now, actual[0]), 0.0], "fire": trigger,
                    "reload": bool(gun and gun["ammo"] <= gun["stats"]["mag"] - 2 and gun["reload"] == 0)}
        distance = math.dist(me["pos"], obj["pos"])
        direct = bearing(origin, self._point(obj, height, self.tick_s * 0.35))
        is_mech = obj.get("kind") == "mech"
        urgent = (is_mech and (distance < 27 or
                  (obj.get("hp", 10000) < 160 and distance < 100) or
                  (distance < 75 and now - self.attacks.get(obj["id"], -100) < 1.2)))
        may_fire = not quiet or urgent
        wishes = {"laser": direct, "lightning": direct}
        gun_in_range = bool(gun and distance <= gun["stats"]["range"] + 2)
        if gun and visible:
            wishes["gun"] = self._gun_aim(origin, obj, height, gun["stats"]["speed"], me["stats"]["radius"] + 0.5)
        mortar = weapons.get("mortar")
        if mortar and mortar["cooldown"] < 0.24 and may_fire:
            shell = self._mortar_aim(arena, me, obj, mortar, now, visible)
            if shell is not None:
                wishes["mortar"] = shell
        laser = weapons.get("laser")
        laser_in_range = bool(laser and distance <= laser["stats"]["range"] + 2)
        laser_release = False
        prealign = False
        if laser and laser_in_range and visible:
            charge = laser.get("charge", 0)
            stats = laser["stats"]
            multiplier = 1 + self.config["cubes"]["damage_per_cube"] * min(me["cubes"], self.config["cubes"]["stat_cap"])
            if "mortar" in obj.get("weapons", ()):
                multiplier *= 1 + stats["cookoff"]
            # Avoid waiting for full charge when a partial beam will finish.
            # Health is public only on some configurations, and the target
            # may have armor, so leave a small margin.
            hp = obj.get("hp", math.inf) * 1.12
            desired = (hp / (stats["damage"] * multiplier) - stats["min_frac"]) / max(0.01, 1 - stats["min_frac"])
            goal_charge = max(stats["min_charge"], min(1, desired) * stats["charge"])
            laser_release = charge > 0 and charge >= goal_charge - self.tick_s * 0.45
            prealign = charge > 0 and charge >= goal_charge - 0.1225
        command = direct
        if may_fire:
            if laser_release or prealign:
                command = direct
            elif "mortar" in wishes:
                command = wishes["mortar"]
            elif gun_in_range and visible and gun["reload"] < 0.15:
                command = wishes["gun"]
        if not visible and "mortar" not in wishes and now - self.blocked_since.get(obj["id"], now) >= 0.65:
            command = (self._scan_yaw(now, actual[0]), 0)
        # Non-blocking rear-armor face: ONLY when we cannot fire (not visible, OR gun reloading & laser not charging/ready).
        if self.rear_bearing is not None and now < self.rear_until:
            gun_dead = (not gun_in_range or gun["reload"] > 0.35 or gun["ammo"] <= 0)
            laser_busy = bool(laser and laser_in_range and visible and (laser.get("charge", 0) > 0 or laser.get("cooldown", 0) < 0.25))
            light_busy = bool("lightning" in weapons and visible and distance < weapons["lightning"]["stats"]["range"] + 1)
            if (not visible and "mortar" not in wishes) or (gun_dead and not laser_busy and not light_busy):
                command = (self.rear_bearing, 0.0)
        command = (command[0], min(me["stats"]["max_pitch"], max(me["stats"]["min_pitch"], command[1])))
        body_height, radius = self._body(obj)
        for i, w in enumerate(me["weapons"]):
            if not w or not may_fire:
                continue
            kind, stats = w["kind"], w["stats"]
            aligned = self._aligned(wishes.get(kind), actual, command, me, radius + 0.30, 0.42 * body_height, distance)
            if kind == "gun":
                trigger[i] = visible and gun_in_range and aligned and w["reload"] == 0 and w["ammo"] > 0
            elif kind == "laser" and laser_in_range and visible:
                # Releasing is irreversible. Confirm the reported torso has
                # actually reached the beam line after switching off gun lead.
                on_beam = self._aligned(direct, actual, actual, me, radius, 0.42 * body_height, distance)
                trigger[i] = not (laser_release and aligned and on_beam)
            elif kind == "lightning":
                cost = stats["energy"]
                mult = 1 + self.config["cubes"]["damage_per_cube"] * min(me["cubes"], self.config["cubes"]["stat_cap"])
                two_kill = obj.get("hp", 9999) <= 2.0 * stats["damage"] * mult
                if me["energy"] < cost:
                    self.light_hold = True
                if self.light_hold and (me["energy"] >= max(10 * cost, 0.30 * me["max_energy"]) or two_kill):
                    self.light_hold = False
                trigger[i] = (not self.light_hold and visible and distance < stats["range"] + 0.3 and
                              abs(angle_delta(actual[0], direct[0])) < math.radians(stats["cone_deg"]) * 0.85
                              and me["energy"] >= cost)
            elif kind == "mortar":
                trigger[i] = aligned
        reload = False
        if gun and gun["reload"] == 0 and gun["ammo"] < gun["stats"]["mag"]:
            reload = gun["ammo"] <= 0 or (not any(trigger) and
                ((quiet and not urgent) or not gun_in_range or (not visible and gun["ammo"] <= gun["stats"]["mag"] * 0.68)))
        return {"aim": list(command), "fire": trigger, "reload": reload}
