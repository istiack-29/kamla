import io
import time

import pytest
from PIL import Image

import tab_render as tr
from tabbycat_client import (BreakCategoryInfo, BreakTeam, DebateInfo, MotionInfo, ResultInfo, ResultTeam, RoundInfo,
                             TeamSlot)

RND = RoundInfo(seq=1, name="Round 1", draw_status="R", motions_released=True, completed=True)


def size_of(png: bytes):
    img = Image.open(io.BytesIO(png))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return img.size


def ap(i, a="Alpha", b="Beta", chair="Chair", panel=("P1",)):
    return DebateInfo(id=i, venue=f"Room {i}", teams=[TeamSlot("Gov", a), TeamSlot("Opp", b)], chair=chair, panel=list(panel))


def bp(i):
    return DebateInfo(id=i, venue=f"Room {i}", teams=[TeamSlot(s, f"{s} team") for s in ("OG", "OO", "CG", "CO")],
                      chair="C", panel=["P"])


def test_ap_draw_is_a_valid_png():
    pages = tr.render_draw("Open", RND, [ap(1), ap(2)])
    assert len(pages) == 1 and size_of(pages[0])[0] == tr.WIDTH


def test_bp_draw_and_teams_only_draw():
    assert len(tr.render_draw("Open", RND, [bp(1), bp(2)])) == 1
    teams_only = RoundInfo(seq=2, name="Round 2", draw_status="T")
    png = tr.render_draw("Open", teams_only, [ap(1, chair="Hidden")])[0]            # adjudicator columns omitted
    assert size_of(png)[0] == tr.WIDTH


def test_many_debates_paginate_without_exceeding_page_height():
    t0 = time.monotonic()
    pages = tr.render_draw("Open", RND, [ap(i) for i in range(1, 121)])
    assert len(pages) > 1
    assert all(size_of(p)[1] <= tr.MAX_PAGE_H + 20 for p in pages)
    assert time.monotonic() - t0 < 15


def test_hostile_content_never_crashes():
    nasty = [
        ap(1, a="x" * 400, b="বাংলা " * 60, chair="", panel=()),
        ap(2, a="", b="🎉🎉🎉", chair="ড. করিম", panel=("র\u200d্য", "ক্ষ", "Rafi")),
        DebateInfo(id=3, venue="", teams=[], chair="", panel=[]),
        DebateInfo(id=4, venue="W" * 200, teams=[TeamSlot("Gov", "A")], chair="c", panel=["p"] * 40),
    ]
    assert tr.render_draw("T" * 300, RND, nasty)
    assert tr.render_draw("", RND, [])                                               # empty draw still renders a card


def test_results_ap_bp_and_missing_values():
    ap_res = [ResultInfo(1, "R1", [ResultTeam("Gov", "A", win=True, score=152.5), ResultTeam("Opp", "B", win=False, score=148)])]
    bp_res = [ResultInfo(2, "R2", [ResultTeam(s, s, points=p, score=200 + p) for s, p in zip(("OG", "OO", "CG", "CO"), (3, 2, 1, 0))])]
    odd = [ResultInfo(3, "", [ResultTeam("Gov", "A"), ResultTeam("Opp", "B")])]       # no points / wins / scores at all
    for res in (ap_res, bp_res, odd):
        assert tr.render_results("Open", RND, res)


def test_break_and_motions():
    assert tr.render_break("Open", BreakCategoryInfo("Open", 16, ""), [BreakTeam(i, f"Team {i}") for i in range(1, 40)])
    motions = [MotionInfo(1, "THW ban X.", "Info"), MotionInfo(2, "বাংলা মোশন " * 30, "তথ্য " * 200), MotionInfo(3, "", "")]
    pages = tr.render_motions("Open", RND, motions)
    assert len(pages) == 3 and all(size_of(p)[1] > 300 for p in pages)


def test_script_runs_keep_bengali_words_whole():
    runs = tr.split_runs("Team বাংলা দল, A1")
    assert runs[0][0] == "E" and any(s == "B" and "বাংলা" in r for s, r in runs)
    assert "".join(r for _, r in runs) == "Team বাংলা দল, A1"


def test_wrap_respects_width_and_ellipsises():
    lines = tr.wrap("word " * 80, 28, 300, max_lines=3)
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert all(tr.text_width(l, 28) <= 300 + 1 for l in lines)
    long_word = tr.wrap("Supercalifragilisticexpialidocious" * 3, 28, 200, max_lines=5)
    assert all(tr.text_width(l, 28) <= 200 + 1 for l in long_word)
    assert tr.wrap("", 28, 100) == [""]


def test_fonts_status_reports_raqm():
    info = tr.check_fonts()
    assert set(info) == {"E.ttf", "B.ttf", "raqm"} and info["raqm"] is True
