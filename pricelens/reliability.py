"""Calling a flaky, rate-limited API politely: retry policy, rate limiter, request budget, circuit breaker.

Everything takes injectable `clock`/`sleep`/`rand`, so the tests run instantly on a fake clock.
All classes are thread-safe: one instance is shared by every worker in a batch.

Policy (see PLAN.md, Layer 4):
- retry: timeouts, connection errors, 5xx, per-minute 429  -> exponential backoff, full jitter, max 4 attempts
- fail fast: auth, 400s, refusals, unreadable images, malformed output (same input -> same bad output)
- stop the whole run: daily quota exhausted, budget used up, circuit open (service down), auth failure
"""
from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import AuthError, PriceLensError, RateLimitError, TransientAPIError

T = TypeVar("T")


class StopRun(PriceLensError):
    """Something that makes every remaining request pointless: stop the batch, resume later."""


class DailyQuotaExceeded(StopRun):
    def __init__(self, message: str, retry_in: float | None = None):
        super().__init__(message)
        self.retry_in = retry_in


class BudgetExhausted(StopRun):
    pass


class CircuitOpen(StopRun):
    pass


_HINT = re.compile(r"retry in\s+((?:\d+h)?(?:\d+m)?(?:\d+(?:\.\d+)?s)?)", re.I)


def parse_retry_hint(message: str) -> float | None:
    """'Please retry in 1h21m38s' -> 4898.0 seconds. Gemini puts this in the message, not a header."""
    m = _HINT.search(message or "")
    if not m or not m.group(1):
        return None
    total = 0.0
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", m.group(1)):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or None


def is_daily_quota(err: Exception) -> bool:
    if not isinstance(err, RateLimitError):
        return False
    msg = str(err).lower()
    hint = parse_retry_hint(str(err)) or err.retry_after or 0
    return "per day" in msg or "daily" in msg or hint > 3600


def is_retryable(err: Exception) -> bool:
    return isinstance(err, (TransientAPIError, RateLimitError)) and not is_daily_quota(err)


class Budget:
    """Hard cap on requests a run may send (failed attempts can count against provider quotas too)."""

    def __init__(self, limit: int | None):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                raise BudgetExhausted(f"request budget of {self.limit} used up")
            self.used += 1

    @property
    def left(self) -> int | None:
        return None if self.limit is None else self.limit - self.used


class RateLimiter:
    """At most `rpm` requests per minute across all threads, evenly spaced (no bursts)."""

    def __init__(self, rpm: float | None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 60.0 / rpm if rpm else 0.0
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:  # reserve a slot, then wait outside the lock
            now = self.clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            self.sleep(slot - now)


class CircuitBreaker:
    """After `threshold` consecutive transient failures (from any worker), stop sending.
    Wait `cooldown`, let ONE probe through; if it fails too, open for good -> CircuitOpen."""

    def __init__(self, threshold: int = 3, cooldown: float = 300.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.threshold, self.cooldown = threshold, cooldown
        self.clock, self.sleep = clock, sleep
        self.failures = 0
        self.state = "closed"   # closed -> open -> half_open -> closed | dead
        self._opened_at = 0.0
        self._probe_taken = False
        self._lock = threading.Lock()

    def before_call(self) -> None:
        while True:
            with self._lock:
                if self.state == "dead":
                    raise CircuitOpen("service keeps failing (circuit open); try again later")
                if self.state == "closed":
                    return
                if self.state == "open":
                    wait = self._opened_at + self.cooldown - self.clock()
                    if wait <= 0:
                        self.state, self._probe_taken = "half_open", False
                        continue
                elif self.state == "half_open":
                    if not self._probe_taken:
                        self._probe_taken = True
                        return  # this caller is the probe
                    wait = 1.0  # others wait for the probe's verdict
            self.sleep(max(wait, 0.01))

    def record_success(self) -> None:
        with self._lock:
            self.failures, self.state = 0, "closed"

    def record_failure(self) -> None:
        with self._lock:
            if self.state == "half_open":
                self.state = "dead"
                return
            self.failures += 1
            if self.failures >= self.threshold and self.state == "closed":
                self.state, self._opened_at = "open", self.clock()


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base: float = 2.0
    cap: float = 60.0

    def delay(self, attempt: int, err: Exception, rand: Callable[[], float] = random.random) -> float:
        """Full jitter: uniform(0, min(cap, base * 2^(attempt-1))); a server's retry hint wins if larger."""
        backoff = rand() * min(self.cap, self.base * 2 ** (attempt - 1))
        hint = getattr(err, "retry_after", None) or parse_retry_hint(str(err)) or 0
        return max(backoff, min(hint, self.cap * 5))


class Caller:
    """Wraps every API request with budget, rate limit, circuit breaker and retries. Share one per run."""

    def __init__(self, policy: RetryPolicy | None = None, limiter: RateLimiter | None = None,
                 budget: Budget | None = None, breaker: CircuitBreaker | None = None,
                 sleep: Callable[[float], None] = time.sleep, rand: Callable[[], float] = random.random):
        self.policy = policy or RetryPolicy()
        self.limiter = limiter or RateLimiter(None)
        self.budget = budget or Budget(None)
        self.breaker = breaker or CircuitBreaker()
        self.sleep, self.rand = sleep, rand
        self._lock = threading.Lock()
        self.requests = 0
        self.retries = 0

    def call(self, fn: Callable[[], T]) -> tuple[T, int]:
        """Returns (result, attempts). Raises the last error, or a StopRun subclass."""
        attempt = 0
        while True:
            attempt += 1
            self.breaker.before_call()
            self.budget.take()
            self.limiter.acquire()
            with self._lock:
                self.requests += 1
            try:
                result = fn()
            except AuthError:
                raise
            except PriceLensError as e:
                if is_daily_quota(e):
                    raise DailyQuotaExceeded(str(e), parse_retry_hint(str(e))) from e
                if not is_retryable(e):
                    raise  # fail fast: retrying the same input won't help
                self.breaker.record_failure()
                if attempt >= self.policy.max_attempts:
                    raise
                with self._lock:
                    self.retries += 1
                self.sleep(self.policy.delay(attempt, e, self.rand))
                continue
            self.breaker.record_success()
            return result, attempt
