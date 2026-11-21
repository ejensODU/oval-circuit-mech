"""Closed-loop mechfight/1 drills. These are small, synthetic test arenas.

Run from the submission root:
  python3 tools/spar.py --kit ../mechfight-kit -- python3 -u bot.py
To compare, replace the bot command with python3 -u ../mechfight-kit/python/example_bot.py.

Reports endpoint HP and objective completion. The duel opponent strafes and leads
shots imperfectly. The wall course uses a tall rectangular prism. The roof drill
closes the ring onto an 18 m ledge. The loot course places a cube, laser and crate
nearby. The drop drill starts at low HP with an opponent approaching the supply
ring. The beam drill starts with the gun reloading and a moving, wounded target. These drills model a
subset of the rules and do not simulate the official 16-player competition.
"""
import argparse
import copy
import gzip
import json
import math
import os
import random
import select
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

DT = 0.05
HALF = 220
WALL = (-17, -31, 17, 31, 80)
ROOF = (48, -24, 96, 24, 18)
CASES = ("duel", "loot", "ring", "roof", "drop", "beam")


def clamp(x, low, high):
    return max(low, min(high, x))


def angle(x):
    return math.atan2(math.sin(x), math.cos(x))


def direction(yaw, pitch):
    return (math.cos(yaw) * math.cos(pitch), math.sin(yaw) * math.cos(pitch), math.sin(pitch))


def fixture(path, wall, kit):
    """Preserve the kit's map structure while substituting an empty, level field."""
    folder = path / "v2"
    folder.mkdir(parents=True)
    world = json.loads((kit / "arena/v2/world.json").read_text())
    world["bounds"] = {"minX": -HALF, "minY": -HALF, "maxX": HALF, "maxY": HALF}
    world["terrain"].update(minX=-HALF, minY=-HALF, nx=2 * HALF + 1, ny=2 * HALF + 1)
    world["prisms"] = []
    if wall:
        x1, y1, x2, y2, height = wall
        world["prisms"].append(dict(kind="building", block=0, base=-2, top=height,
                                    ring=[x1, y1, x2, y1, x2, y2, x1, y2], holes=[]))
    world["trees"]["data"] = []
    (folder / "world.json").write_text(json.dumps(world))
    (folder / "terrain.i16").write_bytes(bytes(2 * (2 * HALF + 1) ** 2))


@contextmanager
def running_bot(cmd, hello):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def exchange(msg, timeout=2):
        proc.stdin.write(json.dumps(msg, separators=(",", ":"), allow_nan=False) + "\n")
        proc.stdin.flush()
        if not select.select([proc.stdout], [], [], timeout)[0]:
            raise RuntimeError(f"Bot timed out on {msg['type']} at tick {msg.get('tick')}")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Bot exited on {msg['type']}")
        return json.loads(line)

    try:
        if exchange(hello, 20).get("type") != "ready":
            raise ValueError("Expected ready")
        yield exchange
        proc.stdin.write('{"type":"end","place":1,"kills":0}\n')
        proc.stdin.flush()
        proc.stdin.close()
        if proc.wait(timeout=2) != 0:
            raise RuntimeError(f"Bot exited with {proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def cylinder_hit(start, end, base, radius, height):
    """Does the swept line intersect this vertical cylinder?"""
    dx, dy, dz = (b - a for a, b in zip(start, end))
    rx, ry, rz = (a - b for a, b in zip(start, base))
    lo, hi = 0.0, 1.0
    if abs(dz) > 1e-9:
        u, v = sorted((-rz / dz, (height - rz) / dz))
        lo, hi = max(lo, u), min(hi, v)
    elif not 0 <= rz <= height:
        return False
    if hi < lo:
        return False
    den = dx * dx + dy * dy
    k = clamp(-(rx * dx + ry * dy) / den, lo, hi) if den else lo
    return (rx + dx * k) ** 2 + (ry + dy * k) ** 2 <= radius * radius


class Drill:
    def __init__(self, case, seed, hello, first):
        self.case = case
        self.prism = {"ring": WALL, "roof": ROOF}.get(case)
        self.rng = random.Random(seed)
        self.enemy_rng = random.Random(seed + 8071)
        self.sounds = []
        self.cfg = hello["config"]
        self.obs = copy.deepcopy(first)
        self.me = self.obs["self"]
        self.obs.update(mechs=[], projectiles=[], cubes=[], crates=[], pickups=[], sounds=[], events=[],
                        beams=[], drops=[], offer=None, alive=2 if case == "duel" else 16)
        self.tick = 0
        self.duration = {"duel": 26, "loot": 18, "ring": 25, "roof": 35, "drop": 8, "beam": 4}[case]
        self.me.update(pos=[-88., self.rng.uniform(-6, 6), 0.], aim=[0., 0.], vel=[0., 0., 0.])
        self.enemy = dict(id=7, kind="mech", hp=1400., max_hp=1400., pos=[45., 0., 0.],
                          vel=[0., 10., 0.], aim=[math.pi, 0.], weapons=["gun"], cubes=0, charging=False)
        self.crate = dict(id=1000, hp=self.cfg["crates"]["hp"], pos=[-55., 5., 0.])
        if case == "loot":
            self.obs["pickups"] = [dict(id=8000, kind="laser", pos=[-75., 7., 0.])]
            self.obs["cubes"] = [dict(id=5000, pos=[-70., 4., 0.])]
            self.obs["crates"] = [self.crate]
        self.shots = []
        self.shot_id = self.cube_count = self.hits = self.shots_fired = 0
        self.took = False
        self.outside = 0.
        self.last_hurt = self.last_boost = self.last_seen = -100.
        self.enemy_gun = dict(cooldown=0., ammo=12, reload=0., stats=self.cfg["weapons"]["gun"])
        self.phase = self.rng.uniform(0, 6.0)
        self.events = []
        self.pending_offer = None
        self.repair = None
        self.beams = []
        self.laser_held = 0.
        self.release_pending = False
        if case == "beam":
            self.enemy.update(hp=180., pos=[-4., 0., 0.])
            self.me["weapons"][0].update(ammo=0, reload=self.cfg["weapons"]["gun"]["reload"])
            self.me["weapons"][1] = dict(kind="laser", cooldown=0., charge=0., stats=self.cfg["weapons"]["laser"])
        if case == "drop":
            self.me.update(hp=730., energy=42.)
            self.last_hurt = 0.
            self.enemy["pos"] = [15., 3., 0.]
            self.enemy_gun.update(ammo=0, reload=self.enemy_gun["stats"]["reload"])
            rules = self.cfg["drops"]
            self.obs["drops"] = [dict(id=20000, pos=[-68., 3., 0.], radius=rules["radius"], capture_s=rules["capture_s"],
                                      progress=0., holder=None, lands_in=0.)]

    def observe(self):
        obs, me = self.obs, self.me
        t = self.tick * DT
        obs.update(t=t, tick=self.tick, events=self.events, sounds=self.sounds)
        self.events = []
        self.sounds = []
        if self.case in ("ring", "roof"):
            final_radius = 2. if self.case == "roof" else 30.
            end = 29. if self.case == "roof" else 18.
            k = clamp((t - 3.) / (end - 3.), 0, 1)
            obs["zone"] = dict(center=[72 * k, 0.], next_center=[72., 0.], radius=145 + (final_radius - 145) * k,
                               next_radius=final_radius, shrink_start=3., shrink_end=end, dps=100.)
        else:
            obs["zone"] = dict(center=[0., 0.], next_center=[0., 0.], radius=205., next_radius=190.,
                               shrink_start=60., shrink_end=110., dps=20.)
        me["in_zone"] = math.dist(me["pos"][:2], obs["zone"]["center"]) <= obs["zone"]["radius"]
        def visible(pos):
            dx, dy = (p - o for p, o in zip(pos[:2], me["pos"][:2]))
            rules = self.cfg["vision"]
            dist = math.dist(pos, me["pos"])
            near = rules["near_range"] + me["stats"]["near_range_bonus"]
            far = rules["range"] + me["stats"]["vision_bonus"]
            in_view = abs(angle(math.atan2(dy, dx) - me["aim"][0])) <= math.radians(rules["fov_deg"]) / 2
            return dist <= near or (dist <= far and in_view)

        if self.case in ("duel", "beam", "drop"):
            if visible(self.enemy["pos"]):
                self.last_seen = t
            obs["mechs"] = [self.enemy] if t - self.last_seen <= self.cfg["vision"]["memory_s"] else []
        obs["projectiles"] = [dict(id=s["id"], kind="gun", pos=s["pos"], vel=s["vel"])
                              for s in self.shots if not s["ours"] and visible(s["pos"])]
        obs["offer"] = self.pending_offer
        view = obs.copy()
        for category in ("crates", "pickups", "cubes"):
            view[category] = [item for item in obs[category] if visible(item["pos"])]
        return view

    def move(self, a):
        me, st = self.me, self.me["stats"]
        x, y, z = me["pos"]
        vx, vy, vz = me["vel"]
        t = self.tick * DT
        dx, dy = a.get("move", [0., 0.])
        norm = max(1., math.hypot(dx, dy))
        boost = a.get("boost", False) and me["energy"] > st["boost_drain"] * DT
        jump = me["grounded"] and a.get("jump", False)
        grounded = me["grounded"] and not jump
        speed = st["boost_speed"] if grounded and boost else st["walk_speed"]
        if any(w and w.get("charge", 0) > 0 for w in me["weapons"]):
            speed *= st["charge_move_mult"]
        ax, ay = speed * dx / norm - vx, speed * dy / norm - vy
        scale = min(1., st["ground_accel" if grounded else "air_accel"] * DT / max(1e-9, math.hypot(ax, ay)))
        vx, vy = vx + ax * scale, vy + ay * scale
        if jump:
            vz = st["jump_speed"]
        if not grounded:
            vz += ((st["jet_accel"] if boost else 0) - st["gravity"]) * DT
            vz = min(vz, st["max_rise_speed"])
        old_z = z
        z = max(0., z + vz * DT)
        r = st["radius"]

        def blocked(px, py):
            if not self.prism:
                return False
            x1, y1, x2, y2, top = self.prism
            return x1 - r < px < x2 + r and y1 - r < py < y2 + r and max(z, old_z) < top

        if blocked(x + vx * DT, y):
            vx = 0.
        x = clamp(x + vx * DT, -HALF + r, HALF - r)
        if blocked(x, y + vy * DT):
            vy = 0.
        y = clamp(y + vy * DT, -HALF + r, HALF - r)
        floor = 0.
        if self.prism:
            x1, y1, x2, y2, top = self.prism
            if x1 <= x <= x2 and y1 <= y <= y2 and old_z >= top:
                floor = top
        z = max(floor, z)
        me.update(pos=[x, y, z], vel=[vx, vy, vz if z > floor else 0.], grounded=z == floor)
        if boost and (not grounded or math.hypot(dx, dy) > .01):
            self.last_boost = t
            me["energy"] = max(0., me["energy"] - st["boost_drain" if grounded else "jet_drain"] * DT)
        elif t - self.last_boost > st["energy_regen_delay"]:
            me["energy"] = min(me["max_energy"], me["energy"] + st["energy_regen"] * DT)
        yaw, pitch = me["aim"]
        want_yaw, want_pitch = a.get("aim", me["aim"])
        me["aim"] = [angle(yaw + clamp(angle(want_yaw - yaw), -st["turn_rate"] * DT, st["turn_rate"] * DT)),
                     pitch + clamp(clamp(want_pitch, st["min_pitch"], st["max_pitch"]) - pitch,
                                   -st["pitch_rate"] * DT, st["pitch_rate"] * DT)]

    def gun(self, who, weapon, fire, reload, ours):
        st = weapon["stats"]
        if weapon["reload"] > 0:
            weapon["reload"] = max(0., weapon["reload"] - DT)
            if not weapon["reload"]:
                weapon["ammo"] = st["mag"]
            return
        weapon["cooldown"] = max(-DT, weapon["cooldown"] - DT)
        if weapon["ammo"] == 0 or (reload and weapon["ammo"] < st["mag"]):
            weapon["reload"] = st["reload"]
        elif fire and weapon["cooldown"] <= 0:
            sigma = math.radians(st["spread_deg"] if ours else 1.5)
            rng = self.rng if ours else self.enemy_rng
            yaw, pitch = [v + rng.gauss(0., sigma) for v in who["aim"]]
            if not ours:
                dx = who["pos"][0] - self.me["pos"][0]
                dy = who["pos"][1] - self.me["pos"][1]
                distance = math.hypot(dx, dy)
                if distance < self.cfg["sound"]["range"]["gun"]:
                    band = "near" if distance < 60 else ("mid" if distance < 160 else "far")
                    self.sounds.append(dict(kind="gun", bearing=math.atan2(dy, dx) + rng.gauss(0, .12),
                                            dist=band, age=DT))
            ray = direction(yaw, pitch)
            origin = who["pos"].copy()
            body = who.get("stats", self.cfg["mech"])
            origin[2] += body["muzzle_height"]
            barrel = body["radius"] + .5
            self.shot_id += 1
            self.shots.append(dict(id=self.shot_id, ours=ours, damage=st["damage"], ttl=st["range"] / st["speed"],
                                   pos=[x + barrel * v for x, v in zip(origin, ray)], vel=[v * st["speed"] for v in ray]))
            weapon["ammo"] -= 1
            weapon["cooldown"] += st["cooldown"]
            self.shots_fired += int(ours)

    def check_hits(self):
        live = []
        for s in self.shots:
            start = s["pos"]
            end = [a + DT * b for a, b in zip(start, s["vel"])]
            if s["ours"] and self.case in ("duel", "beam", "drop"):
                target = self.enemy
            elif s["ours"]:
                target = self.crate if self.case == "loot" and self.crate["hp"] > 0 else None
            else:
                target = self.me
            dimensions = self.cfg["crates"] if target is self.crate else (target or {}).get("stats", self.cfg["mech"])
            radius, height = dimensions["radius"], dimensions["height"]
            if target and cylinder_hit(start, end, target["pos"], radius + .3, height):
                self.last_hurt = self.tick * DT
                rear = "aim" in target and abs(angle(math.atan2(-s["vel"][1], -s["vel"][0]) - target["aim"][0])) > math.pi / 2
                damage = s["damage"] * (self.cfg["mech"]["rear_mult"] if rear else 1.)
                damage *= 1. - target.get("stats", {}).get("armor", 0.)
                target["hp"] = max(0., target["hp"] - damage)
                if s["ours"]:
                    self.hits += 1
                    self.events.append(dict(type="hit", target=target["id"], amount=damage, weapon="gun", rear=rear))
                else:
                    self.events.append(dict(type="damaged", by=self.enemy["id"], rear=rear,
                                            bearing=math.atan2(-s["vel"][1], -s["vel"][0]), amount=damage, weapon="gun"))
                continue
            s.update(pos=end, ttl=s["ttl"] - DT)
            if s["ttl"] > 0 and end[2] >= 0:
                live.append(s)
        self.shots = live

    def loot(self, a):
        me, obs = self.me, self.obs
        if self.crate["hp"] == 0 and obs["crates"]:
            obs["crates"] = []
            obs["cubes"].append(dict(id=5001, pos=self.crate["pos"].copy()))
        for cube in obs["cubes"][:]:
            if abs(me["pos"][2] - cube["pos"][2]) < 4 and math.dist(me["pos"][:2], cube["pos"][:2]) <= 3.5:
                obs["cubes"].remove(cube)
                self.cube_count += 1
                me["cubes"] = self.cube_count
                me["hp"] += 56
                me["max_hp"] += 56
                self.events.append(dict(type="pickup", cubes=me["cubes"]))
                for field in ("height", "radius", "muzzle_height", "eye_height"):
                    me["stats"][field] = self.cfg["mech"][field] * (1 + 0.05 * me["cubes"])
                me["weapons"][0]["stats"]["damage"] = self.cfg["weapons"]["gun"]["damage"] * (1 + .04 * me["cubes"])
                if self.cube_count == 1:
                    self.pending_offer = dict(id=1, expires=self.tick * DT + 5.,
                                              choices=[{k: u[k] for k in ("id", "name", "desc")}
                                                       for u in self.cfg["upgrades"][:3]])
        if self.pending_offer and ("pick" in a or self.tick * DT >= self.pending_offer["expires"]):
            selected = self.cfg["upgrades"][a.get("pick", 0)]
            me["upgrades"].append(selected["id"])
            for fx in selected["effects"]:
                stat = fx["path"].split(".")[1]
                me["stats"][stat] = me["stats"][stat] * fx.get("mul", 1.) + fx.get("add", 0.)
            if selected["id"] == "plating":
                increase = me["max_hp"] * .15
                me["hp"] += increase
                me["max_hp"] += increase
            self.events.append(dict(type="upgrade", id=selected["id"]))
            self.pending_offer = None
        for item in obs["pickups"][:]:
            if (a.get("take") == item["id"] and abs(me["pos"][2]) <= 4 and
                    math.dist(me["pos"][:2], item["pos"][:2]) <= self.cfg["loot"]["pickup_radius"]):
                obs["pickups"].remove(item)
                self.took = True
                me["weapons"][1] = dict(kind="laser", cooldown=1.65, charge=0., stats=self.cfg["weapons"]["laser"])
                self.events.append(dict(type="took", kind="laser", slot=1, id=item["id"]))

    def laser(self, held):
        w = self.me["weapons"][1]
        if not w or w["kind"] != "laser":
            return
        st = w["stats"]
        w["cooldown"] = max(0., w["cooldown"] - DT)
        if w["cooldown"] or (not held and not w["charge"]):
            return
        if not held:
            self.release_pending = True
        if held or self.laser_held < st["min_charge"]:
            self.laser_held += DT
        w["charge"] = min(self.laser_held, st["charge"])
        automatic = self.laser_held + 1e-8 >= st["charge"] + st["hold_s"]
        if not automatic and not (self.release_pending and w["charge"] + 1e-8 >= st["min_charge"]):
            return

        target = self.crate if self.case == "loot" else self.enemy
        body = self.cfg["crates" if self.case == "loot" else "mech"]
        origin = self.me["pos"].copy()
        origin[2] += self.me["stats"]["muzzle_height"]
        ray = direction(*self.me["aim"])
        barrel = self.me["stats"]["radius"] + .5
        start = [p + barrel * v for p, v in zip(origin, ray)]
        end = [p + st["range"] * v for p, v in zip(start, ray)]
        hit = target["hp"] > 0 and cylinder_hit(start, end, target["pos"], body["radius"], body["height"])
        if hit:
            strength = w["charge"] / st["charge"]
            amount = st["damage"] * (st["min_frac"] + strength * (1. - st["min_frac"]))
            amount *= 1 + self.cfg["cubes"]["damage_per_cube"] * self.me["cubes"]
            rear = "aim" in target and abs(angle(self.me["aim"][0] + math.pi - target["aim"][0])) > math.pi / 2
            amount *= self.cfg["mech"]["rear_mult"] if rear else 1.
            target["hp"] = max(0., target["hp"] - amount)
            self.events.append(dict(type="hit", target=target["id"], amount=amount, weapon="laser", rear=rear))
            self.last_hurt = self.tick * DT
        self.beams.append(dict(t=round((self.tick + 1)*DT, 2), charge=round(w["charge"], 2), auto=automatic, hit=hit))
        self.laser_held = 0.
        self.release_pending = False
        w.update(charge=0., cooldown=st["cooldown"])

    def capture(self):
        if not self.obs["drops"]:
            return
        d = self.obs["drops"][0]
        me_dist = math.dist(self.me["pos"][:2], d["pos"][:2])
        foe_dist = math.dist(self.enemy["pos"][:2], d["pos"][:2])
        occupants = [actor for actor, dist in ((self.me, me_dist), (self.enemy, foe_dist)) if dist <= d["radius"]]
        if len(occupants) == 2:
            return
        if not occupants:
            d.update(holder=None, progress=0.)
            return
        holder = occupants[0]
        if d["holder"] != holder["id"]:
            d.update(holder=holder["id"], progress=0.)
        d["progress"] += DT
        if d["progress"] + 1e-8 < d["capture_s"]:
            return
        self.obs["drops"].clear()
        if holder is self.me:
            before_hp, before_energy = self.me["hp"], self.me["energy"]
            cubes = self.cfg["drops"]["cubes"]
            self.me["cubes"] += cubes
            self.me["max_hp"] += cubes * self.cfg["cubes"]["hp_per_cube"] * self.cfg["mech"]["hp"]
            self.me.update(hp=self.me["max_hp"], energy=self.me["max_energy"])
            self.repair = dict(hp_before=round(before_hp), hp_after=round(self.me["hp"]),
                               energy_before=round(before_energy), energy_after=round(self.me["energy"]), cubes=cubes,
                               foe_from_drop_m=round(foe_dist, 1), capture_s=round((self.tick+1)*DT, 2))
            self.events.append(dict(type="drop", id=d["id"], cubes=self.me["cubes"]))

    def step(self, a):
        self.move(a)
        fire = a.get("fire", [False, False])
        self.gun(self.me, self.me["weapons"][0], fire[0], a.get("reload"), True)
        if self.case in ("duel", "beam", "drop"):
            enemy, me = self.enemy, self.me
            if self.case == "drop":
                # Run toward the drop and shoot from just outside its ring.
                enemy["vel"] = [-17., 0., 0.] if enemy["pos"][0] > -58 else [0., 0., 0.]
                enemy["pos"][0] += enemy["vel"][0] * DT
            else:
                yspeed = 10. * (1 if math.sin((self.tick * DT + self.phase) * .55) >= 0 else -1)
                enemy["vel"][1] = yspeed
                enemy["pos"][1] += yspeed * DT
            lead = math.dist(enemy["pos"], me["pos"]) / self.enemy_gun["stats"]["speed"] * .75
            delta = [p + lead * v - q for p, v, q in zip(me["pos"], me["vel"], enemy["pos"])]
            enemy["aim"] = [math.atan2(delta[1], delta[0]), math.atan2(delta[2] - 2.5, math.hypot(*delta[:2]))]
            self.gun(enemy, self.enemy_gun, True, False, False)
        self.check_hits()
        if self.case == "loot":
            self.loot(a)
        self.laser(fire[1])
        if self.case == "drop" and self.me["hp"] > 0:
            self.capture()
        zone, me = self.obs["zone"], self.me
        if math.dist(me["pos"][:2], zone["center"]) > zone["radius"]:
            self.outside += DT
            damage = zone["dps"] * DT
            me["hp"] = max(0., me["hp"] - damage)
            self.last_hurt = self.tick * DT
            self.events.append(dict(type="damaged", by=None, rear=False, amount=damage, bearing=None, weapon="zone"))
        elif self.tick * DT - self.last_hurt > me["stats"]["regen_delay"]:
            me["hp"] = min(me["max_hp"], me["hp"] + me["stats"]["regen_per_s"] * DT)
        self.tick += 1

    def run(self, exchange):
        while self.tick * DT < self.duration and self.me["hp"] > 0 and self.enemy["hp"] > 0:
            action = exchange(self.observe())
            if action.get("tick") != self.tick:
                raise ValueError(f"Stale response at tick {self.tick}")
            for _ in range(2):
                self.step(action)
                if self.me["hp"] <= 0 or self.enemy["hp"] <= 0 or self.repair:
                    break
            if self.repair:
                break
        if self.case == "drop":
            return dict(captured=self.repair is not None, repair=self.repair, own_hp=round(self.me["hp"]))
        if self.case == "beam":
            return dict(enemy_down=self.enemy["hp"] == 0, beams=self.beams, gun_hits=self.hits)
        if self.case == "duel":
            return dict(enemy_down=self.enemy["hp"] == 0, enemy_hp=round(self.enemy["hp"]), own_hp=round(self.me["hp"]),
                        gun_hits=self.hits, gun_shots=self.shots_fired, time_s=round(self.tick * DT, 2))
        if self.case == "loot":
            return dict(laser=self.took, cubes=self.cube_count, crate_open=self.crate["hp"] == 0, upgrade=self.me["upgrades"])
        final_radius = self.obs["zone"]["next_radius"]
        return dict(in_final_ring=math.dist(self.me["pos"][:2], [72., 0.]) <= final_radius,
                    altitude=round(self.me["pos"][2], 1), outside_s=round(self.outside, 2), own_hp=round(self.me["hp"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--seeds", default="1,2,3", help="comma-separated integers")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        parser.error("Supply the entrant command after --")
    with gzip.open(args.kit / "check/stream.jsonl.gz", "rt") as stream:
        hello, first = json.loads(next(stream)), json.loads(next(stream))
    with tempfile.TemporaryDirectory() as temp:
        for case in args.case or CASES:
            mapdir = Path(temp) / case
            fixture(mapdir, {"ring": WALL, "roof": ROOF}.get(case), args.kit)
            hello["map"]["dir"] = str(mapdir)
            for seed in map(int, args.seeds.split(",")):
                hello["seed"] = seed
                drill = Drill(case, seed, hello, first)
                with running_bot(cmd, hello) as exchange:
                    result = drill.run(exchange)
                print(json.dumps(dict(case=case, seed=seed, **result)), flush=True)


if __name__ == "__main__":
    main()
