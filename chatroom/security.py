# Copyright 2026 Warren Schultz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Edge concerns for running chatroom where the internet can reach it.

The bearer token is the only auth boundary this server has. On a trusted LAN
segment that is enough. Published through a tunnel (see CLOUDFLARE_TUNNEL.md)
the same endpoint is reachable by anyone who learns the hostname, so two extra
things matter:

  * **Who is calling.** Behind a tunnel or reverse proxy every TCP peer is the
    proxy itself, so the caller's real address only exists in a forwarded
    header. Those headers are trivially spoofed by a direct client, so we read
    them only when the operator says a proxy is in front
    (``CHATROOM_TRUST_PROXY=on``).

  * **Failed-auth pressure.** Guessing a 256-bit token is hopeless, but nothing
    stopped a scanner from making the server do real work per attempt. A
    sliding-window limiter per caller keeps a spray cheap for us and useless for
    them, and gives the operator a log trail.

Both are process-local on purpose: no shared state, no new dependency, and a
restart is an acceptable reset for a coordination bus.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from contextvars import ContextVar

# Address of the caller currently being served, set by ClientAddressMiddleware.
# A ContextVar (not a parameter) because the MCP tool path hands tools a Context
# with headers but no peer address, and we want one answer for both surfaces.
_CLIENT_ADDR: ContextVar[str] = ContextVar("chatroom_client_addr", default="-")

#: Forwarded-for headers, most trustworthy first. CF-Connecting-IP is set by
#: Cloudflare and, unlike X-Forwarded-For, is a single address it always
#: overwrites rather than appends to.
_FORWARD_HEADERS = ("cf-connecting-ip", "x-real-ip", "x-forwarded-for")


def _flag(name: str, default: str = "off") -> bool:
    return os.environ.get(name, default).strip().lower() in ("on", "true", "1", "yes")


def trust_proxy() -> bool:
    """Whether to believe forwarded client-address headers.

    Off by default: when the port is published directly, any client can send
    ``CF-Connecting-IP`` and would otherwise get a free identity per request,
    defeating the throttle. Turn it on when a tunnel or reverse proxy is the
    only path to this server.
    """
    return _flag("CHATROOM_TRUST_PROXY")


def client_addr() -> str:
    """The caller's address for the request being served ('-' if unknown)."""
    return _CLIENT_ADDR.get()


def _derive_addr(scope: Mapping) -> str:
    peer = scope.get("client") or ()
    direct = peer[0] if peer else "-"
    if not trust_proxy():
        return direct
    headers = scope.get("headers") or ()
    seen = {}
    for raw_name, raw_value in headers:
        try:
            seen[raw_name.decode("latin-1").lower()] = raw_value.decode("latin-1")
        except (UnicodeDecodeError, AttributeError):
            continue
    for name in _FORWARD_HEADERS:
        value = seen.get(name, "").strip()
        if value:
            # X-Forwarded-For is a chain; the left-most entry is the origin client.
            first = value.split(",")[0].strip()
            if first:
                return f"{first} via {direct}"
    return direct


class ClientAddressMiddleware:
    """Pure-ASGI middleware recording the caller's address for the request.

    Pure ASGI rather than BaseHTTPMiddleware so the SSE stream at /v1/stream
    keeps streaming incrementally instead of being buffered.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _CLIENT_ADDR.set(_derive_addr(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _CLIENT_ADDR.reset(token)


class AuthThrottle:
    """Sliding-window limiter for failed credential attempts, keyed per caller.

    Only *failures* are counted, so a healthy agent polling `whats_new` every
    turn never approaches the limit no matter how chatty it is. Callers are
    expected to verify the credential first and consult the budget only on
    failure, so that an address shared by several agents (NAT, or a tunnel
    without CHATROOM_TRUST_PROXY) cannot have one bad token lock out the rest.
    """

    def __init__(self, limit: int, window_s: float, max_tracked: int = 4096):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self.max_tracked = int(max_tracked)
        self._fails: dict[str, deque[float]] = {}
        # Last time we emitted a "now blocking this caller" notice, per caller, so a
        # sustained spray produces one line rather than one per attempt.
        self._noted: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _prune(self, key: str, now: float) -> deque[float]:
        # maxlen bounds memory to `limit` timestamps per caller no matter how hard it
        # sprays, while still sliding correctly: the oldest of the most recent `limit`
        # failures is what the window is measured from, so sustained abuse keeps
        # extending its own block instead of growing a list.
        hits = self._fails.setdefault(key, deque(maxlen=max(1, self.limit)))
        cutoff = now - self.window_s
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def retry_after(self, key: str) -> int | None:
        """Seconds the caller should wait, or None if it may attempt auth now."""
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) < self.limit:
                return None
            return max(1, int(hits[0] + self.window_s - now) + 1)

    def record_failure(self, key: str) -> int:
        """Count a bad credential. Returns the failure count in the window."""
        if not self.enabled:
            return 0
        now = time.monotonic()
        with self._lock:
            if key not in self._fails and len(self._fails) >= self.max_tracked:
                # Bounded memory: a spray from many addresses must not grow the map
                # without limit. Evict callers whose window has fully expired — they
                # would score zero anyway. Each surviving entry is at most `limit`
                # floats, so what remains is proportional to genuinely active callers.
                cutoff = now - self.window_s
                for stale in [k for k, v in self._fails.items() if not v or v[-1] < cutoff]:
                    del self._fails[stale]
            hits = self._prune(key, now)
            hits.append(now)
            return len(hits)

    def note_block(self, key: str) -> bool:
        """True at most once per window, for logging that a caller is now blocked.

        The failure deque is bounded, so its length plateaus at the limit and cannot
        itself mark the transition — hence this explicit once-per-window latch.
        """
        if not self.enabled:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._noted.get(key)
            if last is not None and now - last < self.window_s:
                return False
            if len(self._noted) >= self.max_tracked:
                cutoff = now - self.window_s
                for stale in [k for k, t in self._noted.items() if t < cutoff]:
                    del self._noted[stale]
            self._noted[key] = now
            return True

    # Note there is deliberately no record_success(): a good credential does *not*
    # clear the window. Addresses are routinely shared (NAT, or a tunnel without
    # CHATROOM_TRUST_PROXY), so letting one healthy agent reset the bucket would let
    # an abuser sharing that address spray indefinitely. Since a valid token is never
    # throttled, nothing needs the reset — failures simply age out of the window.


def throttle_from_env() -> AuthThrottle:
    """Build the process throttle. CHATROOM_AUTH_FAIL_LIMIT=0 disables it."""
    limit = int(os.environ.get("CHATROOM_AUTH_FAIL_LIMIT", "20"))
    window = int(os.environ.get("CHATROOM_AUTH_FAIL_WINDOW", "300"))
    return AuthThrottle(limit, window)


def expand_allowed_hosts(entries: Iterable[str]) -> list[str]:
    """Normalise a Host allowlist so one entry covers a host with or without a port.

    The SDK matches ``example.com:*`` by requiring the Host header to literally
    start with ``example.com:`` — a port must be present. A request arriving
    through a tunnel or any HTTPS front door has ``Host: example.com`` with no
    port at all (443 is implied and browsers/clients omit it), so the ``:*``
    form alone silently 421s every tunnelled request. Emitting the bare host
    alongside ``host:*`` matches the intent of "this hostname, any port" and
    removes a trap that is very hard to debug from the client side.
    """
    out: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if entry not in out:
            out.append(entry)
        if entry.endswith(":*"):
            bare = entry[:-2]
            if bare and bare not in out:
                out.append(bare)
    return out


def ui_enabled() -> bool:
    """Whether to serve the /ui dashboard. Off is useful for internet-exposed hosts."""
    return _flag("CHATROOM_ENABLE_UI", "on")
