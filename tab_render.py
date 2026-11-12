"""
tab_render.py — renders Tabbycat data as clean PNG images (no text output).

Fonts:  B.ttf  = Bangla (Kohinur Bangla)      E.ttf = English (Times-style serif)
        NotoColorEmoji.ttf = colour emoji (team emoji in draws / results / breaks)
        All are looked up in the project root (next to this file). If one is missing KAMLA falls back to
        a system font (for emoji: the emoji are simply left out) and logs a warning once.
Bengali needs the RAQM layout engine (libraqm + FriBiDi + HarfBuzz) to shape conjuncts correctly. Without
        it Pillow would draw broken letters, so a loud warning is logged at start-up (see check_fonts()).

Quality / size:
  * Everything is drawn at SCALE x the logical layout size (default 2x → 2800 px wide images), so text and
    emoji stay sharp when Discord shows or zooms the image.
  * There is NO fixed height and NO line limit: a draw, info slide or motion is as tall as its content.
    Only Discord's upload limit is respected — if a PNG would be too big (or absurdly tall) the scale is
    lowered step by step until it fits.

Every function returns a list of PNG byte strings (one per page) and is synchronous: call it with
asyncio.to_thread() so the event loop is never blocked.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont, features

from tabbycat_client import (EMOJI_RE, BreakCategoryInfo, BreakTeam, DebateInfo, MotionInfo, ResultInfo, RoundInfo,
                             SpeakerRow, strip_emoji)

log = logging.getLogger("kamla.render")

ROOT = Path(__file__).resolve().parent
E_CANDIDATES = [ROOT / "E.ttf",
                Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")]
B_CANDIDATES = [ROOT / "B.ttf",
                Path("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf"),
                Path("/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf")]
M_CANDIDATES = [ROOT / "NotoColorEmoji.ttf",
                Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
                Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"),
                Path("/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf")]
BUNDLED = {"B.ttf", "E.ttf", "NotoColorEmoji.ttf"}

# palette
BG, CARD, BORDER = "#EEF1F8", "#FFFFFF", "#D5DBEA"
NAVY, NAVY_2 = "#1F2A44", "#2B3A67"
TEXT, MUTED, ZEBRA = "#1B1F2A", "#6B7280", "#F3F5FB"
SIDE_COLORS = {"Gov": "#1D4ED8", "Opp": "#B91C1C", "OG": "#047857", "OO": "#B45309", "CG": "#1D4ED8", "CO": "#7E22CE"}
GOLD, GREEN = "#B7791F", "#047857"

# logical layout units (everything below is multiplied by the render scale when drawn)
WIDTH, MARGIN, PAD_X, PAD_Y = 1400, 36, 20, 16
HEADER_H, COLHEAD_H, FOOTER_H = 176, 74, 70
MAX_PAGE_H: Optional[int] = None          # None = never split a table into several pages

try:
    SCALE = max(1, min(4, int(os.environ.get("KAMLA_RENDER_SCALE", "2"))))
except ValueError:
    SCALE = 2
MEASURE = 2                                # text is measured at this scale so layout never depends on SCALE
MAX_BYTES = 9 * 1024 * 1024                # Discord's default upload limit is 10 MB
MAX_SIDE_PX = 16000                        # keep images displayable; lower the scale instead of cutting content

EMOJI_TILE_W, EMOJI_TILE_H, EMOJI_GLYPH_BOTTOM = 136, 128, 120    # NotoColorEmoji bitmap strike (size 109)
EMOJI_HEIGHT = 1.15                        # emoji tile height as a multiple of the font size


# ── fonts ─────────────────────────────────────────────────────────────────────

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def raqm_available() -> bool:
    return bool(features.check("raqm"))


@lru_cache(maxsize=None)
def _first_existing(kind: str) -> Optional[str]:
    cands = {"B": B_CANDIDATES, "E": E_CANDIDATES, "M": M_CANDIDATES}[kind]
    for path in cands:
        if path.exists():
            if path.name not in BUNDLED:
                _warn_once(f"fallback-{kind}", f"{kind} font not found in the project root; using fallback font {path.name}.")
            return str(path)
    _warn_once(f"missing-{kind}", f"No usable {kind} font found (add {'NotoColorEmoji' if kind == 'M' else kind}.ttf "
                                  f"to the project root).")
    return None


@lru_cache(maxsize=None)
def _font(kind: str, px: int) -> ImageFont.FreeTypeFont:
    path = _first_existing(kind) or _first_existing("E" if kind == "B" else "B")
    if path is None:
        return ImageFont.load_default(px)
    engine = ImageFont.Layout.RAQM if raqm_available() else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, px, layout_engine=engine)


@lru_cache(maxsize=1)
def _emoji_font() -> Optional[ImageFont.FreeTypeFont]:
    """The colour-bitmap emoji font at its only size (109). None if it is missing or unusable."""
    path = _first_existing("M")
    if path is None:
        return None
    try:
        engine = ImageFont.Layout.RAQM if raqm_available() else ImageFont.Layout.BASIC
        return ImageFont.truetype(path, 109, layout_engine=engine)
    except Exception as exc:  # noqa: BLE001  (e.g. a vector emoji font that has no 109px strike)
        _warn_once("emoji-load", f"Emoji font could not be used ({type(exc).__name__}); emoji will be left out.")
        return None


def emoji_available() -> bool:
    return _emoji_font() is not None


def check_fonts() -> dict[str, bool]:
    """Start-up diagnostics (also shown by /tab status)."""
    info = {
        "E.ttf": (ROOT / "E.ttf").exists(),
        "B.ttf": (ROOT / "B.ttf").exists(),
        "raqm": raqm_available(),
        "emoji": emoji_available(),
    }
    if not info["raqm"]:
        log.error("Pillow has no RAQM support: Bengali text would be drawn with broken letters. "
                  "Install it with:  sudo apt install -y libraqm0 libfribidi0 libharfbuzz0b  (then restart KAMLA).")
    if not info["B.ttf"]:
        log.warning("B.ttf (Bangla font) is not in the project root yet; a fallback font will be used.")
    if not info["E.ttf"]:
        log.warning("E.ttf (English font) is not in the project root yet; a fallback font will be used.")
    if not info["emoji"]:
        log.warning("NotoColorEmoji.ttf is not in the project root; team emoji will be left out of the images.")
    return info


def _prep(text: str) -> str:
    return text if emoji_available() else strip_emoji(text)


# ── text engine ───────────────────────────────────────────────────────────────

def _is_bengali(ch: str) -> bool:
    return "\u0980" <= ch <= "\u09FF"


def split_runs(text: str) -> list[tuple[str, str]]:
    """Split into (script, run): "B" Bengali, "E" everything else, "M" one emoji cluster per run.
    Neutral characters (space, digits, punctuation, ZWJ/ZWNJ) stay with the surrounding text run so Bengali
    words are never cut in the middle of a conjunct."""
    runs: list[tuple[str, str]] = []
    last_text_script = "E"

    def add_text(chunk: str) -> None:
        nonlocal last_text_script
        for ch in chunk:
            if _is_bengali(ch):
                script = "B"
            elif ch.isalpha():
                script = "E"
            else:
                script = last_text_script
            last_text_script = script
            if runs and runs[-1][0] == script:
                runs[-1] = (script, runs[-1][1] + ch)
            else:
                runs.append((script, ch))

    pos = 0
    for m in EMOJI_RE.finditer(text):
        add_text(text[pos:m.start()])
        runs.append(("M", m.group()))
        pos = m.end()
    add_text(text[pos:])
    return runs


@lru_cache(maxsize=2048)
def _emoji_tile(cluster: str, tile_h: int) -> Optional[Image.Image]:
    font = _emoji_font()
    if font is None:
        return None
    tile = Image.new("RGBA", (EMOJI_TILE_W + 24, EMOJI_TILE_H + 24), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((0, 0), cluster, font=font, embedded_color=True)
    tile = tile.crop((0, 0, EMOJI_TILE_W, EMOJI_TILE_H))
    tile_w = max(1, round(tile_h * EMOJI_TILE_W / EMOJI_TILE_H))
    return tile.resize((tile_w, tile_h), Image.LANCZOS)


def _emoji_tile_h(size: float, scale: int) -> int:
    return max(2, round(size * scale * EMOJI_HEIGHT))


def _run_width(script: str, run: str, size: float) -> float:
    """Logical width of one run."""
    if script == "M":
        tile = _emoji_tile(run, _emoji_tile_h(size, MEASURE))
        return (tile.width if tile else 0) / MEASURE
    return _font(script, round(size * MEASURE)).getlength(run) / MEASURE


def text_width(text: str, size: int) -> float:
    return sum(_run_width(script, run, size) for script, run in split_runs(_prep(text)))


def wrap_paragraphs(text: str, size: int, max_width: float, max_lines: Optional[int] = None) -> list[str]:
    """wrap() that keeps the author's line breaks: every paragraph is wrapped on its own and a blank paragraph
    becomes an empty line (used for info slides)."""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        lines.extend([""] if not para.strip() else wrap(para, size, max_width))
    while lines and lines[-1] == "":
        lines.pop()
    while lines and lines[0] == "":
        lines.pop(0)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines or [""]


def wrap(text: str, size: int, max_width: float, max_lines: Optional[int] = None) -> list[str]:
    """Greedy word wrap; over-long words are broken by character. max_lines=None (default) never truncates;
    with a limit the last line gets an ellipsis."""
    words = _prep(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if text_width(trial, size) <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while text_width(word, size) > max_width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and text_width(word[:cut], size) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_width(last + "…", size) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


class Canvas:
    """A drawing surface addressed in logical units; the pixels are scale x bigger."""

    def __init__(self, width: int, height: int, bg: str, scale: int) -> None:
        self.w, self.h, self.scale = width, height, scale
        self.img = Image.new("RGB", (width * scale, height * scale), bg)
        self.d = ImageDraw.Draw(self.img)

    def _p(self, v: float) -> int:
        return int(round(v * self.scale))

    def rect(self, box, fill) -> None:
        self.d.rectangle([self._p(v) for v in box], fill=fill)

    def rrect(self, box, radius, fill=None, outline=None, width=1) -> None:
        self.d.rounded_rectangle([self._p(v) for v in box], radius=self._p(radius), fill=fill, outline=outline,
                                 width=max(1, self._p(width)))

    def line(self, pts, fill, width=1) -> None:
        self.d.line([self._p(v) for v in pts], fill=fill, width=max(1, self._p(width)))

    def paste_rounded(self, other: "Canvas", xy, radius) -> None:
        mask = Image.new("L", other.img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, other.img.width - 1, other.img.height - 1],
                                               radius=self._p(radius), fill=255)
        self.img.paste(other.img, (self._p(xy[0]), self._p(xy[1])), mask)

    def text(self, xy: tuple[float, float], text: str, size: int, fill: str, bold: bool = False,
             anchor_right: bool = False) -> None:
        text = _prep(text)
        x, y = xy
        if anchor_right:
            x -= text_width(text, size)
        S = self.scale
        px = round(size * S)
        # One shared baseline for every run, so Bengali, English and emoji never sit at different heights.
        baseline = self._p(y) + _font("E", px).getmetrics()[0]
        xp = float(self._p(x))
        stroke = max(1, S // 2) if bold else 0
        for script, run in split_runs(text):
            if script == "M":
                tile = _emoji_tile(run, _emoji_tile_h(size, S))
                if tile is not None:
                    top = baseline + 0.05 * px - tile.height * EMOJI_GLYPH_BOTTOM / EMOJI_TILE_H
                    self.img.paste(tile, (int(round(xp)), int(round(top))), tile)
                    xp += tile.width
                continue
            font = _font(script, px)
            if stroke:
                self.d.text((xp, baseline), run, font=font, fill=fill, anchor="ls", stroke_width=stroke, stroke_fill=fill)
            else:
                self.d.text((xp, baseline), run, font=font, fill=fill, anchor="ls")
            xp += font.getlength(run)


def _encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _fit(build: Callable[[int], Canvas]) -> bytes:
    """Render at the highest scale that stays displayable and under Discord's upload limit."""
    scale = SCALE
    while True:
        canvas = build(scale)
        too_big_px = max(canvas.img.size) > MAX_SIDE_PX
        data = None if too_big_px and scale > 1 else _encode(canvas.img)
        if scale <= 1 or (data is not None and len(data) <= MAX_BYTES):
            return data if data is not None else _encode(canvas.img)
        scale -= 1


# ── table engine ──────────────────────────────────────────────────────────────

@dataclass
class Line:
    text: str
    size: int = 28
    color: str = TEXT
    bold: bool = False


@dataclass
class Column:
    header: str
    weight: float
    color: str = NAVY_2


Cell = list[Line]


def cell(text: str, size: int = 28, color: str = TEXT, bold: bool = False) -> Cell:
    return [Line(text, size, color, bold)]


@dataclass
class _LaidRow:
    cells: list[list[tuple[str, Line]]]      # per column: wrapped (text, line-style) pairs
    height: int


def _layout_rows(columns: list[Column], rows: list[list[Cell]], inner_w: int) -> tuple[list[int], list[_LaidRow]]:
    total = sum(c.weight for c in columns)
    widths = [int(inner_w * c.weight / total) for c in columns]
    widths[-1] += inner_w - sum(widths)
    laid = []
    for row in rows:
        out_cells, tallest = [], 0
        for col_idx, cell_lines in enumerate(row):
            avail = widths[col_idx] - 2 * PAD_X
            wrapped, h = [], 0
            for ln in cell_lines:
                for piece in wrap(ln.text, ln.size, avail):          # never truncated
                    wrapped.append((piece, ln))
                    h += int(ln.size * 1.38)
            out_cells.append(wrapped)
            tallest = max(tallest, h)
        laid.append(_LaidRow(out_cells, max(tallest, 40) + 2 * PAD_Y))
    return widths, laid


def _paginate(laid: list[_LaidRow]) -> list[list[_LaidRow]]:
    """One page unless MAX_PAGE_H is set (it is not by default: a table is as tall as it needs to be)."""
    if MAX_PAGE_H is None:
        return [laid]
    budget = MAX_PAGE_H - HEADER_H - COLHEAD_H - FOOTER_H - 2 * MARGIN
    pages, cur, used = [], [], 0
    for row in laid:
        if cur and used + row.height > budget:
            pages.append(cur)
            cur, used = [], 0
        cur.append(row)
        used += row.height
    if cur or not pages:
        pages.append(cur)
    return pages


def _header(c: Canvas, width: int, kicker: str, title: str, subtitle: str = "") -> None:
    c.rect([0, 0, width, HEADER_H], fill=NAVY)
    c.text((MARGIN + 6, 24), kicker.upper(), 24, "#AEB8D6")
    c.text((MARGIN + 6, 56), title, 58, "#FFFFFF", bold=True)
    if subtitle:
        c.text((MARGIN + 6, 128), subtitle, 26, "#C9D2EE")
    c.text((width - MARGIN - 6, 30), "KAMLA", 30, "#FFFFFF", bold=True, anchor_right=True)


def _footer(c: Canvas, width: int, y: int, page: int, pages: int) -> None:
    c.text((MARGIN + 6, y + 18), "Generated by KAMLA", 24, MUTED)
    if pages > 1:
        c.text((width - MARGIN - 6, y + 18), f"Page {page} / {pages}", 24, MUTED, anchor_right=True)


def render_table(kicker: str, title: str, subtitle: str, columns: list[Column], rows: list[list[Cell]]) -> list[bytes]:
    inner_w = WIDTH - 2 * MARGIN
    widths, laid = _layout_rows(columns, rows, inner_w)
    pages = _paginate(laid)
    images = []
    for number, page_rows in enumerate(pages, 1):
        body_h = sum(r.height for r in page_rows)
        height = HEADER_H + MARGIN + COLHEAD_H + body_h + MARGIN + FOOTER_H
        card_w, card_h = WIDTH - 2 * MARGIN, COLHEAD_H + body_h

        def build(scale: int, page_rows=page_rows, height=height, card_w=card_w, card_h=card_h, number=number) -> Canvas:
            cv = Canvas(WIDTH, height, BG, scale)
            _header(cv, WIDTH, kicker, title, subtitle)
            top = HEADER_H + MARGIN
            card = Canvas(card_w, card_h, CARD, scale)

            x = 0
            for col, w in zip(columns, widths):                       # column headings
                card.rect([x, 0, x + w, COLHEAD_H], fill=col.color)
                size = 30
                while size > 18 and text_width(col.header, size) > w - 2 * PAD_X:      # a heading must never be cut off
                    size -= 1
                card.text((x + PAD_X, (COLHEAD_H - size - 4) // 2 - 2), col.header, size, "#FFFFFF", bold=True)
                x += w

            y = COLHEAD_H
            for idx, row in enumerate(page_rows):
                if idx % 2 == 1:
                    card.rect([0, y, card_w, y + row.height], fill=ZEBRA)
                x = 0
                for col_cells, w in zip(row.cells, widths):
                    cy = y + PAD_Y
                    for text, ln in col_cells:
                        card.text((x + PAD_X, cy), text, ln.size, ln.color, ln.bold)
                        cy += int(ln.size * 1.38)
                    x += w
                y += row.height
                if idx < len(page_rows) - 1:
                    card.line([0, y, card_w, y], fill=BORDER, width=1)

            cv.paste_rounded(card, (MARGIN, top), 22)                 # rounded mask → clean corners on the heading cells
            cv.rrect([MARGIN, top, MARGIN + card_w - 1, top + card_h - 1], 22, outline=BORDER, width=2)
            _footer(cv, WIDTH, height - FOOTER_H, number, len(pages))
            return cv

        images.append(_fit(build))
    return images


# ── draw ──────────────────────────────────────────────────────────────────────

def _side_columns(debates: list[DebateInfo]) -> list[str]:
    n = max((len(d.teams) for d in debates), default=2)
    if n == 4:
        return ["OG", "OO", "CG", "CO"]
    if n == 2:
        return ["Gov", "Opp"]
    return [f"Team {i + 1}" for i in range(n)]


def render_draw(tournament: str, rnd: RoundInfo, debates: list[DebateInfo]) -> list[bytes]:
    sides = _side_columns(debates)
    show_adjs = rnd.adjudicators_released and any(d.chair or d.panel for d in debates)
    columns = [Column("Venue", 1.1)] + [Column(s, 2.0, SIDE_COLORS.get(s, NAVY_2)) for s in sides]
    if show_adjs:
        columns += [Column("Chair", 1.5), Column("Panel", 2.0)]
    rows = []
    for d in debates:
        by_side = {t.side: t.name for t in d.teams}
        row = [cell(d.venue or "—", 28, NAVY, bold=True)]
        for i, s in enumerate(sides):
            name = by_side.get(s) or (d.teams[i].name if len(d.teams) == len(sides) and i < len(d.teams) and s.startswith("Team") else "—")
            row.append(cell(name or "—", 28))
        if show_adjs:
            row.append(cell(d.chair or "—", 28))
            panel = list(d.panel) + [f"{t} (T)" for t in d.trainees]
            row.append(cell(", ".join(p for p in panel if p) or "—", 26, MUTED))
        rows.append(row)
    subtitle = f"{len(debates)} debates" + ("" if show_adjs else "  ·  adjudicators not released yet")
    return render_table(tournament or "Tournament", f"{rnd.name}  ·  Draw", subtitle, columns, rows)


# ── results ───────────────────────────────────────────────────────────────────

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _fmt_score(score: Optional[float]) -> str:
    if score is None:
        return ""
    return f"{score:.1f}".rstrip("0").rstrip(".") if abs(score - round(score)) > 1e-9 else str(int(round(score)))


def render_results(tournament: str, rnd: RoundInfo, results: list[ResultInfo]) -> list[bytes]:
    n = max((len(r.teams) for r in results), default=2)
    bp = n == 4
    sides = ["OG", "OO", "CG", "CO"] if bp else ["Gov", "Opp"] if n == 2 else [f"Team {i + 1}" for i in range(n)]
    columns = [Column("Venue", 1.0)] + [Column(s, 2.0, SIDE_COLORS.get(s, NAVY_2)) for s in sides]
    rows = []
    for res in results:
        by_side = {t.side: t for t in res.teams}
        ranked = sorted([t for t in res.teams if t.points is not None], key=lambda t: -t.points)
        rank_of = {id(t): i + 1 for i, t in enumerate(ranked)} if bp else {}
        row = [cell(res.venue or "—", 28, NAVY, bold=True)]
        for i, s in enumerate(sides):
            t = by_side.get(s) or (res.teams[i] if s.startswith("Team") and i < len(res.teams) else None)
            if t is None:
                row.append(cell("—"))
                continue
            win = bool(t.win) if t.win is not None else (rank_of.get(id(t)) == 1)
            lines = [Line(t.name, 28, TEXT, bold=win)]
            bits = []
            if bp and id(t) in rank_of:
                bits.append(_ORDINALS.get(rank_of[id(t)], f"{rank_of[id(t)]}th"))
            elif t.win is not None:
                bits.append("Win" if t.win else "Loss")
            if t.points is not None and bp:
                bits.append(f"{t.points} pts")
            if t.score is not None:
                bits.append(_fmt_score(t.score))
            if bits:
                lines.append(Line("  ·  ".join(bits), 24, GREEN if win else MUTED, bold=win))
            row.append(lines)
        rows.append(row)
    return render_table(tournament or "Tournament", f"{rnd.name}  ·  Results", f"{len(results)} debates", columns, rows)


# ── break ─────────────────────────────────────────────────────────────────────

def render_break(tournament: str, category: BreakCategoryInfo, teams: list[BreakTeam]) -> list[bytes]:
    columns = [Column("Rank", 0.8, GOLD), Column("Team", 5.0), Column("Note", 2.0)]
    rows = [[cell(str(t.rank), 30, GOLD, bold=True), cell(t.name, 30, TEXT, bold=t.rank <= 3), cell(t.remark or "", 26, MUTED)]
            for t in teams]
    size = f"top {category.break_size}  ·  " if category.break_size else ""
    return render_table(tournament or "Tournament", f"Break  ·  {category.name}", f"{size}{len(teams)} teams", columns, rows)


def render_speaker_break(tournament: str, title: str, subtitle: str, labels: list[str],
                         rows: list[SpeakerRow]) -> list[bytes]:
    """Top speakers: Rank, Speaker, Team and the standings' own metric columns (Total, Average ...)."""
    columns = [Column("Rank", 1.0, GOLD), Column("Speaker", 3.8), Column("Team", 3.0)]
    columns += [Column(label, 1.7) for label in labels]
    body = []
    for r in rows:
        line = [cell(str(r.rank), 30, GOLD, bold=True), cell(r.name, 30, TEXT, bold=r.rank <= 3),
                cell(r.team or "", 26, MUTED)]
        line += [cell(value, 28, TEXT) for _, value in r.metrics]
        body.append(line)
    return render_table(tournament or "Tournament", title, subtitle, columns, body)


def render_adjudicator_break(tournament: str, names: list[str]) -> list[bytes]:
    columns = [Column("#", 0.8, GOLD), Column("Adjudicator", 6.0)]
    rows = [[cell(str(i), 30, GOLD, bold=True), cell(n, 30, TEXT)] for i, n in enumerate(names, 1)]
    return render_table(tournament or "Tournament", "Break  ·  Adjudicators", f"{len(names)} adjudicators", columns, rows)


# ── motions & info slides ─────────────────────────────────────────────────────

MOTION_W = 1200
CARD_PAD = 44


@dataclass
class Block:
    lines: list[str]
    size: int
    color: str = TEXT
    bold: bool = False
    gap_before: int = 0
    divider: bool = False          # thin rule drawn in the gap above this block


def _inner_w() -> int:
    return MOTION_W - 2 * MARGIN - 2 * CARD_PAD - 12


def _text_card(tournament: str, title: str, blocks: list[Block], accent: str = "#3B6BFF") -> bytes:
    """One card, exactly as tall as its text — no line limit, no height limit."""
    body_h = 2 * CARD_PAD + sum(b.gap_before + len(b.lines) * int(b.size * 1.42) for b in blocks)
    height = HEADER_H + MARGIN + body_h + MARGIN + FOOTER_H

    def build(scale: int) -> Canvas:
        cv = Canvas(MOTION_W, height, BG, scale)
        _header(cv, MOTION_W, tournament or "Tournament", title)
        top = HEADER_H + MARGIN
        cv.rrect([MARGIN, top, MOTION_W - MARGIN, top + body_h], 22, fill=CARD, outline=BORDER, width=2)
        cv.rrect([MARGIN, top, MARGIN + 12, top + body_h], 6, fill=accent)
        x, y = MARGIN + CARD_PAD, top + CARD_PAD
        for b in blocks:
            if b.divider:
                cv.line([x, y + b.gap_before // 2, MOTION_W - MARGIN - CARD_PAD, y + b.gap_before // 2],
                        fill=BORDER, width=2)
            y += b.gap_before
            for line in b.lines:
                if line:
                    cv.text((x, y), line, b.size, b.color, b.bold)
                y += int(b.size * 1.42)
        _footer(cv, MOTION_W, height - FOOTER_H, 1, 1)
        return cv

    return _fit(build)


def motion_label(idx: int, total: int) -> str:
    return "Motion" if total == 1 else f"Motion {idx}"


def render_motions(tournament: str, rnd: RoundInfo, motions: list[MotionInfo], show_info: bool = True,
                   only: Optional[list[int]] = None) -> list[bytes]:
    """One image per motion. show_info=False leaves the info slide out (organisers can post it separately
    with render_info_slides). only = 1-based positions to render (default: all); the "Motion n" numbering
    always follows the position in the full list."""
    images = []
    for idx, m in enumerate(motions, 1):
        if only is not None and idx not in only:
            continue
        blocks = [Block(wrap_paragraphs(m.text or "—", 54, _inner_w()), 54, TEXT, True)]
        if show_info and m.info_slide:
            blocks.append(Block(["INFO SLIDE"], 24, MUTED, True, gap_before=48, divider=True))
            blocks.append(Block(wrap_paragraphs(m.info_slide, 34, _inner_w()), 34, "#374151", False, gap_before=8))
        images.append(_text_card(tournament, f"{rnd.name}  ·  {motion_label(idx, len(motions))}", blocks))
    return images


def render_info_slides(tournament: str, rnd: RoundInfo, motions: list[MotionInfo],
                       only: Optional[list[int]] = None) -> list[bytes]:
    """One image per motion that has an info slide (only = 1-based positions, default: all)."""
    images = []
    for idx, m in enumerate(motions, 1):
        if not m.info_slide or (only is not None and idx not in only):
            continue
        suffix = "" if len(motions) == 1 else f"  ·  {motion_label(idx, len(motions))}"
        images.append(_text_card(tournament, f"{rnd.name}  ·  Info Slide{suffix}",
                                 [Block(wrap_paragraphs(m.info_slide, 38, _inner_w()), 38, TEXT, False)],
                                 accent="#B7791F"))
    return images
