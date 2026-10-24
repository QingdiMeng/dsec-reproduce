"""Paper-style filter + power-of-k selection, with a local recent overlay."""
import random
import math
import threading
import time

from dsec.contracts.cluster import NoPlacement, Quota


class PlacementEngine:
    def __init__(self, watcher, *, choices=2, overlay_seconds=10,
                 clock=time.monotonic, sampler=None, membership=None):
        if (type(choices) is not int or choices < 1 or isinstance(overlay_seconds, bool)
                or not math.isfinite(overlay_seconds) or overlay_seconds <= 0):
            raise ValueError('Invalid placement configuration')
        self.watcher, self.choices = watcher, choices
        self.overlay_seconds, self.clock = overlay_seconds, clock
        self.sampler = sampler or random.SystemRandom().sample
        self.lock, self.recent = threading.Lock(), []
        self.membership = membership

    def choose(self, backend, environment, demand: Quota, *, capabilities=(), exclude=()):
        views = self.watcher.snapshot()
        eligible = self.membership.active_nodes() if self.membership is not None else set(views)
        with self.lock:
            now = self.clock()
            self.recent = [(node,stamp) for node,stamp in self.recent
                           if now - stamp < self.overlay_seconds]
            candidates = [view for node,view in views.items()
                if node in eligible and node not in exclude and backend in view.backends
                and environment in view.environments
                and set(capabilities) <= set(view.capabilities)
                and demand.fits(view.available)]
            if not candidates:
                raise NoPlacement('No fresh healthy node meets backend/environment/resources/capabilities')
            sampled = self.sampler(candidates, min(self.choices, len(candidates)))
            def score(view):
                return (view.load + sum(node == view.node_id for node,_ in self.recent),
                        view.active, view.node_id)
            selected = min(sampled, key=score)
            self.recent.append((selected.node_id, now))
            return selected.node_id
