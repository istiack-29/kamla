
import pytest

import server_builder as sb
from ratelimit import RequestCoordinator
from tests.fakes import FakeGuild, FakeConfig, http_error


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    fast_bulk = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0})
    monkeypatch.setattr(sb, "bulk", fast_bulk)
    monkeypatch.setattr(sb, "config_manager", FakeConfig())

    async def noop(*a, **k):
        return None
    for name in ("_post_meet_developer", "_post_welcome", "_post_get_role", "_post_how_to_use",
                 "_post_settings_panel", "_post_all_in_panel"):
        monkeypatch.setattr(sb, name, noop)
    return fast_bulk


def names(g, kind):
    return [c.name for c in g.channels if c.kind == kind]


async def build(g, rooms=2, fmt="ap"):
    return await sb.build_server(g, fmt, rooms, "+06:00", "T", 1)


EXPECTED_BASE = sb.base_channel_count()


async def test_fresh_build_creates_expected_structure():
    g = FakeGuild()
    rep = await build(g, rooms=2)
    assert len(g.channels) == EXPECTED_BASE + 2 * sb.room_channel_count("ap")
    assert {"ROOM 01", "ROOM 02"} <= set(names(g, "category"))
    assert rep.ok and len([r for r in g.roles if r.name == "ORG"]) == 1


# 4. second run creates nothing new
async def test_second_run_is_idempotent():
    g = FakeGuild()
    await build(g)
    calls, count = len(g.create_calls), len(g.channels)
    rep = await build(g)
    assert len(g.create_calls) == calls and len(g.channels) == count
    assert rep.created == [] and rep.reused


async def test_name_matching_is_exact_and_parent_aware():
    g = FakeGuild()
    await g.create_text_channel("timer-old")                  # similar name must NOT be reused
    await build(g, rooms=1)
    assert "timer-old" in names(g, "text")
    assert names(g, "text").count("timer") == 1


# 5. partial failure then resume
async def test_partial_build_resumes_without_duplicates():
    g = FakeGuild()
    g.fail["poi"] = [http_error(400, code=50035)]             # permanent failure in ROOM 01
    with pytest.raises(sb.BuildIncomplete) as ei:
        await build(g, rooms=2)
    assert any("poi" in what for what, _, _ in ei.value.report.failed)
    created_first = len(g.create_calls)
    rep = await build(g, rooms=2)                             # fault gone
    new = g.create_calls[created_first:]
    assert [n for _, n in new].count("poi") == 1          # only the failed one is retried (ROOM 02's was created)
    assert rep.ok
    assert len(g.channels) == EXPECTED_BASE + 2 * sb.room_channel_count("ap")
    assert names(g, "text").count("poi") == 2                 # one per room, no duplicates
    assert len(new) < 12                                      # only the missing pieces were created


# 8. one transient failure does not restart the workflow
async def test_transient_failure_retries_only_that_call():
    g = FakeGuild()
    g.fail["⚙️︱settings"] = [http_error(503)]
    await build(g, rooms=1)
    total = EXPECTED_BASE + sb.room_channel_count("ap")
    assert len(g.channels) == total
    assert len(g.create_calls) == total + len(sb.ROLE_CONFIG) + 1      # exactly one extra attempt


# 7. missing permissions: stop quickly instead of hammering
async def test_permission_errors_abort_quickly():
    g = FakeGuild()
    for n in sb.TOP_LEVEL_CHANNELS + ["⚜️︱ORGCOM", "🛅︱ASSIGN"]:
        g.fail[n] = [http_error(403)] * 5
    with pytest.raises(sb.BuildIncomplete) as ei:
        await build(g)
    assert "permission" in str(ei.value).lower()
    assert len(g.create_calls) <= len(sb.ROLE_CONFIG) + 4


async def test_role_failure_aborts_before_channels():
    g = FakeGuild()
    g.fail["ORG"] = [http_error(400)]
    with pytest.raises(sb.BuildIncomplete):
        await build(g)
    assert not g.channels


# 6. concurrent jobs are rejected, not run in parallel
async def test_concurrent_jobs_rejected():
    g = FakeGuild()
    async with sb.building_job(g):
        with pytest.raises(sb.BuildInProgress):
            async with sb.building_job(g):
                pass
    async with sb.building_job(g):      # released afterwards
        pass
    assert g.id not in sb._building_guilds


async def test_resume_marker_matches_params():
    g = FakeGuild()
    data = {"format": "ap", "rooms": 3, "tournament_name": "T", "timezone": "+06:00", "role_name": "ORG"}
    assert not await sb.find_resumable(g, data)
    await sb.mark_build_state(g, data, wiped=True)
    assert await sb.find_resumable(g, data)
    assert not await sb.find_resumable(g, {**data, "rooms": 4})
    assert await sb.find_resumable(g, {**data, "role_name": "CAP"})   # creator role is not structural


def test_capacity_limits():
    assert sb.max_rooms_for_fresh_build("ap") == 45 and sb.max_rooms_for_fresh_build("bp") == 45
    assert sb.capacity_problem_fresh_build("ap", 1000)
    assert sb.capacity_problem_fresh_build("bp", 45) is None


async def test_switch_format_idempotent():
    g = FakeGuild()
    await build(g, rooms=2, fmt="ap")
    await sb.switch_format_rooms(g, "bp", 2)
    assert names(g, "voice").count("OG PREP") == 2 and "GOVT PREP" not in names(g, "voice")
    before = len(g.create_calls)
    await sb.switch_format_rooms(g, "bp", 2)
    assert len(g.create_calls) == before


async def test_normalize_makes_no_requests_when_ordered():
    g = FakeGuild()
    await build(g, rooms=2)
    for i, c in enumerate(sorted(g.categories, key=lambda c: c.id)):
        c.position = i
    for cat in g.categories:
        for j, ch in enumerate(sorted(cat.channels, key=lambda c: c.id)):
            ch.position = j
    g.edits = 0
    # channel positions inside categories follow creation order == canonical order
    await sb.normalize_kamla_order(g)
    assert g.edits == 0
