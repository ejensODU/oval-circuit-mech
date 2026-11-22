"""Relay, a mechfight/1 controller. One encounter plan drives both arms and legs."""
import math
import sys
from dataclasses import dataclass

import mechfight
from arena import Arena
from combat import Combat
from tactics import Tactics, ring_at


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def into_disk(p, center, r):
    f = min(1., max(0., r) / max(.001, distance(p, center)))
    return center[0] + f * (p[0] - center[0]), center[1] + f * (p[1] - center[1])


@dataclass
class Plan:
    mode: str
    goal: tuple | list
    resource: int | None = None


class MatchRead:
    """Practice is the prior. Switch once multiple independent deaths or combat evidence warrants it."""
    def __init__(self, config):
        self.body = config['mech']
        self.growth = config['cubes']['size_per_cube']
        self.start_alive = None
        self.kills = set()
        self.hp = {}
        self.rival_hits = set()
        self.is_ffa = False

    def update(self, obs, rivals):
        if self.start_alive is None:
            self.start_alive = obs['alive']
        t = obs['t']
        our_hits = set()
        for e in obs['events']:
            if e['type'] == 'kill':
                self.kills.add(e['target'])
            elif e['type'] == 'hit':
                our_hits.add(e['target'])
        if self.is_ffa:
            return
        # A rival losing HP alone is ambiguous (drones, storm). Require a rival's
        # beam segment through that body, recent observed HP loss, and no own hit.
        for r in rivals:
            rid, p = r['id'], r['pos']
            hp = r.get('hp')
            last_t, old_hp = self.hp.get(rid, (-10., hp))
            self.hp[rid] = (t, hp)
            if hp is None or old_hp is None or t-last_t > .3 or hp >= old_hp or rid in our_hits:
                continue
            for b in obs.get('beams', ()):
                if b['by'] in (obs['self']['id'], rid):
                    continue
                for a, end in zip(b['path'], b['path'][1:]):
                    dx, dy = end[0]-a[0], end[1]-a[1]
                    f = min(1., max(0., ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / max(.001, dx*dx + dy*dy)))
                    size = 1. + self.growth*r.get('cubes', 0)
                    if (distance(p, (a[0]+f*dx, a[1]+f*dy)) <= self.body['radius']*size+.5 and
                            p[2]-1 <= a[2]+f*(end[2]-a[2]) <= p[2]+self.body['height']*size+1):
                        self.rival_hits.add((b['by'], rid))
        other_deaths = self.start_alive - obs['alive'] - len(self.kills)
        # Reliable FFA Finals latch without false-triggering in Practice.
        self.is_ffa = bool(len(self.rival_hits) >= 2
                           or (t < 100 and other_deaths >= 2)
                           or other_deaths >= 4
                           or (t < 120 and other_deaths >= 1 and self.rival_hits))


class Bot:
    name = 'Relay'

    def __init__(self, hello):
        self.cfg = hello['config']
        self.arena = Arena(hello)
        self.combat = Combat(hello, self.arena)
        self.feet = Tactics(self.arena, self.cfg)
        self.read = MatchRead(self.cfg)
        self.items = {}
        self.blocked_until = {}
        self.actors = {}
        self.focus = None
        self.last_fight_lane = (None, -100.)
        self.resource = None
        self.resource_progress = None
        self.sound = None
        self.visits = {}
        self.sweep_dir = 1 if hello['seed'] % 2 else -1
        self.waypoint = self.shelter = self.peek = None
        self.sweep_due = self.shelter_due = self.peek_due = -1.
        self.cover_ids = self.peek_id = None
        self.last_exchange = -100.
        self.damage_bearing = None
        self.route_due, self.route_key, self.route_extra = -1., None, 0.
        self.repairing = False
        self.mode = 'hunt'
        self.errors = 0

    @property
    def is_ffa(self):
        return self.read.is_ffa

    def remember(self, obs, rivals):
        me, t = obs['self'], obs['t']
        p = me['pos']
        self.read.update(obs, rivals)
        for obj in rivals:
            self.actors[obj['id']] = (t, obj)
        visible = set()
        for group in ('pickups', 'cubes', 'crates'):
            for obj in obs[group]:
                visible.add(obj['id'])
                self.items[obj['id']] = (group, obj, t)
        for e in obs['events']:
            kind = e['type']
            if kind == 'took' or (kind == 'take_failed' and e.get('reason') == 'gone'):
                self.items.pop(e['id'], None)
            if kind == 'kill':
                self.actors.pop(e['target'], None)
            if kind in ('hit', 'damaged') and e.get('weapon') != 'zone':
                self.last_exchange = t
            if kind == 'damaged' and e.get('bearing') is not None:
                self.damage_bearing = (t, e['bearing'])
        for key, (group, obj, stamp) in list(self.items.items()):
            if key in visible:
                continue
            q = obj['pos']
            # Inside proximity sensors, foliage cannot hide an item. A thin
            # wall still can. Forget positively observed empty ground quickly.
            if t-stamp > (75 if group == 'pickups' else 22) or (
                    distance(p, q) < 27 and abs(q[2]-p[2]) < 5 and
                    self.arena.clear((*p[:2], p[2]+me['stats']['eye_height']), (*q[:2], q[2]+1.2))):
                del self.items[key]
        self.visits[(round(p[0]/32), round(p[1]/32))] = t
        sound_rules = self.cfg['sound']
        for s in obs['sounds']:
            if s['kind'] not in ('gun', 'mortar', 'laser', 'laser_charge', 'lightning', 'step', 'boost', 'reload', 'jet'):
                continue
            band = s['dist']
            low, high = {'near': (0, sound_rules['near']), 'mid': (sound_rules['near'], sound_rules['mid']),
                         'far': (sound_rules['mid'], sound_rules['range'].get(s['kind'], 300))}[band]
            reach = min(sound_rules['range'].get(s['kind'], high), high)
            estimate = .5*(low + reach)
            strength = {'near': 3., 'mid': 2., 'far': 1.}[band] + (.45 if s['kind'] == 'laser_charge' else 0.)
            stamp = t - s.get('age', 0.)
            if self.sound and strength < self.sound[2] - .55*(stamp-self.sound[0]):
                continue
            yaw = s['bearing']
            self.sound = (stamp, (p[0]+estimate*math.cos(yaw), p[1]+estimate*math.sin(yaw)), strength)

    def weapon_value(self, kind, obs, rivals):
        if kind == 'gun':
            return 11.  # Recover the permanent arm if presented with a gunless loadout.
        if kind == 'laser':
            return 9.5
        if kind == 'lightning':
            return 9. if obs['zone']['radius'] < 38 else 6.3
        if kind == 'mortar':
            return -2. if any('laser' in r.get('weapons', ()) for r in rivals) else 4.4
        return 0.

    def equip(self, obs, rivals, action):
        me = obs['self']
        arms = me['weapons']
        kinds = [w['kind'] if w else None for w in arms]
        slot = next((i for i,k in enumerate(kinds) if k is None), None)
        if slot is None:
            slot = min((i for i,k in enumerate(kinds) if k != 'gun' and not arms[i].get('charge',0)),
                       key=lambda i: self.weapon_value(kinds[i],obs,rivals), default=None)
        if slot is None or (arms[slot] or {}).get('charge', 0) > 0:
            return
        held = self.weapon_value(kinds[slot], obs, rivals)
        candidates = [(self.weapon_value(item['kind'], obs, rivals), item['id']) for item in obs['pickups']
                      if item['kind'] not in kinds and abs(item['pos'][2] - me['pos'][2]) <= 4 and
                      distance(item['pos'], me['pos']) <= self.cfg['loot']['pickup_radius']]
        if candidates:
            value, item_id = max(candidates)
            if value > max(0., held):
                action['take'] = item_id
                if arms[slot]:
                    action['drop'] = slot
                return
        if held < 0:
            action['drop'] = slot

    def upgrade(self, me, choices, zone):
        values = {'gun_mag': 9.8, 'plating': 9.4, 'nanites': 9.25, 'armor': 8.8,
                  'laser_damage': 8.9, 'gun_damage': 8.6, 'servos': 8.1, 'lightning_range': 8.55,
                  'gun_rate': 7.9, 'gun_velocity': 7.8, 'laser_charge': 8.05,
                  'lightning_efficiency': 7.8, 'mortar_velocity': 7.7, 'mortar_reload': 7.65,
                  'mortar_splash': 7.35, 'jets': 6.5, 'sensors': 6.8, 'reactor': 6.3,
                  'capacitor': 6.1, 'gyros': 5.9, 'lightning_chain': 5.5, 'laser_pierce': 5.4}
        if 'gun_mag' in me['upgrades']:
            values['gun_mag'] -= 1.55
        if 'nanites' in me['upgrades']:
            values['nanites'] -= .5
        if zone['radius'] < 55 or me['hp'] < .55*me['max_hp']:
            values['plating'] += 1.4
        return max(range(len(choices)), key=lambda i: values.get(choices[i]['id'], 5.))

    def contacts(self, obs, rivals):
        me, p = obs['self'], obs['self']['pos']
        st = me['stats']
        origin = (*p[:2], p[2]+st['muzzle_height'])
        arms = {w['kind']: w for w in me['weapons'] if w}
        gun = arms.get('gun', {}).get('stats', self.cfg['weapons']['gun'])
        # First rank all actors cheaply; reserve exact rays for the best few.
        contacts = []
        for r in rivals:
            q = r['pos']
            d = distance(p, q)
            facing = math.cos(math.atan2(p[1]-q[1],p[0]-q[0])-r['aim'][0])
            hp = r.get('hp', self.cfg['mech']['hp'])
            sustained = gun['damage']/(gun['cooldown'] + gun['reload']/gun['mag'])
            sustained *= max(.30, 1. - max(0., d-65)/320) if d < gun['range']+4 else .1
            if 'laser' in arms:
                beam = arms['laser']['stats']
                if d < beam['range']:
                    sustained += beam['damage'] * (1.+beam['cookoff']*('mortar' in r.get('weapons', ()))) / (beam['charge']+beam['cooldown']+.1)
            if 'lightning' in arms and d < arms['lightning']['stats']['range']+5:
                arc = arms['lightning']['stats']
                sustained += arc['damage']/arc['cooldown']
            if 'mortar' in arms and 16 < d < 180:
                sustained += 40.
            if facing < 0:
                sustained *= 1.15
            cost = hp/max(20., sustained) + d/70. - 1.65*(self.focus == r['id'])
            cost -= 2.8*(d < 46) + 1.2*(facing > .9 and r.get('charging', False))
            contacts.append((cost, r['id'], r))
        contacts.sort(key=lambda entry: entry[:2])
        assessed = []
        for i, (cost, rid, r) in enumerate(contacts):
            if i >= 6:
                break
            q = r['pos']
            h = self.cfg['mech']['height']*(1+self.cfg['cubes']['size_per_cube']*r.get('cubes',0))
            opened = self.arena.clear(origin, (*q[:2], q[2]+.55*h)) or self.arena.clear(origin, (*q[:2], q[2]+.90*h))
            assessed.append((cost + (0. if opened else 25.), rid, dict(r, _open=opened)))
        assessed.sort(key=lambda entry: entry[:2])
        return [r for _, _, r in assessed]

    def ground_goal(self, p, center, radius):
        """Prefer ground inside the destination disk. A roof is sometimes the only surface."""
        xy = into_disk(p, center, max(0., radius - min(12., .25*radius)))
        if self.arena.walkable(*xy):
            return (*xy, self.arena.ground(*xy))
        candidates = []
        for offset in (min(9., radius*.6), min(24., radius*.82)):
            if offset < 1.:
                continue
            for j in range(12):
                a = j*math.tau/12
                q = (xy[0]+offset*math.cos(a), xy[1]+offset*math.sin(a))
                if distance(q, center) < radius-.5 and self.arena.walkable(*q):
                    candidates.append(q)
        if candidates:
            xy = min(candidates, key=lambda q: distance(p, q))
            return (*xy, self.arena.ground(*xy))
        return (*xy, self.arena.roof(*xy))

    def rotation(self, obs):
        me, z, t = obs['self'], obs['zone'], obs['t']
        p, c, r = me['pos'], z['next_center'], z['next_radius']
        center, future = ring_at(z, t, 12.)
        need = distance(p,c) - max(0., r-min(12., r*.25))
        if me['in_zone'] and need < max(.25, r*.025) and z['radius'] > 6:
            return None
        # Cache detour and ascent time; actor movement alone must not rebuild A*.
        key = (*c,r)
        if self.route_key != key or t >= self.route_due:
            self.route_key, self.route_due = key, t+2.
            goal = self.ground_goal(p,c,r)
            self.route_extra = min(140., max(0., self.arena.travel_bound(p,goal)-distance(p,goal)))
            self.route_climb = max(0.,goal[2]-p[2]-2.) if not self.arena.walkable(*goal[:2]) else 0.
        eta = (max(0., need) + self.route_extra) / max(1., me['stats']['walk_speed']*.68) + self.route_climb/6.5 + 5.
        if (not me['in_zone'] or z['radius'] < 6 or distance(p,center) > max(0., future-min(5., future*.12))
                or (need > 1. and t+eta >= z['shrink_end'])):
            return Plan('rotate', self.ground_goal(p, c, r))
        return None

    def resources(self, obs, contacts):
        me, t, zone = obs['self'], obs['t'], obs['zone']
        p, st = me['pos'], me['stats']
        kinds = [w['kind'] for w in me['weapons'] if w]
        secondary = next((k for k in kinds if k != 'gun'), None)
        old = self.weapon_value(secondary, obs, contacts)
        cc = self.cfg['cubes']
        cubes = me['cubes']
        next_offer = next((n for n in cc['upgrade_thresholds'] if n>cubes), math.inf)
        marginal = (1. if cubes < cc['stat_cap'] else .60 if cubes < 15 else .19)
        objects = list(self.items.values())
        objects.extend(('npc', a, t) for a in obs['mechs'] if a['kind']=='npc')
        objects.extend(('drop', a, t) for a in obs.get('drops', ()))
        best = None
        for group, item, seen in objects:
            key, q = item['id'], item['pos']
            if self.blocked_until.get(key, -1.) > t:
                continue
            d = distance(p,q)
            if d > (300 if group=='drop' else 172):
                continue
            climb = max(0., q[2]-p[2]-3.) if q[2] > self.arena.ground(*q[:2])+3. else 0.
            if climb > st['max_rise_speed']*me['max_energy']/max(1.,st['jet_drain'])*.84:
                continue
            eta = d / (st['walk_speed']*1.12) + climb/6.5
            owner = item.get('holder')
            if group=='pickups':
                if item['kind'] in kinds:
                    continue
                gain = self.weapon_value(item['kind'], obs, contacts)-max(0.,old)
                if gain <= 0:
                    continue
                benefit = (9. if not secondary else 1.) + 2.3*gain
            elif group=='drop':
                if owner not in (None, me['id']) and item['progress']+eta >= item['capture_s']+.3:
                    continue
                cap = item['capture_s'] - (item['progress'] if owner == me['id'] else 0.)
                eta = max(item['lands_in'], eta)+max(0.,cap)
                benefit = 16.*marginal + 65.*(1-me['hp']/me['max_hp'])
                if owner == me['id']:
                    benefit += item['progress']*7.
            else:
                yield_count = self.cfg['npc' if group=='npc' else 'crates']['cubes'] if group!='cubes' else 1
                benefit = marginal*(8.*yield_count + 5.*(yield_count >= next_offer-cubes))
                if group!='cubes':
                    eta += item.get('hp',300.)/(170. if secondary else 103.)
                    if group=='crates' and not secondary:
                        benefit += 5.
            c,r = ring_at(zone,t,eta+3.)
            if distance(c,q) > r-min(1.5, r*.1):
                continue
            if self.resource == key:
                benefit *= 1.14
            benefit *= max(.45, 1.-(t-seen)*.028)
            if contacts and group in ('cubes','crates','npc','drop'):
                foe_distance = min(distance(q, e['pos']) for e in contacts)
                if foe_distance + 8 < d or (group=='drop' and foe_distance <= item['radius']):
                    benefit *= .18
            value = benefit / (eta+2.4)
            if best is None or value > best[0]:
                best = (value, group, item)
        return best

    def follow(self, resource, obs):
        value, group, obj = resource
        p, q = obs['self']['pos'], obj['pos']
        if group in ('npc','crates'):
            # A roof crate needs a firing platform on the same roof. Start on
            # the approach side, then look around the object if that side is blocked.
            st = obs['self']['stats']
            gap = max(13., st['muzzle_height']*1.65) + (8. if group=='npc' else 0.)
            bearing = math.atan2(p[1]-q[1],p[0]-q[0])
            raised = q[2] > self.arena.ground(*q[:2])+3.
            height = self.cfg['npc' if group=='npc' else 'crates']['height']
            for offset in (0.,.7,-.7,1.5,-1.5,math.pi):
                a = bearing + offset
                xy = q[0]+gap*math.cos(a),q[1]+gap*math.sin(a)
                if raised:
                    z = self.arena.roof(*xy)
                    if abs(z-q[2]) > 3.5:
                        continue
                else:
                    if not self.arena.walkable(*xy):
                        continue
                    z = self.arena.ground(*xy)
                dz = q[2]+height*.55-z-st['muzzle_height']
                if st['min_pitch'] < math.atan2(dz,gap) < st['max_pitch'] and self.arena.clear(
                        (*xy,z+st['muzzle_height']), (*q[:2],q[2]+height*.55)):
                    q = (*xy,z)
                    break
        return Plan('capture' if group=='drop' else 'collect', q, obj['id'])

    def cover_goal(self, obs, threats, c, r):
        t, p = obs['t'], obs['self']['pos']
        ids = tuple(sorted(e['id'] for e in threats[:3]))
        if t >= self.shelter_due or self.cover_ids != ids or (self.shelter and distance(self.shelter,c)>r):
            self.shelter_due, self.cover_ids = t+.85, ids
            xy = self.arena.cover(p, threats[:3], c, max(0.,r-2), retreat=bool(threats))
            self.shelter = (*xy,self.arena.ground(*xy)) if xy else None
        return self.shelter

    def search(self, obs, c, r):
        me, t = obs['self'], obs['t']
        p = me['pos']
        if self.sound and t-self.sound[0] < 4. and not (self.is_ffa and obs['alive'] > 2):
            q = self.sound[1]
            # A bearing + distance band gives a sector. Approach an offset inside
            # the safe disk for a new sightline, never fire at this estimate.
            d = max(.01, distance(p,q))
            f = min(.68, 62./d)
            xy = into_disk((p[0]+f*(q[0]-p[0]), p[1]+f*(q[1]-p[1])),c,max(0.,r-5.))
            if self.arena.walkable(*xy):
                return (*xy,self.arena.ground(*xy))
        if self.waypoint and t < self.sweep_due and distance(p,self.waypoint) > 8 and distance(c,self.waypoint)<r:
            return self.waypoint
        # Walk successive sectors just inside the occupied annulus. The radial
        # term draws far spawns inward; visited cells and wall distance choose lanes.
        theta = math.atan2(p[1]-c[1],p[0]-c[0])
        belt = min(r*.78, max(r*.44, obs['zone']['next_radius']*.82))
        best, high = None, -math.inf
        for side in (self.sweep_dir,-self.sweep_dir):
            bypass = theta + side*.36
            anchor = (c[0]+belt*math.cos(bypass),c[1]+belt*math.sin(bypass))
            heading = math.atan2(anchor[1]-p[1],anchor[0]-p[0])
            for step in (38.,72.):
                for delta in (0.,-.44,.44,-.88,.88):
                    a = heading+delta
                    q = into_disk((p[0]+step*math.cos(a),p[1]+step*math.sin(a)),c,max(0.,r-3.))
                    if not self.arena.walkable(*q) or distance(p,q)<4.:
                        continue
                    visit = self.visits.get((round(q[0]/32),round(q[1]/32)),-100.)
                    barrier = self.arena.ground_clearance(*q)
                    v = (min(1., (t-visit)/30.) + .45*(side==self.sweep_dir) - abs(delta)*.37
                         - .012*abs(barrier-11.) - .005*distance(anchor,q))
                    if v > high:
                        high,best = v,q
        self.sweep_due = t+6.5
        self.waypoint = (*best,self.arena.ground(*best)) if best else self.ground_goal(c,c,r)
        return self.waypoint

    def plan(self, obs, contacts, resource):
        me, t, z = obs['self'], obs['t'], obs['zone']
        p = me['pos']
        center, radius = ring_at(z, t, 8.)
        target = contacts[0] if contacts else None
        visible = [e for e in contacts if e['_open']]
        fraction = me['hp']/me['max_hp']
        finals = self.is_ffa and obs['alive'] > 2
        rotate = self.rotation(obs)
        if rotate:
            return rotate
        if resource:
            value, group, obj = resource
            d = distance(p,obj['pos'])
            if group == 'drop':
                rem_cap = obj['capture_s'] - (obj['progress'] if obj.get('holder') == me['id'] else 0.)
                drop_ambush = (len(visible) >= 2 and min(distance(p, e['pos']) for e in visible) < 65. and rem_cap > 1.2)
                if not drop_ambush and (obj.get('holder') == me['id'] or
                        (value > 1.7 and (not visible or fraction < .78 or d < obj['radius']+5.))):
                    return self.follow(resource,obs)
            # A second arm on our route pays for itself in a single duel.
            # Avoid crossing a rival or delaying a finish to obtain it.
            if visible and group in ('pickups','cubes'):
                foe = min(distance(p,e['pos']) for e in visible)
                bare = not all(me['weapons'])
                reach = min(68.,.55*foe) if group=='pickups' and bare else min(13.,.30*foe)
                if d < reach and value > 1.1 and target.get('hp',1400) > 280:
                    return self.follow(resource,obs)
            if target and not visible and group=='pickups' and not all(me['weapons']) and d < 30. and value > 1.2:
                return self.follow(resource,obs)
        self.repairing = fraction < (.57 if finals else .38) or (self.repairing and fraction < (.89 if finals else .72))
        finish = (target is not None and target.get('hp',1400) <= min(.5*me['hp'], 390.) and len(visible)<=2)
        if finals and target and not finish and radius>18:
            # A rival aiming away usually has somewhere else to spend its ammo.
            engaged = any(e.get('charging') or math.cos(math.atan2(p[1]-e['pos'][1],p[0]-e['pos'][0])-e['aim'][0]) > .88 for e in visible)
            if not engaged and t-self.last_exchange>3. and min(distance(p,e['pos']) for e in contacts)>53.:
                safe = self.cover_goal(obs, contacts, center, radius)
                if safe:
                    return Plan('hold',safe)
        if self.repairing and radius>18. and not finish:
            safe = self.cover_goal(obs, contacts, center, radius)
            if safe:
                return Plan('recover',safe)
            if not visible:
                return Plan('recover',p)
        if target and target['_open']:
            return Plan('fight',target['pos'])
        if target:
            # Keep a cover break long enough to beat the incoming release or
            # to reload. The first occluded observation is often the best place
            # to stand for another few ticks. Then resume the pursuit.
            last_id, last_t = self.last_fight_lane
            guns_ready = any(w and max(w['cooldown'],w.get('reload',0.)) < .35 and
                             (w['kind'] != 'lightning' or distance(p,target['pos']) < w['stats']['range'])
                             for w in me['weapons'])
            if last_id == target['id'] and t-last_t < .85 and (not guns_ready or target.get('charging')):
                return Plan('fight',target['pos'])
            # Actors in server memory have real current coordinates. A lateral
            # peek usually rebuilds the lane faster than running at the wall.
            if self.peek_id != target['id'] or t >= self.peek_due:
                self.peek_id,self.peek_due = target['id'],t+.65
                xy = self.arena.peek_spot(p,target,center,max(0.,radius-2.))
                self.peek = (*xy,self.arena.ground(*xy)) if xy else None
            if not finals and (self.peek or not resource or self.focus==target['id'] or target.get('hp',1400)<500):
                return Plan('hunt', self.peek or target['pos'])
        if resource:
            value, group, obj = resource
            if (not finals and value > (.80 if me['cubes']>=10 and all(me['weapons']) else .31)) or (
                    finals and value > (.95 if me['cubes']>=6 and all(me['weapons']) else .45)):
                return self.follow(resource,obs)
        if finals:
            safe = self.cover_goal(obs,contacts,center,radius)
            if safe:
                return Plan('hold',safe)
        last = self.actors.get(self.focus)
        if not finals and last and t-last[0] < 3. and distance(last[1]['pos'],center)<radius:
            return Plan('hunt',last[1]['pos'])
        return Plan('hunt', self.search(obs,center,radius))

    def update_progress(self, plan, obs):
        t = obs['t']
        d = math.dist(obs['self']['pos'], plan.goal) if len(plan.goal) == 3 else distance(obs['self']['pos'], plan.goal)
        hitting = any(e['type']=='hit' and e['target']==plan.resource for e in obs['events'])
        capturing = plan.mode=='capture' and any(d['id']==plan.resource and
                       (d.get('holder') == obs['self']['id'] or d['lands_in']>0) for d in obs.get('drops',()))
        if self.resource != plan.resource or self.resource_progress is None:
            self.resource_progress = (t,d)
        elif plan.resource is not None:
            since, best = self.resource_progress
            if best-d > 3. or hitting or capturing:
                self.resource_progress = (t,d)
            elif t-since > (12. if plan.mode=='capture' else 7.):
                self.blocked_until[plan.resource] = t+12.
                self.resource_progress = None
        self.resource = plan.resource

    def farm_target(self, obs, plan):
        me, p = obs['self'], obs['self']['pos']
        choices = []
        for obj in obs['mechs']:
            if obj['kind']=='npc':
                choices.append(obj)
        choices.extend(dict(crate,kind='crate') for crate in obs['crates'])
        # Collect what we shoot. An uncollected distant cube buys no advantage.
        candidates = []
        for obj in choices:
            q = obj['pos']
            d = distance(p,q)
            direct = obj['id'] == plan.resource
            if not direct and (d>53. or distance(q,plan.goal)>46.):
                continue
            gain = (1.45 if obj['kind']=='npc' else 1.) / (1.+d/45.)
            gain *= 1.+.55*(1.-min(1.,obj.get('hp',300.)/self.cfg['npc' if obj['kind']=='npc' else 'crates']['hp']))
            candidates.append((gain+2.*direct,obj['id'],obj))
        eye = (*p[:2], p[2]+me['stats']['muzzle_height'])
        for _, _, obj in sorted(candidates,reverse=True)[:3]:
            q = obj['pos']
            body = self.cfg['npc' if obj['kind']=='npc' else 'crates']
            if self.arena.clear(eye,(*q[:2],q[2]+.6*body['height'])):
                return obj
        return None

    def decide(self, obs):
        me, t, p = obs['self'], obs['t'], obs['self']['pos']
        self.arena.set_body(me['stats'])
        rivals = [a for a in obs['mechs'] if a['kind']=='mech' and a.get('hp',1)>0]
        self.remember(obs,rivals)
        contacts = self.contacts(obs,rivals)
        resources = self.resources(obs,contacts)
        plan = self.plan(obs,contacts,resources)
        self.mode = plan.mode
        self.update_progress(plan,obs)
        target = contacts[0] if contacts else None
        if target:
            self.focus = target['id']
            if target['_open'] and plan.mode=='fight':
                self.last_fight_lane = target['id'],t
        object_to_shoot = target
        if plan.mode == 'hold' or (plan.mode=='recover' and (target is None or not target['_open'])):
            object_to_shoot = None
        elif target is None and plan.mode == 'collect':
            object_to_shoot = self.farm_target(obs,plan)
        if target:
            goal = target['pos']
            yaw = math.atan2(goal[1]-p[1],goal[0]-p[0])
        elif self.damage_bearing and t-self.damage_bearing[0] < 1.1:
            yaw = self.damage_bearing[1]
        elif self.sound and t-self.sound[0] < 2.1:
            q = self.sound[1]
            yaw = math.atan2(q[1]-p[1],q[0]-p[0])
        else:
            yaw = me['aim'][0] + self.sweep_dir*.42
        action = {'tick':obs['tick'], 'aim':[math.atan2(math.sin(yaw),math.cos(yaw)),0.]}
        self.combat.engage(obs,object_to_shoot,action)
        self.feet.move(obs,target,contacts,plan.goal,plan.mode,action)
        self.equip(obs,contacts,action)
        choices = (obs.get('offer') or {}).get('choices',())
        if choices:
            action['pick'] = self.upgrade(me,choices,obs['zone'])
        return action

    def act(self, obs):
        try:
            return self.decide(obs)
        except Exception as exc:
            # Limit diagnostics on the real-time wire. A later good frame can
            # resume normal play; never replay a stale trigger or pickup.
            self.errors += 1
            if self.errors <= 2:
                print(f"Relay fallback on tick {obs.get('tick')}: {type(exc).__name__}: {exc}",file=sys.stderr)
            return {'tick':obs.get('tick',0),'move':[0.,0.],'aim':obs.get('self',{}).get('aim',[0.,0.]),
                    'fire':[False,False],'boost':False,'jump':False}


if __name__ == '__main__':
    mechfight.run(Bot)
