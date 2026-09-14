#!/usr/bin/env python3
"""classify() must not walk every annotation to find the area markups.

Radlett WP5 RAD-BWB-A1EX-IT1-DP-C-1301 is a photometric lighting sheet carrying 36,135
lux-grid square annotations. page.annots() loads each one by xref, and that lookup rescans
the annotation list from the beginning, so the walk is O(n^2): the sheet hung past the
robustness harness's 180s watchdog and produced NO state at all — not an area, not a
refusal. That is the one outcome the four-state contract forbids, and a hang is worse than
a crash because nothing downstream can even see it happened.

The regression is guarded the deterministic way rather than by a stopwatch: page.annots()
is replaced with a landmine, so any future edit that reintroduces the full walk fails here
instead of quietly costing three minutes a sheet.

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import inspect
import time
from pathlib import Path

import fitz
from tests import _FixtureNotPresent, _require_fixture, ck

print("router annotation scaling")
import router
from router import classify

_TMP = Path(__file__).resolve().parent.parent / "_tmp_router_annots.pdf"
_doc = fitz.open()
_page = _doc.new_page(width=842, height=595)
for _i in range(60):
    _page.draw_line((_i, 0), (_i + 5, 40))
for _i in range(40):
    _page.add_rect_annot(fitz.Rect(_i * 4, 300, _i * 4 + 3, 303))
_marked = _page.add_polygon_annot([(20, 20), (120, 20), (120, 120), (20, 120)])
_marked.set_info(content="Area: 1,234.5 sq m")
_marked.update()
_plain = _page.add_polygon_annot([(200, 20), (300, 20), (300, 120), (200, 120)])
_plain.set_info(content="a cloud around a note")
_plain.update()
_doc.save(str(_TMP))
_doc.close()

_landmine_hits = []
_real_annots = fitz.Page.annots


def _landmine(self, *a, **kw):
    _landmine_hits.append(1)
    return _real_annots(self, *a, **kw)


try:
    fitz.Page.annots = _landmine
    _typ, _route, _conf, _detail = classify(str(_TMP))
    ck("classify never walks the annotation list — the O(n^2) path stays unused",
       not _landmine_hits, f"page.annots() called {len(_landmine_hits)} time(s)")
finally:
    fitz.Page.annots = _real_annots

ck("a Bluebeam area polygon is still counted",
   _detail.get("area_markups") == 1, _detail)
ck("...and a polygon that is not an area markup is not counted",
   _detail.get("area_markups") == 1 and _typ == "MARKED vector", (_typ, _detail))

_source = inspect.getsource(router.classify)
ck("classify filters by annotation subtype before loading anything",
   "annot_xrefs()" in _source and "PDF_ANNOT_POLYGON" in _source)
ck("the 300k-path page is scanned once, not twice",
   _source.count("get_drawings()") == 1, _source.count("get_drawings()"))

_TMP.unlink(missing_ok=True)

_photometric = Path("drawings/radlett_wp5/wp5_33.pdf")
try:
    _require_fixture(_photometric, "Radlett WP5 photometric sheet not present")
    _started = time.time()
    _typ2, _route2, _conf2, _detail2 = classify(str(_photometric))
    _elapsed = time.time() - _started
    ck("the 36,135-annotation photometric sheet classifies instead of hanging",
       _elapsed < 60, f"{_elapsed:.1f}s, {_detail2.get('vector_paths')} vector paths")
    ck("...and it reads as an unmarked vector sheet with no area markups",
       _typ2 == "UNMARKED vector" and _detail2.get("area_markups") == 0, _detail2)
except _FixtureNotPresent as _e:
    print(f"  [SKIP] photometric sheet scaling guard — {_e} — fixture not present")
