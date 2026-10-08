"""Worker-local episode slots and external policy API quotas.

A quota instance can be explicitly shared by schedulers on one event loop.
It is not a distributed provider-account quota and owns no sandbox resources.
"""
from __future__ import annotations

import asyncio
from collections import deque
import time
from types import MappingProxyType

from dsec.contracts.resources import APILimits


class EpisodeQuota:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("Invalid episode quota capacity")
        self.capacity = capacity
        self._leases = {}

    @property
    def leases(self):
        return MappingProxyType(self._leases)

    @property
    def reserved(self):
        return sum(self._leases.values())

    def available(self, slots):
        return self.reserved + slots <= self.capacity

    def reserve(self, job_id, slots):
        if not self.available(slots):
            raise ValueError("Episode quota unavailable")
        self.restore(job_id, slots)

    def restore(self, job_id, slots):
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValueError("Invalid episode slots")
        if job_id in self._leases:
            raise ValueError("Duplicate episode lease ID")
        self._leases[job_id] = slots

    def release(self, job_id):
        return self._leases.pop(job_id)


class APIQuota:
    def __init__(self, limits: APILimits):
        if not isinstance(limits, APILimits):
            raise TypeError("API quota requires APILimits")
        self.limits = limits
        self.condition = asyncio.Condition()
        self.window = deque()
        self.calls = self.tokens = self.inflight = self.errors = self.rate_limited = 0
        self.pause_until = 0.0

    def _prune(self, now):
        while self.window and now - self.window[0]["time"] >= 60:
            self.window.popleft()

    async def _acquire(self, blocked_seconds, sample_interval):
        async with self.condition:
            while True:
                now = time.monotonic()
                self._prune(now)
                reserve = max(self.limits.token_reserve,
                              max((item["tokens"] for item in self.window), default=0))
                tokens = sum(item["tokens"] for item in self.window)
                blockers = []
                if self.inflight >= self.limits.inflight:
                    blockers.append("api_inflight")
                if (now < self.pause_until or len(self.window) >= self.limits.rpm or
                        tokens + reserve > self.limits.tpm):
                    blockers.append("api_rate")
                if not blockers:
                    # Reserve rate and concurrency together immediately before
                    # calling the provider. A queued/cancelled request consumes
                    # neither; an attempted request keeps its rate reservation
                    # even if its response is lost or cancelled.
                    entry = {"time": now, "tokens": reserve}
                    self.window.append(entry)
                    self.inflight += 1
                    return entry
                waited_at = time.monotonic()
                try:
                    await asyncio.wait_for(self.condition.wait(), sample_interval)
                except asyncio.TimeoutError:
                    pass
                finally:
                    elapsed = time.monotonic() - waited_at
                    for reason in blockers:
                        blocked_seconds[reason] += elapsed

    async def call(self, policy, model, messages, request_kwargs, *, blocked_seconds,
                   sample_interval=1.0):
        entry = await self._acquire(blocked_seconds, sample_interval)
        try:
            completion = await policy.chat.completions.create(
                model=model, messages=messages, extra_body=request_kwargs)
            actual = getattr(getattr(completion, "usage", None), "total_tokens", None)
            if isinstance(actual, int) and actual >= 0:
                entry["tokens"] = actual
            self.calls += 1
            self.tokens += entry["tokens"]
            return completion, entry["tokens"]
        except Exception as exc:
            self.errors += 1
            if getattr(exc, "status_code", None) == 429:
                self.rate_limited += 1
                headers = getattr(getattr(exc, "response", None), "headers", {})
                try:
                    retry_after = float(headers.get("retry-after", 30))
                except (TypeError, ValueError):
                    retry_after = 30
                self.pause_until = max(self.pause_until,
                                       time.monotonic() + max(1, min(120, retry_after)))
            raise
        finally:
            self.inflight -= 1
            async with self.condition:
                self.condition.notify_all()

    def report(self):
        return {"api_calls": self.calls, "api_tokens": self.tokens,
                "api_errors": self.errors, "api_429": self.rate_limited,
                "api_cooldown_remaining_seconds": max(0.0, self.pause_until-time.monotonic())}
