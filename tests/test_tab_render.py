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


def test_ap_draw_is_a_valid_wide_png():
    pages = tr.render_draw("Open", RND, [ap(1), ap(2)], tr.MODE_ALL)
    w, h = size_of(pages[0])
    assert len(pages) == 1 and w >= tr.DRAW_MIN_WIDTH * tr.SCALE
    assert w > 4 * h                                          # wide, not tall


def test_bp_draw_and_teams_only_draw():
    assert len(tr.render_draw("Open", RND, [bp(1), bp(2)], tr.MODE_ALL)) == 1
    png = tr.render_draw("Open", RND, [ap(1, chair="", panel=())])[0]        # no allocation at all
    assert size_of(png)[0] >= tr.DRAW_MIN_WIDTH * tr.SCALE


def test_many_debates_stay_one_image_as_tall_as_needed():
    t0 = time.monotonic()
    pages = tr.render_matchup("Open", RND, [ap(i) for i in range(1, 41)], tr.MODE_ALL)
    assert len(pages) == 1
    w, h = size_of(pages[0])
    assert h > 1500 and max(w, h) <= tr.MAX_SIDE_PX
    assert len(pages[0]) <= tr.MAX_BYTES
    assert time.monotonic() - t0 < 30


def test_rows_are_compact():
    one = size_of(tr.render_matchup("Open", RND, [ap(1)], tr.MODE_ALL)[0])[1]
    ten = size_of(tr.render_matchup("Open", RND, [ap(i) for i in range(1, 11)], tr.MODE_ALL)[0])[1]
    per_row = (ten - one) / 9 / tr.SCALE
    assert per_row < 52                                       # logical units per room: dense, single-line rows


def test_scale_constant_exists_and_is_in_range():
    assert 1 <= tr.SCALE <= 4
    w, _ = size_of(tr.render_draw("Open", RND, [ap(1)])[0])
    assert w >= tr.DRAW_MIN_WIDTH * tr.SCALE and w <= tr.DRAW_MAX_WIDTH * tr.SCALE


# ── BP / AP columns ───────────────────────────────────────────────────────────

def test_bp_has_exactly_four_team_columns_and_ap_exactly_two():
    assert tr._side_columns([bp(1)]) == [("OG", "OG"), ("OO", "OO"), ("CG", "CG"), ("CO", "CO")]
    assert tr._side_columns([ap(1)]) == [("Gov", "Government"), ("Opp", "Opposition")]


def test_team_names_are_aligned_to_their_columns_even_when_the_data_order_differs():
    d = DebateInfo(id=1, venue="R", teams=[TeamSlot("CO", "d"), TeamSlot("OG", "a"), TeamSlot("CG", "c"), TeamSlot("OO", "b")])
    assert tr._team_names(d, tr._side_columns([d])) == ["a", "b", "c", "d"]


# ── adjudicators: one allocation per matchup, ©  Ⓟ  Ⓣ ─────────────────────────

def adj(i, chair="Chair", panel=(), trainees=()):
    return DebateInfo(id=i, venue=f"Room {i}", teams=[TeamSlot("Gov", "A"), TeamSlot("Opp", "B")],
                      chair=chair, panel=list(panel), trainees=list(trainees))


def test_markers_and_order_chair_then_panel_then_trainee():
    d = adj(1, chair="Ann", panel=["Bob", "Cy"], trainees=["Di"])
    assert tr.adjudicator_entries(d) == [("Ann", "©"), ("Bob", "Ⓟ"), ("Cy", "Ⓟ"), ("Di", "Ⓣ")]
    assert tr.adjudicator_text(d) == "Ann©, BobⓅ, CyⓅ, DiⓉ"
    assert (tr.MARK_CHAIR, tr.MARK_PANEL, tr.MARK_TRAINEE) == ("©", "Ⓟ", "Ⓣ")


def test_only_one_chair_and_nobody_is_listed_twice():
    d = adj(1, chair="Ann", panel=["ann", "Bob", " Bob ", "", "  "], trainees=["Bob", "Ann"])
    assert tr.adjudicator_entries(d) == [("Ann", "©"), ("Bob", "Ⓟ")]
    assert sum(1 for _n, m in tr.adjudicator_entries(d) if m == "©") == 1
    assert tr.adjudicator_text(adj(2, chair="", panel=[], trainees=[])) == "—"


def test_markers_are_drawn_as_badges_so_no_font_can_turn_them_into_boxes():
    runs = tr.split_runs("Ann©, BobⓅ, CyⓉ")
    assert [r for s, r in runs if s == "K"] == ["©", "Ⓟ", "Ⓣ"]
    assert tr.text_width("Ⓟ", 24) == pytest.approx(24 * tr.MARK_W)
    plain = tr.render_matchup("Open", RND, [adj(1, chair="Ann")], tr.MODE_ADJ)[0]
    marked = tr.render_matchup("Open", RND, [adj(1, chair="Ann", panel=["Bob"], trainees=["Cy"])], tr.MODE_ADJ)[0]
    assert plain != marked


# ── the three draw types ───────────────────────────────────────────────────────

def test_without_an_allocation_every_type_becomes_teams_only():
    no_alloc = [ap(1, chair="", panel=()), ap(2, chair="", panel=())]
    teams = tr.render_matchup("Open", RND, no_alloc, tr.MODE_TEAMS)
    assert tr.render_matchup("Open", RND, no_alloc, tr.MODE_ALL) == teams
    assert tr.render_matchup("Open", RND, no_alloc, tr.MODE_ADJ) == teams
    assert tr.render_matchup("Open", RND, no_alloc, "nonsense") == teams
    assert not tr.has_adjudicator_allocation(no_alloc)


def test_with_an_allocation_all_adj_and_teams_are_three_different_images():
    debates = [ap(1, panel=["P1"]), adj(2, panel=["P2"], trainees=["T"])]
    imgs = {m: tr.render_matchup("Open", RND, debates, m)[0] for m in (tr.MODE_ALL, tr.MODE_ADJ, tr.MODE_TEAMS)}
    assert len(set(imgs.values())) == 3
    assert tr.has_adjudicator_allocation(debates)


def test_draw_never_crashes_on_hostile_input():
    weird = [adj(1, chair="", panel=[], trainees=[]), adj(2, chair="ড. করিম", panel=["🎉🎉🎉"], trainees=["x" * 200])]
    for mode in (tr.MODE_ALL, tr.MODE_ADJ, tr.MODE_TEAMS):
        assert tr.render_matchup("Open" * 80, RND, weird, mode)
        assert tr.render_matchup("", RND, [], mode)


def test_team_emoji_are_drawn_not_dropped():
    assert tr.emoji_available()
    with_emoji = tr.render_draw("Open", RND, [ap(1, a="\U0001F98A Foxes", b="\U0001F1E7\U0001F1E9 Tigers")])[0]
    without = tr.render_draw("Open", RND, [ap(1, a="Foxes", b="Tigers")])[0]
    assert with_emoji != without
    img = Image.open(io.BytesIO(with_emoji)).convert("RGB")
    colours = {p for p in img.resize((300, 100)).getdata()}
    assert any(abs(r - g) > 90 or abs(g - b) > 90 for r, g, b in colours if (r, g, b) not in ((255, 255, 255),))  # colour pixels
    assert tr.text_width("\U0001F98A A", 28) > tr.text_width(" A", 28)    # emoji take space in the layout


def test_emoji_runs_are_whole_clusters():
    runs = tr.split_runs("A \U0001F1E7\U0001F1E9 \U0001F469\u200d\U0001F4BB \u09b0\u200d\u09cd\u09af")
    assert [r for s, r in runs if s == "M"] == ["\U0001F1E7\U0001F1E9", "\U0001F469\u200d\U0001F4BB"]
    assert any(s == "B" and "\u200d" in r for s, r in runs)             # Bengali ZWJ is not taken for an emoji joiner


def test_info_slide_is_never_truncated_and_keeps_paragraphs():
    long_info = "\n\n".join(f"Paragraph {i}. " + "word " * 20 for i in range(12))
    lines = tr.wrap_paragraphs(long_info, 34, 900)
    assert "…" not in "".join(lines) and lines.count("") == 11            # blank line between the 12 paragraphs
    short = tr.render_info_slides("Open", RND, [MotionInfo(1, "M", "Short info")])[0]
    tall = tr.render_info_slides("Open", RND, [MotionInfo(1, "M", long_info)])[0]
    assert size_of(tall)[1] > 4 * size_of(short)[1]


def test_info_slides_only_for_motions_that_have_one():
    ms = [MotionInfo(1, "A", "info A"), MotionInfo(2, "B", ""), MotionInfo(3, "C", "info C")]
    assert len(tr.render_info_slides("Open", RND, ms)) == 2
    assert tr.render_info_slides("Open", RND, [MotionInfo(1, "A", "")]) == []


def test_motion_without_info_option_is_shorter():
    m = [MotionInfo(1, "THW ban X.", "Some info " * 50)]
    assert size_of(tr.render_motions("Open", RND, m, show_info=False)[0])[1] < size_of(tr.render_motions("Open", RND, m)[0])[1]


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
    assert tr.render_break("Open", BreakCategoryInfo("Open", 16, ""), [BreakTeam(i, f"Team {i}") for i in range(1, 16)])
    motions = [MotionInfo(1, "THW ban X.", "Info"), MotionInfo(2, "বাংলা মোশন " * 10, "তথ্য " * 40), MotionInfo(3, "", "")]
    pages = tr.render_motions("Open", RND, motions)
    assert len(pages) == 3 and all(size_of(p)[1] > 300 for p in pages)


def test_script_runs_keep_bengali_words_whole():
    runs = tr.split_runs("Team বাংলা দল, A1")
    assert runs[0][0] == "E" and any(s == "B" and "বাংলা" in r for s, r in runs)
    assert "".join(r for _, r in runs) == "Team বাংলা দল, A1"


def test_wrap_never_truncates_by_default():
    lines = tr.wrap("word " * 300, 28, 300)
    assert len(lines) > 10 and not any(l.endswith("…") for l in lines)


def test_wrap_respects_width_and_ellipsises():
    lines = tr.wrap("word " * 80, 28, 300, max_lines=3)
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert all(tr.text_width(l, 28) <= 300 + 1 for l in lines)
    long_word = tr.wrap("Supercalifragilisticexpialidocious" * 3, 28, 200, max_lines=5)
    assert all(tr.text_width(l, 28) <= 200 + 1 for l in long_word)
    assert tr.wrap("", 28, 100) == [""]


def test_fonts_status_reports_raqm():
    info = tr.check_fonts()
    assert set(info) == {"E.ttf", "B.ttf", "raqm", "emoji"} and info["raqm"] is True and info["emoji"] is True


def test_speaker_break_renders_with_emoji_bengali_and_any_number_of_metric_columns():
    from tabbycat_client import SpeakerRow
    rows = [SpeakerRow(1, "সুমি আক্তার", "🦊 Dhaka Debaters", [("Average", "83.5"), ("Total", "417.5"), ("Speeches", "5")]),
            SpeakerRow(2, "Ann " + "Longname " * 12, "🇧🇩 Team", [("Average", "82"), ("Total", "410"), ("Speeches", "5")])]
    pages = tr.render_speaker_break("Open", "Break  ·  Top 10 Speakers", "Overall  ·  2 speakers",
                                    ["Average", "Total", "Speeches"], rows)
    assert len(pages) == 1 and size_of(pages[0])[0] == tr.WIDTH * tr.SCALE   # scale-independent
    none = tr.render_speaker_break("Open", "Break", "x", [], [SpeakerRow(1, "A", "T", [])])           # no metric columns at all
    assert len(none) == 1


def test_speaker_break_of_100_rows_is_one_image():
    from tabbycat_client import SpeakerRow
    rows = [SpeakerRow(i, f"Speaker {i}", f"Team {i}", [("Total", str(400 - i))]) for i in range(1, 41)]
    pages = tr.render_speaker_break("Open", "Break  ·  Top 100", "Overall", ["Total"], rows)
    assert len(pages) == 1 and len(pages[0]) <= tr.MAX_BYTES


def test_adjudicator_break_renders():
    pages = tr.render_adjudicator_break("Open", ["Dr Chair", "পানেল ওয়ান", "🧑‍⚖️ Judge J"])
    assert len(pages) == 1 and size_of(pages[0])[1] > 300
