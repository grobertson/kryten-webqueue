import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window rate limiter for OTP requests."""

    def __init__(self, max_requests: int = 3, window_seconds: int = 300):
        self._max = max_requests
        self._window = window_seconds
        self._requests: dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window = self._requests[key]
        # Remove expired entries
        while window and window[0] < now - self._window:
            window.popleft()
        if len(window) >= self._max:
            return False
        window.append(now)
        return True

    def remaining(self, key: str) -> int:
        now = time.time()
        window = self._requests[key]
        while window and window[0] < now - self._window:
            window.popleft()
        return max(0, self._max - len(window))


class QuotaLimiter:
    """Sliding-window limiter that enforces several named tiers at once.

    Each tier is ``(label, max_requests, window_seconds)``. An action is only
    permitted when *every* tier still has room; a permitted action is recorded
    against all tiers atomically. Used for hard per-user submission quotas
    (e.g. 2/day and 6/week) rather than short-burst throttling.
    """

    def __init__(self, tiers: list[tuple[str, int, int]]):
        if not tiers:
            raise ValueError("QuotaLimiter requires at least one tier")
        self._tiers = list(tiers)
        self._max_window = max(window for _, _, window in self._tiers)
        self._events: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> str | None:
        """Return the label of the first exhausted tier, or ``None`` if allowed.

        When allowed the action is recorded; when blocked nothing is recorded.
        """
        now = time.time()
        events = self._events[key]
        while events and events[0] < now - self._max_window:
            events.popleft()
        for label, max_requests, window in self._tiers:
            count = sum(1 for ts in events if ts >= now - window)
            if count >= max_requests:
                return label
        events.append(now)
        return None
