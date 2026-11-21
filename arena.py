"""Fast runtime campus geometry, tree canopy concealment, and ground/roof navigation."""
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
    """Rebuilds exact 1 m height/LOS + fast 2 m clearance/canopy + 6 m nav graph from /arena + hello.config."""

    def __init__(self, hello):
        directory = Path(hello["map"]["dir"])
        files = hello["map"].get("files", [])
        world_file = next((f for f in files if f.endswith("world.json")), "v2/world.json")
        with (directory / world_file).open() as f:
            world = json.load(f)
        meta = world["terrain"]
        self.ox, self.oy = float(meta["minX"]), float(meta["minY"])
        self.step, self.nx, self.ny = float(meta["step"]), int(meta["nx"]), int(meta["ny"])
        self.inv = 1.0 / self.step
        self.scale = float(meta.get("scale", 0.01))
        self.heights = array("h")
        with (directory / world_file).with_name(meta.get("file", "terrain.i16")).open("rb") as f:
            self.heights.fromfile(f, self.nx * self.ny)
        if sys.byteorder != "little":
            self.heights.byteswap()

        mech = hello["config"]["mech"]
        self.body = float(mech["radius"])
        self.height = float(mech["height"])
        self.base_height = self.height
        self.max_step = float(mech["step_up"])
        self.slope = math.tan(math.radians(float(mech["max_slope_deg"])))
        self.bounds = world["bounds"]
        cube_rules = hello["config"].get("cubes", {})
        self.size_per_cube = float(cube_rules.get("size_per_cube", 0.05))
        self.muzzle = float(mech.get("muzzle_height", self.height * 0.78))
        self.eye = float(mech.get("eye_height", self.height * 0.89))
        self.drone_height = float(hello["config"].get("npc", {}).get("height", 3.5))

        rules = world.get("rules", {})
        self.canopy_density = float(rules.get("canopyDensity", 0.35))
        self.canopy_block_t = float(rules.get("canopyBlockT", 0.25))
        # Beer-Lambert threshold length in metres through foliage to block >45m vision
        self.canopy_block_len = -math.log(max(1e-4, self.canopy_block_t)) / max(1e-4, self.canopy_density)

        # 1 m exact solid raster for LOS, shots, roofs, and ground height
        count = self.nx * self.ny
        self.floor = array("h", [32767]) * count
        self.ceiling = array("h", [-32768]) * count
        self.bridge_base = array("h", [32767]) * count
        self.bridge_top = array("h", [-32768]) * count
        for prism in world.get("prisms", []):
            self._rasterize_prism(prism)

        # 2 m grid for fast clearances & tree canopy concealment
        self.c_step = 2.0 * self.step
        self.c_inv = 1.0 / self.c_step
        self.nx2 = (self.nx + 1) // 2
        self.ny2 = (self.ny + 1) // 2
        self.canopy = bytearray(self.nx2 * self.ny2)
        self._rasterize_trees(world.get("trees"))

        self.base_margin = self._clearances2(self.height)
        self.giant_margin = self._clearances2(1e6)
        self.margin = self.base_margin
        # 2 m chamfer: 3 axial = 2 m, 4 diagonal = 2.83 m. Add 0.6 m raster safety margin.
        self.min_margin = max(1, math.ceil((self.body + 0.6) * self.c_inv * 3))

        # 6 m navigation graph (stride 3 on the 2 m grid)
        self.g_stride = 3
        self.gap = self.g_stride * self.c_step
        self.width = (self.nx2 - 1) // self.g_stride + 1
        self.rows = (self.ny2 - 1) // self.g_stride + 1
        self._build_graph()

        self.route = []
        self.last_target = None
        self.route_time = -100.0
        self.needs_jet = False

    def _rasterize_prism(self, prism):
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
                    lo_j = max(0, math.ceil(min(ay, by)))
                    hi_j = min(ny, math.ceil(max(ay, by)))
                    for j in range(lo_j, hi_j):
                        rows.setdefault(j, []).append(ax + (j - ay) * slope)
                ax, ay = bx, by
        if prism.get("kind") == "bridge":
            base_field, top_field = self.bridge_base, self.bridge_top
        else:
            base_field, top_field = self.floor, self.ceiling
        base = max(-32768, min(32767, round(100.0 * prism["base"])))
        top = max(-32768, min(32767, round(100.0 * prism["top"])))
        for j, xs in rows.items():
            xs.sort()
            offset = j * nx
            for lo, hi in zip(xs[::2], xs[1::2]):
                p0 = offset + max(0, math.ceil(lo))
                p1 = offset + min(nx, math.ceil(hi))
                for p in range(p0, p1):
                    if top > top_field[p]:
                        top_field[p] = top
                    if base < base_field[p]:
                        base_field[p] = base

    def _rasterize_trees(self, trees):
        if not trees or "fields" not in trees or "data" not in trees:
            return
        fields = trees["fields"]
        data = trees["data"]
        nf = len(fields)
        if nf == 0:
            return
        try:
            ix_x = fields.index("x")
            ix_y = fields.index("y")
            ix_h = fields.index("height")
            ix_cr = fields.index("crownR")
            ix_cb = fields.index("crownBase")
            ix_cx = fields.index("crownX") if "crownX" in fields else ix_x
            ix_cy = fields.index("crownY") if "crownY" in fields else ix_y
        except ValueError:
            return
        ox, oy, c_inv = self.ox, self.oy, self.c_inv
        nx2, ny2 = self.nx2, self.ny2
        canopy = self.canopy
        for base in range(0, len(data) - nf + 1, nf):
            cb = data[base + ix_cb]
            h = data[base + ix_h]
            # Only crowns overlapping mech torso/eye/top height (roughly 2 m .. 14 m above ground)
            if cb > 12.5 or h < 3.5:
                continue
            cr = data[base + ix_cr]
            if cr < 1.0:
                continue
            cx = data[base + ix_cx]
            cy = data[base + ix_cy]
            gx = (cx - ox) * c_inv
            gy = (cy - oy) * c_inv
            rad = cr * c_inv
            r2 = rad * rad
            i0 = max(0, int(math.floor(gx - rad)))
            i1 = min(nx2 - 1, int(math.ceil(gx + rad)))
            j0 = max(0, int(math.floor(gy - rad)))
            j1 = min(ny2 - 1, int(math.ceil(gy + rad)))
            for j in range(j0, j1 + 1):
                dy = j - gy
                dy2 = dy * dy
                row_off = j * nx2
                for i in range(i0, i1 + 1):
                    dx = i - gx
                    if dx * dx + dy2 <= r2:
                        idx = row_off + i
                        v = canopy[idx]
                        if v < 250:
                            canopy[idx] = v + 1

    def _clearances2(self, height):
        nx, ny = self.nx, self.ny
        nx2, ny2 = self.nx2, self.ny2
        b = self.bounds
        left = max(1, math.ceil((b["minX"] - self.ox) * self.c_inv))
        right = min(nx2 - 2, math.floor((b["maxX"] - self.ox) * self.c_inv))
        bottom = max(1, math.ceil((b["minY"] - self.oy) * self.c_inv))
        top = min(ny2 - 2, math.floor((b["maxY"] - self.oy) * self.c_inv))
        margin = bytearray(nx2 * ny2)
        heights = self.heights
        floor = self.floor
        ceiling = self.ceiling
        b_base = self.bridge_base
        b_top = self.bridge_top
        ground_scale = 100.0 * self.scale
        head = 100.0 * height
        step = 100.0 * self.max_step
        steep2 = 4.0 * (self.slope * self.step / self.scale) ** 2

        for j2 in range(bottom, top + 1):
            j1a = j2 * 2
            j1b = min(ny - 2, j1a + 1)
            row2 = j2 * nx2
            for i2 in range(left, right + 1):
                i1a = i2 * 2
                i1b = min(nx - 2, i1a + 1)
                blocked = False
                for jj in (j1a, j1b):
                    off = jj * nx
                    for ii in (i1a, i1b):
                        p = off + ii
                        z = heights[p] * ground_scale
                        if ceiling[p] > z + step and floor[p] < z + head:
                            blocked = True
                            break
                        if b_top[p] > z + step and b_base[p] < z + head:
                            blocked = True
                            break
                        if ii > 0 and jj > 0:
                            dx = heights[p - 1] - heights[p + 1]
                            dy = heights[p - nx] - heights[p + nx]
                            if dx * dx + dy * dy > steep2:
                                blocked = True
                                break
                    if blocked:
                        break
                if not blocked:
                    q = row2 + i2
                    m = margin[q - 1] + 3
                    v = margin[q - nx2] + 3
                    if v < m:
                        m = v
                    v = margin[q - nx2 - 1] + 4
                    if v < m:
                        m = v
                    v = margin[q - nx2 + 1] + 4
                    if v < m:
                        m = v
                    margin[q] = 240 if m > 240 else m

        for j2 in range(top, bottom - 1, -1):
            row2 = j2 * nx2
            for q in range(row2 + right, row2 + left - 1, -1):
                m = margin[q]
                if m > 3:
                    v = margin[q + 1] + 3
                    if v < m:
                        m = v
                    v = margin[q + nx2] + 3
                    if v < m:
                        m = v
                    v = margin[q + nx2 - 1] + 4
                    if v < m:
                        m = v
                    v = margin[q + nx2 + 1] + 4
                    if v < m:
                        m = v
                    margin[q] = m
        return margin

    def _pixel(self, x, y):
        i = int((x - self.ox) * self.inv + 0.5)
        j = int((y - self.oy) * self.inv + 0.5)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return None

    def _pixel2(self, x, y):
        i = int((x - self.ox) * self.c_inv)
        j = int((y - self.oy) * self.c_inv)
        if 0 <= i < self.nx2 and 0 <= j < self.ny2:
            return j * self.nx2 + i
        return None

    def ground(self, x, y):
        gx = min(self.nx - 1.001, max(0.0, (x - self.ox) * self.inv))
        gy = min(self.ny - 1.001, max(0.0, (y - self.oy) * self.inv))
        ix, iy = int(gx), int(gy)
        fx, fy = gx - ix, gy - iy
        p = iy * self.nx + ix
        h = self.heights
        return self.scale * ((1.0 - fy) * ((1.0 - fx) * h[p] + fx * h[p + 1]) +
                             fy * ((1.0 - fx) * h[p + self.nx] + fx * h[p + self.nx + 1]))

    def roof(self, x, y):
        p = self._pixel(x, y)
        z = self.ground(x, y)
        return z if p is None else max(z, 0.01 * max(self.ceiling[p], self.bridge_top[p]))

    def canopy_at(self, x, y):
        q = self._pixel2(x, y)
        return 0 if q is None else self.canopy[q]

    def ground_clearance(self, x, y):
        q = self._pixel2(x, y)
        return 0.0 if q is None else self.margin[q] * self.c_step / 3.0

    def walkable(self, x, y):
        q = self._pixel2(x, y)
        return q is not None and self.margin[q] >= self.min_margin

    def clear(self, a, b):
        """Shot LOS against terrain, buildings, stadium, bridges (trees do NOT block shots)."""
        return self._clear_sampled(a, b, self.inv)

    def _clear_sampled(self, a, b, samples_per_metre):
        x, y, z = a
        dx, dy, dz = b[0] - x, b[1] - y, b[2] - z
        steps = max(1, math.ceil(max(abs(dx), abs(dy), abs(dz)) * samples_per_metre))
        gx = (x - self.ox) * self.inv + 0.5
        gy = (y - self.oy) * self.inv + 0.5
        sx, sy, sz = dx * self.inv / steps, dy * self.inv / steps, dz * 100.0 / steps
        hz = z * 100.0
        nx, ny = self.nx, self.ny
        ground_scale = 100.0 * self.scale
        heights = self.heights
        floor = self.floor
        ceiling = self.ceiling
        b_base = self.bridge_base
        b_top = self.bridge_top
        for _ in range(steps):
            i, j = int(gx), int(gy)
            if not (0 <= i < nx and 0 <= j < ny):
                return False
            p = j * nx + i
            if heights[p] * ground_scale > hz + 5.0:
                return False
            if floor[p] <= hz <= ceiling[p] or b_base[p] <= hz <= b_top[p]:
                return False
            gx += sx
            gy += sy
            hz += sz
        return True

    def canopy_blocks_ray(self, a, b, near_range=45.0):
        """Fast check whether tree canopy alone blocks visual LOS between a and b (> near_range)."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        dist = math.hypot(dx, dy)
        if dist <= near_range:
            return False
        steps = max(2, int(dist * 0.4))
        step_m = dist / steps
        ox, oy, c_inv = self.ox, self.oy, self.c_inv
        nx2, ny2 = self.nx2, self.ny2
        canopy = self.canopy
        foliage_m = 0.0
        x, y = a[0], a[1]
        sx, sy = dx / steps, dy / steps
        thresh = self.canopy_block_len
        for _ in range(steps):
            i = int((x - ox) * c_inv)
            j = int((y - oy) * c_inv)
            if 0 <= i < nx2 and 0 <= j < ny2 and canopy[j * nx2 + i]:
                foliage_m += step_m
                if foliage_m >= thresh:
                    return True
            x += sx
            y += sy
        return False

    def vision_clear(self, a, b, near_range=45.0):
        """Approximate visual LOS including tree canopy blockage beyond near_range."""
        if not self._clear_sampled(a, b, 0.5):
            return False
        return not self.canopy_blocks_ray(a, b, near_range)

    def set_body(self, stats):
        radius = float(stats.get("radius", self.body))
        height = float(stats.get("height", self.height))
        if abs(radius - self.body) < 1e-4 and abs(height - self.height) < 1e-4:
            return
        self.body, self.height = radius, height
        self.min_margin = max(1, math.ceil((radius + 0.6) * self.c_inv * 3))
        self.margin = self.giant_margin if height > self.base_height + 0.01 else self.base_margin
        self.route.clear()

    def _ground_line(self, a, b):
        return self._line_margin2(a, b, self.margin) >= self.min_margin

    def _line_margin2(self, a, b, margin):
        gx = (a[0] - self.ox) * self.c_inv
        gy = (a[1] - self.oy) * self.c_inv
        hx = (b[0] - self.ox) * self.c_inv
        hy = (b[1] - self.oy) * self.c_inv
        ix, iy, ex, ey = math.floor(gx), math.floor(gy), math.floor(hx), math.floor(hy)
        nx2, ny2 = self.nx2, self.ny2
        if not (0 <= ix < nx2 and 0 <= iy < ny2 and 0 <= ex < nx2 and 0 <= ey < ny2):
            return 0
        dx, dy = hx - gx, hy - gy
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        tx = 1.0 / abs(dx) if dx else math.inf
        ty = 1.0 / abs(dy) if dy else math.inf
        next_x = ((ix + 1 - gx) if dx >= 0 else (gx - ix)) * tx if dx else math.inf
        next_y = ((iy + 1 - gy) if dy >= 0 else (gy - iy)) * ty if dy else math.inf
        p = iy * nx2 + ix
        clearance = margin[p]
        while ix != ex or iy != ey:
            if ix != ex and iy != ey and abs(next_x - next_y) < 1e-10:
                c1, c2 = margin[p + sx], margin[p + sy * nx2]
                if c1 < clearance:
                    clearance = c1
                if c2 < clearance:
                    clearance = c2
                ix += sx
                iy += sy
                p += sx + sy * nx2
                next_x += tx
                next_y += ty
            elif (next_x < next_y and ix != ex) or iy == ey:
                ix += sx
                p += sx
                next_x += tx
            else:
                iy += sy
                p += sy * nx2
                next_y += ty
            if margin[p] < clearance:
                clearance = margin[p]
                if not clearance:
                    break
        return clearance

    def _elevated_line(self, pos, target, look=12.0):
        dx, dy = target[0] - pos[0], target[1] - pos[1]
        length = min(math.hypot(dx, dy), look)
        ux, uy = unit(dx, dy)
        side = self.body * 0.86
        steps = max(1, math.ceil(length / 2.0))
        z100 = pos[2] * 100.0
        for k in range(1, steps + 1):
            x = pos[0] + ux * length * k / steps
            y = pos[1] + uy * length * k / steps
            for offset in (0.0, -side, side):
                px, py = x - uy * offset, y + ux * offset
                i = self._pixel(px, py)
                if i is None:
                    return False
                if (self.heights[i] * self.scale > pos[2] + self.max_step
                    or (self.floor[i] < z100 + self.height * 100.0 and self.ceiling[i] > z100 + self.max_step * 100.0)
                    or (self.bridge_base[i] < z100 + self.height * 100.0 and self.bridge_top[i] > z100 + self.max_step * 100.0)):
                    return False
        return True

    def open_step(self, pos, direction, meters=7.0):
        ux, uy = unit(direction[0], direction[1])
        goal = (pos[0] + ux * meters, pos[1] + uy * meters)
        if len(pos) > 2 and pos[2] > self.ground(pos[0], pos[1]) + 2.0:
            return self._elevated_line(pos, goal, meters)
        return self._ground_line(pos, goal)

    def _build_graph(self):
        w, rows, stride = self.width, self.rows, self.g_stride
        nx2 = self.nx2
        count = w * rows
        graph = [[] for _ in range(count)]
        free = bytearray(count)
        ox, oy, gap = self.ox, self.oy, self.gap
        # Cell center on 2m grid
        self.points = [(ox + (i % w) * gap + 1.0, oy + (i // w) * gap + 1.0) for i in range(count)]
        base_m = self.base_margin
        giant_m = self.giant_margin
        min_m = self.min_margin
        for j in range(rows):
            j2 = j * stride
            row_off2 = j2 * nx2
            row_g = j * w
            for i in range(w):
                n = row_g + i
                if base_m[row_off2 + i * stride] < min_m:
                    continue
                free[n] = 1
                pn = self.points[n]
                for di, dj in ((-1, 0), (0, -1), (-1, -1), (1, -1)):
                    ni, nj = i + di, j + dj
                    if not (0 <= ni < w and nj >= 0):
                        continue
                    other = nj * w + ni
                    if not free[other]:
                        continue
                    po = self.points[other]
                    c_base = self._line_margin2(pn, po, base_m)
                    if c_base >= min_m:
                        c_giant = self._line_margin2(pn, po, giant_m)
                        cost = 14 if di and dj else 10
                        graph[n].append((other, cost, c_giant))
                        graph[other].append((n, cost, c_giant))
        self.graph = graph
        self.component = array("i", [-1]) * count
        largest = []
        comp = 0
        for n in range(count):
            if not free[n] or self.component[n] != -1:
                continue
            nodes = [n]
            self.component[n] = comp
            for node in nodes:
                for other, _, _ in graph[node]:
                    if self.component[other] == -1:
                        self.component[other] = comp
                        nodes.append(other)
            if len(nodes) > len(largest):
                largest = nodes
            comp += 1
        self.main_component = self.component[largest[0]] if largest else -1

        self.landmarks = []
        if largest:
            anchor = largest[0]
            nearest = array("i", [100000000]) * count
            for _ in range(4):
                distances = array("i", [100000000]) * count
                distances[anchor] = 0
                queue = [(0, anchor)]
                while queue:
                    cost, node = heappop(queue)
                    if distances[node] != cost:
                        continue
                    for other, edge, _ in graph[node]:
                        nd = cost + edge
                        if nd < distances[other]:
                            distances[other] = nd
                            heappush(queue, (nd, other))
                self.landmarks.append(distances)
                next_dist = -1
                for node in largest:
                    closest = nearest[node]
                    dn = distances[node]
                    if dn < closest:
                        closest = dn
                        nearest[node] = closest
                    if closest > next_dist:
                        next_dist, anchor = closest, node

    def _nearest(self, point, component=None, require_line=False):
        ix = min(self.width - 1, max(0, round((point[0] - self.ox - 1.0) / self.gap)))
        iy = min(self.rows - 1, max(0, round((point[1] - self.oy - 1.0) / self.gap)))
        n = iy * self.width + ix

        def usable(node):
            cid = self.component[node]
            if cid < 0 or (component is not None and cid != component):
                return False
            xy = self.points[node]
            return self.walkable(xy[0], xy[1]) and (not require_line or self._ground_line(point, xy))

        if usable(n):
            return n
        for radius in range(1, 5 if require_line else 24):
            best = None
            d2best = float("inf")
            for j in range(max(0, iy - radius), min(self.rows, iy + radius + 1)):
                lo, hi = max(0, ix - radius), min(self.width - 1, ix + radius)
                cols = range(lo, hi + 1) if abs(j - iy) == radius else (lo, hi)
                for i in cols:
                    cand = j * self.width + i
                    if not usable(cand):
                        continue
                    xy = self.points[cand]
                    d2 = (xy[0] - point[0]) ** 2 + (xy[1] - point[1]) ** 2
                    if d2 < d2best:
                        best, d2best = cand, d2
            if best is not None:
                return best
        return None

    def _path(self, start, finish):
        if start == finish:
            return [start]
        w = self.width
        gx, gy = finish % w, finish // w
        landmarks = [(f, f[finish]) for f in self.landmarks if f[finish] < 100000000]

        def estimate(n):
            dx, dy = abs(n % w - gx), abs(n // w - gy)
            h = 10 * max(dx, dy) + 4 * min(dx, dy)
            for field, val in landmarks:
                detour = abs(field[n] - val)
                if detour > h:
                    h = detour
            return h

        costs, parent = {start: 0}, {}
        best, best_h = start, estimate(start)
        queue = [(best_h, 0, start)]
        giant = self.margin is self.giant_margin
        min_m = self.min_margin
        for _ in range(1200):
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
            for other, edge, g_width in self.graph[node]:
                if giant and g_width < min_m:
                    continue
                nc = cost + edge
                if nc < costs.get(other, 100000000):
                    costs[other] = nc
                    parent[other] = node
                    heappush(queue, (nc + 1.25 * estimate(other), nc, other))
        path = [best]
        while path[-1] != start:
            path.append(parent[path[-1]])
        return path[::-1]

    def travel_bound(self, pos, goal):
        straight = math.dist(pos[:2], goal[:2])
        start = self._nearest(pos)
        finish = self._nearest(goal)
        if start is None or finish is None or self.component[start] != self.component[finish]:
            return math.inf
        grid = max((abs(f[start] - f[finish]) for f in self.landmarks if f[start] < 100000000), default=0)
        attach = math.dist(pos[:2], self.points[start]) + math.dist(goal[:2], self.points[finish])
        return max(straight, grid * self.gap / 10.0 + attach)

    def steer(self, pos, goal, t):
        x, y = pos[0], pos[1]
        target = (goal[0], goal[1])
        dx, dy = target[0] - x, target[1] - y
        self.needs_jet = False
        dist = math.hypot(dx, dy)
        if dist < 0.25:
            return (0.0, 0.0)
        elevated = len(pos) > 2 and pos[2] > self.ground(x, y) + 2.0
        roof_goal = (len(goal) > 2 and goal[2] > self.ground(target[0], target[1]) + 2.5
                     and abs(self.roof(target[0], target[1]) - goal[2]) < 4.5)
        if elevated and self._elevated_line(pos, target):
            return unit(dx, dy)
        if dist < 85.0 and self._ground_line(pos, target):
            return unit(dx, dy)
        if roof_goal:
            here_z = pos[2] if len(pos) > 2 else self.ground(x, y)
            ux, uy = unit(dx, dy)
            ahead = min(9.0, dist)
            top = self.roof(x + ux * ahead, y + uy * ahead)
            if dist <= 12.0 or (dist <= 48.0 and abs(top - goal[2]) < 3.5):
                self.needs_jet = here_z < goal[2] - self.max_step
                return ux, uy
        if (not self.route or self.last_target is None
            or math.dist(self.last_target, target) > 8.0 or t - self.route_time > 5.0
            or (math.dist(self.points[self.route[0]], (x, y)) > 12.0
                and not self._ground_line(pos, self.points[self.route[0]]))):
            start = self._nearest(pos, require_line=self.walkable(x, y))
            finish = None if start is None else self._nearest(target, self.component[start])
            self.route = [] if finish is None else self._path(start, finish)
            self.last_target = target
            self.route_time = t
        if self.route:
            for k in range(min(6, len(self.route) - 1), 0, -1):
                if self._ground_line(pos, self.points[self.route[k]]):
                    del self.route[:k]
                    break
            waypoint = self.points[self.route[0]]
            if len(self.route) == 1 and math.dist(waypoint, (x, y)) < 3.0:
                self.route.clear()
                if roof_goal:
                    self.needs_jet = True
                    return unit(dx, dy)
                if self.walkable(target[0], target[1]):
                    return unit(dx, dy)
                return (0.0, 0.0)
            return unit(waypoint[0] - x, waypoint[1] - y)
        return unit(dx, dy)

    def occlusion_score(self, px, py, z):
        """Score how well hidden (px, py, z) is from distant sightlines (buildings + tree canopy)."""
        eye_z = z + self.height * 0.85
        wall_hits = 0.0
        canopy_hits = 0
        for k in range(6):
            ang = k * (math.tau / 6.0)
            ca, sa = math.cos(ang), math.sin(ang)
            p18 = (px + 18.0 * ca, py + 18.0 * sa, eye_z)
            if not self._clear_sampled((px, py, eye_z), p18, 0.25):
                wall_hits += 1.0
            else:
                p48 = (px + 48.0 * ca, py + 48.0 * sa, eye_z)
                if not self._clear_sampled((px, py, eye_z), p48, 0.16):
                    wall_hits += 0.55
                elif self.canopy_blocks_ray((px, py, eye_z), (px + 65.0 * ca, py + 65.0 * sa, eye_z), near_range=10.0):
                    canopy_hits += 1
        in_canopy = 1.6 if self.canopy_at(px, py) > 0 else 0.0
        return min(wall_hits, 4.5) * 5.5 + canopy_hits * 2.5 + in_canopy * 3.0

    def hide_spot(self, pos, center, radius, danger_dirs, npcs, rng):
        """Find a high-occlusion hiding spot well inside (center, radius), away from danger & NPCs."""
        x, y = pos[0], pos[1]
        start = self._nearest(pos)
        cid = self.component[start] if start is not None else self.main_component
        cx, cy = center[0], center[1]
        safe_r = max(1.0, radius - min(14.0, 0.14 * radius))
        cands = set()
        if start is not None and math.hypot(self.points[start][0] - cx, self.points[start][1] - cy) <= safe_r:
            cands.add(start)
        for dist in (16.0, 34.0, 62.0):
            for k in range(6):
                ang = k * (math.tau / 6.0) + rng.uniform(-0.25, 0.25)
                px, py = x + dist * math.cos(ang), y + dist * math.sin(ang)
                if math.hypot(px - cx, py - cy) > safe_r:
                    continue
                ix = round((px - self.ox - 1.0) / self.gap)
                iy = round((py - self.oy - 1.0) / self.gap)
                if 0 <= ix < self.width and 0 <= iy < self.rows:
                    n = iy * self.width + ix
                    if self.component[n] == cid:
                        cands.add(n)
        for _ in range(18):
            ang = rng.uniform(-math.pi, math.pi)
            r = safe_r * math.sqrt(rng.uniform(0.05, 0.92))
            px, py = cx + r * math.cos(ang), cy + r * math.sin(ang)
            ix = round((px - self.ox - 1.0) / self.gap)
            iy = round((py - self.oy - 1.0) / self.gap)
            if 0 <= ix < self.width and 0 <= iy < self.rows:
                n = iy * self.width + ix
                if self.component[n] == cid:
                    cands.add(n)
        if not cands:
            piv = self._nearest(center, cid)
            if piv is not None:
                cands.add(piv)
        # Cheap geometric pre-filter before raycasting
        pre = []
        for n in cands:
            px, py = self.points[n]
            if not self.walkable(px, py):
                continue
            radial = math.hypot(px - cx, py - cy)
            if radial > safe_r:
                continue
            if any(math.hypot(px - npc["pos"][0], py - npc["pos"][1]) < 52.0 for npc in npcs):
                continue
            wall_d = self.ground_clearance(px, py)
            travel = math.hypot(px - x, py - y)
            base_s = -0.08 * travel - 0.50 * min(28.0, wall_d) - 4.0 * (radial / max(15.0, safe_r))
            if self.canopy_at(px, py) > 0:
                base_s += 5.0
            for bearing_rad, weight in danger_dirs:
                proj = (px - x) * math.cos(bearing_rad) + (py - y) * math.sin(bearing_rad)
                if proj > 0:
                    base_s -= weight * min(6.0, proj / 25.0)
            pre.append((base_s, px, py))
        pre.sort(key=lambda it: -it[0])
        best, best_score = None, -float("inf")
        for base_s, px, py in pre[:14]:
            z = self.ground(px, py)
            score = base_s + self.occlusion_score(px, py, z)
            if score > best_score:
                best, best_score = (px, py), score
        return best

    def escape_cover(self, pos, enemies, center, radius, keep_moving=False):
        """Find best nearby position that strictly breaks LOS from enemies inside safe disk."""
        x, y = pos[0], pos[1]
        start = self._nearest(pos)
        if start is None:
            return None
        cid = self.component[start]
        threats = []
        for enemy in enemies:
            if isinstance(enemy, dict):
                ex, ey, ez = enemy["pos"]
                muzzle = (self.drone_height * 0.8 if enemy.get("kind") == "npc" else
                          self.muzzle * (1.0 + self.size_per_cube * enemy.get("cubes", 0)))
            else:
                ex, ey, ez = enemy
                muzzle = self.muzzle
            threats.append((ex, ey, ez + muzzle))
        threats.sort(key=lambda e: math.hypot(e[0] - x, e[1] - y))
        threats = [e for e in threats[:3] if math.hypot(e[0] - x, e[1] - y) <= 320.0]
        cx, cy = center[0], center[1]
        radius = max(0.0, radius)
        candidates = set()
        if not keep_moving:
            candidates.add(start)
        piv = self._nearest(center, cid) if math.hypot(cx - x, cy - y) <= 95.0 else None
        if piv is not None:
            candidates.add(piv)
        rings = (18.0, 34.0, 54.0, 74.0) if keep_moving else (10.0, 22.0, 38.0, 58.0)
        for length in rings:
            for k in range(10):
                angle = (k + (int(length) % 3) * 0.25) * (math.tau / 10.0)
                px, py = x + length * math.cos(angle), y + length * math.sin(angle)
                ix = round((px - self.ox - 1.0) / self.gap)
                iy = round((py - self.oy - 1.0) / self.gap)
                if 0 <= ix < self.width and 0 <= iy < self.rows:
                    n = iy * self.width + ix
                    if self.component[n] == cid:
                        candidates.add(n)
        # Pre-rank top 18 candidates before raycasting to guarantee < 2 ms worst-case tick time
        pre = []
        for n in candidates:
            px, py = self.points[n]
            if not self.walkable(px, py):
                continue
            radial = math.hypot(px - cx, py - cy)
            if radial > radius:
                continue
            travel = math.hypot(px - x, py - y)
            if travel > 85.0 or (keep_moving and travel < 12.0):
                continue
            wall = self.ground_clearance(px, py)
            base_s = (-0.25 if keep_moving else -0.45) * travel - 0.42 * min(28.0, wall) - max(0.0, 6.0 - radius + radial)
            if self.canopy_at(px, py) > 0:
                base_s += 6.0
            for i, (ex, ey, _) in enumerate(threats):
                base_s += (0.75 / (i + 1)) * min(150.0, math.hypot(px - ex, py - ey))
            pre.append((base_s, px, py))
        pre.sort(key=lambda it: -it[0])
        best, best_score = None, -float("inf")
        for base_s, px, py in pre[:18]:
            z = self.ground(px, py)
            blocked_all = True
            cover_val = 0.0
            for i, (ex, ey, ez) in enumerate(threats):
                d_en = math.hypot(px - ex, py - ey)
                if not self._clear_sampled((ex, ey, ez), (px, py, z + self.height * 0.90), 0.30):
                    cover_val += (85.0, 52.0, 32.0)[i]
                else:
                    blocked_all = False
                    if d_en > 46.0 and self.canopy_blocks_ray((ex, ey, ez), (px, py, z + self.height * 0.85), near_range=45.0):
                        cover_val += (24.0, 14.0, 8.0)[i]
            if threats and cover_val <= 0.0:
                continue
            score = base_s + cover_val + (35.0 if (threats and blocked_all) else 0.0)
            if self._ground_line(pos, (px, py)):
                score += 12.0
            if score > best_score:
                best, best_score = (px, py), score
        return best
