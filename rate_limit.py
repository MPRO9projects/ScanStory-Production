import threading
import time
from collections import defaultdict, deque


class InMemoryRateLimiter:
    """Tiny fixed-window limiter for local process protection.

    It is intentionally dependency-free for this hardening slice. The API is
    shaped so a Redis-backed store can replace the in-memory deques later.
    """

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._events.clear()

    def check(self, key, limit, window_seconds):
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now))
                return False, retry_after
            bucket.append(now)
            return True, 0


limiter = InMemoryRateLimiter()
