"""Reconstructable fleet views. Missing health never releases a quota grant."""
from dataclasses import asdict, dataclass
import threading
import math
import time

from dsec.contracts.cluster import NodeView, identifier


@dataclass(frozen=True)
class Observation:
    view: NodeView
    observed_at: float


class Watcher:
    def __init__(self, probes, *, max_age=10, clock=time.monotonic):
        if isinstance(max_age, bool) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError('Positive watcher freshness window required')
        self.probes = dict(probes)
        for node in self.probes:
            identifier(node)
        self.max_age, self.clock = max_age, clock
        self.lock = threading.Lock()
        self.views, self.generation, self.errors = {}, {}, {}
        self.last_views = {}

    def probe(self, node):
        # Begin ordering fences a slow old completion after a newer probe.
        with self.lock:
            generation = self.generation.get(node, 0) + 1
            self.generation[node] = generation
        began = self.clock()
        try:
            view = self.probes[node]()
            if not isinstance(view, NodeView) or view.node_id != node:
                raise ValueError('Probe returned another node or malformed view')
        except Exception as exc:
            with self.lock:
                if self.generation[node] == generation:
                    self.views.pop(node, None)
                    self.errors[node] = type(exc).__name__
            return False
        with self.lock:
            if self.generation[node] != generation or self.clock() - began >= self.max_age:
                return False
            self.views[node] = Observation(view, began)
            self.last_views[node] = self.views[node]
            self.errors.pop(node, None)
        return True

    def poll(self):
        # Independent bounded probe calls may be scheduled by the service.
        return {node:self.probe(node) for node in self.probes}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            return {node:observation.view for node, observation in self.views.items()
                    if now - observation.observed_at < self.max_age}

    def inventory(self):
        """Configured fleet including missing/stale nodes; no placement authority."""
        with self.lock:
            now = self.clock()
            items = []
            for node in sorted(self.probes):
                last = self.last_views.get(node)
                fresh = node in self.views and now - self.views[node].observed_at < self.max_age
                items.append(dict(node_id=node,
                    state='HEALTHY' if fresh else ('UNREACHABLE' if node in self.errors else
                        ('STALE' if last else 'UNOBSERVED')),
                    schedulable=fresh, valid_for_seconds=max(0, self.max_age-(now-last.observed_at)) if fresh else 0,
                    observation_age_seconds=max(0, now-last.observed_at) if last else None,
                    error=self.errors.get(node), last_view=asdict(last.view) if last else None))
            return items
