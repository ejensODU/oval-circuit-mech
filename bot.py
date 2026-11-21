"""Exp 2: Pure Stealth / Zero-Fire Cover Survival Probe for Mech Royale."""
import math
import random
import sys
import traceback

import mechfight
from arena import Arena
from combat import DefensiveAim
from tactics import Footwork, distance, future_ring, inside_goal, unit


class Bot:
    name = "Oval Stealth-v2"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        self.rng = random.Random(int(hello.get("seed", 0)))
        self.arena = None
        try:
            self.arena = Arena(hello)
        except Exception:
            print(f"Arena init failed:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        self.aim = DefensiveAim(hello)
        self.feet = Footwork(hello)

        self.last_damage = -99.0
        self.last_seen_enemy = -99.0
        self.damage_source = None
        self.recent_enemies = {}   # id -> (pos, t, cubes)
        self.danger_sounds = []    # list of (t, bearing, weight)
        self.shelter = None
        self.shelter_until = -1.0
        self.hide_goal = None
        self.hide_zone_key = None
        self.hide_until = -1.0
        self.route_budget = (None, -1.0, 0.0)
        self.mode = "hide"

    def _remember(self, obs):
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        for e in obs.get("events", []):
            if e.get("type") == "damaged" and e.get("weapon") != "zone":
                self.last_damage = t
                if e.get("bearing") is not None:
                    yaw = float(e["bearing"])
                    self.damage_source = (pos[0] + 65.0 * math.cos(yaw), pos[1] + 65.0 * math.sin(yaw), pos[2])
        for m in obs.get("mechs", []):
            if m.get("kind") == "mech":
                self.recent_enemies[m["id"]] = (tuple(m["pos"]), t, m.get("cubes", 0))
                self.last_seen_enemy = t
        # Expire old enemy sightings (> 12 s)
        for mid in [k for k, v in self.recent_enemies.items() if t - v[1] > 12.0]:
            del self.recent_enemies[mid]
        for s in obs.get("sounds", []):
            kind = s.get("kind", "")
            if kind in ("step", "boost", "jet", "gun", "laser", "laser_charge", "lightning", "mortar"):
                dist = s.get("dist", "far")
                w = 3.0 if dist == "near" else 1.5 if dist == "mid" else 0.5
                if s.get("bearing") is not None:
                    self.danger_sounds.append((t, float(s["bearing"]), w))
        self.danger_sounds = [d for d in self.danger_sounds if t - d[0] < 10.0][-16:]

    def _upgrade(self, obs):
        offer = obs.get("offer")
        if not offer:
            return None
        choices = offer.get("choices") or []
        if not choices:
            return None
        # Pure survival priority: max HP, damage reduction, regen, speed, jet/boost efficiency, sensors
        values = {
            "plating": 12.0,
            "armor": 10.8,
            "nanites": 10.2,
            "servos": 9.5,
            "jets": 8.6,
            "reactor": 8.0,
            "sensors": 7.8,
            "capacitor": 7.4,
            "gyros": 6.5,
            "lightning_range": 5.0,
            "lightning_efficiency": 4.8,
            "laser_charge": 4.5,
            "laser_damage": 4.4,
            "gun_mag": 4.2,
            "gun_damage": 4.1,
            "gun_rate": 4.0,
            "gun_velocity": 3.9,
        }
        return max(range(len(choices)), key=lambda idx: values.get(choices[idx].get("id", ""), 3.0))

    def _silent_pickup(self, obs):
        """Only pick up a non-mortar weapon if literally within pickup_radius and a slot is empty."""
        me = obs["self"]
        weapons = me.get("weapons", [])
        held = [w["kind"] if w else None for w in weapons]
        if None not in held:
            return {}
        reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        pos = me["pos"]
        for p in obs.get("pickups", []):
            kind = p.get("kind")
            # Never pick up mortar (takes +60% cook-off damage from enemy lasers!)
            if kind not in ("laser", "lightning") or kind in held:
                continue
            pp = p["pos"]
            if distance(pp, pos) < reach - 0.15 and abs(pp[2] - pos[2]) < 3.8:
                return {"take": p["id"]}
        return {}

    def _fallback_act(self, obs):
        me = obs["self"]
        pos = me["pos"]
        goal = obs["zone"]["next_center"]
        ux, uy = unit(goal[0] - pos[0], goal[1] - pos[1])
        act = {
            "tick": obs["tick"],
            "move": [ux, uy],
            "boost": not me.get("in_zone", True),
            "jump": False,
            "aim": list(me.get("aim", [0.0, 0.0])),
            "fire": [False, False],
            "reload": False,
        }
        pick = self._upgrade(obs)
        if pick is not None:
            act["pick"] = pick
        return act

    def _plan(self, obs):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        pos = me["pos"]
        arena = self.arena
        visible_mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]

        # Check direct LOS threats
        eye = (pos[0], pos[1], pos[2] + me["stats"]["eye_height"])
        los_threats = []
        for m in visible_mechs:
            mp = m["pos"]
            h = self.cfg["mech"]["height"] * (1.0 + self.cfg["cubes"]["size_per_cube"] * m.get("cubes", 0))
            if arena.clear(eye, (mp[0], mp[1], mp[2] + 0.72 * h)):
                los_threats.append(m)

        # Zone timing & safety
        center14, radius14 = future_ring(zone, t, 14.0)
        next_center, next_radius = zone["next_center"], zone["next_radius"]
        safe_next = max(0.0, next_radius - min(16.0, 0.18 * next_radius))
        to_next = distance(pos, next_center)
        remaining = max(0.0, to_next - safe_next)
        if remaining > 16.0:
            key, expires, detour = self.route_budget
            cur = (next_center[0], next_center[1], next_radius)
            if key != cur or expires <= t:
                dest_tmp = inside_goal(pos, next_center, safe_next)
                tb = arena.travel_bound(pos, dest_tmp)
                detour = min(130.0, max(0.0, tb - remaining)) if math.isfinite(tb) else 40.0
                self.route_budget = (cur, t + 2.5, detour)
            remaining += detour

        walk = max(1.0, float(me["stats"].get("walk_speed", 11.7)))
        silent_spd = max(1.0, min(walk, float(self.cfg.get("sound", {}).get("step_speed", 5.0)) - 0.5))
        roof_extra = 0.0
        if next_radius < 14.0 and not arena.walkable(next_center[0], next_center[1]):
            rz = arena.roof(next_center[0], next_center[1])
            if rz > pos[2] + 3.0:
                roof_extra = 8.0 + (rz - pos[2]) / 6.0

        eta_fast = 6.0 + remaining / (walk * 0.72) + roof_extra
        eta_silent = 8.0 + remaining / (silent_spd * 0.80) + roof_extra

        # 1. Tiny final circle: exact center positioning
        if zone["radius"] <= 6.0:
            return "last", next_center, 0.0, False, False

        # 2. Urgent zone rotation (outside zone, about to be clipped, or won't make shrink_end at fast walk)
        urgent_rotate = (
            not me["in_zone"]
            or distance(pos, center14) > max(0.0, radius14 - min(6.0, 0.12 * radius14))
            or (to_next > safe_next + 3.0 and t + eta_fast >= zone["shrink_end"])
        )
        if urgent_rotate:
            dest = inside_goal(pos, next_center, safe_next)
            if distance(dest, pos) < 4.0:
                dest = inside_goal(pos, center14, max(0.0, radius14 - min(22.0, 0.4 * radius14)))
            need_boost = (not me["in_zone"]) or (t + eta_fast + 4.0 >= zone["shrink_end"]) or bool(los_threats)
            return "rotate", dest, 0.0, False, need_boost

        # 3. Emergency escape: spotted, taking fire, enemy nearby, or shaking 5 s memory!
        under_fire = (t - self.last_damage) < 6.5
        shaking_memory = (t - self.last_seen_enemy) < 6.5
        near_sound_threat = any(t - st < 1.2 and w >= 3.0 for st, _, w in self.danger_sounds)
        enemy_close = any(distance(pos, m["pos"]) < 130.0 for m in visible_mechs)
        if (los_threats or under_fire or enemy_close or shaking_memory) and zone["radius"] > 14.0:
            # Build attacker list (visible first, then recent memory, then damage bearing)
            attackers = list(los_threats) if los_threats else list(visible_mechs)
            if not attackers:
                attackers = [ep for ep, et, _ in self.recent_enemies.values() if t - et < 8.0]
            if not attackers and self.damage_source is not None and under_fire:
                attackers = [self.damage_source]
            c8, r8 = future_ring(zone, t, 8.0)
            safe_r8 = max(2.0, r8 - min(6.0, 0.10 * r8))
            # While shaking 5 s memory behind cover, keep putting distance & buildings between us and pursuers
            keep_moving = (not los_threats) and (shaking_memory or under_fire)
            if (self.shelter is None or t >= self.shelter_until
                    or distance(self.shelter, c8) > safe_r8
                    or (los_threats and distance(pos, self.shelter) < 4.5)
                    or (keep_moving and distance(pos, self.shelter) < 6.0)):
                self.shelter = arena.escape_cover(pos, attackers, c8, safe_r8, keep_moving=keep_moving)
                self.shelter_until = t + 1.35
            if self.shelter is not None:
                in_danger_now = bool(los_threats) or (t - self.last_damage < 2.2)
                # Walk normally (no boost noise, letting energy regen) while shaking memory out of LOS
                return "escape", self.shelter, 1.5, False, in_danger_now
            if attackers:
                ax = sum((a["pos"][0] if isinstance(a, dict) else a[0]) for a in attackers) / len(attackers)
                ay = sum((a["pos"][1] if isinstance(a, dict) else a[1]) for a in attackers) / len(attackers)
                dx, dy = unit(pos[0] - ax, pos[1] - ay)
                esc = inside_goal((pos[0] + 45.0 * dx, pos[1] + 45.0 * dy), c8, safe_r8)
                return "escape", esc, 0.0, False, True
        else:
            self.shelter = None
            self.shelter_until = -1.0

        # 4. Silent opportunistic ground cube grab (zero risk, zero noise)
        if not visible_mechs and not under_fire and not near_sound_threat and (t - self.last_seen_enemy > 9.0):
            best_cube = None
            best_d = 20.0
            for c in obs.get("cubes", []):
                cp = c["pos"]
                d = distance(pos, cp)
                if d < best_d and abs(cp[2] - pos[2]) < 3.0 and distance(cp, center14) < max(2.0, radius14 - 8.0):
                    if not any(distance(cp, npc["pos"]) < 48.0 for npc in npcs):
                        best_cube, best_d = cp, d
            if best_cube is not None:
                return "cube", best_cube[:2], 0.5, True, False

        # 5. Proactive silent hold at a high-occlusion hide spot inside next_zone
        zone_key = (round(next_center[0], 1), round(next_center[1], 1), round(next_radius, 1))
        npc_too_close = any(distance(pos, npc["pos"]) < 48.0 for npc in npcs)
        hide_invalid = (
            self.hide_goal is None
            or self.hide_zone_key != zone_key
            or t >= self.hide_until
            or distance(self.hide_goal, next_center) > max(2.0, next_radius - min(8.0, 0.12 * next_radius))
            or npc_too_close
            or (near_sound_threat and distance(pos, self.hide_goal) < 8.0)
        )
        if hide_invalid:
            d_dirs = [(b, w) for _, b, w in self.danger_sounds]
            self.hide_goal = arena.hide_spot(pos, next_center, max(4.0, next_radius), d_dirs, npcs, self.rng)
            if self.hide_goal is None:
                self.hide_goal = inside_goal(pos, next_center, safe_next)
            self.hide_zone_key = zone_key
            self.hide_until = t + 28.0

        # Walk silently to hide spot if we have time; otherwise walk normally (no boost)
        use_silent = (to_next <= safe_next + 5.0) or (t + eta_silent < zone["shrink_end"])
        return "hide", self.hide_goal, 2.0, use_silent, False

    def act(self, obs):
        if self.arena is None:
            return self._fallback_act(obs)
        try:
            self.arena.set_body(obs["self"]["stats"])
            self._remember(obs)
            mode, goal, anchor, silent, urgent_boost = self._plan(obs)
            self.mode = mode
            action = self.aim.act(obs, self.arena)
            pos = obs["self"]["pos"]
            climbing = False
            if len(goal) == 2 and mode in ("last", "rotate", "hide") and not self.arena.walkable(goal[0], goal[1]):
                zone = obs["zone"]
                radial = min(zone["next_radius"] * 0.7, 26.0)
                ground_goals = []
                if radial >= 2.0:
                    for j in range(12):
                        theta = j * math.tau / 12.0
                        p = (goal[0] + radial * math.cos(theta), goal[1] + radial * math.sin(theta))
                        if distance(p, zone["next_center"]) < zone["next_radius"] and self.arena.walkable(p[0], p[1]):
                            ground_goals.append(p)
                if ground_goals:
                    goal = min(ground_goals, key=lambda p: distance(pos, p))
                else:
                    goal = (goal[0], goal[1], self.arena.roof(goal[0], goal[1]))
            if len(goal) == 3:
                climbing = (goal[2] > self.arena.ground(goal[0], goal[1]) + 3.8
                            and (goal[2] > pos[2] + 0.4
                                 or (not obs["self"]["grounded"] and pos[2] < goal[2] + 1.5)))
            action.update(self.feet.move(obs, self.arena, goal, mode, climbing, anchor, silent, urgent_boost))
            action.update(self._silent_pickup(obs))
            pick = self._upgrade(obs)
            if pick is not None:
                action["pick"] = pick
            action["tick"] = obs["tick"]
            return action
        except Exception:
            print(f"act error at tick {obs.get('tick')}:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return self._fallback_act(obs)


if __name__ == "__main__":
    mechfight.run(Bot)
