"""naming.py — the ONE place that defines how KAMLA names its channels and categories.

Every channel / category KAMLA creates or manages is called ``{emoji}︱{channel-name}`` (the separator is
U+FE31 PRESENTATION FORM FOR VERTICAL EM DASH, the same character the original server layout used).

Anything that has to *recognise* a KAMLA channel goes through :func:`base_name` / :func:`is_kind` instead of
comparing literal strings, so the emoji can change without breaking the timer, All-In, draw, restore guards…
"""

from __future__ import annotations

import re

SEP = "︱"


def chan(emoji: str, name: str) -> str:
    """``chan("⏱️", "timer")`` -> ``"⏱️︱timer"``."""
    return f"{emoji}{SEP}{name}"


def base_name(name: str) -> str:
    """The part after the first separator (``"⏱️︱timer"`` -> ``"timer"``); a name without one is returned as is."""
    head, sep, tail = (name or "").partition(SEP)
    return tail if sep else head


def is_kind(name: str, kind: str) -> bool:
    """True for ``kind`` itself and for the emoji-prefixed form (case-insensitive)."""
    return base_name(name).strip().lower() == kind.lower()


def valid_discord_name(name: str) -> bool:
    """Discord accepts 1-100 characters; text-channel names may not contain spaces (Discord would rewrite them)."""
    return 1 <= len(name) <= 100 and name == name.strip()


# ── installation / management channels ──────────────────────────────────────────────────────────────────
CH_CONFIG = chan("⚙️", "kamla-config")
CH_STATUS = chan("📊", "server-status")

# ── top-level channels ──────────────────────────────────────────────────────────────────────────────────
CH_DEVELOPERS = chan("👨‍💻", "meet-our-developers")
CH_MEMBERS = chan("👥", "members-update")
CH_GET_ROLE = chan("🎭", "get-role")
CH_HOW_TO = chan("📖", "how-to-use-this-server")
CH_LINKS = chan("🔗", "links")

# ── security ─────────────────────────────────────────────────────────────────────────────────────────────
CAT_SECURITY = chan("🛡️", "SECURITY")
CH_SECURITY = chan("🚨", "security-alert")

# ── categories ──────────────────────────────────────────────────────────────────────────────────────────
CAT_ORGCOM = chan("⚜️", "ORGCOM")
CAT_GA = chan("🏟️", "GRAND AUDITORIUM")
CAT_PLAY = chan("🎮", "PLAY")
CAT_INFO = chan("ℹ️", "INFORMATION")

# ── ORGCOM ──────────────────────────────────────────────────────────────────────────────────────────────
CH_SETTINGS = chan("🛠️", "settings")
CH_ORG = chan("🏴", "org")
CH_DOCUMENT = chan("📂", "document")
VC_ORG = chan("🎙️", "ORG")
VC_CONTROL = chan("🎛️", "Control Room")

# ── GRAND AUDITORIUM ────────────────────────────────────────────────────────────────────────────────────
CH_GA_TEXT = chan("📨", "ga-text")
CH_MOTION = chan("🎓", "motion")
CH_ANNOUNCEMENT = chan("🎓", "announcement")
CH_BREAK = chan("🎓", "break")
CH_MATCHUP = chan("🎓", "matchup")
CH_BALLOT = chan("🎓", "ballot")
CH_POLL = chan("📊", "poll")
CH_YES_NO = chan("📊", "yes-no-voting")
VC_GA = chan("🏟️", "GRAND AUDITORIUM")
VC_CLASH = chan("😤", "CLASH-EQUITY ROOM")

# ── PLAY ────────────────────────────────────────────────────────────────────────────────────────────────
CH_TTT = chan("❌", "tic-tac-toe") + SEP + "⭕"
CH_RPS = chan("🤛", "rock✊-paper📰-scissors✌️")
CH_TOSS = chan("🪙", "toss-coin")

# ── INFORMATION ─────────────────────────────────────────────────────────────────────────────────────────
CH_SCHEDULE = chan("📅", "schedule")
CH_FORMS = chan("📝", "important-forms")
CH_DEBATER_BRIEF = chan("🗣️", "debater-briefing")
CH_JUDGE_BRIEF = chan("📜", "judge-briefing")
CH_EQUITY_BRIEF = chan("🤝", "equity-briefing")

# ── ROOM XX ─────────────────────────────────────────────────────────────────────────────────────────────
CH_TIMER = chan("⏱️", "timer")
VC_DEBATE = chan("🎤", "DEBATE ROOM")
VC_ADJ = chan("⚖️", "ADJUDICATION")
ROOM_EMOJI = "🚪"
PREP_EMOJI = "🔊"

ROOM_CATEGORY_RE = re.compile(r"^(?:[^︱]+︱)?ROOM (?P<number>\d{2})$")


def room_category(index: int) -> str:
    return chan(ROOM_EMOJI, f"ROOM {index:02d}")


def prep_name(base: str) -> str:
    """``prep_name("OG PREP")`` -> ``"🔊︱OG PREP"``."""
    return chan(PREP_EMOJI, base)


AP_PREP = ("GOVT PREP", "OPP PREP")
BP_PREP = ("OG PREP", "OO PREP", "CG PREP", "CO PREP")

# Channels that must never accept a message from a human, the server owner included.
READ_ONLY_CHANNELS = frozenset({CH_MEMBERS, CH_CONFIG, CH_SETTINGS, CH_DEVELOPERS})

# Pre-rename (legacy) names -> current names. Used by the migration that renames, never deletes, KAMLA's channels.
LEGACY_RENAMES = {
    "🔒︱kamla-config": CH_CONFIG,
    "⚙️︱settings": CH_SETTINGS,
    "👐🏻︱meet-the-developer": CH_DEVELOPERS,
    "👋🏻︱welcome": CH_MEMBERS,
    "❓︱get-role": CH_GET_ROLE,
    "🦧︱how-to-use-this-server": CH_HOW_TO,
    "ORG": VC_ORG,
    "Control Room": VC_CONTROL,
    "schedule": CH_SCHEDULE,
    "important-forms": CH_FORMS,
    "debater-briefing": CH_DEBATER_BRIEF,
    "judge-briefing": CH_JUDGE_BRIEF,
    "equity-briefing": CH_EQUITY_BRIEF,
}
LEGACY_ROOM_CHILDREN = {
    "timer": CH_TIMER,
    "DEBATE ROOM": VC_DEBATE,
    "ADJUDICATION": VC_ADJ,
    **{p: prep_name(p) for p in (*AP_PREP, *BP_PREP)},
}
