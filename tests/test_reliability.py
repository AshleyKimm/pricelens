"""Retry policy, rate limiter, budget and circuit breaker, on a fake clock (instant, deterministic)."""
import threading

import pytest

from pricelens.errors import (AuthError, MalformedOutputError, PermanentAPIError, RateLimitError, RefusalError,
                              TransientAPIError, UnreadableImageError)
from pricelens.reliability import (Budget, BudgetExhausted, Caller, CircuitBreaker, CircuitOpen, DailyQuotaExceeded,
                                   RateLimiter, RetryPolicy, is_daily_quota, is_retryable, parse_retry_hint)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def __call__(self):
        return self.now

    def sleep(self, s):
        with self._lock:
            self.sleeps.append(s)
            self.now += s


def flaky(*outcomes):
    """A function that raises/returns the given outcomes in order (the last one repeats)."""
    seq = list(outcomes)
    calls = []

    def fn():
        calls.append(1)
        o = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(o, Exception):
            raise o
        return o
    fn.calls = calls
    return fn


def caller(clock, **kw):
    return Caller(policy=kw.pop("policy", RetryPolicy(max_attempts=4, base=2, cap=60)),
                  breaker=kw.pop("breaker", CircuitBreaker(threshold=99, clock=clock, sleep=clock.sleep)),
                  sleep=clock.sleep, rand=lambda: 1.0, **kw)


# --- classification ---------------------------------------------------------------------------

def test_hint_parsing():
    assert parse_retry_hint("Please retry in 1h21m38s or upgrade") == 4898
    assert parse_retry_hint("retry in 38s") == 38
    assert parse_retry_hint("retry in 15h9m7s") == 54547
    assert parse_retry_hint("no hint here") is None


def test_daily_quota_vs_per_minute():
    daily = RateLimitError("Rate limit exceeded (limit: 20 requests per day on Free Tier). Please retry in 15h9m7s")
    minute = RateLimitError("Rate limit exceeded. Please retry in 20s", retry_after=20)
    assert is_daily_quota(daily) and not is_retryable(daily)
    assert not is_daily_quota(minute) and is_retryable(minute)


@pytest.mark.parametrize("err,retry", [
    (TransientAPIError("503"), True), (RateLimitError("429"), True),
    (AuthError("bad key"), False), (PermanentAPIError("400"), False), (RefusalError("no"), False),
    (UnreadableImageError("x"), False), (MalformedOutputError("bad json"), False),
])
def test_what_is_retried(err, retry):
    assert is_retryable(err) is retry


# --- retries ----------------------------------------------------------------------------------

def test_retries_transient_then_succeeds_with_exponential_backoff():
    clock = FakeClock()
    fn = flaky(TransientAPIError("503"), TransientAPIError("503"), "ok")
    assert caller(clock).call(fn) == ("ok", 3)
    assert clock.sleeps == [2.0, 4.0]  # rand=1.0 -> the top of each jitter window: 2, 4


def test_jitter_stays_within_window():
    p = RetryPolicy(base=2, cap=60)
    for attempt, top in [(1, 2), (2, 4), (3, 8), (7, 60)]:
        assert 0 <= p.delay(attempt, TransientAPIError("x"), rand=lambda: 0.999) <= top
        assert p.delay(attempt, TransientAPIError("x"), rand=lambda: 0.0) == 0


def test_gives_up_after_max_attempts():
    clock = FakeClock()
    fn = flaky(TransientAPIError("503"))
    with pytest.raises(TransientAPIError):
        caller(clock).call(fn)
    assert len(fn.calls) == 4 and len(clock.sleeps) == 3


def test_honours_server_retry_after():
    clock = FakeClock()
    fn = flaky(RateLimitError("slow down", retry_after=30), "ok")
    caller(clock).call(fn)
    assert clock.sleeps == [30]


@pytest.mark.parametrize("err", [AuthError("bad key"), PermanentAPIError("400"), MalformedOutputError("x"),
                                 UnreadableImageError("x"), RefusalError("no")])
def test_permanent_errors_fail_fast(err):
    clock = FakeClock()
    fn = flaky(err)
    with pytest.raises(type(err)):
        caller(clock).call(fn)
    assert len(fn.calls) == 1 and clock.sleeps == []


def test_daily_quota_stops_without_retrying():
    clock = FakeClock()
    fn = flaky(RateLimitError("limit: 20 requests per day. Please retry in 15h9m7s"))
    with pytest.raises(DailyQuotaExceeded) as e:
        caller(clock).call(fn)
    assert len(fn.calls) == 1 and e.value.retry_in == 54547


# --- budget -----------------------------------------------------------------------------------

def test_budget_counts_every_attempt_including_retries():
    clock = FakeClock()
    c = caller(clock, budget=Budget(3))
    with pytest.raises(BudgetExhausted):
        c.call(flaky(TransientAPIError("503")))  # 3 attempts allowed, the 4th needs budget
    assert c.budget.used == 3 and c.budget.left == 0


def test_budget_is_thread_safe():
    b = Budget(100)
    ok = []

    def grab():
        for _ in range(50):
            try:
                b.take()
                ok.append(1)
            except BudgetExhausted:
                pass
    threads = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 100 and b.used == 100


# --- rate limiter -----------------------------------------------------------------------------

def test_rate_limiter_spaces_requests_evenly():
    clock = FakeClock()
    lim = RateLimiter(rpm=10, clock=clock, sleep=clock.sleep)  # one every 6 s
    starts = []
    for _ in range(4):
        lim.acquire()
        starts.append(clock.now)
    assert starts == [0, 6, 12, 18]


def test_rate_limiter_across_threads_never_bursts():
    lock, times = threading.Lock(), []
    import time
    lim = RateLimiter(rpm=600)  # real clock, 0.1 s apart

    def go():
        lim.acquire()
        with lock:
            times.append(time.monotonic())
    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    times.sort()
    assert all(b - a >= 0.09 for a, b in zip(times, times[1:]))


# --- circuit breaker --------------------------------------------------------------------------

def test_circuit_opens_waits_probes_and_recovers():
    clock = FakeClock()
    br = CircuitBreaker(threshold=3, cooldown=300, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        br.record_failure()
    assert br.state == "open"
    br.before_call()                 # waits out the cooldown, becomes the probe
    assert clock.now >= 300 and br.state == "half_open"
    br.record_success()
    assert br.state == "closed" and br.failures == 0


def test_circuit_gives_up_if_probe_fails():
    clock = FakeClock()
    br = CircuitBreaker(threshold=3, cooldown=300, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        br.record_failure()
    br.before_call()
    br.record_failure()              # probe failed: service still down
    with pytest.raises(CircuitOpen):
        br.before_call()


def test_outage_stops_the_caller_instead_of_hammering():
    # The real failure from today: 503s for 45 minutes. Old loop: 24 requests. Now: 3 + 1 probe, then stop.
    clock = FakeClock()
    c = Caller(policy=RetryPolicy(max_attempts=10), breaker=CircuitBreaker(threshold=3, cooldown=300, clock=clock,
               sleep=clock.sleep), sleep=clock.sleep, rand=lambda: 0.5)
    fn = flaky(TransientAPIError("503 high demand"))
    with pytest.raises(CircuitOpen):
        c.call(fn)
    assert len(fn.calls) == 4


def test_success_resets_the_failure_count():
    clock = FakeClock()
    br = CircuitBreaker(threshold=3, clock=clock, sleep=clock.sleep)
    br.record_failure(), br.record_failure(), br.record_success(), br.record_failure(), br.record_failure()
    assert br.state == "closed"
