"""Temporary in-process rate limiting.

This module is intentionally small for the V1 hardening slice, but it is
process-local only: counters are not shared across Gunicorn workers and are
lost on restart. Before ScanStory runs horizontally scaled production traffic,
replace this with the same small API backed by Redis or another shared store.
"""
import threading
import time
from collections import defaultdict, deque


class InMemoryRateLimiter:
    """Tiny fixed-window limiter for local process protection.

    It is intentionally dependency-free for this hardening slice. The API is
    shaped so a Redis-backed store can replace the in-memory deques later.
    """

    def __init__(self, clock=None, max_keys=10000):
        self._clock = clock or time.monotonic
        self._max_keys = max_keys
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._events.clear()

    def check(self, key, limit, window_seconds):
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            self._prune(cutoff)
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now))
                return False, retry_after
            bucket.append(now)
            self._enforce_key_bound()
            return True, 0

    def _prune(self, cutoff):
        for key, bucket in list(self._events.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                self._events.pop(key, None)

    def _enforce_key_bound(self):
        if len(self._events) <= self._max_keys:
            return
        oldest = sorted(
            self._events,
            key=lambda item: self._events[item][0] if self._events[item] else 0,
        )
        for key in oldest[:len(self._events) - self._max_keys]:
            self._events.pop(key, None)


limiter = InMemoryRateLimiter()
