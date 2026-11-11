import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import tabbycat_client as tc
from tabbycat_client import TabbycatClient, TabbycatError

SLUG = "kijani"


def build_app(token="secret-token", host_override=None):
    state = {"auth_seen": [], "hits": []}

    def u(request, path):
        base = host_override or f"{request.scheme}://{request.host}"
        return f"{base}/api/v1/tournaments/{SLUG}{path}"

    async def guard(request):
        state["hits"].append(request.path)
        state["auth_seen"].append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != f"Token {token}":
            raise web.HTTPUnauthorized()

    routes = web.RouteTableDef()
    root = f"/api/v1/tournaments/{SLUG}"

    @routes.get(root)
    async def tournament(request):
        await guard(request)
        return web.json_response({"name": "Kijani Open", "short_name": "Kijani", "slug": SLUG})

    @routes.get(root + "/rounds")
    async def rounds(request):
        await guard(request)
        return web.json_response([
            {"seq": 1, "name": "Round 1", "abbreviation": "R1", "stage": "P", "draw_status": "R", "motions_released": True,
             "completed": True, "silent": False,
             "motions": [{"seq": 1, "text": "THW ban X 🙂", "info_slide_plain": "Info here", "info_slide": "<p>Info here</p>"}]},
            {"seq": 2, "name": "Round 2", "stage": "P", "draw_status": "D", "motions_released": False, "completed": False,
             "silent": False, "motions": [{"seq": 1, "text": "SECRET"}]},
            {"seq": 3, "name": "Round 3", "stage": "P", "draw_status": "T", "motions_released": False, "completed": False,
             "silent": True, "motions": []},
        ])

    @routes.get(root + "/teams")
    async def teams(request):
        await guard(request)
        return web.json_response([
            {"id": i, "url": u(request, f"/teams/{i}"), "short_name": n} for i, n in
            enumerate(["Alpha", "Beta", "বাংলা দল", "Delta"], 1)])

    @routes.get(root + "/adjudicators")
    async def adjs(request):
        await guard(request)
        return web.json_response([{"url": u(request, f"/adjudicators/{i}"), "name": n}
                                  for i, n in enumerate(["Dr Chair", "Panel One", "Trainee T"], 1)])

    @routes.get(root + "/venues")
    async def venues(request):
        await guard(request)
        return web.json_response([{"url": u(request, f"/venues/{i}"), "name": f"Room {i}", "display_name": f"Room {i}"}
                                  for i in (1, 2)])

    @routes.get(root + "/rounds/{seq}/pairings")
    async def pairings(request):
        await guard(request)
        seq = request.match_info["seq"]
        if seq == "1":
            return web.json_response([
                {"id": 11, "venue": u(request, "/venues/1"), "bracket": 1.0, "room_rank": 2,
                 "teams": [{"team": u(request, "/teams/1"), "side": "aff"}, {"team": u(request, "/teams/2"), "side": "neg"}],
                 "adjudicators": {"chair": u(request, "/adjudicators/1"), "panellists": [u(request, "/adjudicators/2")],
                                  "trainees": [u(request, "/adjudicators/3")]}},
                {"id": 12, "venue": u(request, "/venues/2"), "bracket": 2.0, "room_rank": 1,
                 "teams": [{"team": u(request, "/teams/3"), "side": "aff"}, {"team": u(request, "/teams/4"), "side": "neg"}],
                 "adjudicators": None}])
        return web.json_response([
            {"id": 31, "venue": u(request, "/venues/1"),
             "teams": [{"team": u(request, f"/teams/{i}"), "side": s} for i, s in enumerate(["og", "oo", "cg", "co"], 1)],
             "adjudicators": {"chair": u(request, "/adjudicators/1"), "panellists": [], "trainees": []}}])

    @routes.get(root + "/rounds/1/pairings/{debate}/ballots")
    async def ballots(request):
        await guard(request)
        d = request.match_info["debate"]
        if d == "12":
            return web.json_response([{"version": 1, "confirmed": False, "discarded": False, "result": {"sheets": []}}])
        sheet = {"teams": [{"side": "aff", "team": u(request, "/teams/1"), "win": True, "score": 152.5},
                           {"side": "neg", "team": u(request, "/teams/2"), "win": False, "score": 148.0}]}
        return web.json_response([
            {"version": 1, "confirmed": False, "discarded": True, "result": {"sheets": [{"teams": [
                {"side": "aff", "team": u(request, "/teams/1"), "win": False, "score": 1}]}]}},
            {"version": 2, "confirmed": True, "discarded": False, "result": {"sheets": [sheet]}}])

    @routes.get(root + "/break-categories")
    async def cats(request):
        await guard(request)
        return web.json_response([{"url": u(request, "/break-categories/1"), "name": "Open", "break_size": 8,
                                   "_links": {"breaking_teams": u(request, "/break-categories/1/break")}}])

    @routes.get(root + "/break-categories/1/break")
    async def brk(request):
        await guard(request)
        return web.json_response([
            {"team": u(request, "/teams/2"), "rank": 2, "break_rank": 2, "remark": ""},
            {"team": u(request, "/teams/1"), "rank": 1, "break_rank": 1, "remark": ""},
            {"team": u(request, "/teams/3"), "rank": 3, "break_rank": None, "remark": ""},
            {"team": u(request, "/teams/4"), "rank": 4, "break_rank": 3, "remark": "C"}])

    @routes.get("/redirect/api/v1/tournaments/" + SLUG)
    async def redir(request):
        raise web.HTTPFound("http://169.254.169.254/latest/meta-data")

    @routes.get("/plain/api/v1/tournaments/" + SLUG)
    async def plain(request):
        return web.Response(text="<html>not json</html>")

    app = web.Application()
    app.add_routes(routes)
    return app, state


@pytest.fixture
async def server():
    app, state = build_app()
    srv = TestServer(app)
    await srv.start_server()
    yield srv, state
    await srv.close()


def client_for(srv, token="secret-token", prefix=""):
    return TabbycatClient(f"http://127.0.0.1:{srv.port}{prefix}", SLUG, token, allow_insecure=True)


# ── validation / SSRF ─────────────────────────────────────────────────────────

def test_url_validation():
    assert tc.validate_base_url("https://TAB.example.com/") == "https://tab.example.com"
    assert tc.validate_base_url("tab.example.com/api/v1") == "https://tab.example.com"
    assert tc.validate_base_url("https://tab.example.com:8443/sub/") == "https://tab.example.com:8443/sub"
    for bad in ("http://tab.example.com", "https://127.0.0.1", "https://10.0.0.5/x", "https://user:pw@tab.example.com",
                "", "https://tab.example.com/?x=1", "ftp://tab.example.com"):
        with pytest.raises(TabbycatError) as e:
            tc.validate_base_url(bad)
        assert e.value.kind == "config"


def test_slug_validation():
    assert tc.validate_slug(" kijani-2026/ ") == "kijani-2026"
    for bad in ("", "a b", "a/b", "../x", "x" * 101):
        with pytest.raises(TabbycatError):
            tc.validate_slug(bad)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.9", "100.64.0.1", "localhost"])
async def test_resolver_blocks_non_public_addresses(host):
    resolver = tc.SafeResolver()
    with pytest.raises(OSError):
        await resolver.resolve(host, 443)
    await resolver.close()


async def test_client_cannot_reach_internal_hosts():
    c = TabbycatClient("https://localhost", SLUG, "t")
    with pytest.raises(TabbycatError) as e:
        await c.tournament()
    assert e.value.kind == "network"
    await c.close()


# ── parsing against a realistic server ────────────────────────────────────────

async def test_rounds_and_availability(server):
    srv, state = server
    c = client_for(srv)
    rounds = await c.rounds()
    r1, r2, r3 = rounds
    assert r1.draw_available and r1.adjudicators_released and r1.motions_available and r1.results_available
    assert not r2.draw_available and not r2.motions_available and not r2.results_available     # draft: nothing public
    assert r3.draw_available and not r3.adjudicators_released and not r3.results_available      # teams only, silent
    assert r1.motions[0].text == "THW ban X 🙂" and r1.motions[0].info_slide == "Info here"      # emoji kept for the renderer
    assert state["auth_seen"][0] == "Token secret-token"
    await c.close()


async def test_pairings_sorted_and_named(server):
    srv, _ = server
    c = client_for(srv)
    r1 = (await c.rounds())[0]
    debates = await c.pairings(r1)
    assert [d.id for d in debates] == [12, 11]                       # bracket high -> low, room_rank
    d11 = debates[1]
    assert [(t.side, t.name) for t in d11.teams] == [("Gov", "Alpha"), ("Opp", "Beta")]
    assert (d11.venue, d11.chair, d11.panel, d11.trainees) == ("Room 1", "Dr Chair", ["Panel One"], ["Trainee T"])
    assert debates[0].teams[0].name == "বাংলা দল" and debates[0].chair == ""
    await c.close()


async def test_teams_only_round_hides_adjudicators(server):
    srv, _ = server
    c = client_for(srv)
    r3 = (await c.rounds())[2]
    debates = await c.pairings(r3)
    assert [t.side for t in debates[0].teams] == ["OG", "OO", "CG", "CO"]
    assert debates[0].chair == ""                                    # draw_status T: adjudicators not shown
    await c.close()


async def test_results_use_the_confirmed_ballot_only(server):
    srv, _ = server
    c = client_for(srv)
    r1 = (await c.rounds())[0]
    results = await c.results(r1)
    assert len(results) == 1                                         # debate 12 has no confirmed ballot yet
    res = results[0]
    assert res.venue == "Room 1"
    assert [(t.side, t.name, t.win, t.score) for t in res.teams] == [("Gov", "Alpha", True, 152.5), ("Opp", "Beta", False, 148.0)]
    await c.close()


async def test_break(server):
    srv, _ = server
    c = client_for(srv)
    cats = await c.break_categories()
    assert cats[0].name == "Open" and cats[0].break_size == 8
    teams = await c.breaking_teams(cats[0])
    assert [(t.rank, t.name) for t in teams] == [(1, "Alpha"), (2, "Beta")]   # capped / not-breaking excluded
    await c.close()


# ── failure modes ─────────────────────────────────────────────────────────────

async def test_wrong_token_is_reported_without_leaking_it(server):
    srv, _ = server
    c = client_for(srv, token="wrong-token-123")
    with pytest.raises(TabbycatError) as e:
        await c.tournament()
    assert e.value.kind == "auth" and "wrong-token-123" not in e.value.message
    await c.close()


async def test_redirects_are_never_followed(server):
    srv, _ = server
    c = client_for(srv, prefix="/redirect")
    with pytest.raises(TabbycatError) as e:
        await c.tournament()
    assert e.value.kind == "config"
    await c.close()


async def test_non_json_and_404(server):
    srv, _ = server
    c = client_for(srv, prefix="/plain")
    with pytest.raises(TabbycatError) as e:
        await c.tournament()
    assert e.value.kind == "badresponse"
    await c.close()
    c2 = TabbycatClient(f"http://127.0.0.1:{srv.port}", "nope", "secret-token", allow_insecure=True)
    with pytest.raises(TabbycatError) as e2:
        await c2.tournament()
    assert e2.value.kind == "notfound"
    await c2.close()


async def test_links_to_other_hosts_are_not_followed():
    app, state = build_app(host_override="http://169.254.169.254")     # a malicious server pointing at metadata
    srv = TestServer(app)
    await srv.start_server()
    c = client_for(srv)
    r1 = (await c.rounds())[0]
    debates = await c.pairings(r1)           # names resolve by PATH, nothing is fetched from the foreign host
    assert debates and all(h.startswith("/api/v1/tournaments/") for h in state["hits"])
    with pytest.raises(TabbycatError) as e:
        await c._get("http://169.254.169.254/latest/meta-data")
    assert e.value.kind == "badresponse"
    await c.close()
    await srv.close()


async def test_pagination(monkeypatch):
    app = web.Application()

    async def page1(request):
        return web.json_response({"results": [{"a": 1}], "next": f"http://{request.host}/p2"})

    async def page2(request):
        return web.json_response({"results": [{"a": 2}], "next": None})
    app.router.add_get("/api/v1/tournaments/kijani/teams", page1)
    app.router.add_get("/p2", page2)
    srv = TestServer(app)
    await srv.start_server()
    c = client_for(srv, token="")
    assert await c._get(c.root + "/teams") == [{"a": 1}, {"a": 2}]
    await c.close()
    await srv.close()


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_clean_text_keeps_bengali_joiners_and_emoji():
    assert tc.clean_text("  Team 🎉  ✨ A  ") == "Team 🎉 ✨ A"
    assert tc.strip_emoji("  Team 🎉  ✨ A  ") == "Team A"
    assert tc.clean_block("a  b\n\n\n\nc\r\nd") == "a b\n\nc\nd"
    assert tc.clean_text("র\u200d্য") == "র\u200d্য"                    # ZWJ is part of the Bengali letter


def test_side_labels():
    assert tc.side_label("aff") == "Gov" and tc.side_label("co") == "CO" and tc.side_label(1, 2) == "Opp"
    assert tc.side_label(2, 4) == "CG"


def test_multi_sheet_majority():
    names = {"/t/1": "A", "/t/2": "B"}
    sheets = [{"teams": [{"side": "aff", "team": "/t/1", "win": True, "score": 150}, {"side": "neg", "team": "/t/2", "win": False, "score": 140}]},
              {"teams": [{"side": "aff", "team": "/t/1", "win": True, "score": 152}, {"side": "neg", "team": "/t/2", "win": False, "score": 142}]},
              {"teams": [{"side": "aff", "team": "/t/1", "win": False, "score": 148}, {"side": "neg", "team": "/t/2", "win": True, "score": 149}]}]
    out = tc.summarise_sheets(sheets, names, lambda u: u, 2)
    assert [(t.name, t.win, round(t.score, 1)) for t in out] == [("A", True, 150.0), ("B", False, 143.7)]


async def test_malformed_data_becomes_a_friendly_error():
    from aiohttp import web as w
    app = w.Application()

    async def rounds(request):
        return w.json_response([{"name": "no seq here"}])               # KeyError territory
    app.router.add_get("/api/v1/tournaments/kijani/rounds", rounds)
    srv = TestServer(app)
    await srv.start_server()
    c = client_for(srv, token="")
    with pytest.raises(TabbycatError) as e:
        await c.rounds()
    assert e.value.kind == "badresponse"
    await c.close()
    await srv.close()
