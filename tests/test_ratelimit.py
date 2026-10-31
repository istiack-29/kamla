import asyncio
import time

import pytest

import discord
from ratelimit import Kind, classify, retry_call, RequestCoordinator, CoalescingSender, SecretScrubber
from tests.fakes import http_error


async def _nosleep(_):
    return None


# 3. route-specific limits are not confused with global/Cloudflare blocks
def test_classification():
    assert classify(http_error(429, via=True, retry_after=2)) is Kind.RATE_LIMIT
    assert classify(http_error(429, via=False)) is Kind.GLOBAL_BLOCK
    assert classify(http_error(403)) is Kind.PERMISSION
    assert classify(http_error(404)) is Kind.NOT_FOUND
    assert classify(http_error(400, code=30013)) is Kind.INVALID
    assert classify(http_error(503)) is Kind.TRANSIENT_HTTP
    assert classify(asyncio.TimeoutError()) is Kind.NETWORK
    assert classify(ValueError("x")) is Kind.OTHER


async def test_rate_limit_waits_at_least_server_delay():
    sleeps, calls = [], []

    async def fake_sleep(d):
        sleeps.append(d)

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise http_error(429, via=True, retry_after=7.5)
        return "ok"

    assert await retry_call(op, label="t", attempts=4, sleep=fake_sleep) == "ok"
    assert len(calls) == 3 and all(s >= 7.5 for s in sleeps)


async def test_cloudflare_block_is_never_retried():
    calls = []

    async def op():
        calls.append(1)
        raise http_error(429, via=False)

    with pytest.raises(discord.HTTPException):
        await retry_call(op, label="t", attempts=5, sleep=_nosleep)
    assert len(calls) == 1


# 7. permission errors: reported, never retried
@pytest.mark.parametrize("status", [403, 404, 400])
async def test_permanent_errors_not_retried(status):
    calls = []

    async def op():
        calls.append(1)
        raise http_error(status)

    with pytest.raises(discord.HTTPException):
        await retry_call(op, label="t", attempts=5, sleep=_nosleep)
    assert len(calls) == 1


async def test_transient_backoff_bounded_with_jitter():
    sleeps, calls = [], []

    async def fake_sleep(d):
        sleeps.append(d)

    async def op():
        calls.append(1)
        raise http_error(503)

    with pytest.raises(discord.HTTPException):
        await retry_call(op, label="t", attempts=4, base_delay=1, sleep=fake_sleep, rng=lambda: 1.0)
    assert len(calls) == 4 and sleeps == [1.0, 2.0, 4.0]      # exponential, capped attempts


async def test_wait_longer_than_max_is_not_slept():
    calls = []

    async def op():
        calls.append(1)
        raise http_error(429, via=True, retry_after=3600)

    with pytest.raises(discord.HTTPException):
        await retry_call(op, label="t", attempts=4, max_wait=60, sleep=_nosleep)
    assert len(calls) == 1


# 6. bounded concurrency + pacing
async def test_coordinator_bounds_concurrency_and_spacing():
    coord = RequestCoordinator("t", concurrency=2, min_interval=0.02)
    starts = []

    async def op():
        starts.append(time.monotonic())
        await asyncio.sleep(0.05)

    await asyncio.gather(*[coord.run(op, label="x") for _ in range(12)])
    assert coord.max_in_flight <= 2 and coord.total == 12
    gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
    assert min(gaps) >= 0.015


async def test_coalescing_sender_latest_wins_and_bounded():
    coord = RequestCoordinator("t", concurrency=3, min_interval=0.01)
    sender = CoalescingSender(coord)
    sent = []

    def make(k, i):
        async def f():
            sent.append((k, i))
        return f

    for i in range(50):
        for k in range(100):                 # 5000 submissions for 100 "timers"
            sender.submit(k, make(k, i))
    await asyncio.sleep(0.4)
    await sender.aclose()
    assert sender.superseded > 4000
    assert len(sent) <= 100 + 45             # ~ budget: 1 pass + what pacing allowed
    assert coord.max_in_flight <= 3


# 9. no duplicate workers
async def test_sender_single_worker():
    sender = CoalescingSender(RequestCoordinator("t", 2, 0))
    async def f(): await asyncio.sleep(0.01)
    sender.submit("a", f); first = sender._task
    sender.submit("b", f)
    assert sender._task is first
    await sender.aclose()


def test_secret_scrubber():
    # assembled at runtime so no secret-shaped literal is committed
    token = ".".join(["M" + "A" * 23, "G" + "b" * 5, "c" * 30])
    s = SecretScrubber([token])
    assert token not in s.scrub(f"login with {token}")
    assert "REDACTED" in s.scrub("Authorization: Bot abcdefghijklmnopqrstuvwxyz012345")
    assert "tok123" not in s.scrub("https://discord.com/api/webhooks/123456/tok123456789abcdef")
