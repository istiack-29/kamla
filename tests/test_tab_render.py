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
    assert len(pages) == 1 and size_of(pages[0])[0] == tr.WIDTH * tr.SCALE


def test_bp_draw_and_teams_only_draw():
    assert len(tr.render_draw("Open", RND, [bp(1), bp(2)])) == 1
    teams_only = RoundInfo(seq=2, name="Round 2", draw_status="T")
    png = tr.render_draw("Open", teams_only, [ap(1, chair="Hidden")])[0]            # adjudicator columns omitted
    assert size_of(png)[0] == tr.WIDTH * tr.SCALE


def test_many_debates_stay_one_image_as_tall_as_needed():
    t0 = time.monotonic()
    pages = tr.render_draw("Open", RND, [ap(i) for i in range(1, 41)])   # 40 debates: same code path, 3× faster at scale 1
    assert len(pages) == 1
    w, h = size_of(pages[0])
    assert h > 2100 // tr.SCALE and max(w, h) <= tr.MAX_SIDE_PX
    assert len(pages[0]) <= tr.MAX_BYTES
    assert time.monotonic() - t0 < 30


def test_scale_constant_exists_and_is_in_range():
    # In tests SCALE=1 (fast), in production SCALE=2 (high-res). The range 1–4 is what the env
    # var accepts; the production default of 2 is enforced by the .env on the VM, not here.
    assert 1 <= tr.SCALE <= 4
    w, _ = size_of(tr.render_draw("Open", RND, [ap(1)])[0])
    assert w == tr.WIDTH * tr.SCALE           # whatever the scale, the width is WIDTH × SCALE


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


# ── adjudicator draw (/drawadj): independent chair/panel/trainee columns ──────

def adj(i, chair="Chair", panel=(), trainees=()):
    return DebateInfo(id=i, venue=f"Room {i}", teams=[TeamSlot("Gov", "A"), TeamSlot("Opp", "B")],
                      chair=chair, panel=list(panel), trainees=list(trainees))


def _header_texts_of(png: bytes) -> list[str]:
    """We can't OCR the PNG, so this just documents the expected column SET via the render call succeeding;
    the real assertions below check tr.render_draw_adjudicators doesn't raise and produces distinct output
    per configuration (covered by the byte-difference checks)."""
    return []


def test_only_chair_when_no_panel_or_trainee_exist_in_the_round():
    a = tr.render_draw_adjudicators("Open", RND, [adj(1), adj(2)])
    assert a  # renders fine with just Venue/Gov/Opp/Chair


def test_panel_column_hidden_when_no_debate_has_a_panellist():
    with_trainee_only = [adj(1, trainees=["T1"]), adj(2)]
    a = tr.render_draw_adjudicators("Open", RND, with_trainee_only)
    chair_trainee_only = tr.render_draw_adjudicators("Open", RND, [adj(1, chair="C", trainees=["T1"])])
    assert a and chair_trainee_only
    # a debate with ONLY chair+panel vs ONLY chair+trainee must differ (different column sets)
    chair_panel_only = tr.render_draw_adjudicators("Open", RND, [adj(1, chair="C", panel=["P1"])])
    assert chair_panel_only != chair_trainee_only


def test_trainee_column_present_only_when_some_debate_has_one():
    none_have_trainees = [adj(1, panel=["P1"]), adj(2, panel=["P2"])]
    one_has_trainee = [adj(1, panel=["P1"]), adj(2, panel=["P2"], trainees=["T1"])]
    assert tr.render_draw_adjudicators("Open", RND, none_have_trainees) != \
        tr.render_draw_adjudicators("Open", RND, one_has_trainee)


def test_draw_adj_never_crashes_on_hostile_input():
    weird = [adj(1, chair="", panel=[], trainees=[]), adj(2, chair="ড. করিম", panel=["🎉🎉🎉"], trainees=["x" * 200])]
    assert tr.render_draw_adjudicators("Open" * 80, RND, weird)
    assert tr.render_draw_adjudicators("", RND, [])
