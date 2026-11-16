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

    def _hits(self, key: str, window: float) -> deque:
        hits = self._windows[key]
        while hits and hits[0] < time.monotonic() - window:
            hits.popleft()
        return hits

    def is_allowed(self, key: str, max_per_window: int, window: float = 1.0) -> bool:
        hits = self._hits(key, window)
        if len(hits) >= max_per_window:
            return False
        hits.append(time.monotonic())
        return True

    def is_blocked(self, key: str, max_per_window: int, window: float = 1.0) -> bool:
        """Over the limit, without counting this call (see record)."""
        return len(self._hits(key, window)) >= max_per_window

    def record(self, key: str) -> None:
        """Count one event, e.g. a failed login: right passwords don't use up the limit."""
        self._windows[key].append(time.monotonic())


def is_local_scrape(request: Request) -> bool:
    """A request from the robot itself, and not a visitor forwarded by the
    Cloudflare tunnel (which also arrives from loopback, with CF-Connecting-IP)."""
    peer = request.client.host if request.client else ""
    return peer in _LOOPBACK and "cf-connecting-ip" not in request.headers


rate_limiter = RateLimiter()
