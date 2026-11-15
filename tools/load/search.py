"""Bounded adaptive search; capacity still uses measure.capacity's sorted prefix."""
from __future__ import annotations


class Search:
    def __init__(self, target=20, ceiling=40, step=10, resolution=5, live_cap=None):
        self.ceiling = min(ceiling, live_cap) if live_cap is not None else ceiling
        if not 1 < target <= self.ceiling or step < 1 or resolution < 1:
            raise ValueError('search target must be inside the hard/live ceiling; positive step/resolution required')
        self.target, self.step, self.resolution = target, step, resolution
        self.low, self.high, self.retry = None, None, set()
        self.path, self.finished = [], False

    def observe(self, n, verdict):
        if self.finished or not 1 <= n <= self.ceiling:
            raise ValueError('search point outside bounds or search already finished')
        if n == 1 and verdict != 'PASS':
            next_n, reason = None, 'baseline is not PASS; stop'
        elif verdict == 'INVALID':
            if n in self.retry:
                next_n, reason = None, 'INVALID twice; abort without bisection'
            else:
                self.retry.add(n)
                next_n, reason = n, 'INVALID; retry once without changing bounds'
        else:
            if verdict == 'PASS':
                self.low = max(self.low or 1, n)
            elif verdict == 'FAIL':
                self.high = min(self.high or n, n)
            else:
                raise ValueError('unknown verdict')
            if n == 1:
                next_n, reason = self.target, 'valid baseline; test target'
            elif self.high is not None:
                if self.high - self.low <= self.resolution:
                    next_n, reason = None, 'bracket reached participant resolution'
                else:
                    mid = ((self.low + self.high) // 2 // self.resolution) * self.resolution
                    next_n = mid if self.low < mid < self.high else self.low + self.resolution
                    reason = 'bisect passing/failing bracket'
            elif n >= self.ceiling:
                next_n, reason = None, 'all tested points pass; hard/live ceiling reached'
            else:
                next_n, reason = min(self.ceiling, n + self.step), 'PASS; increase by search step'
        if next_n is not None and not 1 <= next_n <= self.ceiling:
            raise ValueError('proposed point exceeds hard/live ceiling')
        self.path.append({'n': n, 'verdict': verdict, 'next_n': next_n, 'reason': reason})
        self.finished = next_n is None
        return next_n

    def untested(self):
        missing = sorted(set(range(1, self.ceiling + 1)) - {p['n'] for p in self.path})
        intervals = []
        for n in missing:
            if intervals and intervals[-1]['through'] == n - 1:
                intervals[-1]['through'] = n
            else:
                intervals.append({'from': n, 'through': n})
        return intervals
