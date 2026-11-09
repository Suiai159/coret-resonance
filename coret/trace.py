"""GUI-owned compact raw arrays with incremental extrema cache; no receiver locks."""
from array import array
from bisect import bisect_left, bisect_right


def reduce_points(points, budget):
    if len(points) <= budget:
        return points
    width = max(1, (len(points) + max(1, budget // 2) - 1) // max(1, budget // 2))
    out = [points[0]]
    for start in range(0, len(points), width):
        block = points[start:start + width]
        out.extend(sorted(set((min(block, key=lambda p:p[1]), max(block, key=lambda p:p[1])))))
    out.append(points[-1])
    return out


class Trace:
    WIDTH = 256

    def __init__(self):
        self.indices = array('I')
        self.values = array('H')
        self.buckets = []
        self.contiguous = True
        self.minimum = self.maximum = None
        self.first = self.last = 0

    def append(self, indices, values):
        if len(indices) != len(values):
            raise ValueError('显示数据长度不一致')
        if not indices:
            return
        prev = self.indices[-1] if self.indices else indices[0] - 1
        self.contiguous &= indices[0] == prev + 1 and all(b == a + 1 for a,b in zip(indices, indices[1:]))
        if not self.indices: self.first, self.last = indices[0], indices[0]
        self.indices.extend(indices); self.values.extend(values)
        lo, hi = min(values), max(values)
        self.minimum = lo if self.minimum is None else min(lo, self.minimum)
        self.maximum = hi if self.maximum is None else max(hi, self.maximum)
        self.first, self.last = min(self.first, min(indices)), max(self.last, max(indices))
        # Reuse completed buckets. At most WIDTH-1 points remain unindexed.
        while (len(self.buckets) + 1) * self.WIDTH <= len(self.values):
            start = len(self.buckets) * self.WIDTH
            block = self.values[start:start + self.WIDTH]
            lo, hi = min(block), max(block)
            self.buckets.append(((self.indices[start + block.index(lo)], lo),
                                 (self.indices[start + block.index(hi)], hi)))

    def points(self, left, right, budget):
        if not self.indices:
            return []
        a = max(0, bisect_left(self.indices, left) - 1)
        b = min(len(self.indices), bisect_right(self.indices, right) + 1)
        if a >= b:
            return []
        if b - a <= budget * 2:
            return reduce_points(list(zip(self.indices[a:b], self.values[a:b])), budget)
        start = min(b, ((a + self.WIDTH - 1) // self.WIDTH) * self.WIDTH)
        end = max(start, min(b // self.WIDTH, len(self.buckets)) * self.WIDTH)
        points = list(zip(self.indices[a:start], self.values[a:start]))
        for bucket in self.buckets[start // self.WIDTH:end // self.WIDTH]:
            points.extend(sorted(set(bucket)))
        points.extend(zip(self.indices[end:b], self.values[end:b]))
        # Preserve clipped boundary points as well as all extrema.
        points = [(self.indices[a], self.values[a])] + points + [(self.indices[b-1], self.values[b-1])]
        return reduce_points(points, budget)
