"""
tab_render.py — renders Tabbycat data as clean PNG images (no text output).

Fonts:  B.ttf  = Bangla (Kohinur Bangla)      E.ttf = English (Times-style serif)
        Both are looked up in the project root (next to this file). If one is missing KAMLA falls back to
        a system font and logs a warning once — images are still produced, they just look different.
Bengali needs the RAQM layout engine (libraqm + FriBiDi + HarfBuzz) to shape conjuncts correctly. Without
        it Pillow would draw broken letters, so a loud warning is logged at start-up (see check_fonts()).

Every function returns a list of PNG byte strings (one per page) and is synchronous: call it with
asyncio.to_thread() so the event loop is never blocked.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, features

from tabbycat_client import BreakCategoryInfo, BreakTeam, DebateInfo, MotionInfo, ResultInfo, RoundInfo

log = logging.getLogger("kamla.render")

ROOT = Path(__file__).resolve().parent
E_CANDIDATES = [ROOT / "E.ttf",
                Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")]
B_CANDIDATES = [ROOT / "B.ttf",
                Path("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf"),
                Path("/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf")]

# palette
BG, CARD, BORDER = "#EEF1F8", "#FFFFFF", "#D5DBEA"
NAVY, NAVY_2 = "#1F2A44", "#2B3A67"
TEXT, MUTED, ZEBRA = "#1B1F2A", "#6B7280", "#F3F5FB"
SIDE_COLORS = {"Gov": "#1D4ED8", "Opp": "#B91C1C", "OG": "#047857", "OO": "#B45309", "CG": "#1D4ED8", "CO": "#7E22CE"}
GOLD, GREEN = "#B7791F", "#047857"

WIDTH, MARGIN, PAD_X, PAD_Y = 1400, 36, 20, 16
HEADER_H, COLHEAD_H, FOOTER_H = 176, 74, 70
MAX_PAGE_H = 2100


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
    for path in (B_CANDIDATES if kind == "B" else E_CANDIDATES):
        if path.exists():
            if path.name not in ("B.ttf", "E.ttf"):
                _warn_once(f"fallback-{kind}", f"{kind}.ttf not found in the project root; using fallback font {path.name}.")
            return str(path)
    _warn_once(f"missing-{kind}", f"No usable {kind} font found (add {kind}.ttf to the project root).")
    return None


@lru_cache(maxsize=None)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = _first_existing(kind) or _first_existing("E" if kind == "B" else "B")
    if path is None:
        return ImageFont.load_default(size)
    engine = ImageFont.Layout.RAQM if raqm_available() else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, size, layout_engine=engine)


def check_fonts() -> dict[str, bool]:
    """Start-up diagnostics (also shown by /tab status)."""
    info = {
        "E.ttf": (ROOT / "E.ttf").exists(),
        "B.ttf": (ROOT / "B.ttf").exists(),
        "raqm": raqm_available(),
    }
    if not info["raqm"]:
        log.error("Pillow has no RAQM support: Bengali text would be drawn with broken letters. "
                  "Install it with:  sudo apt install -y libraqm0 libfribidi0 libharfbuzz0b  (then restart KAMLA).")
    if not info["B.ttf"]:
        log.warning("B.ttf (Bangla font) is not in the project root yet; a fallback font will be used.")
    if not info["E.ttf"]:
        log.warning("E.ttf (English font) is not in the project root yet; a fallback font will be used.")
    return info


def _is_bengali(ch: str) -> bool:
    return "\u0980" <= ch <= "\u09FF"


def split_runs(text: str) -> list[tuple[str, str]]:
    """Split into (script, run). Neutral characters (space, digits, punctuation, ZWJ/ZWNJ) stay with the
    surrounding run so Bengali words are never cut in the middle of a conjunct."""
    runs: list[tuple[str, str]] = []
    for ch in text:
        if _is_bengali(ch):
            script = "B"
        elif ch.isalpha():
            script = "E"
        else:
            script = runs[-1][0] if runs else "E"
        if runs and runs[-1][0] == script:
            runs[-1] = (script, runs[-1][1] + ch)
        else:
            runs.append((script, ch))
    return runs


def text_width(text: str, size: int) -> float:
    return sum(_font(script, size).getlength(run) for script, run in split_runs(text))


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, size: int, fill: str,
              bold: bool = False, anchor_right: bool = False) -> None:
    x, y = xy
    if anchor_right:
        x -= text_width(text, size)
    # One shared baseline for every run, so Bengali and English text never sit at different heights.
    baseline = y + _font("E", size).getmetrics()[0]
    for script, run in split_runs(text):
        font = _font(script, size)
        if bold:
            draw.text((x, baseline), run, font=font, fill=fill, anchor="ls", stroke_width=1, stroke_fill=fill)
        else:
            draw.text((x, baseline), run, font=font, fill=fill, anchor="ls")
        x += font.getlength(run)


def wrap(text: str, size: int, max_width: float, max_lines: int = 3) -> list[str]:
    """Greedy word wrap; over-long words are broken by character; the last line gets an ellipsis."""
    words = (text or "").split()
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
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_width(last + "…", size) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


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
                for piece in wrap(ln.text, ln.size, avail, max_lines=3):
                    wrapped.append((piece, ln))
                    h += int(ln.size * 1.38)
            out_cells.append(wrapped)
            tallest = max(tallest, h)
        laid.append(_LaidRow(out_cells, max(tallest, 40) + 2 * PAD_Y))
    return widths, laid


def _paginate(laid: list[_LaidRow]) -> list[list[_LaidRow]]:
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


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _header(draw: ImageDraw.ImageDraw, width: int, kicker: str, title: str, subtitle: str = "") -> None:
    draw.rectangle([0, 0, width, HEADER_H], fill=NAVY)
    draw_text(draw, (MARGIN + 6, 24), kicker.upper(), 24, "#AEB8D6")
    draw_text(draw, (MARGIN + 6, 56), title, 58, "#FFFFFF", bold=True)
    if subtitle:
        draw_text(draw, (MARGIN + 6, 128), subtitle, 26, "#C9D2EE")
    draw_text(draw, (width - MARGIN - 6, 30), "KAMLA", 30, "#FFFFFF", bold=True, anchor_right=True)


def _footer(draw: ImageDraw.ImageDraw, width: int, y: int, page: int, pages: int) -> None:
    draw_text(draw, (MARGIN + 6, y + 18), "Generated by KAMLA", 24, MUTED)
    if pages > 1:
        draw_text(draw, (width - MARGIN - 6, y + 18), f"Page {page} / {pages}", 24, MUTED, anchor_right=True)


def render_table(kicker: str, title: str, subtitle: str, columns: list[Column], rows: list[list[Cell]]) -> list[bytes]:
    inner_w = WIDTH - 2 * MARGIN
    widths, laid = _layout_rows(columns, rows, inner_w)
    pages = _paginate(laid)
    images = []
    for number, page_rows in enumerate(pages, 1):
        body_h = sum(r.height for r in page_rows)
        height = HEADER_H + MARGIN + COLHEAD_H + body_h + MARGIN + FOOTER_H
        img = Image.new("RGB", (WIDTH, height), BG)
        d = ImageDraw.Draw(img)
        _header(d, WIDTH, kicker, title, subtitle)

        top = HEADER_H + MARGIN
        card_w, card_h = WIDTH - 2 * MARGIN, COLHEAD_H + body_h
        card = Image.new("RGB", (card_w, card_h), CARD)
        c = ImageDraw.Draw(card)

        # column headings
        x = 0
        for col, w in zip(columns, widths):
            c.rectangle([x, 0, x + w, COLHEAD_H], fill=col.color)
            draw_text(c, (x + PAD_X, (COLHEAD_H - 34) // 2 - 2), col.header, 30, "#FFFFFF", bold=True)
            x += w

        y = COLHEAD_H
        for idx, row in enumerate(page_rows):
            if idx % 2 == 1:
                c.rectangle([0, y, card_w, y + row.height], fill=ZEBRA)
            x = 0
            for col_cells, w in zip(row.cells, widths):
                cy = y + PAD_Y
                for text, ln in col_cells:
                    draw_text(c, (x + PAD_X, cy), text, ln.size, ln.color, ln.bold)
                    cy += int(ln.size * 1.38)
                x += w
            y += row.height
            if idx < len(page_rows) - 1:
                c.line([0, y, card_w, y], fill=BORDER, width=1)

        # the card is composited through a rounded mask so the header cells get clean rounded corners
        mask = Image.new("L", (card_w, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=22, fill=255)
        img.paste(card, (MARGIN, top), mask)
        d.rounded_rectangle([MARGIN, top, MARGIN + card_w - 1, top + card_h - 1], radius=22, outline=BORDER, width=2)
        _footer(d, WIDTH, height - FOOTER_H, number, len(pages))
        images.append(_png(img))
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


# ── motions ───────────────────────────────────────────────────────────────────

def render_motions(tournament: str, rnd: RoundInfo, motions: list[MotionInfo]) -> list[bytes]:
    images = []
    inner_w = 1200 - 2 * MARGIN - 2 * 44
    for idx, m in enumerate(motions, 1):
        text_lines = wrap(m.text or "—", 54, inner_w, max_lines=14)
        slide_lines = wrap(m.info_slide, 32, inner_w, max_lines=22) if m.info_slide else []
        body_h = 44 + len(text_lines) * int(54 * 1.42) + 40
        if slide_lines:
            body_h += 40 + 36 + len(slide_lines) * int(32 * 1.45) + 30
        width = 1200
        height = HEADER_H + MARGIN + body_h + MARGIN + FOOTER_H
        img = Image.new("RGB", (width, height), BG)
        d = ImageDraw.Draw(img)
        label = "Motion" if len(motions) == 1 else f"Motion {idx}"
        _header(d, width, tournament or "Tournament", f"{rnd.name}  ·  {label}")
        top = HEADER_H + MARGIN
        d.rounded_rectangle([MARGIN, top, width - MARGIN, top + body_h], radius=22, fill=CARD, outline=BORDER, width=2)
        d.rounded_rectangle([MARGIN, top, MARGIN + 12, top + body_h], radius=6, fill="#3B6BFF")
        y = top + 44
        for line in text_lines:
            draw_text(d, (MARGIN + 44, y), line, 54, TEXT, bold=True)
            y += int(54 * 1.42)
        if slide_lines:
            y += 40
            d.line([MARGIN + 44, y, width - MARGIN - 44, y], fill=BORDER, width=2)
            y += 24
            draw_text(d, (MARGIN + 44, y), "INFO SLIDE", 24, MUTED, bold=True)
            y += 40
            for line in slide_lines:
                draw_text(d, (MARGIN + 44, y), line, 32, "#374151")
                y += int(32 * 1.45)
        _footer(d, width, height - FOOTER_H, 1, 1)
        images.append(_png(img))
    return images
