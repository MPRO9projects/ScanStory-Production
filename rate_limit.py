"""Centralized request rate limiting.

One API - ``limiter.check(key, limit, window_seconds) -> (allowed, retry_after)``
- with two interchangeable backends:

* :class:`InMemoryRateLimiter` - process-local. Correct for a single process
  and for deterministic tests, and NOT correct for production: under
  ``gunicorn -w N`` every published limit silently becomes ``N x limit`` and a
  rolling restart clears every counter.
* :class:`RedisRateLimiter` - shared across workers and across restarts. This is
  the production backend, selected by setting ``RATE_LIMIT_REDIS_URL`` (an
  environment variable that was documented but read by no code until now).

Every limited endpoint routes through the single module-level ``limiter`` built
by :func:`build_limiter`, so there is exactly one mechanism and one policy
surface rather than a second parallel limiter per feature.

REDIS-UNAVAILABLE POLICY: **fail closed.** If ``RATE_LIMIT_REDIS_URL`` is set,
the operator has declared that shared limiting is required, and the endpoints
behind this limiter are authentication, OTP mail and abuse-reporting paths where
an unlimited window is a real security exposure. A Redis outage also already
takes ``/ready`` to 503 (RQ needs the same Redis), so the deployment is out of
rotation anyway; allowing unmetered credential spray during that window would
trade a bounded availability problem for an unbounded security one. Failures are
logged and return a short ``Retry-After`` so recovery is immediate.
"""
import hashlib
import logging
import os
import threading
import time
from collections import defaultdict, deque


logger = logging.getLogger(__name__)

# Short Retry-After used when the shared backend is unreachable (fail closed).
FAIL_CLOSED_RETRY_AFTER = 5

# Every limiter call sits in front of a user-visible request (login, OTP mail,
# report submission), so its Redis socket MUST be bounded. redis-py defaults
# socket_timeout to None, and an unreachable-but-not-refusing Redis (firewall
# DROP, hung server) then blocks the request thread forever - a fail-closed
# policy that never returns is not fail-closed, it is a hang. With the bound,
# the outage lands in check()'s except branch and answers 429 immediately.
# Deliberately duplicated rather than imported from processing_queue: this
# module is dependency-free by design and is imported before the queue layer.
REDIS_SOCKET_TIMEOUT_DEFAULT = 5


def identity_digest(value):
    """Stable short digest for an identifier used inside a rate-limit key.

    Keys reach Redis and logs, so identifiers (email addresses) are hashed
    rather than embedded. Secrets - OTP codes, passwords, tokens, signatures -
    must never be passed to this function or into a key at all; the value here
    is only ever a non-secret identity.
    """
    if not value:
        return "-"
    return hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()[:32]


class InMemoryRateLimiter:
    """Tiny sliding-window limiter for local process protection.

    Dependency-free and deterministic, which is what the test suite needs. Not
    shared across processes - see the module docstring.
    """

    backend = "memory"
    shared = False

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


class RedisRateLimiter:
    """Fixed-window limiter backed by Redis, shared by every worker process.

    The counter lives in Redis, so two Gunicorn workers - or the same worker
    after a restart - observe one budget rather than one each. Namespacing keeps
    one endpoint's limit from bleeding into another's.
    """

    backend = "redis"
    shared = True

    def __init__(self, client, namespace="scanstory:rl", fail_closed=True):
        self._client = client
        self._namespace = namespace
        self._fail_closed = fail_closed

    def clear(self):
        # Never flush a shared datastore from application code.
        return None

    def _key(self, key):
        return f"{self._namespace}:{key}"

    def check(self, key, limit, window_seconds):
        redis_key = self._key(key)
        try:
            pipe = self._client.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            count, ttl = pipe.execute()
            count = int(count)
            ttl = int(ttl)
            if ttl < 0:
                # First hit in this window (or a key with no expiry): start the
                # window now. Not refreshed on later hits, so sustained abuse
                # cannot extend its own ban indefinitely.
                self._client.expire(redis_key, int(window_seconds))
                ttl = int(window_seconds)
            if count > limit:
                return False, max(1, ttl)
            return True, 0
        except Exception as exc:
            logger.error(
                "rate_limit_backend_unavailable backend=redis error=%s", type(exc).__name__
            )
            if self._fail_closed:
                return False, FAIL_CLOSED_RETRY_AFTER
            return True, 0


def build_limiter(redis_url=None, client=None, fail_closed=True):
    """Return the shared limiter for this runtime.

    Redis when ``RATE_LIMIT_REDIS_URL`` is configured (production), otherwise the
    deterministic in-memory limiter (local development and the test suite).
    """
    if client is not None:
        return RedisRateLimiter(client, fail_closed=fail_closed)
    url = redis_url if redis_url is not None else os.environ.get("RATE_LIMIT_REDIS_URL")
    if not url:
        return InMemoryRateLimiter()
    try:
        import redis as redis_module

        try:
            timeout = max(1, int(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", REDIS_SOCKET_TIMEOUT_DEFAULT)))
        except (TypeError, ValueError):
            timeout = REDIS_SOCKET_TIMEOUT_DEFAULT
        return RedisRateLimiter(
            redis_module.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout),
            fail_closed=fail_closed,
        )
    except Exception as exc:
        # Misconfiguration must be loud, not a silent downgrade to a limiter
        # that is ineffective in the multi-worker deployment it was set for.
        raise RuntimeError(
            "RATE_LIMIT_REDIS_URL is set but a Redis limiter could not be created: %s"
            % type(exc).__name__
        ) from exc


limiter = build_limiter()
