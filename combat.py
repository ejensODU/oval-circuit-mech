"""Defensive torso orientation, threat tracking, and rear-armor protection (Exp 2 zero-fire stealth)."""
import math


def angle_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


class DefensiveAim:
    """Zero-fire stealth controller: locks front 180-deg armor onto threats and scans sightlines."""

    def __init__(self, hello):
        self.last_bearing = None
        self.bearing_until = -1.0
        self.scan_sign = 1.0 if int(hello.get("seed", 0)) % 2 else -1.0
        self.attackers = {}

    def act(self, obs, arena):
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        actual = me["aim"]

        # Track damage bearings & recent enemy sounds
        for e in obs.get("events", []):
            if e.get("type") == "damaged" and e.get("weapon") != "zone":
                if e.get("bearing") is not None:
                    self.last_bearing = float(e["bearing"])
                    self.bearing_until = t + 3.5
                if e.get("by") is not None:
                    self.attackers[e["by"]] = t

        if self.bearing_until < t:
            cues = [s for s in obs.get("sounds", []) if s.get("kind") in
                    ("laser_charge", "lightning", "step", "boost", "jet", "gun", "laser", "mortar", "reload")]
            if cues:
                dist_rank = {"near": 4, "mid": 2, "far": 0}
                cue = max(cues, key=lambda s: (
                    dist_rank.get(s.get("dist", "far"), 0)
                    + (2 if s.get("kind") in ("laser_charge", "step", "boost", "lightning") else 0),
                    -float(s.get("age", 0.0)),
                ))
                if cue.get("bearing") is not None:
                    self.last_bearing = float(cue["bearing"])
                    self.bearing_until = t + (2.0 if cue.get("dist") == "near" else 0.9)

        # Priority 1: Face the most dangerous visible enemy mech to protect 180-deg front armor!
        mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        if mechs:
            def threat_key(m):
                mp = m["pos"]
                d = math.hypot(mp[0] - pos[0], mp[1] - pos[1])
                to_me = math.atan2(pos[1] - mp[1], pos[0] - mp[0])
                looking = math.cos(m["aim"][0] - to_me) > 0.75
                return (
                    (200.0 if m.get("charging") else 0.0)
                    + (80.0 if looking else 0.0)
                    + (60.0 if t - self.attackers.get(m["id"], -100.0) < 4.0 else 0.0)
                    - d
                )
            primary = max(mechs, key=threat_key)
            mp = primary["pos"]
            yaw = math.atan2(mp[1] - pos[1], mp[0] - pos[0])
            return {"aim": [yaw, 0.0], "fire": [False, False], "reload": False}

        # Priority 2: Face recent attacker/sound bearing (slight sweep to spot flankers while keeping front arc)
        if self.last_bearing is not None and t < self.bearing_until:
            yaw = angle_delta(self.last_bearing + 0.35 * math.sin(t * 2.2), 0.0)
            return {"aim": [yaw, 0.0], "fire": [False, False], "reload": False}

        # Priority 3: Gentle 360-degree scan to spot approaching house bots at 200 m early
        yaw = angle_delta(actual[0] + self.scan_sign * 0.55, 0.0)
        return {"aim": [yaw, 0.0], "fire": [False, False], "reload": False}
