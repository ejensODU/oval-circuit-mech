"""Fire control for a single selected arena actor and two shared-aim slots."""
import math


def clamp(value, lo, hi):
    return min(hi, max(lo, value))


def yaw_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def angles(start, end):
    x, y, z = (b - a for a, b in zip(start, end))
    return math.atan2(y, x), math.atan2(z, math.hypot(x, y))


class Combat:
    def __init__(self, hello, arena=None):
        self.cfg = hello["config"]
        match = self.cfg["match"]
        self.tick = 1 / match["tick_hz"]
        self.step = self.tick * match["decide_every"]
        self.arena = arena
        self.last_laser_line = -math.inf
        self.last_enemy_line = -math.inf
        self.refilling_arc = False
        self.lens_effects = {
            upgrade["id"]: tuple(e for e in upgrade.get("effects", ())
                                 if e["path"] == "weapons.laser.damage")
            for upgrade in self.cfg.get("upgrades", ())
            if any(e["path"] == "weapons.laser.damage" for e in upgrade.get("effects", ()))
        }

    def body(self, actor):
        kind = actor.get("kind", "crate")
        section = {"mech": "mech", "crate": "crates"}.get(kind, "npc")
        size = 1.0
        if kind == "mech":
            size += actor.get("cubes", 0) * self.cfg["cubes"]["size_per_cube"]
        body = self.cfg[section]
        return size * body["height"], size * body["radius"]

    def clear(self, start, end):
        return self.arena is None or self.arena.clear(start, end)

    def point(self, actor, height, dt):
        x, y, z = actor["pos"]
        vx, vy, vz = actor.get("vel", (0, 0, 0))
        # A direction seen now conveys little information several seconds out.
        # All gun leads use unattenuated horizontal velocity. Shells approach
        # a bounded displacement as the uncertainty in a long lob increases.
        travel = dt if dt <= 1.0 else 1.0 - math.expm1(1.0 - dt)
        x, y = x + travel * vx, y + travel * vy
        if abs(vz) > 0.5:
            vertical_dt = min(dt, 0.65)
            z += vz * vertical_dt - 0.5 * self.cfg["mech"]["gravity"] * vertical_dt**2
            if self.arena:
                z = max(z, self.arena.ground(x, y))
        return x, y, z + height

    def muzzle(self, me, height, dt):
        p = me["pos"]
        v = me["vel"]
        return p[0]+dt*v[0], p[1]+dt*v[1], p[2]+dt*v[2]+height

    def gun_line(self, me, target, offset, weapon, barrel):
        stats = weapon["stats"]
        delay = clamp(weapon.get("cooldown", 0), self.tick, self.step)
        muzzle = self.muzzle(me, me["stats"]["muzzle_height"], delay)
        flight = math.dist(muzzle, self.point(target, offset, delay)) / stats["speed"]
        # Fixed point contraction is bounded by target speed / shot speed.
        for _ in range(3):
            hit = self.point(target, offset, delay + flight)
            flight = max(0.0, math.dist(muzzle, hit) - barrel) / stats["speed"]
        if flight * stats["speed"] > stats["range"] or not self.clear(muzzle, hit):
            return None
        return angles(muzzle, hit)

    def shell_line(self, me, target, height, weapon, barrel):
        stats = weapon["stats"]
        body = me["stats"]
        speed, grav = stats["speed"], stats["gravity"]
        delay = clamp(weapon.get("cooldown", 0), self.tick, self.step)
        start = self.muzzle(me, body["height"] + 0.5, delay)
        dist = math.dist(target["pos"], me["pos"])
        if dist < stats["splash_radius"] + body["radius"] + barrel:
            return None
        for high in (False, True):
            # Refine flight time, target motion and the displaced shot origin.
            flight = min(stats["ttl"], 1.7 * speed / grav if high else dist / speed)
            pitch = 1.1 if high else 0.0
            solution = self._arc(start, target, height, stats, body, barrel, delay, high, flight, pitch)
            if solution:
                return solution
        return None

    def _arc(self, start, target, height, stats, body, barrel, delay, high, flight, pitch):
        speed, grav = stats["speed"], stats["gravity"]
        for _ in range(6):
            hit = self.point(target, 0.32 * height, flight + delay)
            dx, dy, dz = (b - a for a, b in zip(start, hit))
            run = math.hypot(dx, dy) - barrel * math.cos(pitch)
            rise = dz - barrel * math.sin(pitch)
            disc = speed**4 - grav * (grav * run**2 + 2 * rise * speed**2)
            if run < 0.2 or disc < 0:
                return None
            root = math.sqrt(disc)
            pitch = math.atan2(speed**2 + (root if high else -root), grav * run)
            flight = run / (speed * math.cos(pitch))
        if not body["min_pitch"] <= pitch <= body["max_pitch"] or flight > stats["ttl"]:
            return None
        yaw = math.atan2(dy, dx)
        ray = math.cos(yaw)*math.cos(pitch), math.sin(yaw)*math.cos(pitch), math.sin(pitch)
        segments = max(3, math.ceil(flight / 0.38))
        last = start
        for i in range(1, segments+1):
            t = i * flight / segments
            path = barrel + speed * t
            end = (start[0] + path*ray[0], start[1] + path*ray[1],
                   start[2] + path*ray[2] - grav*t*t*0.5)
            if not self.clear(last, end):
                return None
            last = end
        return yaw, pitch

    def charge_needed(self, me, target, laser):
        stats = laser["stats"]
        cubes = self.cfg["cubes"]
        factor = 1 + cubes["damage_per_cube"] * min(me.get("cubes", 0), cubes["stat_cap"])
        damage = stats["damage"]
        if factor != 1:
            # Anchor the two possible wire representations to the configured
            # laser and this mech's upgrades. Apply cube scaling exactly once.
            lens_damage = self.cfg["weapons"]["laser"]["damage"]
            for upgrade in me.get("upgrades", ()):
                for effect in self.lens_effects.get(upgrade, ()):
                    lens_damage = lens_damage * effect.get("mul", 1) + effect.get("add", 0)
            if abs(damage - lens_damage) <= abs(damage - lens_damage * factor):
                damage *= factor
        if "mortar" in target.get("weapons", ()):
            damage *= 1 + stats["cookoff"]
        # Allow a margin for hidden armor on mechs. Crate HP and armor are exact.
        hp = target.get("hp", math.inf)
        if target.get("kind") == "mech":
            hp *= 1.10
        fraction = (hp / damage - stats["min_frac"]) / max(0.001, 1 - stats["min_frac"])
        return clamp(stats["charge"] * fraction, stats["min_charge"], stats["charge"])

    def aligned(self, actual, command, line, me, width, height, dist, when):
        if line is None:
            return False
        st = me["stats"]
        yaw = actual[0] + clamp(yaw_delta(command[0], actual[0]), -st["turn_rate"]*when, st["turn_rate"]*when)
        pitch = actual[1] + clamp(command[1] - actual[1], -st["pitch_rate"]*when, st["pitch_rate"]*when)
        return (abs(yaw_delta(yaw, line[0])) <= math.atan2(width, dist)
                and abs(pitch - line[1]) <= math.atan2(height, dist))

    def engage(self, obs, target, action):
        """Mutate aim and triggers. The caller supplies the search aim while idle."""
        me, now = obs["self"], obs["t"]
        weapons = {w["kind"]: w for w in me["weapons"] if w}
        gun, laser = weapons.get("gun"), weapons.get("laser")
        trigger = [False, False]
        action["fire"] = trigger
        if gun and gun.get("reload", 0) <= 0:
            missing = gun["stats"]["mag"] - gun.get("ammo", gun["stats"]["mag"])
            action["reload"] = gun.get("ammo") == 0 or (
                target is None and missing >= (2 if now-self.last_enemy_line > 0.7 else 6))
        if target is None:
            if laser and laser.get("charge", 0) > 0:
                trigger[me["weapons"].index(laser)] = now - self.last_laser_line < 0.85
            return

        body = me["stats"]
        barrel = body["radius"] + 0.5
        origin = self.muzzle(me, body["muzzle_height"], self.tick)
        height, radius = self.body(target)
        # Tracking persists for five seconds, even behind walls. Test the
        # muzzle-to-body segment and try another exposed band at a roof lip.
        offset = height * 0.53
        aim_point = self.point(target, offset, self.tick)
        exposed = self.clear(origin, aim_point)
        if not exposed:
            for fraction in (0.88, 0.22):
                candidate = self.point(target, height * fraction, self.tick)
                if self.clear(origin, candidate):
                    offset, aim_point, exposed = height * fraction, candidate, True
                    break
        direct = angles(origin, aim_point)
        dist = max(0.1, math.dist(origin, aim_point))
        if exposed and target.get("kind") == "mech":
            self.last_enemy_line = now

        gun_aim = None
        if gun and exposed and gun.get("reload", 0) <= self.step and gun.get("ammo", 1) > 0:
            gun_aim = self.gun_line(me, target, offset, gun, barrel)

        # Each slot votes for a torso solution only near its own firing window.
        # The gun can keep firing during the laser wind-up. A charged release
        # owns the torso for a single decision, then hands it back to gun lead.
        command = gun_aim or direct
        mortar = weapons.get("mortar")
        shell = None
        if mortar and mortar["cooldown"] <= self.step + self.tick:
            shell = self.shell_line(me, target, height, mortar, barrel)
            if shell:
                command = shell
        beam = False
        charge = laser.get("charge", 0) if laser else 0
        release = False
        if laser:
            beam = exposed and dist <= laser["stats"]["range"] + barrel
            if beam:
                self.last_laser_line = now
                goal = self.charge_needed(me, target, laser)
                # Wire seconds may be rounded (e.g. a 0.421875 s upgraded
                # full charge reported as 0.42). Avoid holding it to timeout.
                release = charge > 0 and charge + min(0.0051, self.tick*0.25) >= goal
                if charge > 0 and charge + self.step >= goal - 1e-6:
                    command = direct
        action["aim"] = (command[0], clamp(command[1], body["min_pitch"], body["max_pitch"]))
        actual = me["aim"]
        # Far shots at the top of a silhouette have less vertical error budget.
        vertical = max(0.1, min(offset, height - offset))
        for slot, w in enumerate(me["weapons"]):
            if not w:
                continue
            kind, stats = w["kind"], w["stats"]
            delay = clamp(w["cooldown"], self.tick, self.step)
            if kind == "gun":
                trigger[slot] = self.aligned(actual, action["aim"], gun_aim, me, radius, vertical, dist, delay)
            elif kind == "laser":
                on_target = self.aligned(actual, action["aim"], direct, me, radius*0.90, vertical*0.90, dist, self.tick)
                trigger[slot] = (beam and not (release and on_target)) or (
                    charge > 0 and not beam and now - self.last_laser_line < 0.85)
            elif kind == "mortar":
                trigger[slot] = self.aligned(actual, action["aim"], shell, me, radius+0.5, height*0.30, dist, delay)
            elif kind == "lightning":
                cone = math.radians(stats["cone_deg"])
                projected = actual[0] + clamp(yaw_delta(action["aim"][0], actual[0]), -body["turn_rate"]*delay, body["turn_rate"]*delay)
                cost = stats["energy"]
                energy = me["energy"]
                # Paying the regeneration delay for each single shot starves
                # the arc after a long exchange. Bank a burst before resuming.
                burst = min(me["max_energy"], clamp(0.30*me["max_energy"], 6*cost, 12*cost))
                if energy < cost:
                    self.refilling_arc = True
                elif energy >= burst:
                    self.refilling_arc = False
                urgent = target.get("charging", False) or target.get("hp", math.inf) <= stats["damage"]*0.90
                trigger[slot] = (exposed and dist <= stats["range"] and energy >= cost
                                 and (not self.refilling_arc or urgent)
                                 and abs(yaw_delta(projected, direct[0])) <= cone)

        if gun and not exposed and gun.get("reload", 0) == 0 and gun.get("ammo", 0) <= gun["stats"]["mag"] * 0.6:
            action["reload"] = True
