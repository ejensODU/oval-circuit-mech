"""Choose a firing position, then steer a physically reachable velocity toward it."""
import math

from arena import unit


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def ring_at(zone, now, horizon):
    end = zone['shrink_end']
    if now >= end:
        return tuple(zone['next_center']), zone['next_radius']
    start = max(now, zone['shrink_start'])
    fraction = min(1., max(0., (now + horizon - start) / max(.001, end - start)))
    center = tuple(a + fraction * (b-a) for a, b in zip(zone['center'], zone['next_center']))
    return center, zone['radius'] + fraction * (zone['next_radius']-zone['radius'])


class Tactics:
    def __init__(self, arena, config=None):
        self.arena = arena
        config = config or {}
        weapons = config.get('weapons', {})
        self.arc_range = weapons.get('lightning', {}).get('range', 45.)
        self.charge_time = weapons.get('laser', {}).get('charge', .75)
        self.charge_id = None
        self.charge_since = 0.
        self.shell_gravity = weapons.get('mortar', {}).get('gravity', 22.)
        self.blast_radius = weapons.get('mortar', {}).get('splash_radius', 7.)
        self.anchor = self.key = None
        self.until = -1.
        self.sprint = True
        self.shelter = None
        self.shelter_until = -1.
        self.climbing = False
        self.last_motion = None
        self.unstick_until = -1.

    def _range(self, me, enemy):
        guns = {w['kind']: w for w in me['weapons'] if w}
        opposing = enemy.get('weapons', ())
        arc = guns.get('lightning')
        if arc and (me['energy'] > 12 or 'laser' in opposing):
            return .78 * arc['stats']['range']
        if 'lightning' in opposing:
            # Allow for a coil upgrade and a boost during one decision horizon.
            return max(90., self.arc_range * 1.5 + 20.)
        if 'laser' in opposing and 'laser' not in guns:
            return 82.   # Shorter flight time for our gun. Beam damage ignores range.
        if 'mortar' in opposing:
            return 119. if 'laser' in guns else 85.
        return 112. if 'laser' in guns else 98.

    def _firing_position(self, obs, target, threats):
        arena = self.arena
        me, t = obs['self'], obs['t']
        p, enemy = me['pos'], target['pos']
        radius = me['stats']['radius']
        ready = any(w and w.get('reload', 0) < .25 and w.get('cooldown', 0) < .35 and
                    (w['kind'] != 'lightning' or distance(p, enemy) < w['stats']['range'] + 7)
                    for w in me['weapons'])
        # The server keeps actors in the observation through walls for five seconds.
        block = []
        ordered = sorted((e for e in threats if e['id'] != target['id']),
                         key=lambda e: distance(e['pos'], p) - 45*bool(e.get('charging')))
        for e in ordered[:3]:
            ex, ey, ez = e['pos']
            height = arena.muzzle * (1. + arena.size_per_cube * e.get('cubes', 0))
            block.append(((ex, ey, ez + height), 26 + 18*bool(e.get('charging'))))
        charging = target.get('charging', False)
        if not charging:
            self.charge_id = None
        elif target['id'] != self.charge_id:
            self.charge_id, self.charge_since = target['id'], t
        run_speed = me['stats']['boost_speed'] if me['energy'] > 20. else me['stats']['walk_speed']
        if any(w and w.get('charge', 0.) > 0. for w in me['weapons']):
            run_speed *= me['stats']['charge_move_mult']
        duck_reach = 1. + run_speed * max(.12, self.charge_time - (t-self.charge_since))
        ax, ay = unit(enemy[0]-p[0], enemy[1]-p[1])
        facing = target.get('aim', (0., 0.))[0]
        charged_at_us = charging and -ax*math.cos(facing)-ay*math.sin(facing) > .90
        key = (target['id'], ready, charged_at_us, tuple(e['id'] for e in ordered[:3]))
        if key == self.key and t < self.until:
            return self.anchor
        self.key, self.until = key, t + .48
        ideal = self._range(me, target)
        center, zone_radius = ring_at(obs['zone'], t, 2.)
        sight_height = me['stats']['muzzle_height']
        tall = arena.base_height * (1. + arena.size_per_cube * target.get('cubes', 0))
        mark = (enemy[0], enemy[1], enemy[2] + .57*tall)
        elevated = p[2] > arena.ground(*p[:2]) + 2.
        lateral = (-ay, ax)
        # Rotate this stencil with the engagement axis. Include small immediate
        # cover breaks as well as angles far enough out to clear a building corner.
        here_range = distance(p, enemy)
        toward = max(-1., min(1., (here_range-ideal)/20.))
        directions = [unit(ax*toward + side*lateral[0], ay*toward + side*lateral[1]) for side in (-1., 1.)]
        directions += [(ax, ay), (-ax, -ay), lateral, (-lateral[0], -lateral[1])]
        for sign in (-1., 1.):
            for advance in (-.74, .74):
                directions.append(unit(advance*ax + sign*lateral[0], advance*ay + sign*lateral[1]))
        finisher = target.get('hp', 1400) < 240 and me['hp'] > 300
        best, value = None, -math.inf
        any_shot = False
        for length in (0., 7., 17., 29.):
            for ux, uy in directions if length else [(0., 0.)]:
                x, y = p[0] + length*ux, p[1] + length*uy
                if length and not arena.open_step(p, (ux, uy), length):
                    continue
                base = arena.roof(x, y) if elevated else arena.ground(x, y)
                if elevated and abs(base - p[2]) > 6 and length:
                    continue
                origin = x, y, base + sight_height
                ray = arena.clear(origin, mark)
                any_shot |= ray
                target_dist = distance(origin, mark)
                score = -.47 * abs(target_dist - ideal)
                if ray:
                    score += 30. if ready else -6.
                    if finisher:
                        score += 28.
                    if charged_at_us and not finisher:
                        score -= 42.
                elif charged_at_us and length > duck_reach and ready:
                    score -= 20.    # A distant hiding spot cannot beat this release.
                score -= 3. * max(0., distance(origin, center) - max(0., zone_radius - 3.))
                for source, weight in block:
                    if arena._clear_sampled(source, origin, arena.inv * .55):
                        score -= weight
                travel_time = length / max(1., me['stats']['walk_speed'])
                score -= .95 * travel_time
                score -= .13 * min(32., max(0., arena.ground_clearance(x, y) - radius))
                if ray:
                    score += 3.7 * abs(ux*lateral[0] + uy*lateral[1])
                if self.anchor is not None:
                    score -= .09 * min(20., distance(origin, self.anchor))
                if score > value:
                    best, value = (x, y, base), score
        if not any_shot and ready and not charged_at_us:
            # Locally buried behind an entire building. Follow a campus route
            # until a reachable firing lane appears on a subsequent decision.
            best = tuple(enemy)
        self.anchor = best
        return best

    def _hazards(self, obs):
        me = obs['self']
        p, v = me['pos'], me['vel']
        height = me['stats']['height']
        hazards = []
        for shot in obs.get('projectiles', ()):
            s, w = shot['pos'], shot['vel']
            dx, dy = s[0]-p[0], s[1]-p[1]
            if shot['kind'] == 'mortar':
                # Intersect the observed parabola with the local landing surface.
                # Nearby wall impacts are found along the same short arc.
                if math.hypot(dx, dy) > 1.3 * (math.hypot(w[0], w[1]) + me['stats']['boost_speed']) + self.blast_radius:
                    continue
                prev = s
                for step in range(1, 15):
                    dt = step * .09
                    sample = (s[0]+w[0]*dt, s[1]+w[1]*dt,
                              s[2]+w[2]*dt-.5*self.shell_gravity*dt*dt)
                    if (sample[2] < self.arena.ground(*sample[:2]) or
                            not self.arena.clear(prev, sample)):
                        if distance(prev, p) < self.blast_radius + 26:
                            hazards.append((dt, prev[0], prev[1], 0., 0., self.blast_radius + 2., 22.))
                        break
                    prev = sample
            else:
                rvx, rvy = w[0]-v[0], w[1]-v[1]
                dt = -(dx*rvx + dy*rvy)/max(1., rvx*rvx + rvy*rvy)
                if not 0.01 < dt < 1.06:
                    continue
                z = s[2] + dt*w[2] - p[2] - dt*v[2]
                if -3. < z < height+3. and math.hypot(dx+dt*rvx, dy+dt*rvy) < 6 + 22*dt:
                    nx, ny = unit(-w[1], w[0])
                    hazards.append((dt, s[0]+w[0]*dt, s[1]+w[1]*dt, nx, ny,
                                    me['stats']['radius']+.7, 13.))
        return sorted(hazards)[:8]

    def _dodge(self, obs, direction, hazards, boost, anchor, cap):
        me = obs['self']
        p, v = me['pos'], me['vel']
        st = me['stats']
        speed = st['boost_speed'] if boost and me['grounded'] else st['walk_speed']
        if any(w and w.get('charge', 0.) > 0. for w in me['weapons']):
            speed *= st['charge_move_mult']
        accel = st['ground_accel'] if me['grounded'] else st['air_accel']
        choices = [direction, (0., 0.), unit(-v[0], -v[1])]
        # Generate candidate controls in each incoming projectile's frame.
        for _, x, y, nx, ny, _, _ in hazards[:4]:
            if not (nx or ny):
                nx, ny = unit(p[0]-x, p[1]-y)
            for s in (-1., 1.):
                choices.append((s*nx, s*ny))
                choices.append(unit(.8*direction[0]+s*nx, .8*direction[1]+s*ny))
        center, radius = ring_at(obs['zone'], obs['t'], .9)
        best, best_score = direction, -math.inf
        for ux, uy in choices:
            if (ux or uy) and not self.arena.open_step(p, (ux, uy), max(2., .36*speed)):
                continue
            dx, dy = ux*speed-v[0], uy*speed-v[1]
            settle = max(.0001, math.hypot(dx, dy)/max(1., accel))
            score = 2.4 * (ux*direction[0] + uy*direction[1])
            for dt, x, y, nx, ny, size, cost in hazards:
                ramp = min(settle, dt)
                integral = .5*ramp*ramp/settle + (dt-ramp)
                rx, ry = p[0]+v[0]*dt+dx*integral-x, p[1]+v[1]*dt+dy*integral-y
                miss = abs(rx*nx+ry*ny) if nx or ny else math.hypot(rx, ry)
                score -= cost * min(1.5, max(0., 1.5 - miss/size)) / (1.+.5*dt)
            end = (p[0]+.48*speed*ux, p[1]+.48*speed*uy)
            score -= 2.4 * max(0., distance(end, center) - radius)
            if cap:
                score -= 9. * max(0., distance(end, anchor) - cap)
            if score > best_score:
                best, best_score = (ux, uy), score
        return best

    def move(self, obs, target, threats, goal, mode, action):
        arena, now = self.arena, obs['t']
        me, zone = obs['self'], obs['zone']
        st, p, v = me['stats'], me['pos'], me['vel']
        arena.set_body(st)
        if mode == 'fight' and target is not None:
            goal = self._firing_position(obs, target, threats)
        else:
            self.key = self.charge_id = None
        if mode == 'recover' and threats and goal is None:
            if now >= self.shelter_until:
                center, radius = ring_at(zone, now, 5.)
                self.shelter = arena.cover(p, threats, center, radius, retreat=True)
                self.shelter_until = now + 1.
            goal = self.shelter
        if zone['radius'] < 7.:
            center = zone['next_center']
            z = arena.ground(*center) if arena.walkable(*center) else arena.roof(*center)
            mode, goal = 'rotate', (*center, z)
        goal = goal if goal is not None else p
        dist = distance(p, goal)
        cap = 0.
        if mode == 'capture':
            drop = min(obs.get('drops', ()), key=lambda d: distance(d['pos'], goal), default=None)
            if drop:
                cap = max(.4, drop['radius'] - 1.)
        stop = (cap * .68 if cap else 1.)
        direction = arena.steer(p, goal, now) if dist > stop else (0., 0.)
        on_roof = (len(goal) > 2 and goal[2] > arena.ground(*goal[:2])+2.5 and
                   abs(arena.roof(*goal[:2])-goal[2]) < 3.5)
        dz = goal[2]-p[2] if on_roof else 0.
        if not on_roof or dz < -.5 or (me['grounded'] and dz <= st['step_up']):
            self.climbing = False
        need_launch = on_roof and dz > st['step_up'] and (arena.needs_jet or dist < 10.)
        reserve = max(14., 14. + max(0., dz)*st['jet_drain']/max(1., st['max_rise_speed']))
        if need_launch and (not me['grounded'] or me['energy'] >= min(.96*me['max_energy'], reserve)):
            self.climbing = True
        arc = next((w for w in me['weapons'] if w and w['kind'] == 'lightning'), None)
        if arc and target and distance(p, target['pos']) < arc['stats']['range']+12:
            reserve = max(reserve, .85*me['max_energy'])
        if me['energy'] < reserve:
            self.sprint = False
        elif me['energy'] > min(.99*me['max_energy'], reserve + 25):
            self.sprint = True
        speed_needed = mode == 'fight' or dist > max(8., stop+4.)
        boost = bool(self.sprint and speed_needed and me['grounded'] and mode != 'hold' and dist > stop)
        if not self.climbing and dist <= max(stop, 3.8) and mode != 'fight':
            # Stop before momentum carries us outside the pickup / final disk.
            dx = (goal[0]-p[0]-v[0]*.11) / 4.
            dy = (goal[1]-p[1]-v[1]*.11) / 4.
            if cap:
                dx, dy = 0., 0.
            size = max(1., math.hypot(dx, dy))
            direction, boost = (dx/size, dy/size), False
        if mode == 'hold' and not threats:
            direction = direction[0]*.39, direction[1]*.39
        if self.last_motion is None or now - self.last_motion[0] >= 1.3:
            if self.last_motion is not None:
                then, old, old_goal = self.last_motion
                if (mode in ('collect','rotate','hunt') and old_goal is not None and
                        distance(goal, old_goal) < 5. and dist > 6. and distance(p, old) < 1.2 and
                        not on_roof and me['grounded'] and me['energy'] > 38. and now-then < 2.):
                    arena.route.clear()
                    self.unstick_until = now + .48
            self.last_motion = now, tuple(p), tuple(goal) if dist > 6. else None
        jump = False
        if self.climbing:
            direction = unit(goal[0]-p[0], goal[1]-p[1])
            boost = dz > -.3
            jump = boost and me['grounded']
        elif now < self.unstick_until:
            jump, boost = me['grounded'], True
        else:
            hazards = self._hazards(obs)
            if hazards:
                direction = self._dodge(obs, direction, hazards, boost, goal, cap)
        action.update(move=list(direction), boost=bool(boost), jump=bool(jump))
