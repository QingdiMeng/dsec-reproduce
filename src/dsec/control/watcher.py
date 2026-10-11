"""Reconstructable fleet views. Missing health never releases a quota grant."""
from dataclasses import dataclass
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
