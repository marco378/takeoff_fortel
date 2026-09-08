#!/usr/bin/env python3
"""detect_scale_bar rotation/segmented bars, proved on real Winvic sheets.

Sections in this module (printed in this order):
  - scale: detect_scale_bar rotation-agnostic + segmented-bar + crash-guard (Aryan field report — real SGP sheet 'title 1:250 only — no scale bar detected'; root cause: every real Fortel A0/A1 sheet is landscape content in a portrait MediaBox with page /Rotate 90/270, and PyMuPDF returns RAW pre-rotation coordinates, so a visually- horizontal bar is a stack of near-VERTICAL strokes the old horizontal-only test could never match; also the real bar is SEGMENTED [alternating-fill tick blocks] with a fused '25m' terminal label, and ms[0]-anchoring on text-extraction order crashed with 'max() arg is an empty sequence' on two real Winvic sheets)
  - scale: real-sheet proof — Winvic sheets that already detected still detect after the fix, and the SGP-family real sheet that previously missed entirely now detects + VERIFIES against its title-block scale (never bypassing scale_consensus)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from reportlab.pdfgen import canvas
from scale import detect_scale_bar
from tests import ck
from scale import scale_consensus

print("scale: detect_scale_bar rotation-agnostic + segmented-bar + crash-guard "
      "(Aryan field report — real SGP sheet 'title 1:250 only — no scale bar detected'; root "
      "cause: every real Fortel A0/A1 sheet is landscape content in a portrait MediaBox with "
      "page /Rotate 90/270, and PyMuPDF returns RAW pre-rotation coordinates, so a visually- "
      "horizontal bar is a stack of near-VERTICAL strokes the old horizontal-only test could "
      "never match; also the real bar is SEGMENTED [alternating-fill tick blocks] with a fused "
      "'25m' terminal label, and ms[0]-anchoring on text-extraction order crashed with "
      "'max() arg is an empty sequence' on two real Winvic sheets)")
try:
    import fitz as _fitz_sb

    def _gen_rotated_segmented_bar_sb(out_path, rotation=270):
        """Portrait-mediabox page (mimics the real 2384x3370 Winvic sheets) with a scale bar drawn
        as 4 stacked alternating-fill blocks (reportlab 're' rects) + a fused '25m' terminal tick,
        then rotated via PyMuPDF post-process — reproducing 'visually horizontal bar, raw-space
        near-vertical strokes' exactly as found on drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf."""
        _c = canvas.Canvas(out_path, pagesize=(850, 1200))
        _c.setFont("Helvetica", 10)
        _c.drawString(50, 150, "Scale 1:250")
        x0, y0, block_h = 120, 400, 30
        for i in range(4):
            y = y0 + i * block_h
            _c.setFillColorRGB(0, 0, 0) if i % 2 == 0 else _c.setFillColorRGB(1, 1, 1)
            _c.rect(x0, y, 6, block_h, fill=1, stroke=1)
        for i, lab in enumerate(["0", "5", "10", "15", "20", "25m"]):
            _c.drawString(x0 + 10, y0 + i * block_h - 3, lab)
        _c.save()
        _d = _fitz_sb.open(out_path)
        _d[0].set_rotation(rotation)
        _d.saveIncr()
        _d.close()

    _sb_expected_k = 25 / 120   # 25 m over the 4x30pt stacked-block span

    _path_sb270 = "/tmp/_sb_rotated270.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb270, rotation=270)
    _k_sb270, _info_sb270 = detect_scale_bar(_path_sb270)
    ck("rotation=270 segmented tick-block bar (real Winvic sheet style) now detects",
       _k_sb270 is not None and abs(_k_sb270 - _sb_expected_k) < 1e-9, (_k_sb270, _info_sb270))

    _path_sb90 = "/tmp/_sb_rotated90.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb90, rotation=90)
    _k_sb90, _info_sb90 = detect_scale_bar(_path_sb90)
    ck("rotation=90 segmented tick-block bar also detects",
       _k_sb90 is not None and abs(_k_sb90 - _sb_expected_k) < 1e-9, (_k_sb90, _info_sb90))

    _path_sb0 = "/tmp/_sb_unrotated_control.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb0, rotation=0)
    _k_sb0, _info_sb0 = detect_scale_bar(_path_sb0)
    ck("rotation=0 control (same fixture, no rotation) also detects — proves the fix is "
       "additive, not rotation-only", _k_sb0 is not None and abs(_k_sb0 - _sb_expected_k) < 1e-9,
       (_k_sb0, _info_sb0))

    # Unrotated segmented bar with a WIDE fused terminal tick ('50m'), several alternating blocks —
    # exercises the horizontal branch of the same clustering/merge logic.
    def _gen_segmented_bar_h_sb(out_path):
        _c = canvas.Canvas(out_path, pagesize=(1400, 900))
        _c.setFont("Helvetica", 10)
        _c.drawString(100, 800, "Scale 1:200")
        x0, y0, block_w = 200, 300, 40
        for i in range(5):
            x = x0 + i * block_w
            _c.setFillColorRGB(0, 0, 0) if i % 2 == 0 else _c.setFillColorRGB(1, 1, 1)
            _c.rect(x, y0, block_w, 8, fill=1, stroke=1)
        for i, lab in enumerate(["0", "10", "20", "30", "40", "50m"]):
            _c.drawString(x0 + i * block_w - 5, y0 - 15, lab)
        _c.save()

    _path_sb_h = "/tmp/_sb_segmented_h.pdf"
    _gen_segmented_bar_h_sb(_path_sb_h)
    _k_sbh, _info_sbh = detect_scale_bar(_path_sb_h)
    _expected_sbh = 50 / (5 * 40)
    ck("horizontal segmented alternating-fill bar with fused '50m' terminal tick",
       _k_sbh is not None and abs(_k_sbh - _expected_sbh) < 1e-9, (_k_sbh, _info_sbh))

    # Crash-guard regression: an early, text-order-first 'm' token with NO nearby bar/digits at
    # all must not raise (old code: max() on an empty generator -> ValueError, reproduced directly
    # on drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf and Dock_Slab_Area_Proposed_Site_Plan.pdf).
    # The real scale bar (a plain line + bare 'm' label, further down the page) must still be found.
    def _gen_bad_anchor_sb(out_path):
        _c = canvas.Canvas(out_path, pagesize=(1400, 900))
        _c.setFont("Helvetica", 10)
        _c.drawString(700, 850, "m")                      # unrelated early 'm', no nearby digits
        _c.line(100, 150, 500, 150)
        _c.drawString(250, 160, "0          40 m")
        _c.save()

    _path_bad = "/tmp/_sb_bad_anchor.pdf"
    _gen_bad_anchor_sb(_path_bad)
    try:
        _k_bad, _info_bad = detect_scale_bar(_path_bad)
        ck("no crash when the first text-order 'm' token has zero nearby bar/digits "
           "(old ms[0] anchor -> max() on empty sequence -> ValueError)", True, (_k_bad, _info_bad))
        ck("bad-anchor fixture still finds the REAL bar via a later, valid label",
           _k_bad is not None and abs(_k_bad - 0.1) < 1e-9, (_k_bad, _info_bad))
    except Exception as _e:
        ck("no crash when the first text-order 'm' token has zero nearby bar/digits "
           "(old ms[0] anchor -> max() on empty sequence -> ValueError)", False,
           f"{type(_e).__name__}: {_e}")

    # A page with literally no scale-bar shape at all must still return cleanly, never raise.
    _path_none = "/tmp/_sb_no_bar_at_all.pdf"
    _c_none = canvas.Canvas(_path_none, pagesize=(800, 600))
    _c_none.drawString(100, 100, "no bar here, just some m words and 5 10 15 numbers")
    _c_none.save()
    try:
        _k_none, _info_none = detect_scale_bar(_path_none)
        ck("page with no real scale-bar shape returns (None, ...) cleanly, never raises",
           _k_none is None, (_k_none, _info_none))
    except Exception as _e:
        ck("page with no real scale-bar shape returns (None, ...) cleanly, never raises",
           False, f"{type(_e).__name__}: {_e}")

    # Detection improvements must never bypass verification: scale_consensus still gates a
    # disagreeing bar-vs-title pair (a rotated segmented bar detected via the fix, paired with a
    # deliberately wrong title-block scale) exactly as it does for the unrotated path.
    _k_gate, _flags_gate = scale_consensus([(_k_sb270, 1), (25 / 40, 1)], tol=0.03)
    ck("scale_consensus still REFUSES when the (correctly-detected, rotation-fixed) bar "
       "disagrees with a second reference beyond tol — detection fix does not bypass the gate",
       # Assert BEHAVIOUR (refusal) plus the surviving diagnostic, not the exact copy — the
       # assessor-facing sentence was rewritten into plain English after Inderjit could not
       # read it, and a wording change must not be able to mask a bypassed gate.
       _k_gate is None and any("MIXED-SCALE" in f for f in _flags_gate)
       and any("disagree" in f.lower() for f in _flags_gate), _flags_gate)

    _k_agree, _flags_agree = scale_consensus([(_k_sb270, 1), (_k_sb270 * 1.01, 1)], tol=0.03)
    ck("scale_consensus VERIFIES the rotation-fixed bar reading when a second reference agrees "
       "within tol", _k_agree is not None, _flags_agree)

except ImportError as _e:
    print(f"  [SKIP] scale.py rotation/segmented-bar tests — missing dependency: {_e}")

print("scale: real-sheet proof — Winvic sheets that already detected still detect after the fix, "
      "and the SGP-family real sheet that previously missed entirely now detects + VERIFIES "
      "against its title-block scale (never bypassing scale_consensus)")
try:
    import os as _os_sb

    _real_sheets_unchanged = [
        ("drawings/_int_d77.pdf", 0.08819445326652144),
        ("drawings/_int_d77_borders.pdf", 0.08819445326652144),
    ]
    for _pdf_path, _expected_k in _real_sheets_unchanged:
        if _os_sb.path.exists(_pdf_path):
            _k_chk, _info_chk = detect_scale_bar(_pdf_path)
            ck(f"unrotated gold fixture {_pdf_path} still detects the same k as before the fix",
               _k_chk is not None and abs(_k_chk - _expected_k) < 1e-6, (_k_chk, _info_chk))
        else:
            print(f"  [SKIP] real-sheet scale regression for {_pdf_path} — fixture not present")

    # The real Winvic sheets (270/90-rotated) must no longer crash, and the two with a genuine
    # readable segmented bar (Yard, Dock — same title-block template) must now agree with each
    # other (same physical bar) instead of one crashing and the other silently mis-anchoring.
    _winvic_rotated = [
        "drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf",
        "drawings/winvic/Dock_Slab_Area_Proposed_Site_Plan.pdf",
    ]
    _winvic_ks = {}
    for _wp in _winvic_rotated:
        if _os_sb.path.exists(_wp):
            try:
                _k_w, _info_w = detect_scale_bar(_wp)
                _winvic_ks[_wp] = _k_w
                ck(f"{_wp} (rotation 270) no longer crashes calling detect_scale_bar",
                   True, (_k_w, _info_w))
            except Exception as _e:
                ck(f"{_wp} (rotation 270) no longer crashes calling detect_scale_bar",
                   False, f"{type(_e).__name__}: {_e}")
        else:
            print(f"  [SKIP] rotated Winvic scale regression for {_wp} — fixture not present")

    if len(_winvic_ks) == 2 and all(_v is not None for _v in _winvic_ks.values()):
        _vals = list(_winvic_ks.values())
        ck("Yard and Dock (same rotated title-block template, same '0 5 10 15 20 25m' bar) "
           "agree on k within 0.1% — both correctly read the same physical scale bar",
           abs(_vals[0] - _vals[1]) / _vals[1] < 0.001, _winvic_ks)

    # Full scale_for() (takeoff_unmarked's consensus-gated wrapper) on the real UNMARKED-vector,
    # rotated, segmented-bar sheet that most closely matches Aryan's real SGP sheet's shape
    # (same title-block family: rotated A0/A1, printed 1:N scale + graphical bar) — must now
    # VERIFY rather than fall back to 'title only — no scale bar detected'.
    _tp_site_plan = ("drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_Site_Plan.pdf")
    if _os_sb.path.exists(_tp_site_plan):
        import takeoff_unmarked as _TU_sb
        _k_tp, _verified_tp, _note_tp, _sources_tp = _TU_sb.scale_for(_tp_site_plan)
        ck("real rotated tender-pack Proposed_Site_Plan.pdf: scale bar now VERIFIED against "
           "title-block (was previously undetectable/unverified pre-fix)",
           _verified_tp is True, _note_tp)
        ck("...and it went through scale_consensus (both sources present), not a bypass",
           "scale_bar" in _sources_tp and "title_block" in _sources_tp, _sources_tp)
    else:
        print(f"  [SKIP] tender-pack scale verification for {_tp_site_plan} — fixture not present")
except ImportError as _e:
    print(f"  [SKIP] scale.py real-sheet regression tests — missing dependency: {_e}")



# ── Deploy survival, ghost-job neutralisation, case-level quotation ───────────
# Railway's container filesystem is EPHEMERAL and this has already destroyed real assessor
# work once.  These tests pin the three properties that keep a case resumable across a deploy:
# artifacts resolve onto the mounted volume, the approval email never mints a duplicate job,
# and a case whose documents carry perimeters from DIFFERENT sources still exports.
