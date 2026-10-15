import time
from collections import defaultdict, deque

from starlette.requests import Request

_LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """The real client address. Behind the Cloudflare tunnel every request
    arrives from loopback with the visitor in CF-Connecting-IP; anyone else
    could send that header themselves to dodge a per-IP limit, so it's only
    trusted from loopback."""
    peer = request.client.host if request.client else "unknown"
    if peer in _LOOPBACK:
        return request.headers.get("cf-connecting-ip") or peer
    return peer


class RateLimiter:
    def __init__(self):
        self._windows: dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str, max_per_window: int, window: float = 1.0) -> bool:
        now = time.monotonic()
        hits = self._windows[key]
        while hits and hits[0] < now - window:
            hits.popleft()
        if len(hits) >= max_per_window:
            return False
        hits.append(now)
        return True


rate_limiter = RateLimiter()
