"""Campus geometry and ground navigation, rebuilt from each match's mounted map."""

from array import array
from heapq import heappop, heappush
import json
import math
from pathlib import Path
import sys


def unit(x, y):
    length = math.hypot(x, y)
    return (x / length, y / length) if length > 0.01 else (0.0, 0.0)


class Arena:
    """Positions use metres. A goal with a third coordinate can request a roof."""

    def __init__(self, hello):
        directory = Path(hello["map"]["dir"])
        files = hello["map"].get("files", [])
        world_file = next((f for f in files if f.endswith("world.json")), "v2/world.json")
        with (directory / world_file).open() as f:
            world = json.load(f)
        meta = world["terrain"]
        self.ox, self.oy = meta["minX"], meta["minY"]
        self.step, self.nx, self.ny = meta["step"], meta["nx"], meta["ny"]
        self.inv = 1.0 / self.step
        self.scale = meta.get("scale", 0.01)
        self.heights = array("h")
        with (directory / world_file).with_name(meta.get("file", "terrain.i16")).open("rb") as f:
            self.heights.fromfile(f, self.nx * self.ny)
        if sys.byteorder != "little":
            self.heights.byteswap()
        mech = hello["config"]["mech"]
        self.body = mech["radius"]
        self.height = mech["height"]
        self.max_step = mech["step_up"]
        self.slope = math.tan(math.radians(mech["max_slope_deg"]))
        self.bounds = world["bounds"]
        count = self.nx * self.ny
        self.floor = array("h", [32767]) * count
        self.ceiling = array("h", [-32768]) * count
        self.bridge_base = array("h", [32767]) * count
        self.bridge_top = array("h", [-32768]) * count
        for prism in world["prisms"]:
            self._rasterize(prism)

        self.base_height = self.height
        self.base_margin = self._clearances(self.height)
        cube_rules = hello["config"].get("cubes", {})
        self.size_per_cube = cube_rules.get("size_per_cube", 0.05)
        self.muzzle = mech.get("muzzle_height", self.height * 0.78)
        self.drone_height = hello["config"].get("npc", {}).get("height", 3.5)
        # Precompute two conservative headroom tiers while keeping the observed
        # horizontal radius exact. Geometry keeps growing above the HP stat cap.
        self.tall_height = self.height * 1.5
        self.tall_margin = self._clearances(self.tall_height)
        self.giant_margin = self._clearances(math.inf)
        self.margin = self.base_margin
        # Chamfer units: 3 axial / 4 diagonal. Round up to allow for raster error.
        self.min_margin = max(1, math.ceil((self.body + 0.25) * self.inv * 3))
        self.spacing = max(1, round(4.0 * self.inv))
        self.gap = self.spacing * self.step
        self.width = (self.nx - 1) // self.spacing + 1
        self.rows = (self.ny - 1) // self.spacing + 1
        self._graph()
        self.route = []
        self.last_target = None
        self.route_time = -100.0
        self.needs_jet = False

    def _rasterize(self, prism):
        """Fill even/odd scanline intervals, including courtyard holes."""
        nx, ny, inv = self.nx, self.ny, self.inv
        rows = {}
        for ring in (prism["ring"], *prism.get("holes", [])):
            if len(ring) < 6:
                continue
            ax, ay = (ring[-2] - self.ox) * inv, (ring[-1] - self.oy) * inv
            for k in range(0, len(ring), 2):
                bx, by = (ring[k] - self.ox) * inv, (ring[k + 1] - self.oy) * inv
                if ay != by:
                    slope = (bx - ax) / (by - ay)
                    for j in range(max(0, math.ceil(min(ay, by))), min(ny, math.ceil(max(ay, by)))):
                        rows.setdefault(j, []).append(ax + (j - ay) * slope)
                ax, ay = bx, by
        if prism.get("kind") == "bridge":
            base_field, top_field = self.bridge_base, self.bridge_top
        else:
            base_field, top_field = self.floor, self.ceiling
        base = max(-32768, min(32767, round(100 * prism["base"])))
        top = max(-32768, min(32767, round(100 * prism["top"])))
        for j, xs in rows.items():
            xs.sort()
            offset = j * nx
            for lo, hi in zip(xs[::2], xs[1::2]):
                for p in range(offset + max(0, math.ceil(lo)), offset + min(nx, math.ceil(hi))):
                    if top > top_field[p]:
                        top_field[p] = top
                    if base < base_field[p]:
                        base_field[p] = base

    def _clearances(self, height):
        nx, ny = self.nx, self.ny
        b = self.bounds
        left = max(1, math.ceil((b["minX"] - self.ox) * self.inv))
        right = min(nx - 2, math.floor((b["maxX"] - self.ox) * self.inv))
        bottom = max(1, math.ceil((b["minY"] - self.oy) * self.inv))
        top = min(ny - 2, math.floor((b["maxY"] - self.oy) * self.inv))
        margin = bytearray(nx * ny)
        heights = self.heights
        ground_scale = 100 * self.scale
        head = 100 * height
        step = 100 * self.max_step
        steep = self.slope * self.step / self.scale
        for j in range(bottom, top + 1):
            for p in range(j * nx + left, j * nx + right + 1):
                z = heights[p] * ground_scale
                if self.ceiling[p] > z + step and self.floor[p] < z + head:
                    continue
                if self.bridge_top[p] > z + step and self.bridge_base[p] < z + head:
                    continue
                if abs(heights[p-1] - heights[p+1]) ** 2 + abs(heights[p-nx] - heights[p+nx]) ** 2 > 4 * steep ** 2:
                    continue
                margin[p] = min(240, margin[p-1]+3, margin[p-nx]+3,
                                margin[p-nx-1]+4, margin[p-nx+1]+4)
        for j in range(top, bottom - 1, -1):
            for p in range(j * nx + right, j * nx + left - 1, -1):
                if margin[p] > 3:
                    margin[p] = min(margin[p], margin[p+1]+3, margin[p+nx]+3,
                                    margin[p+nx-1]+4, margin[p+nx+1]+4)
        return margin

    def _pixel(self, x, y):
        i = int((x - self.ox) * self.inv + 0.5)
        j = int((y - self.oy) * self.inv + 0.5)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return None
        return j * self.nx + i

    def ground(self, x, y):
        gx = min(self.nx - 1.001, max(0.0, (x - self.ox) * self.inv))
        gy = min(self.ny - 1.001, max(0.0, (y - self.oy) * self.inv))
        ix, iy = int(gx), int(gy)
        fx, fy = gx - ix, gy - iy
        p = iy * self.nx + ix
        h = self.heights
        return self.scale * ((1-fy) * ((1-fx)*h[p] + fx*h[p+1]) +
                             fy * ((1-fx)*h[p+self.nx] + fx*h[p+self.nx+1]))

    def roof(self, x, y):
        p = self._pixel(x, y)
        z = self.ground(x, y)
        return z if p is None else max(z, 0.01 * max(self.ceiling[p], self.bridge_top[p]))

    def ground_clearance(self, x, y):
        """Approximate metres between a ground position and the nearest barrier."""
        pixel = self._pixel(x, y)
        return 0 if pixel is None else self.margin[pixel] * self.step / 3

    def walkable(self, x, y):
        p = self._pixel(x, y)
        return p is not None and self.margin[p] >= self.min_margin

    def clear(self, a, b):
        """Test a shot segment against terrain, structures and bridge spans."""
        return self._clear_sampled(a, b, self.inv)

    def _clear_sampled(self, a, b, samples_per_metre):
        x, y, z = a
        dx, dy, dz = b[0]-x, b[1]-y, b[2]-z
        steps = max(1, math.ceil(max(abs(dx), abs(dy), abs(dz)) * samples_per_metre))
        gx = (x - self.ox) * self.inv + 0.5
        gy = (y - self.oy) * self.inv + 0.5
        sx, sy, sz = dx*self.inv/steps, dy*self.inv/steps, dz*100/steps
        hz = z * 100
        nx, ny = self.nx, self.ny
        ground_scale = 100 * self.scale
        # Leave out the target endpoint: a crate, for example, rests on terrain.
        for _ in range(steps):
            i, j = int(gx), int(gy)
            if not (0 <= i < nx and 0 <= j < ny):
                return False
            p = j*nx+i
            if self.heights[p]*ground_scale > hz + 5:
                return False
            if self.floor[p] <= hz <= self.ceiling[p] or self.bridge_base[p] <= hz <= self.bridge_top[p]:
                return False
            gx += sx
            gy += sy
            hz += sz
        return True

    def set_body(self, stats):
        """Apply observed cube-scaled dimensions to local steering and route widths."""
        radius = stats.get("radius", self.body)
        height = stats.get("height", self.height)
        if radius == self.body and height == self.height:
            return
        self.body, self.height = radius, height
        self.min_margin = max(1, math.ceil((radius + 0.25) * self.inv * 3))
        if height > self.tall_height + 0.01:
            self.margin = self.giant_margin
        elif height > self.base_height + 0.01:
            self.margin = self.tall_margin
        else:
            self.margin = self.base_margin
        self.route.clear()

    def _ground_line(self, a, b):
        return self._line_margin(a, b, self.margin) >= self.min_margin

    def _line_margin(self, a, b, margin):
        # Walk every raster cell the segment enters. Uniform-distance samples
        # can skip the corner of an occupied pixel when smoothing an A* route.
        gx, gy = (a[0]-self.ox)*self.inv+0.5, (a[1]-self.oy)*self.inv+0.5
        hx, hy = (b[0]-self.ox)*self.inv+0.5, (b[1]-self.oy)*self.inv+0.5
        ix, iy, ex, ey = math.floor(gx), math.floor(gy), math.floor(hx), math.floor(hy)
        if not (0 <= ix < self.nx and 0 <= iy < self.ny
                and 0 <= ex < self.nx and 0 <= ey < self.ny):
            return 0
        dx, dy = hx-gx, hy-gy
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        tx = 1/abs(dx) if dx else math.inf
        ty = 1/abs(dy) if dy else math.inf
        next_x = ((ix+1-gx) if dx >= 0 else (gx-ix))*tx if dx else math.inf
        next_y = ((iy+1-gy) if dy >= 0 else (gy-iy))*ty if dy else math.inf
        p = iy*self.nx+ix
        clearance = margin[p]
        while ix != ex or iy != ey:
            if ix != ex and iy != ey and abs(next_x-next_y) < 1e-10:
                clearance = min(clearance, margin[p+sx], margin[p+sy*self.nx])
                ix += sx
                iy += sy
                p += sx+sy*self.nx
                next_x += tx
                next_y += ty
            elif (next_x < next_y and ix != ex) or iy == ey:
                ix += sx
                p += sx
                next_x += tx
            else:
                iy += sy
                p += sy*self.nx
                next_y += ty
            if margin[p] < clearance:
                clearance = margin[p]
                if not clearance:
                    break
        return clearance

    def _elevated_line(self, pos, target, look=12.0):
        # Check a trunk-sized footprint ahead of the feet. Lower roofs and
        # unobstructed roof edges are traversable in the downhill direction.
        dx, dy = target[0]-pos[0], target[1]-pos[1]
        length = min(math.hypot(dx, dy), look)
        ux, uy = unit(dx, dy)
        side = self.body * 0.86
        steps = max(1, math.ceil(length / 2.0))
        for k in range(1, steps+1):
            x = pos[0] + ux * length*k/steps
            y = pos[1] + uy * length*k/steps
            for offset in (0.0, -side, side):
                px, py = x - uy*offset, y + ux*offset
                i = self._pixel(px, py)
                if i is None:
                    return False
                z = pos[2] * 100
                if (self.heights[i]*self.scale > pos[2]+self.max_step
                    or (self.floor[i] < z+self.height*100 and self.ceiling[i] > z+self.max_step*100)
                    or (self.bridge_base[i] < z+self.height*100 and self.bridge_top[i] > z+self.max_step*100)):
                    return False
        return True

    def open_step(self, pos, direction, meters=7):
        ux, uy = unit(*direction[:2])
        goal = (pos[0]+ux*meters, pos[1]+uy*meters)
        if len(pos) > 2 and pos[2] > self.ground(*pos[:2]) + 2.0:
            return self._elevated_line(pos, goal, meters)
        return self._ground_line(pos, goal)

    def _graph(self):
        """An 8-connected graph with actual clearance checked across each edge."""
        w, rows, spacing = self.width, self.rows, self.spacing
        count = w*rows
        graph = [[] for _ in range(count)]
        free = bytearray(count)
        self.points = [(self.ox + i % w * self.gap, self.oy + i // w * self.gap)
                       for i in range(count)]
        for j in range(rows):
            for i in range(w):
                n = j*w+i
                if self.margin[j*spacing*self.nx+i*spacing] < self.min_margin:
                    continue
                free[n] = 1
                for di, dj in ((-1,0), (0,-1), (-1,-1), (1,-1)):
                    if not (0 <= i+di < w and j+dj >= 0):
                        continue
                    other = n+dj*w+di
                    if not free[other]:
                        continue
                    endpoints = self.points[n], self.points[other]
                    clearance = self._line_margin(*endpoints, self.base_margin)
                    if clearance >= self.min_margin:
                        tall_clearance = self._line_margin(*endpoints, self.tall_margin)
                        giant_clearance = self._line_margin(*endpoints, self.giant_margin)
                        cost = 14 if di and dj else 10
                        graph[n].append((other, cost, tall_clearance, giant_clearance))
                        graph[other].append((n, cost, tall_clearance, giant_clearance))
        self.graph = graph
        self.component = array("i", [-1])*count
        largest = []
        comp = 0
        for n in range(count):
            if not free[n] or self.component[n] != -1:
                continue
            nodes = [n]
            self.component[n] = comp
            for node in nodes:
                for other, _, _, _ in graph[node]:
                    if self.component[other] == -1:
                        self.component[other] = comp
                        nodes.append(other)
            if len(nodes) > len(largest):
                largest = nodes
            comp += 1
        # Distances to landmarks tighten the A* lower bound around long buildings.
        self.landmarks = []
        if largest:
            anchor = largest[0]
            nearest = array("i", [100000000])*count
            for _ in range(6):
                distances = array("i", [100000000])*count
                distances[anchor] = 0
                queue = [(0, anchor)]
                while queue:
                    cost, node = heappop(queue)
                    if distances[node] != cost:
                        continue
                    for other, edge, _, _ in graph[node]:
                        distance = cost + edge
                        if distance < distances[other]:
                            distances[other] = distance
                            heappush(queue, (distance, other))
                self.landmarks.append(distances)
                next_dist = -1
                for node in largest:
                    closest = min(nearest[node], distances[node])
                    nearest[node] = closest
                    if closest > next_dist:
                        next_dist, anchor = closest, node

    def _nearest(self, point, component=None, require_line=False):
        ix = min(self.width-1, max(0, round((point[0]-self.ox)/self.gap)))
        iy = min(self.rows-1, max(0, round((point[1]-self.oy)/self.gap)))
        n = iy*self.width + ix
        def usable(node):
            cid = self.component[node]
            if cid < 0 or (component is not None and cid != component):
                return False
            xy = self.points[node]
            return self.walkable(*xy) and (not require_line or self._ground_line(point, xy))
        if usable(n):
            return n
        # If the exact cell is occupied, find the closest accessible perimeter.
        for radius in range(1, 5 if require_line else 31):
            best = None
            d2best = float("inf")
            for j in range(max(0, iy-radius), min(self.rows, iy+radius+1)):
                lo, hi = max(0, ix-radius), min(self.width-1, ix+radius)
                cols = range(lo, hi+1) if abs(j-iy) == radius else (lo, hi)
                for i in cols:
                    candidate = j*self.width+i
                    if not usable(candidate):
                        continue
                    xy = self.points[candidate]
                    d2 = (xy[0]-point[0])**2 + (xy[1]-point[1])**2
                    if d2 < d2best:
                        best, d2best = candidate, d2
            if best is not None:
                return best
        return None

    def _path(self, start, finish):
        if start == finish:
            return [start]
        w = self.width
        gx, gy = finish % w, finish // w
        landmarks = [(field, field[finish]) for field in self.landmarks if field[finish] < 100000000]
        def estimate(n):
            dx, dy = abs(n%w-gx), abs(n//w-gy)
            h = 10*max(dx, dy) + 4*min(dx, dy)
            for field, value in landmarks:
                detour = abs(field[n]-value)
                if detour > h:
                    h = detour
            return h
        costs, parent = {start: 0}, {}
        best, best_h = start, estimate(start)
        queue = [(best_h, 0, start)]
        # A partial route remains useful across the campus. Replan near its end.
        tall = self.margin is self.tall_margin
        giant = self.margin is self.giant_margin
        for _ in range(1550):
            if not queue:
                break
            _, cost, node = heappop(queue)
            if node == finish:
                best = node
                break
            if cost != costs[node]:
                continue
            h = estimate(node)
            if h < best_h:
                best, best_h = node, h
            for other, edge, width, giant_width in self.graph[node]:
                if (tall and width < self.min_margin) or (giant and giant_width < self.min_margin):
                    continue
                new_cost = cost + edge
                if new_cost < costs.get(other, 100000000):
                    costs[other] = new_cost
                    parent[other] = node
                    heappush(queue, (new_cost + 1.22*estimate(other), new_cost, other))
        path = [best]
        while path[-1] != start:
            path.append(parent[path[-1]])
        return path[::-1]

    def travel_bound(self, pos, goal):
        """Approximate metres to budget for a ground rotation around structures.

        Landmarks bound distance on the base walking graph. Independent snap
        offsets and body growth make this a scheduling estimate in the game.
        Infinity means the surfaces are disconnected in the ground graph.
        """
        straight = math.dist(pos[:2], goal[:2])
        start = self._nearest(pos)
        finish = self._nearest(goal)
        if start is None or finish is None or self.component[start] != self.component[finish]:
            return math.inf
        grid = max((abs(f[start]-f[finish]) for f in self.landmarks
                    if f[start] < 100000000), default=0)
        attachment = math.dist(pos[:2], self.points[start]) + math.dist(goal[:2], self.points[finish])
        return max(straight, grid*self.gap/10 + attachment)

    def steer(self, pos, goal, t):
        x, y = pos[:2]
        target = goal[:2]
        dx, dy = target[0]-x, target[1]-y
        self.needs_jet = False
        if math.hypot(dx, dy) < 0.25:
            return (0.0, 0.0)
        elevated = len(pos) > 2 and pos[2] > self.ground(x, y) + 2
        roof_goal = (len(goal) > 2 and goal[2] > self.ground(*target)+2.5
                     and abs(self.roof(*target)-goal[2]) < 4.0)
        if elevated and self._elevated_line(pos, target):
            return unit(dx, dy)
        if math.hypot(dx, dy) < 85 and self._ground_line(pos, target):
            return unit(dx, dy)
        if roof_goal:
            here_z = pos[2] if len(pos) > 2 else self.ground(x,y)
            # Once at the base of the destination roof, climb its face.
            ux, uy = unit(dx, dy)
            ahead = min(9.0, math.hypot(dx,dy))
            top = self.roof(x + ux*ahead, y + uy*ahead)
            if (math.hypot(dx,dy) <= 12 or
                (math.hypot(dx,dy) <= 48 and abs(top-goal[2]) < 3.0)):
                self.needs_jet = here_z < goal[2] - self.max_step
                return ux, uy
        if (not self.route or self.last_target is None
            or math.dist(self.last_target, target) > 8 or t-self.route_time > 5
            or (math.dist(self.points[self.route[0]], (x,y)) > 10
                and not self._ground_line(pos, self.points[self.route[0]]))):
            start = self._nearest(pos, require_line=self.walkable(x, y))
            finish = None if start is None else self._nearest(target, self.component[start])
            self.route = [] if finish is None else self._path(start, finish)
            self.last_target = tuple(target)
            self.route_time = t
        if self.route:
            for k in range(min(6, len(self.route)-1), 0, -1):
                if self._ground_line(pos, self.points[self.route[k]]):
                    del self.route[:k]
                    break
            waypoint = self.points[self.route[0]]
            if len(self.route) == 1 and math.dist(waypoint, (x,y)) < 2.2:
                self.route.clear()
                if roof_goal:
                    self.needs_jet = True
                    return unit(dx, dy)
                if self.walkable(*target):
                    return unit(dx, dy)
                return (0.0, 0.0)
            return unit(waypoint[0]-x, waypoint[1]-y)
        return unit(dx, dy)

    def cover(self, pos, enemies, center, radius, retreat=False):
        """Prefer nearby positions that break incoming fire within the safe disk."""
        x, y = pos[:2]
        start = self._nearest(pos)
        if start is None:
            return None
        cid = self.component[start]
        threats = []
        for enemy in enemies:
            if isinstance(enemy, dict):
                ex, ey, ez = enemy["pos"]
                muzzle = (self.drone_height * 0.8 if enemy.get("kind") == "npc" else
                          self.muzzle * (1 + self.size_per_cube * enemy.get("cubes", 0)))
            else:
                ex, ey, ez = enemy
                muzzle = self.muzzle
            threats.append((ex, ey, ez + muzzle))
        threats.sort(key=lambda e: math.dist(e[:2], (x,y)))
        threats = [e for e in threats[:3] if math.hypot(e[0]-x, e[1]-y) <= 320]
        cx, cy = center[:2]
        radius = max(0.0, radius)
        candidates = {start}
        # Include tiny final circles even if none of the polar samples hit them.
        pivot = self._nearest(center, cid) if math.hypot(cx-x, cy-y) <= 80 else None
        if pivot is not None:
            candidates.add(pivot)
        for length in (9, 18, 32, 49):
            for k in range(12):
                angle = (k + (length % 3)*0.2) * math.tau / 12
                px, py = x+length*math.cos(angle), y+length*math.sin(angle)
                ix = round((px-self.ox)/self.gap)
                iy = round((py-self.oy)/self.gap)
                if 0 <= ix < self.width and 0 <= iy < self.rows:
                    n = iy*self.width+ix
                    if self.component[n] == cid:
                        candidates.add(n)
        best, best_score = None, -float("inf")
        for n in candidates:
            px, py = self.points[n]
            if not self.walkable(px, py):
                continue
            radial = math.hypot(px-cx, py-cy)
            if radial > radius:
                continue
            travel = math.hypot(px-x, py-y)
            # A distant ring-center sample is only a fallback.
            if travel > 80:
                continue
            z = self.ground(px,py)
            wall = self.margin[self._pixel(px,py)] * self.step / 3.0
            score = -0.43*travel - 0.35*min(30, wall) - max(0, 5-radius+radial)
            cover_value = 0
            for i, e in enumerate(threats):
                ex,ey,ez = e
                if not self._clear_sampled((ex,ey,ez), (px,py,z+self.height*0.88), self.inv*0.42):
                    cover_value += (64, 38, 22)[i]
                if retreat:
                    score += (0.4 / (i+1)) * min(100.0, math.hypot(px-ex, py-ey))
            if threats and not cover_value:
                continue
            score += cover_value
            if retreat and self._ground_line(pos, (px,py)):
                score += 7
            if score > best_score:
                best, best_score = (px,py), score
        return best

    def short_cover(self, pos, enemies, center, radius, max_d=14.0):
        """Fallback close-range cover search <= max_d when cover() picked a farther point."""
        x, y = pos[:2]
        start = self._nearest(pos)
        if start is None or not enemies:
            return None
        cid = self.component[start]
        threats = []
        for enemy in enemies:
            ex, ey, ez = enemy["pos"]
            muzzle = self.muzzle * (1 + self.size_per_cube * enemy.get("cubes", 0))
            threats.append((ex, ey, ez + muzzle))
        threats.sort(key=lambda e: math.dist(e[:2], (x, y)))
        threats = threats[:3]
        cx, cy = center[:2]
        candidates = set()
        for length in (4.5, 8.5, 13.0, 17.0):
            if length > max_d + 1.0:
                continue
            for k in range(12):
                angle = (k + (int(length) % 3) * 0.2) * math.tau / 12
                px, py = x + length * math.cos(angle), y + length * math.sin(angle)
                ix = round((px - self.ox) / self.gap)
                iy = round((py - self.oy) / self.gap)
                if 0 <= ix < self.width and 0 <= iy < self.rows:
                    n = iy * self.width + ix
                    if self.component[n] == cid:
                        candidates.add(n)
        best, best_score = None, -float("inf")
        for n in candidates:
            px, py = self.points[n]
            if not self.walkable(px, py) or math.hypot(px - cx, py - cy) > radius:
                continue
            travel = math.hypot(px - x, py - y)
            if travel > max_d:
                continue
            z = self.ground(px, py)
            cv = 0
            for i, (ex, ey, ez) in enumerate(threats):
                if not self._clear_sampled((ex, ey, ez), (px, py, z + self.height * 0.88), self.inv * 0.42):
                    cv += (64, 38, 22)[i]
            if not cv:
                continue
            sc = cv - 0.65 * travel + (10.0 if self._ground_line(pos, (px, py)) else 0.0)
            if sc > best_score:
                best, best_score = (px, py), sc
        return best
