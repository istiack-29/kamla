"""
ratelimit.py — application-level pacing, retry and diagnostics for KAMLA.

IMPORTANT: this module does NOT replace discord.py's HTTP layer. discord.py
already tracks per-route buckets, the global limit, and sleeps/retries on
JSON 429 responses. What it cannot do is stop *KAMLA itself* from producing
huge bursts (setup loops, DM fan-out, per-second timer edits, history scans).
That is what the pieces here are for:

  classify()            map an exception to a Kind (global block, rate limit,
                        permission, not-found, invalid, transient, network...)
  retry_call()          bounded retry: honours Retry-After, exponential backoff
                        with jitter for transient failures, never retries
                        permanent errors (403/404/400/Cloudflare block)
  RequestCoordinator    bounded concurrency + minimum spacing between request
                        starts for bulk producers (cooperates with the library)
  CoalescingSender      "latest wins" paced sender (used for timer edits)
  RateLimitWatcher      logging handler that counts the library's own 429 log
                        lines so route-specific vs global limits are visible
  SecretScrubber        logging filter that removes tokens / auth headers

Tunables (all optional environment variables):
  KAMLA_BULK_CONCURRENCY   default 2     setup / wipe / structure changes
  KAMLA_BULK_INTERVAL      default 0.30  seconds between request starts
  KAMLA_NOTIFY_CONCURRENCY default 3     DMs, mention cleanup, poll fan-out
  KAMLA_NOTIFY_INTERVAL    default 0.10
  KAMLA_EDIT_CONCURRENCY   default 4     timer message edits
  KAMLA_EDIT_INTERVAL      default 0.05  (=> at most ~20 edits/s overall)
  KAMLA_RETRY_ATTEMPTS     default 4
  KAMLA_RETRY_MAX_WAIT     default 60    seconds; longer waits are not slept
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Hashable, Optional

import aiohttp
import discord

log = logging.getLogger("kamla.ratelimit")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ── Classification ───────────────────────────────────────────────────────────

class Kind(str, enum.Enum):
    GLOBAL_BLOCK = "global_block"      # 429 without Discord's Via header (Cloudflare / IP-level)
    RATE_LIMIT = "rate_limit"          # 429 from Discord (route-specific or global bucket)
    PERMISSION = "permission"          # 401 / 403
    NOT_FOUND = "not_found"            # 404
    INVALID = "invalid"                # other 4xx (bad request, capacity limits ...)
    TRANSIENT_HTTP = "transient_http"  # 5xx
    NETWORK = "network"                # connection reset, timeout, DNS ...
    OTHER = "other"

    @property
    def retryable(self) -> bool:
        return self in (Kind.RATE_LIMIT, Kind.TRANSIENT_HTTP, Kind.NETWORK)


_RateLimitedExc = getattr(discord, "RateLimited", None)  # discord.py >= 2.5


def _headers(exc: BaseException) -> dict[str, str]:
    resp = getattr(exc, "response", None)
    raw = getattr(resp, "headers", None)
    try:
        return {str(k).lower(): str(v) for k, v in (raw or {}).items()}
    except Exception:
        return {}


def retry_after_of(exc: BaseException) -> Optional[float]:
    """Server-provided wait, if the exception carries one."""
    ra = getattr(exc, "retry_after", None)
    if isinstance(ra, (int, float)):
        return float(ra)
    h = _headers(exc)
    for key in ("retry-after", "x-ratelimit-reset-after"):
        if key in h:
            try:
                return float(h[key])
            except ValueError:
                pass
    return None


def classify(exc: BaseException) -> Kind:
    if _RateLimitedExc is not None and isinstance(exc, _RateLimitedExc):
        return Kind.RATE_LIMIT
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", 0)
        if status == 429:
            # discord.py itself treats a 429 with no "Via" header (or a non-JSON
            # body) as "banned by Cloudflare" and raises immediately.
            if "via" not in _headers(exc):
                return Kind.GLOBAL_BLOCK
            return Kind.RATE_LIMIT
        if status in (401, 403):
            return Kind.PERMISSION
        if status == 404:
            return Kind.NOT_FOUND
        if status >= 500:
            return Kind.TRANSIENT_HTTP
        return Kind.INVALID
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)):
        return Kind.NETWORK
    for name in ("GatewayNotFound", "ConnectionClosed"):
        cls = getattr(discord, name, None)
        if cls is not None and isinstance(exc, cls):
            return Kind.NETWORK
    return Kind.OTHER


def describe(exc: BaseException) -> str:
    """Short, secret-free description for logs and user-facing reports."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    text = getattr(exc, "text", None) or str(exc)
    text = re.sub(r"\s+", " ", str(text))[:160]
    bits = [type(exc).__name__]
    if status:
        bits.append(f"status={status}")
    if code:
        bits.append(f"code={code}")
    bits.append(text)
    return " ".join(bits)


# ── Retry ────────────────────────────────────────────────────────────────────

def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0,
                  rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter. attempt is 1-based."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return ceiling * (0.5 + 0.5 * rng())


async def retry_call(
    factory: Callable[[], Awaitable[Any]],
    *,
    label: str = "operation",
    attempts: Optional[int] = None,
    base_delay: float = 1.0,
    max_wait: Optional[float] = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> Any:
    """
    Run `factory()` with bounded retries. The ORIGINAL exception is re-raised
    when giving up, so callers keep catching discord.Forbidden etc.

    * RATE_LIMIT      wait at least the server-provided retry delay (+ jitter),
                      never earlier. If it exceeds max_wait we stop instead of
                      parking a task for minutes.
    * TRANSIENT/NETWORK  exponential backoff with jitter.
    * everything else    raised immediately — permission errors, 404, invalid
                      requests and Cloudflare blocks are never retried.
    """
    attempts = attempts if attempts is not None else _env_int("KAMLA_RETRY_ATTEMPTS", 4)
    max_wait = max_wait if max_wait is not None else _env_float("KAMLA_RETRY_MAX_WAIT", 60.0)
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            kind = classify(exc)
            if not kind.retryable:
                log.warning("[%s] not retrying (%s): %s", label, kind.value, describe(exc))
                raise
            if attempt >= attempts:
                log.error("[%s] retries exhausted after %d attempts (%s): %s",
                          label, attempt, kind.value, describe(exc))
                raise
            if kind is Kind.RATE_LIMIT:
                server_wait = retry_after_of(exc)
                delay = (server_wait if server_wait is not None else 1.0) + 0.25 * rng()
            else:
                delay = backoff_delay(attempt, base_delay, max_wait, rng)
            if delay > max_wait:
                log.error("[%s] required wait %.1fs exceeds max %.1fs; giving up (%s)",
                          label, delay, max_wait, kind.value)
                raise
            log.warning("[%s] attempt %d/%d failed (%s); retrying in %.2fs",
                        label, attempt, attempts, kind.value, delay)
            await sleep(delay)
    assert last is not None  # pragma: no cover
    raise last


# ── Coordinator ──────────────────────────────────────────────────────────────

class RequestCoordinator:
    """
    Bounded concurrency + minimum spacing between request *starts* for KAMLA's
    own bulk producers. It deliberately does not model Discord's buckets — it
    only guarantees our code cannot burst; discord.py still enforces the real
    per-route and global limits.
    """

    def __init__(self, name: str, concurrency: int = 2, min_interval: float = 0.3,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 retry_kwargs: Optional[dict] = None) -> None:
        self.name = name
        self.retry_kwargs = dict(retry_kwargs or {})   # e.g. injected sleep/rng for tests
        self.concurrency = max(1, int(concurrency))
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._sem: Optional[asyncio.Semaphore] = None
        self._pace_lock: Optional[asyncio.Lock] = None
        self._next_start = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self.total = 0

    @classmethod
    def from_env(cls, name: str, prefix: str, concurrency: int, interval: float) -> "RequestCoordinator":
        return cls(name,
                   _env_int(f"{prefix}_CONCURRENCY", concurrency),
                   _env_float(f"{prefix}_INTERVAL", interval))

    def _ensure(self) -> None:
        # Created lazily so the object can be built at import time, before a loop exists.
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
            self._pace_lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        self._ensure()
        assert self._sem is not None and self._pace_lock is not None
        async with self._sem:
            async with self._pace_lock:
                now = self._clock()
                wait = max(0.0, self._next_start - now)
                self._next_start = max(now, self._next_start) + self.min_interval
            if wait:
                await self._sleep(wait)
            self.in_flight += 1
            self.total += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                yield
            finally:
                self.in_flight -= 1

    async def run(self, factory: Callable[[], Awaitable[Any]], *, label: str = "request",
                  attempts: Optional[int] = None, **retry_kwargs: Any) -> Any:
        """One paced, bounded, retried API operation."""
        async def attempt() -> Any:
            async with self.slot():
                return await factory()
        return await retry_call(attempt, label=label, attempts=attempts,
                                **{**self.retry_kwargs, **retry_kwargs})

    async def best_effort(self, factory: Callable[[], Awaitable[Any]], *, label: str = "request") -> bool:
        """Like run(), but never raises: returns True on success. Expected,
        permanent failures (closed DMs, deleted message, missing perms) are
        logged at DEBUG; anything else at WARNING."""
        try:
            await self.run(factory, label=label)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            kind = classify(exc)
            level = logging.DEBUG if kind in (Kind.PERMISSION, Kind.NOT_FOUND) else logging.WARNING
            log.log(level, "[%s] best-effort operation failed (%s): %s", label, kind.value, describe(exc))
            return False


bulk = RequestCoordinator.from_env("bulk", "KAMLA_BULK", 2, 0.30)
notify = RequestCoordinator.from_env("notify", "KAMLA_NOTIFY", 3, 0.10)
edits = RequestCoordinator.from_env("edits", "KAMLA_EDIT", 4, 0.05)


class CoalescingSender:
    """
    "Latest wins" paced sender. submit(key, factory) replaces any not-yet-sent
    update for the same key; a worker drains the queue through a
    RequestCoordinator (bounded concurrency + spacing). Many timers therefore
    cannot exceed the edit budget: under overload each one simply updates less
    often instead of flooding the API. Updates for one key are never in flight
    twice at once, so ordering per key is preserved.
    """

    def __init__(self, coordinator: RequestCoordinator) -> None:
        self._coord = coordinator
        self._pending: dict[Hashable, Callable[[], Awaitable[Any]]] = {}
        self._inflight: set[Hashable] = set()
        self._task: Optional[asyncio.Task] = None
        self.superseded = 0
        self.sent = 0

    def submit(self, key: Hashable, factory: Callable[[], Awaitable[Any]]) -> None:
        if key in self._pending:
            self.superseded += 1
        self._pending[key] = factory
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._drain())

    def discard(self, key: Hashable) -> None:
        self._pending.pop(key, None)

    async def _send(self, key: Hashable, factory: Callable[[], Awaitable[Any]]) -> None:
        try:
            await self._coord.run(factory, label=f"edit:{key}", attempts=2)
            self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - already logged by retry_call
            pass
        finally:
            self._inflight.discard(key)

    async def _drain(self) -> None:
        active: set[asyncio.Task] = set()
        while self._pending or active:
            while len(active) < self._coord.concurrency:
                key = next((k for k in self._pending if k not in self._inflight), None)
                if key is None:
                    break
                factory = self._pending.pop(key)
                self._inflight.add(key)
                task = asyncio.get_running_loop().create_task(self._send(key, factory))
                active.add(task)
                task.add_done_callback(active.discard)
            if not active:
                break
            await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)

    async def aclose(self) -> None:
        self._pending.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


timer_edits = CoalescingSender(edits)


# ── Diagnostics: logging helpers ─────────────────────────────────────────────

class SecretScrubber(logging.Filter):
    """Removes the bot token, Authorization headers and webhook tokens."""

    _PATTERNS = [
        re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bot\s+)?[A-Za-z0-9._\-]{20,}"),
        re.compile(r"(?i)\bBot\s+[A-Za-z0-9._\-]{20,}"),
        re.compile(r"(discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/)[A-Za-z0-9._\-]+"),
        re.compile(r"[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,}"),
    ]

    def __init__(self, extra_secrets: Optional[list[str]] = None) -> None:
        super().__init__()
        self._extra = [s for s in (extra_secrets or []) if s and len(s) >= 8]

    def scrub(self, text: str) -> str:
        for s in self._extra:
            text = text.replace(s, "[REDACTED]")
        text = self._PATTERNS[0].sub(r"\1[REDACTED]", text)
        text = self._PATTERNS[1].sub("Bot [REDACTED]", text)
        text = self._PATTERNS[2].sub(r"\1[REDACTED]", text)
        text = self._PATTERNS[3].sub("[REDACTED]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = self.scrub(msg)
        if scrubbed != msg:
            record.msg, record.args = scrubbed, ()
        return True


class RateLimitWatcher(logging.Handler):
    """
    Counts the *library's own* rate-limit log lines (discord.http logger) so we
    can tell route-specific limits from global ones. We do not invent numbers:
    these are only the events discord.py actually reported.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.route_limited = 0
        self.global_limited = 0
        self.sub_ratelimit = 0
        self.last_event_at: Optional[float] = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        low = msg.lower()
        if "global rate limit has been hit" in low:
            self.global_limited += 1
        elif "sub-ratelimit" in low or "sub ratelimit" in low:
            self.sub_ratelimit += 1
        elif "rate limited" in low:
            self.route_limited += 1
        else:
            return
        self.last_event_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {"route_limited": self.route_limited, "global_limited": self.global_limited,
                "sub_ratelimit": self.sub_ratelimit, "last_event_at": self.last_event_at}


watcher = RateLimitWatcher()


def configure_logging(token: Optional[str] = None) -> None:
    """Idempotent logging setup (safe to call more than once)."""
    root = logging.getLogger()
    if getattr(root, "_kamla_configured", False):
        return
    level = getattr(logging, os.getenv("KAMLA_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(SecretScrubber([token] if token else None))
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("discord").setLevel(max(level, logging.INFO) if level > logging.INFO else logging.INFO)
    logging.getLogger("discord.http").addHandler(watcher)
    root._kamla_configured = True  # type: ignore[attr-defined]
