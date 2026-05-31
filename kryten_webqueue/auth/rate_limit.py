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
