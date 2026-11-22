"""Oval Circuit, a placement-oriented controller for the Mech Royale arena."""
import math
import random

import mechfight
from arena import Arena
from combat import Combat
from tactics import Footwork, distance, future_ring, inside_goal, unit


class Bot:
    name = "Oval Circuit"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello["seed"])
        self.arena = Arena(hello)
        self.combat = Combat(hello)
        self.feet = Footwork(hello)
        self.items = {}
        self.enemy_hp = {}
        self.enemy_hurt = {}
        self.route_budget = (None, -1, 0.0)
        self.skip_until = {}
        self.item_goal = None
        self.item_check = None
        self.last_damage = -99
        self.damage_source = None
        self.recovering = False
        self.shelter = None
        self.shelter_until = -1
        self.hold = None
        self.hold_until = -1
        self.waypoint = None
        self.waypoint_until = -1
        self.visits = {}
        self.mode = "explore"

    def remember(self, obs):
        t, me = obs["t"], obs["self"]
        pos = me["pos"]
        seen = set()
        for group in ("crates", "cubes", "pickups"):
            for item in obs[group]:
                i = item["id"]
                self.items[i] = (group, item, t)
                seen.add(i)
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("weapon") != "zone":
                self.last_damage = t
                if e.get("bearing") is not None:
                    yaw = e["bearing"]
                    self.damage_source = {"pos": (pos[0] + 70 * math.cos(yaw), pos[1] + 70 * math.sin(yaw), pos[2])}
            if e["type"] == "took" or (e["type"] == "take_failed" and e.get("reason") in ("gone", "have")):
                self.items.pop(e["id"], None)
        for i, (group, item, last) in list(self.items.items()):
            if i in seen:
                continue
            p = item["pos"]
            if t - last > (80 if group == "pickups" else 25) or (distance(p, pos) < 7 and abs(p[2]-pos[2]) < 4):
                del self.items[i]
        own_hits = {e["target"] for e in obs["events"] if e["type"] == "hit"}
        for enemy in obs["mechs"]:
            if enemy["kind"] != "mech" or "hp" not in enemy:
                continue
            i = enemy["id"]
            previous, last = self.enemy_hp.get(i, (enemy["hp"], -99))
            if (i not in own_hits and t-last < 0.3 and previous-enemy["hp"] >= 12
                    and distance(enemy["pos"], obs["zone"]["center"]) < obs["zone"]["radius"]):
                self.enemy_hurt[i] = t
            self.enemy_hp[i] = enemy["hp"], t
        self.visits[(round(pos[0] / 28), round(pos[1] / 28))] = t

    def gear_value(self, kind, obs):
        # The arm gun complements every secondary and consumes no energy.
        if kind == "laser":
            return 4.3
        if kind == "lightning":
            return 4.7 if obs["zone"]["radius"] < 60 else 3.65
        return {"mortar": 2.85, "gun": 3.1}.get(kind, 0)

    def equipment(self, obs, threats=()):
        me = obs["self"]
        held = [w["kind"] if w else None for w in me["weapons"]]
        laser_danger = any("laser" in m.get("weapons", ()) for m in threats)
        take = {}
        best = 0
        for p in obs["pickups"]:
            if p["kind"] == "mortar" and laser_danger:
                continue
            if p["kind"] in held or distance(p["pos"], me["pos"]) >= self.cfg["loot"]["pickup_radius"]:
                continue
            if abs(p["pos"][2] - me["pos"][2]) >= 4:
                continue
            if None in held:
                slot = held.index(None)
                gain = self.gear_value(p["kind"], obs)
            else:
                # Retain the reliable arm gun even late in the ring.
                slot = next((i for i, w in enumerate(held) if w != "gun"), 1)
                gain = self.gear_value(p["kind"], obs) - self.gear_value(held[slot], obs)
                if me["weapons"][slot].get("charge", 0) > 0:
                    continue
            if gain > best + 0.02:
                best = gain
                take = {"take": p["id"]}
                if held[slot]:
                    take["drop"] = slot
        # Anti-cookoff: drop mortar immediately if a visible enemy holds a laser!
        if not take and "mortar" in held and laser_danger:
            take = {"drop": held.index("mortar")}
        return take

    def upgrade(self, obs):
        me = obs["self"]
        values = {"gun_mag": 9.4, "plating": 9.25, "gun_damage": 8.35, "armor": 8.5,
                  "servos": 7.9, "nanites": 8.7, "laser_damage": 8.6,
                  "lightning_range": 8.5, "gun_rate": 7.25, "gun_velocity": 7.1,
                  "laser_charge": 6.7, "mortar_reload": 7.55, "mortar_velocity": 7.65,
                  "lightning_efficiency": 7.65, "mortar_splash": 7.2, "jets": 6.0,
                  "reactor": 5.9, "capacitor": 5.5, "sensors": 6.4, "gyros": 5.8,
                  "laser_pierce": 4.2, "lightning_chain": 4.5}
        if "gun_mag" in me["upgrades"]:
            values["gun_mag"] -= 1.55
        if obs["zone"]["radius"] < 75:
            values["plating"] += 1.35
            values["nanites"] -= 1.5
            values["lightning_efficiency"] += 1.0
        if "nanites" in me["upgrades"]:
            values["nanites"] -= 0.8
        return max(enumerate(obs["offer"]["choices"]), key=lambda it: values.get(it[1]["id"], 4))[0]

    def threat_list(self, obs):
        me = obs["self"]
        pos = me["pos"]
        eye = (*pos[:2], pos[2] + me["stats"]["eye_height"])
        ranked = sorted((m for m in obs["mechs"] if m["kind"] == "mech"), key=lambda m: distance(m["pos"], pos))
        threats = []
        for m in ranked[:5]:
            p = m["pos"]
            height = self.cfg["mech"]["height"] * (1 + self.cfg["cubes"]["size_per_cube"] * m.get("cubes", 0))
            if self.arena.clear(eye, (*p[:2], p[2] + 0.72 * height)):
                threats.append(m)
        return threats, ranked

    def loot(self, obs, enemies):
        me, t, zone = obs["self"], obs["t"], obs["zone"]
        pos = me["pos"]
        armed = [w["kind"] for w in me["weapons"] if w]
        secondary = next((k for k in armed if k != "gun"), None)
        fraction = me["hp"] / me["max_hp"]
        candidate, highest = None, 0
        objects = list(self.items.values())
        objects.extend(("npc", m, t) for m in obs["mechs"] if m["kind"] == "npc")
        objects.extend(("drop", m, t) for m in obs.get("drops", []))
        for group, item, last in objects:
            p, i = item["pos"], item["id"]
            if self.skip_until.get(i, 0) > t:
                continue
            d = distance(pos, p)
            climb = max(0, p[2] - self.arena.ground(*p[:2]) - 3.7)
            uphill = max(0, p[2] - pos[2] - 3)
            if climb and uphill > min(43, me["max_energy"] * 0.41):
                continue
            if d > (215 if group == "drop" else 153):
                continue
            travel = d / max(1, me["stats"]["walk_speed"] * 0.97) + (uphill / 6.5 if climb else 0)
            center, radius = future_ring(zone, t, travel + 5)
            over = max(0, distance(p, center) - radius + 2)
            if over > 3.0:
                continue
            owner = None
            if group == "pickups":
                if item["kind"] in armed:
                    continue
                if item["kind"] == "mortar" and any("laser" in m.get("weapons", ()) for m in enemies):
                    continue
                gain = self.gear_value(item["kind"], obs) - (self.gear_value(secondary, obs) if secondary else 0)
                if gain <= 0.1:
                    continue
                worth = (4 + gain * 3.6) if not secondary else (2 + gain * 2.4)
            elif group == "drop":
                owner = item.get("holder")
                if owner is not None and owner != me["id"] and item["progress"] + travel > item["capture_s"] + 1:
                    continue
                wait = max(0, item["lands_in"] - travel)
                if wait > 9:
                    continue
                worth = 16 + (1 - fraction) * 32
                if me["cubes"] >= self.cfg["cubes"]["stat_cap"] and fraction > 0.9:
                    worth *= 0.52
                travel += wait + max(0, item["capture_s"] - (item["progress"] if owner == me["id"] else 0))
            else:
                worth = {"crates": 6.8, "cubes": 8.4, "npc": 10.8}[group]
                if me["cubes"] == 0:
                    worth *= 1.28
                if group == "crates" and not secondary:
                    worth += 3
                if group != "cubes":
                    travel += 1 + item.get("hp", 300) / (230 if secondary else 150)
                if me["cubes"] >= self.cfg["cubes"]["stat_cap"]:
                    worth *= 0.30 if group != "cubes" else 0.48
            score = worth / (2.25 + travel + over)
            if self.item_goal == i:
                score *= 1.25
            score *= max(0.5, 1 - (t - last) * 0.022)
            if enemies:
                if any(distance(e["pos"], p) < 30 for e in enemies):
                    score *= 0.25 if group == "drop" else 0.48
                if group in ("npc", "crates") or d > 28:
                    score *= 0.57
            if group == "drop" and owner == me["id"]:
                score += 8
            if score > highest:
                candidate, highest = (group, item), score
        return candidate, highest

    def remember_goal(self, candidate, obs):
        if candidate is None:
            self.item_goal = self.item_check = None
            return
        item = candidate[1]
        t, pos = obs["t"], obs["self"]["pos"]
        d = math.dist(item["pos"], pos)
        if self.item_goal == item["id"] and self.item_check:
            start, best = self.item_check
            if d < best - 5:
                self.item_check = (t, d)
            elif t - start > (11 if candidate[0] == "drop" else 6) and d > 6:
                self.skip_until[item["id"]] = t + 20
                self.item_check = None
        else:
            self.item_check = (t, d)
        self.item_goal = item["id"]

    def explore(self, obs):
        t, me, zone = obs["t"], obs["self"], obs["zone"]
        pos = me["pos"]
        center, radius = future_ring(zone, t, 20)
        if self.waypoint and self.waypoint_until > t and distance(pos, self.waypoint) > 6 and distance(self.waypoint, center) <= radius:
            return self.waypoint
        best = None
        value = -math.inf
        for _ in range(23):
            a = self.rng.uniform(-math.pi, math.pi)
            d = self.rng.uniform(min(27, radius * 0.35), min(88, max(12, radius)))
            p = inside_goal((pos[0]+d*math.cos(a), pos[1]+d*math.sin(a)), center, radius * 0.90)
            if not self.arena.walkable(*p):
                continue
            recent = self.visits.get((round(p[0]/28), round(p[1]/28)), -100)
            score = min(1.1, (t-recent)/36) + self.rng.random() * 0.3
            clearance = self.arena.ground_clearance(*p)
            score += 0.85 * max(0, 1 - abs(clearance - 12) / 36)
            score -= abs(distance(p, center) - 0.61 * min(radius, zone["next_radius"])) / max(70, radius)
            if score > value:
                best, value = p, score
        self.waypoint = best or center
        self.waypoint_until = t + 9
        return self.waypoint

    def protect(self, pos, enemies, zone, t):
        center, radius = future_ring(zone, t, 8)
        if t >= self.shelter_until or (self.shelter is not None and distance(self.shelter, center) > radius):
            self.shelter = self.arena.cover(pos, enemies, center, max(1, radius - min(4, radius*0.1)), retreat=True)
            self.shelter_until = t + 1.25
        return self.shelter

    def plan(self, obs, threats, known):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        pos = me["pos"]
        hp = me["hp"] / me["max_hp"]
        self.recovering = (hp < 0.60 or (self.recovering and hp < 0.91)) and me["in_zone"]
        if self.recovering and zone["radius"] < 20:
            self.recovering = False
        center, radius = future_ring(zone, t, 15)
        to_next = distance(pos, zone["next_center"])
        safe_next = max(0, zone["next_radius"] - min(15, 0.22 * zone["next_radius"]))
        remaining = max(0.0, to_next - safe_next)
        if remaining > 16:
            key, expires, detour = self.route_budget
            current = (*zone["next_center"], zone["next_radius"])
            if key != current or expires <= t:
                dest = inside_goal(pos, zone["next_center"], safe_next)
                detour = min(125, max(0, self.arena.travel_bound(pos, dest) - remaining))
                self.route_budget = current, t + 2, detour
            remaining += detour
        eta = 5 + remaining / max(1, me["stats"]["walk_speed"] * 0.67)
        if (zone["next_radius"] < 10 and not self.arena.walkable(*zone["next_center"])
                and self.arena.roof(*zone["next_center"]) > pos[2] + 3):
            eta += 8 + max(0, self.arena.roof(*zone["next_center"]) - pos[2]) / 6
        rotate = (not me["in_zone"] or distance(pos, center) > max(0, radius - min(5, radius*0.1))
                  or (to_next > safe_next + 3 and t + eta >= zone["shrink_end"]))
        # Bank a near-complete repair before moving again. The reward is
        # immediate, and exiting the capture area resets its progress.
        for drop in obs.get("drops", []):
            remaining = drop["capture_s"] - drop["progress"]
            if (drop.get("holder") == me["id"] and drop["lands_in"] <= 0 and remaining <= 0.8
                    and distance(pos, drop["pos"]) < drop["radius"]
                    and me["hp"] > max(0, remaining) * (zone["dps"] if not me["in_zone"] else 0) + 1
                    and not any(distance(m["pos"], drop["pos"]) < drop["radius"] for m in threats)):
                return "capture", drop["pos"], None, drop["radius"] * 0.7, False
        if zone["radius"] <= 5:
            return "last", zone["next_center"], None, 0, False
        if rotate:
            dest = inside_goal(pos, zone["next_center"], safe_next)
            # A current ring and its following ring can differ greatly. Move
            # well clear of the current edge even if the next edge is close.
            if distance(pos, dest) < 4:
                dest = inside_goal(pos, center, max(0, radius - min(22, radius*0.4)))
            return "rotate", dest, None, 0, False
        loot, value = self.loot(obs, threats)
        closest = distance(pos, threats[0]["pos"]) if threats else math.inf
        if loot and loot[0] == "drop":
            drop = loot[1]
            radius_cap = drop["radius"]
            capturing = distance(pos, drop["pos"]) < radius_cap and drop["lands_in"] < 1
            if (capturing or hp < 0.72) and closest > (15 if capturing else 42) and value > 0.65:
                self.remember_goal(loot, obs)
                return "capture", drop["pos"], None, radius_cap * 0.47, self.recovering
        under_fire = t - self.last_damage < 2.8
        outnumbered = sum(distance(pos, m["pos"]) < 130 for m in threats) >= 2
        aimed = sum(math.cos(m["aim"][0] - math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])) > 0.92 for m in threats)
        # In a free-for-all, two opponents exchanging fire can yield a place
        # for free. Look for an actual health loss and another mech facing it.
        trading = False
        if len(threats) >= 2 and not under_fire and not aimed:
            for victim in threats:
                if t - self.enemy_hurt.get(victim["id"], -99) > 1.25 or victim.get("hp", 0) < 300:
                    continue
                for shooter in threats:
                    if shooter["id"] == victim["id"]:
                        continue
                    vp, sp = victim["pos"], shooter["pos"]
                    bearing = math.atan2(vp[1] - sp[1], vp[0] - sp[0])
                    if math.cos(shooter["aim"][0] - bearing) > 0.97:
                        trading = True
                        break
        hurt_or_focused = self.recovering or trading or (outnumbered and (under_fire or hp < 0.88 or aimed >= 2))
        attackers = threats or known or ([self.damage_source] if under_fire and self.damage_source else [])
        if attackers and hurt_or_focused and zone["radius"] > 16:
            escape = self.protect(pos, attackers, zone, t)
            if escape is not None:
                return "retreat", escape, None, 1.0, trading or (self.recovering and not threats)
            if outnumbered or not known:
                dx, dy = unit(pos[0] - attackers[0]["pos"][0], pos[1] - attackers[0]["pos"][1])
                escape = inside_goal((pos[0]+35*dx, pos[1]+35*dy), center, radius*0.92)
                return "retreat", escape, None, 0, trading
        else:
            self.shelter = None
            self.shelter_until = -1
        # Once equipped, expose ourselves for a useful pickup or a rotation.
        # In a shrinking, quiet circle a sheltered edge costs less health.
        equipped = all(me["weapons"]) and me["cubes"] >= 6
        hold_ground = (not threats and 18 < radius < 150 and equipped and value < 0.95)
        if hold_ground:
            if self.hold is None or self.hold_until < t or distance(self.hold, center) > radius - 4:
                self.hold = self.arena.cover(pos, [], center, radius-4)
                self.hold_until = t + 7
            if self.hold is not None:
                return "hold", self.hold, None, 1.25, True
        if loot:
            group, obj = loot
            easy = distance(pos, obj["pos"]) <= (16 if group == "cubes" else 21)
            if not threats or (group in ("pickups","cubes","drop") and easy and closest > 16 and value > 0.8):
                self.remember_goal(loot, obs)
                if self.recovering and group in ("crates", "npc"):
                    return "heal", pos, None, 1.5, True
                if group in ("crates", "npc") and abs(obj["pos"][2] - pos[2]) < 4:
                    # The pitch limit leaves a blind spot at the mech's feet.
                    dx, dy = unit(pos[0] - obj["pos"][0], pos[1] - obj["pos"][1])
                    gap = max(12, me["stats"]["muzzle_height"] * 1.7) + (8 if group == "npc" else 0)
                    if dx == dy == 0:
                        dx = 1
                    vantage = (obj["pos"][0] + gap*dx, obj["pos"][1] + gap*dy)
                    if self.arena.walkable(*vantage) and distance(vantage, center) < radius:
                        return "harvest", vantage, obj, 1.0, False
                return ("capture" if group == "drop" else "loot", obj["pos"],
                        obj if group in ("crates", "npc") else None,
                        obj.get("radius", 6) * 0.48 if group == "drop" else 0, self.recovering)
        if threats:
            w_by_k = {w["kind"]: w for w in me["weapons"] if w}
            gw = w_by_k.get("gun")
            lw = w_by_k.get("laser")
            ltw = w_by_k.get("lightning")
            in_light = bool(ltw and closest <= ltw["stats"]["range"] + 4 and me["energy"] >= ltw["stats"]["energy"])
            dead_window = (not in_light and closest > 45.0 and zone["radius"] > 22
                           and (gw is None or gw.get("reload", 0.0) > 0.55)
                           and (lw is None or (lw.get("charge", 0.0) <= 0.0 and lw.get("cooldown", 0.0) > 0.65)))
            enemy_charging_at_me = (not in_light and closest > 45.0 and zone["radius"] > 22
                                    and (lw is None or lw.get("charge", 0.0) < 0.45)
                                    and any(m.get("charging") and math.cos(
                                        m["aim"][0] - math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
                                    ) > 0.90 for m in threats))
            laser_peek_duck = (not in_light and lw is not None and lw.get("charge", 0.0) <= 0.0
                               and lw.get("cooldown", 0.0) > 0.65 and zone["radius"] > 22
                               and (len(threats) >= 2 or closest > 135.0 or hp < 0.72)
                               and not any(m.get("hp", 1400) <= 350 for m in threats))
            if dead_window or enemy_charging_at_me or laser_peek_duck:
                duck = self.protect(pos, threats, zone, t)
                max_d = 18.0 if dead_window else (14.0 if enemy_charging_at_me else 12.0)
                if duck is not None and distance(pos, duck) <= max_d:
                    return "retreat", duck, None, 1.0, False
            return "duel", threats[0]["pos"], None, 0, False
        self.remember_goal(None, obs)
        if self.recovering:
            return "heal", pos, None, 1, True
        if zone["radius"] < 19:
            return "hold", inside_goal(pos, center, radius * 0.6), None, 0.8, False
        return "explore", self.explore(obs), None, 0, False

    def act(self, obs):
        try:
            return self._act_inner(obs)
        except Exception:
            me = obs.get("self", {})
            return {"tick": obs.get("tick", 0), "move": [0.0, 0.0], "boost": False, "jump": False,
                    "aim": list(me.get("aim", [0.0, 0.0])), "fire": [False, False], "reload": False}

    def _act_inner(self, obs):
        self.arena.set_body(obs["self"]["stats"])
        self.remember(obs)
        threats, known = self.threat_list(obs)
        mode, goal, focus, anchor, quiet = self.plan(obs, threats, known)
        self.mode = mode
        action = self.combat.act(obs, self.arena, focus, quiet)
        primary = self.combat.target
        if primary is None or primary.get("kind") != "mech":
            primary = threats[0] if threats else None
        elif mode == "duel":
            goal = primary["pos"]
        pos = obs["self"]["pos"]
        climbing = False
        if mode != "duel":
            if len(goal) == 2 and mode in ("last", "rotate", "hold") and not self.arena.walkable(*goal):
                # If any nearby ground is inside the destination ring, take it.
                # A ring entirely above a building instead requires a jet ascent.
                zone = obs["zone"]
                radial = min(zone["next_radius"]*0.7, 26)
                ground_goals = []
                if radial >= 2:
                    for j in range(12):
                        theta = j * math.tau / 12
                        p = goal[0] + radial * math.cos(theta), goal[1] + radial * math.sin(theta)
                        if distance(p, zone["next_center"]) < zone["next_radius"] and self.arena.walkable(*p):
                            ground_goals.append(p)
                if ground_goals:
                    goal = min(ground_goals, key=lambda p: distance(pos, p))
                else:
                    goal = (*goal, self.arena.roof(*goal))
            if len(goal) == 3:
                climbing = (goal[2] > self.arena.ground(*goal[:2]) + 3.8 and (goal[2] > pos[2] + 0.4 or (not obs["self"]["grounded"] and pos[2] < goal[2] + 1.5)))
        action.update(self.feet.move(obs, self.arena, goal, mode, primary, climbing, anchor))
        action.update(self.equipment(obs, threats))
        if obs.get("offer"):
            pick = self.upgrade(obs)
            if pick is not None:
                action["pick"] = pick
        action["tick"] = obs["tick"]
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
