"""
tabbycat_client.py — a small, defensive client for the Tabbycat REST API (v1).

Endpoints used (all under  {base}/api/v1/tournaments/{slug}):
    ""                                   tournament name
    /rounds                              rounds, draw_status, motions (+ motions_released), completed, silent
    /rounds/{seq}/pairings               debates: venue, teams[side], adjudicators{chair,panellists,trainees}
    /rounds/{seq}/pairings/{id}/ballots  ballots: result.sheets[].teams[]{side,points,win,score}
    /teams  /adjudicators  /venues       lookups (the pairings only contain hyperlinks)
    /break-categories  (+ /break link)   break categories and breaking teams

Safety (organisers type the URL in, so this is treated as untrusted input):
  * https only, hostname only (no IP literals), no credentials in the URL;
  * every resolved address must be a public, global address (blocks localhost, private ranges and the
    cloud metadata address 169.254.169.254) — enforced inside the connector so DNS rebinding can't bypass it;
  * redirects are never followed; hyperlinks inside responses are re-based onto the configured host;
  * bounded timeouts, bounded response size, bounded concurrency; the token is never logged or echoed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("kamla.tabbycat")

MAX_BYTES = 20 * 1024 * 1024
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class TabbycatError(Exception):
    """kind: config | auth | notfound | ratelimit | network | server | badresponse"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ── input validation ──────────────────────────────────────────────────────────

def validate_base_url(url: str, *, allow_insecure: bool = False) -> str:
    """Return the normalised base URL (scheme://host[:port][/prefix], no trailing slash)."""
    raw = (url or "").strip()
    if not raw:
        raise TabbycatError("config", "The Tabbycat site URL is empty.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlparse(raw)
    if parts.scheme not in (("https", "http") if allow_insecure else ("https",)):
        raise TabbycatError("config", "The Tabbycat site URL must start with https://")
    host = parts.hostname
    if not host or parts.username or parts.password:
        raise TabbycatError("config", "The Tabbycat site URL is not valid (no username/password allowed).")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not allow_insecure:
            raise TabbycatError("config", "Please use the site's host name, not an IP address.")
    if parts.query or parts.fragment:
        raise TabbycatError("config", "The site URL must not contain ? or # parts.")
    path = parts.path.rstrip("/")
    for tail in ("/api/v1", "/api"):
        if path.endswith(tail):
            path = path[: -len(tail)]
    netloc = host + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{path}"


def validate_slug(slug: str) -> str:
    slug = (slug or "").strip().strip("/")
    if not SLUG_RE.match(slug):
        raise TabbycatError("config", "The tournament slug may only contain letters, numbers, - and _.")
    return slug


class SafeResolver(aiohttp.abc.AbstractResolver):
    """Resolve normally, but refuse any address that is not a public, global IP."""

    def __init__(self, allow_private: bool = False) -> None:
        self._inner = aiohttp.ThreadedResolver()
        self._allow_private = allow_private

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        infos = await self._inner.resolve(host, port, family)
        if not self._allow_private:
            for info in infos:
                if not ipaddress.ip_address(info["host"]).is_global:
                    raise OSError(f"blocked non-public address for {host}")
        return infos

    async def close(self) -> None:
        await self._inner.close()


# ── result models ─────────────────────────────────────────────────────────────

SIDE_LABELS = {"aff": "Gov", "neg": "Opp", "og": "OG", "oo": "OO", "cg": "CG", "co": "CO", "bye": "Bye"}
# Emoji (incl. flags, skin tones, ZWJ sequences, keycaps). U+200D (ZWJ) is only part of an emoji when it sits
# between two emoji; on its own it is kept because Bengali needs it (e.g. র‍্য).
_EBASE = ("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2B06\u2B07\u2B1B\u2B1C\u2934\u2935\u231A\u231B"
          "\u23E9-\u23FA\u24C2\u3030\u303D\u3297\u3299]")
_EMOD = "[\uFE0F\U0001F3FB-\U0001F3FF\U000E0020-\U000E007F]"
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"                                  # flags
    "|[0-9#*]\uFE0F?\u20E3"                                       # keycaps
    f"|{_EBASE}{_EMOD}*(?:\u200D{_EBASE}{_EMOD}*)*"               # pictographs, modifiers, ZWJ sequences
    "|[\u00A9\u00AE]\uFE0F"                                        # (c) / (R) only when asked to look like emoji
)
_STRAY_RE = re.compile("[\uFE0F\u20E3]")


def strip_emoji(value: Any) -> str:
    """Text with every emoji removed (used when no emoji font is installed)."""
    text = _STRAY_RE.sub("", EMOJI_RE.sub("", str(value or "")))
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_text(value: Any) -> str:
    """Display-safe single-line text: emoji are KEPT (the renderer draws them with a colour emoji font),
    whitespace is tidied."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_block(value: Any) -> str:
    """Like clean_text but keeps the author's paragraph structure (used for info slides)."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00A0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def side_label(side: Any, n_teams: int = 2) -> str:
    if isinstance(side, str):
        return SIDE_LABELS.get(side.lower(), side.upper())
    if isinstance(side, int):
        if n_teams == 2:
            return ["Gov", "Opp"][side] if 0 <= side < 2 else f"Side {side + 1}"
        if n_teams == 4:
            return ["OG", "OO", "CG", "CO"][side] if 0 <= side < 4 else f"Side {side + 1}"
        return f"Side {side + 1}"
    return ""


@dataclass
class MotionInfo:
    seq: int
    text: str
    info_slide: str = ""


@dataclass
class RoundInfo:
    seq: int
    name: str
    abbreviation: str = ""
    stage: str = "P"
    draw_status: str = "N"
    motions_released: bool = False
    completed: bool = False
    silent: bool = False
    motions: list[MotionInfo] = field(default_factory=list)

    @property
    def draw_available(self) -> bool:
        return self.draw_status in ("R", "T")

    @property
    def adjudicators_released(self) -> bool:
        return self.draw_status == "R"

    @property
    def motions_available(self) -> bool:
        return self.motions_released and bool(self.motions)

    @property
    def results_available(self) -> bool:
        return self.completed and not self.silent


@dataclass
class TeamSlot:
    side: str
    name: str


@dataclass
class DebateInfo:
    id: int
    venue: str
    teams: list[TeamSlot]
    chair: str = ""
    panel: list[str] = field(default_factory=list)
    trainees: list[str] = field(default_factory=list)
    bracket: Optional[float] = None
    room_rank: Optional[int] = None


@dataclass
class ResultTeam:
    side: str
    name: str
    points: Optional[int] = None
    win: Optional[bool] = None
    score: Optional[float] = None


@dataclass
class ResultInfo:
    debate_id: int
    venue: str
    teams: list[ResultTeam]


@dataclass
class BreakCategoryInfo:
    name: str
    break_size: int
    teams_path: str


@dataclass
class BreakTeam:
    rank: int
    name: str
    remark: str = ""


@dataclass
class SpeakerCategoryInfo:
    name: str
    slug: str


@dataclass
class SpeakerRow:
    rank: int
    name: str
    team: str
    metrics: list[tuple[str, str]]          # (label, formatted value), e.g. ("Total", "412.5")


METRIC_LABELS = {"total": "Total", "average": "Average", "avg": "Average", "trimmed_mean": "Trimmed mean",
                 "stdev": "Std dev", "count": "Speeches", "replies_avg": "Reply avg", "replies_stdev": "Reply std dev",
                 "replies_count": "Replies"}
MAX_METRIC_COLUMNS = 3
MAX_TOP_SPEAKERS = 100


def metric_label(slug: Any) -> str:
    slug = str(slug or "").strip()
    return METRIC_LABELS.get(slug.lower()) or slug.replace("_", " ").capitalize() or "Score"


def format_metric(value: Any) -> str:
    if value is None or value == "":
        return "–"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return clean_text(value) or "–"
    return f"{num:.2f}".rstrip("0").rstrip(".")


def _metrics_of(row: dict) -> list[tuple[str, Any]]:
    """Standings metrics as [(slug, value)], whether Tabbycat sent a list of {metric, value} or a mapping."""
    raw = row.get("metrics")
    out: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        out = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict):
                out.append((str(m.get("metric") or m.get("slug") or m.get("name") or ""), m.get("value")))
    return out


def _rank_of(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("rank", value.get("value"))
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


REMARKS = {"C": "capped", "I": "ineligible", "D": "different break", "d": "disqualified", "t": "lost coin toss", "w": "withdrawn"}


# ── client ────────────────────────────────────────────────────────────────────

def _safe_parse(fn):
    """Unexpected JSON shapes (a different Tabbycat version, a proxy error page...) must surface as a
    TabbycatError, not as a KeyError that leaves a Discord interaction hanging."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except TabbycatError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            log.warning("Unexpected Tabbycat data in %s: %s", fn.__name__, type(exc).__name__)
            raise TabbycatError("badresponse", "Tabbycat returned data in an unexpected format.")
    return wrapper


_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>|</\s*(p|div|li|h[1-6])\s*>")
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(value: Any) -> str:
    """info_slide (HTML/markdown) → plain text with paragraph breaks, used only when info_slide_plain is missing."""
    import html
    text = _BR_RE.sub("\n", str(value or ""))
    return html.unescape(_TAG_RE.sub("", text))


class TabbycatClient:
    def __init__(self, base_url: str, slug: str, token: str = "", *, allow_insecure: bool = False,
                 timeout: float = 15.0) -> None:
        self._insecure = allow_insecure
        self.base = validate_base_url(base_url, allow_insecure=allow_insecure)
        self.slug = validate_slug(slug)
        self._token = (token or "").strip()
        self._timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._sem = asyncio.Semaphore(4)
        self._names: Optional[dict[str, dict[str, str]]] = None
        self._adj_breaking: list[str] = []
        self._speakers: Optional[dict[str, tuple[str, str]]] = None
        self._host = urlparse(self.base).netloc

    # -- plumbing --------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Accept": "application/json", "User-Agent": "KAMLA-Discord-Bot/1.0"}
            if self._token:
                headers["Authorization"] = f"Token {self._token}"
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=SafeResolver(allow_private=self._insecure), limit=8),
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers=headers,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def root(self) -> str:
        return f"/api/v1/tournaments/{self.slug}"

    def _rebase(self, url_or_path: str) -> str:
        """Hyperlinks inside responses are re-based on the configured host (never trust another host)."""
        parsed = urlparse(url_or_path)
        if parsed.netloc and parsed.netloc != self._host:
            raise TabbycatError("badresponse", "The Tabbycat site returned a link to a different host; ignored.")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return self.base + path if path.startswith("/") else self.base + "/" + path

    async def _request(self, url: str) -> Any:
        async with self._sem:
            for attempt in (1, 2):
                try:
                    async with self._get_session().get(url, allow_redirects=False) as resp:
                        status = resp.status
                        if status == 429 and attempt == 1:
                            wait = min(float(resp.headers.get("Retry-After", "2") or 2), 5.0)
                            await asyncio.sleep(wait)
                            continue
                        if 300 <= status < 400:
                            raise TabbycatError("config", "The Tabbycat site redirected the request. "
                                                          "Use the final https:// address of the site.")
                        if status in (401, 403):
                            raise TabbycatError("auth", "Tabbycat refused access. Check the API token (or the token's "
                                                        "permissions), and that the API is enabled for the tournament.")
                        if status == 404:
                            raise TabbycatError("notfound", "Not found on the Tabbycat site (check the URL and slug).")
                        if status == 429:
                            raise TabbycatError("ratelimit", "The Tabbycat site is rate limiting requests. Try again shortly.")
                        if status >= 500:
                            raise TabbycatError("server", f"The Tabbycat site had an error (HTTP {status}).")
                        if status != 200:
                            raise TabbycatError("badresponse", f"Unexpected response from Tabbycat (HTTP {status}).")
                        body = await resp.content.read(MAX_BYTES + 1)
                        if len(body) > MAX_BYTES:
                            raise TabbycatError("badresponse", "The Tabbycat response was too large.")
                        try:
                            return json.loads(body)
                        except ValueError:
                            raise TabbycatError("badresponse", "The site did not return JSON — is this a Tabbycat URL?")
                except TabbycatError:
                    raise
                except asyncio.TimeoutError:
                    raise TabbycatError("network", "The Tabbycat site took too long to respond.")
                except (aiohttp.ClientError, OSError) as exc:
                    log.warning("Tabbycat request failed (%s)", type(exc).__name__)
                    raise TabbycatError("network", "Could not reach the Tabbycat site (or its address is not allowed).")
            raise TabbycatError("ratelimit", "The Tabbycat site is rate limiting requests. Try again shortly.")

    async def _get(self, path: str) -> Any:
        data = await self._request(self._rebase(path))
        # tolerate paginated responses {"results": [...], "next": url}
        if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
            results = list(data["results"])
            nxt = data.get("next")
            guard = 0
            while nxt and guard < 50:
                page = await self._request(self._rebase(nxt))
                results += page.get("results", [])
                nxt = page.get("next")
                guard += 1
            return results
        return data

    # -- lookups ---------------------------------------------------------------

    @staticmethod
    def _key(url: Any) -> str:
        return urlparse(str(url or "")).path.rstrip("/")

    @_safe_parse
    async def tournament(self) -> dict:
        data = await self._get(self.root)
        if not isinstance(data, dict) or "name" not in data:
            raise TabbycatError("badresponse", "That address does not look like a Tabbycat tournament.")
        return data

    @_safe_parse
    async def names(self, refresh: bool = False) -> dict[str, dict[str, str]]:
        """teams / adjudicators / venues: hyperlink path -> display name."""
        if self._names is not None and not refresh:
            return self._names
        teams, adjs, venues = await asyncio.gather(
            self._get(self.root + "/teams"), self._get(self.root + "/adjudicators"), self._get(self.root + "/venues"))
        out: dict[str, dict[str, str]] = {"teams": {}, "adjs": {}, "venues": {}}
        for t in teams if isinstance(teams, list) else []:
            name = clean_text(t.get("short_name") or t.get("long_name") or t.get("reference")
                              or t.get("code_name") or f"Team {t.get('id', '?')}")
            emoji = clean_text(t.get("emoji"))
            if emoji and not EMOJI_RE.search(name):               # Tabbycat keeps the team emoji in its own field
                name = f"{emoji} {name}"
            out["teams"][self._key(t.get("url"))] = name
        breaking: list[str] = []
        for a in adjs if isinstance(adjs, list) else []:
            adj_name = clean_text(a.get("name") or "")
            out["adjs"][self._key(a.get("url"))] = adj_name
            if a.get("breaking") is True and adj_name:              # marked "breaking" on Tabbycat's Feedback page
                breaking.append(adj_name)
        self._adj_breaking = breaking
        for v in venues if isinstance(venues, list) else []:
            out["venues"][self._key(v.get("url"))] = clean_text(v.get("display_name") or v.get("name") or "")
        self._names = out
        return out

    def forget_names(self) -> None:
        self._names = None
        self._speakers = None

    # -- rounds / motions ------------------------------------------------------

    @_safe_parse
    async def rounds(self) -> list[RoundInfo]:
        data = await self._get(self.root + "/rounds")
        if not isinstance(data, list):
            raise TabbycatError("badresponse", "Unexpected rounds data from Tabbycat.")
        rounds = []
        for r in data:
            motions = [
                MotionInfo(seq=int(m.get("seq") or i + 1),
                           text=clean_text(m.get("text")),
                           info_slide=clean_block(m.get("info_slide_plain") or _html_to_text(m.get("info_slide"))))
                for i, m in enumerate(r.get("motions") or []) if isinstance(m, dict)
            ]
            rounds.append(RoundInfo(
                seq=int(r["seq"]),
                name=clean_text(r.get("name") or f"Round {r['seq']}"),
                abbreviation=clean_text(r.get("abbreviation")),
                stage=r.get("stage") or "P",
                draw_status=r.get("draw_status") or "N",
                motions_released=bool(r.get("motions_released")),
                completed=bool(r.get("completed")),
                silent=bool(r.get("silent")),
                motions=sorted(motions, key=lambda m: m.seq),
            ))
        return sorted(rounds, key=lambda r: r.seq)

    # -- draw ------------------------------------------------------------------

    @_safe_parse
    async def pairings(self, rnd: RoundInfo) -> list[DebateInfo]:
        raw, names = await asyncio.gather(self._get(f"{self.root}/rounds/{rnd.seq}/pairings"), self.names())
        if not isinstance(raw, list):
            raise TabbycatError("badresponse", "Unexpected pairings data from Tabbycat.")
        debates = []
        for d in raw:
            teams_raw = d.get("teams") or []
            slots = [
                TeamSlot(side=side_label(t.get("side"), len(teams_raw)),
                         name=names["teams"].get(self._key(t.get("team")), "TBA"))
                for t in teams_raw
            ]
            adjs = d.get("adjudicators") if rnd.adjudicators_released else None
            adjs = adjs or {}
            debates.append(DebateInfo(
                id=int(d.get("id") or 0),
                venue=names["venues"].get(self._key(d.get("venue")), "") if d.get("venue") else "",
                teams=slots,
                chair=names["adjs"].get(self._key(adjs.get("chair")), "") if adjs.get("chair") else "",
                panel=[names["adjs"].get(self._key(u), "") for u in adjs.get("panellists") or []],
                trainees=[names["adjs"].get(self._key(u), "") for u in adjs.get("trainees") or []],
                bracket=d.get("bracket"),
                room_rank=d.get("room_rank"),
            ))
        return sort_debates(debates)

    # -- results ---------------------------------------------------------------

    @_safe_parse
    async def results(self, rnd: RoundInfo) -> list[ResultInfo]:
        names = await self.names()
        raw = await self._get(f"{self.root}/rounds/{rnd.seq}/pairings")
        debates = {int(d.get("id") or 0): d for d in raw if isinstance(d, dict)}

        async def one(debate_id: int, d: dict) -> Optional[ResultInfo]:
            try:
                ballots = await self._get(f"{self.root}/rounds/{rnd.seq}/pairings/{debate_id}/ballots")
            except TabbycatError as exc:
                if exc.kind in ("notfound", "auth"):
                    return None
                raise
            ballot = pick_confirmed_ballot(ballots)
            if ballot is None:
                return None
            teams = summarise_sheets((ballot.get("result") or {}).get("sheets") or [], names["teams"], self._key,
                                     len(d.get("teams") or []))
            venue = names["venues"].get(self._key(d.get("venue")), "") if d.get("venue") else ""
            return ResultInfo(debate_id=debate_id, venue=venue, teams=teams) if teams else None

        done = await asyncio.gather(*(one(i, d) for i, d in debates.items()))
        results = [r for r in done if r is not None]
        order = {d.id: n for n, d in enumerate(sort_debates(
            [DebateInfo(id=i, venue="", teams=[], bracket=d.get("bracket"), room_rank=d.get("room_rank"))
             for i, d in debates.items()]))}
        return sorted(results, key=lambda r: order.get(r.debate_id, 0))

    # -- break -----------------------------------------------------------------

    @_safe_parse
    async def break_categories(self) -> list[BreakCategoryInfo]:
        data = await self._get(self.root + "/break-categories")
        out = []
        for c in data if isinstance(data, list) else []:
            links = c.get("_links") or {}
            path = self._key(links.get("breaking_teams") or (str(c.get("url", "")).rstrip("/") + "/break"))
            out.append(BreakCategoryInfo(name=clean_text(c.get("name") or "Break"),
                                         break_size=int(c.get("break_size") or 0), teams_path=path))
        return out

    @_safe_parse
    async def breaking_teams(self, category: BreakCategoryInfo) -> list[BreakTeam]:
        data = await self._get(category.teams_path)
        names = await self.names()
        teams = []
        for b in data if isinstance(data, list) else []:
            rank = b.get("break_rank")
            remark = b.get("remark") or ""
            if rank is None or remark:
                continue                                  # not (or no longer) in the break
            teams.append(BreakTeam(rank=int(rank), name=names["teams"].get(self._key(b.get("team")), "TBA"),
                                   remark=REMARKS.get(remark, remark)))
        return sorted(teams, key=lambda t: t.rank)


    @_safe_parse
    async def breaking_adjudicators(self) -> list[str]:
        """Adjudicators marked 'breaking' in Tabbycat, alphabetical. Costs no extra request: it comes from the
        adjudicator list that names() already loads."""
        await self.names()
        return sorted(self._adj_breaking, key=natural_key)

    @_safe_parse
    async def speaker_categories(self) -> list[SpeakerCategoryInfo]:
        data = await self._get(self.root + "/speaker-categories")
        out = []
        for c in data if isinstance(data, list) else []:
            slug = str(c.get("slug") or "").strip()
            if slug:
                out.append(SpeakerCategoryInfo(name=clean_text(c.get("name") or slug), slug=slug))
        return out

    async def _speaker_names(self) -> dict[str, tuple[str, str]]:
        """speaker hyperlink path -> (name, team hyperlink path); one request, kept until the next refresh."""
        if self._speakers is None:
            data = await self._get(self.root + "/speakers")
            self._speakers = {self._key(sp.get("url")): (clean_text(sp.get("name") or ""), self._key(sp.get("team")))
                              for sp in (data if isinstance(data, list) else []) if isinstance(sp, dict)}
        return self._speakers

    @_safe_parse
    async def speaker_standings(self, category_slug: Optional[str] = None,
                                top: int = 10) -> tuple[list[str], list[SpeakerRow]]:
        """The top speakers (ties at the last place included), overall or for one speaker category.
        Returns (metric column labels, rows)."""
        from urllib.parse import quote
        top = max(1, min(int(top), MAX_TOP_SPEAKERS))
        path = self.root + "/speakers/standings"
        if category_slug:
            path += "?category=" + quote(category_slug, safe="")
        data = await self._get(path)
        if not isinstance(data, list):
            raise TabbycatError("badresponse", "Unexpected speaker standings data from Tabbycat.")
        speakers, names = await asyncio.gather(self._speaker_names(), self.names())
        rows: list[tuple[int, int, dict]] = []
        for idx, row in enumerate(r for r in data if isinstance(r, dict)):
            rank = _rank_of(row.get("rank"))
            rows.append((rank if rank is not None else idx + 1, idx, row))
        rows.sort(key=lambda t: (t[0], t[1]))
        out: list[SpeakerRow] = []
        labels: list[str] = []
        for rank, _, row in rows:
            if rank > top or len(out) >= MAX_TOP_SPEAKERS:
                break
            name, team_key = speakers.get(self._key(row.get("speaker")), ("", ""))
            name = name or clean_text(row.get("speaker_name") or row.get("name") or "") or "TBA"
            team = names["teams"].get(team_key) or names["teams"].get(self._key(row.get("team")), "")
            metrics = _metrics_of(row)[:MAX_METRIC_COLUMNS]
            if not labels:
                labels = [metric_label(slug) for slug, _ in metrics]
            values = [format_metric(v) for _, v in metrics]
            values += ["–"] * (len(labels) - len(values))
            out.append(SpeakerRow(rank=rank, name=name, team=team, metrics=list(zip(labels, values[:len(labels)]))))
        return labels, out


# ── pure helpers (unit-tested) ────────────────────────────────────────────────

def natural_key(text: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text or "")]


def sort_debates(debates: list[DebateInfo]) -> list[DebateInfo]:
    if debates and all(d.room_rank is not None for d in debates):
        return sorted(debates, key=lambda d: (-(d.bracket or 0), d.room_rank or 0, d.id))
    return sorted(debates, key=lambda d: (natural_key(d.venue), d.id))


def pick_confirmed_ballot(ballots: Any) -> Optional[dict]:
    if not isinstance(ballots, list):
        return None
    good = [b for b in ballots if isinstance(b, dict) and b.get("confirmed") and not b.get("discarded")]
    return max(good, key=lambda b: b.get("version") or 0) if good else None


def summarise_sheets(sheets: list, team_names: dict[str, str], key, n_teams: int) -> list[ResultTeam]:
    """One sheet (consensus / single adjudicator) is used as is. With several sheets, wins are decided by
    majority and scores are averaged, so the picture matches what the tab shows overall."""
    if not sheets:
        return []
    per_side: dict[Any, list[dict]] = {}
    order: list[Any] = []
    for sheet in sheets:
        for t in sheet.get("teams") or []:
            side = t.get("side")
            if side not in per_side:
                per_side[side] = []
                order.append(side)
            per_side[side].append(t)
    teams = []
    for side in order:
        entries = per_side[side]
        first = entries[0]
        wins = [e.get("win") for e in entries if e.get("win") is not None]
        scores = [e.get("score") for e in entries if e.get("score") is not None]
        points = [e.get("points") for e in entries if e.get("points") is not None]
        teams.append(ResultTeam(
            side=side_label(side, n_teams or len(order)),
            name=team_names.get(key(first.get("team")), "TBA"),
            points=round(sum(points) / len(points)) if points else None,
            win=(sum(1 for w in wins if w) * 2 > len(wins)) if wins else None,
            score=(sum(scores) / len(scores)) if scores else None,
        ))
    return teams
