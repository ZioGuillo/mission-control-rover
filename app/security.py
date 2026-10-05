import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self):
        self._windows: dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str, max_per_second: int) -> bool:
        now = time.monotonic()
        window = self._windows[key]
        while window and window[0] < now - 1.0:
            window.popleft()
        if len(window) >= max_per_second:
            return False
        window.append(now)
        return True


rate_limiter = RateLimiter()
