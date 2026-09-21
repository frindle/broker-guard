"""Retry with exponential backoff and full jitter, for flaky network calls."""
import logging
import random
import time

log = logging.getLogger("broker_guard.retry")


class RetryExhausted(RuntimeError):
    """Raised when every attempt failed; ``__cause__`` is the last error."""


def backoff_delays(attempts: int, base_delay: float = 1.0, max_delay: float = 60.0,
                   jitter: bool = True, rng=None) -> list[float]:
    """The delay to sleep BEFORE each retry (so ``attempts - 1`` entries).

    Pure and rng-injectable so the schedule can be asserted in tests without
    sleeping. Full jitter (uniform 0..cap) is used rather than fixed backoff so
    that several brokers failing at once do not retry in lockstep.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("delays must be non-negative")
    pick = (rng or random).uniform
    delays = []
    for i in range(attempts - 1):
        cap = min(base_delay * (2 ** i), max_delay)
        delays.append(pick(0, cap) if jitter else cap)
    return delays


def with_retry(fn, attempts: int = 3, base_delay: float = 1.0, max_delay: float = 60.0,
               retry_on=(Exception,), sleep=time.sleep, jitter: bool = True,
               rng=None, on_error=None, description: str = "call"):
    """Call ``fn()``, retrying on *retry_on* with backoff.

    ``sleep`` is injectable so tests never actually wait. Exceptions outside
    *retry_on* propagate immediately -- a 404 or a bad-config error should not
    be retried.
    """
    delays = backoff_delays(attempts, base_delay, max_delay, jitter, rng)
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if on_error is not None:
                on_error(attempt, exc)
            log.warning(
                "retryable failure",
                extra={"op": description, "attempt": attempt, "attempts": attempts,
                       "error": "{}: {}".format(type(exc).__name__, exc)},
            )
            if attempt < attempts:
                sleep(delays[attempt - 1])
    raise RetryExhausted(
        "{} failed after {} attempt(s)".format(description, attempts)
    ) from last
