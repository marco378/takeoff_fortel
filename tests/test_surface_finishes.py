#!/usr/bin/env python3
"""The Surface Finishes Plan legend names the priced surface; its hatch colour finds it.

Radlett WP5 0700/0701 identifies each construction in its legend and draws it in that row's
hatch colour. Aryan confirmed on 14 Sep that this is the identity source and that the CAD
layer is supporting information only — the layer names on those sheets are measurably wrong.

The synthetic sheet here is built at rotation 0 AND rotation 270 and the two must agree
exactly. That is not ceremony: page.get_text and page.get_drawings return UNROTATED
coordinates while the raster and the reader work in the rotated frame, and that mismatch has
cost this project twice — once putting a whole mask somewhere else on the sheet, once reading
a sheet's Notes column and reporting it as the legend. A fixture drawn only upright cannot
catch it.

The block's area is known by construction, so this is one of the few places in the suite where
a measurement is checked against a real answer rather than against itself.

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import tempfile
from pathlib import Path

import fitz
import cv2
import numpy as np

import hatch_legend_raster
import layer_surfaces
import surface_finishes
from tests import ck
from tests._sfp_fixture import build as build_sfp

print("surface finishes plan (legend colour identity + hatch extent)")

_K = 0.5 * 25.4 / 72          # 1:500 on a sheet measured in points
_S = 2.0
# The fixture draws each construction as a 280 x 420 pt block.
_TRUTH_M2 = (280 * _K) * (420 * _K)

_DIR = Path(tempfile.mkdtemp(prefix="sfp_fixture_"))
_SHEETS = {}
for _rotation in (0, 270):
    _path = _DIR / f"sfp_{_rotation}.pdf"
    build_sfp(str(_path), _rotation)
    _SHEETS[_rotation] = _path


def _measure(path):
    page = fitz.open(str(path))[0]
    legend = surface_finishes.read_legend(page)
    if not legend["ok"]:
        return legend, {}
    exclusion = surface_finishes._legend_exclusion(legend["rows"], page.rotation_matrix)
    out = {}
    for row in legend["rows"]:
        ink, marks = surface_finishes._colour_ink(page, row["colour"], exclusion, _S)
        period = surface_finishes._hatch_period_px(ink)
        radius = period * surface_finishes.CLOSE_AT_PERIOD
        closer = layer_surfaces._Closer(ink, radius + 2)
        closed, _voids, _n = layer_surfaces._fill_holes(
            closer.close(radius), per_px_m2=(_K / _S) ** 2, keep_m2=layer_surfaces.HOLE_KEEP_M2)
        kept, areas, _parts = surface_finishes._row_regions(closed, ink, _S, _K)
        out[row["name"]] = {
            "area_m2": sum(areas), "depth_mm": row["depth_mm"], "marks": marks,
            "retention": float((kept & ink.astype(bool)).sum()) / float(ink.sum()),
            "period_m": period * _K / _S,
        }
    return legend, out


_upright_legend, _upright = _measure(_SHEETS[0])
_unsplit_doc = fitz.open(str(_SHEETS[0]))
_upright_offer = surface_finishes.candidates(_unsplit_doc[0], _K, S=_S)
_unsplit_doc.close()
_rotated_legend, _rotated = _measure(_SHEETS[270])

ck("the sheet declares itself a Surface Finishes Plan",
   surface_finishes.is_surface_finishes_plan(fitz.open(str(_SHEETS[0]))[0]))
_plain_path = _DIR / "plain.pdf"
_plain = fitz.open()
_plain_page = _plain.new_page(width=842, height=595)
_plain_page.insert_text((60, 60), "PROPOSED SITE PLAN", fontsize=12)
_plain.save(str(_plain_path))
_plain.close()
ck("an ordinary site plan does not — the gate is the sheet's own title",
   not surface_finishes.is_surface_finishes_plan(fitz.open(str(_plain_path))[0]))

ck("the legend reads on an upright sheet", _upright_legend["ok"], _upright_legend["reason"])
ck("...and on the SAME sheet rotated 270 degrees",
   _rotated_legend["ok"], _rotated_legend["reason"])
ck("both rotations find the same constructions",
   sorted(_upright) == sorted(_rotated) and len(_upright) == 2, list(_upright))

_hgv = next(name for name in _upright if "HGV" in name)
_container = next(name for name in _upright if "Container" in name)

ck("each construction carries the thickness its own legend row states",
   _upright[_hgv]["depth_mm"] == 200 and _upright[_container]["depth_mm"] == 375,
   {name: _upright[name]["depth_mm"] for name in _upright})

ck("the measured area is within 2% of the block's true area",
   all(abs(row["area_m2"] - _TRUTH_M2) / _TRUTH_M2 < 0.02 for row in _upright.values()),
   {"truth_m2": round(_TRUTH_M2), **{n: round(r["area_m2"]) for n, r in _upright.items()}})

ck("rotating the sheet does not move the answer",
   all(abs(_upright[name]["area_m2"] - _rotated[name]["area_m2"]) < 1.0 for name in _upright),
   {n: (round(_upright[n]["area_m2"]), round(_rotated[n]["area_m2"])) for n in _upright})

ck("the outline holds the hatch it claims to have measured",
   all(row["retention"] >= 0.95 for row in _upright.values()),
   {n: round(r["retention"], 3) for n, r in _upright.items()})

ck("the period is measured on the PLAN, not the denser legend swatch",
   all(row["period_m"] > 2.0 for row in _upright.values()),
   {n: round(r["period_m"], 2) for n, r in _upright.items()})

ck("two legend rows within the colour tolerance are NOT separately identifiable",
   surface_finishes._same_colour((0.0, 1.0, 1.0), (0.0, 0.99, 0.98)))
ck("...while the real legend's own swatches are comfortably apart",
   not surface_finishes._same_colour((0.0, 1.0, 1.0), (1.0, 0.75, 0.0))
   and not surface_finishes._same_colour((0.0, 1.0, 0.0), (0.36, 0.72, 0.0)))

# The 225mm channelised edge arrives as ~30 fragments, each under MIN_REGION_M2. A
# per-component floor discarded every one and reported 0 m² for a real 1,565 m² strip.
_fragments = np.zeros((200, 700), np.uint8)
for _index in range(6):
    _fragments[40:120, 20 + _index * 110:100 + _index * 110] = 1
_frag_kept, _frag_areas, _frag_parts = surface_finishes._row_regions(
    _fragments, _fragments, _S, _K)
ck("a construction drawn in many small pieces keeps all of them",
   len(_frag_areas) == 6 and _frag_kept.sum() == _fragments.sum(),
   {"pieces": len(_frag_areas),
    "each_m2": round(_frag_areas[0], 1) if _frag_areas else None,
    "floor_m2": layer_surfaces.MIN_REGION_M2})
ck("...each of which is individually below the floor that would have binned it",
   bool(_frag_areas) and max(_frag_areas) < layer_surfaces.MIN_REGION_M2
   and sum(_frag_areas) > layer_surfaces.MIN_REGION_M2,
   {"max_piece_m2": round(max(_frag_areas), 1) if _frag_areas else None,
    "total_m2": round(sum(_frag_areas), 1)})

ck("the closing radius is derived from the hatch, never searched",
   surface_finishes.CLOSE_AT_PERIOD == 1.5)

# 0701 draws its Container slab in two separate places. It measures 17,689 m2 and no single
# outline reproduces that number, so the whole construction was withheld -- the biggest thing
# on the sheet, absent from the offer. A construction drawn in two places is still one
# construction; it is offered a piece at a time, each piece a shape the assessor can see.
_SPLIT = {}
for _rotation in (0, 270):
    _split_path = _DIR / f"sfp_split_{_rotation}.pdf"
    build_sfp(str(_split_path), _rotation, split=True)
    _doc = fitz.open(str(_split_path))
    _SPLIT[_rotation] = surface_finishes.candidates(_doc[0], _K, S=_S)
    _doc.close()

_split_rows = [r for r in _SPLIT[0]["rows"] if "Container" in r["name"]]
_split_parts = [r for r in _split_rows if r.get("part_count")]
ck("a construction drawn in two places is offered as two parts, not withheld",
   _SPLIT[0]["ok"] and len(_split_parts) == 2
   and {r["part_index"] for r in _split_parts} == {1, 2},
   {"rows": len(_split_rows), "parts": len(_split_parts),
    "reason": _split_rows[0]["reason"] if _split_rows else None})

ck("every part carries its own outline — an offer with no shape is not an offer",
   bool(_split_parts) and all(r["polygon_pts"] for r in _split_parts))

_split_total = sum(r["area_m2"] for r in _split_parts)
# Each part is measured against ITS OWN block, which is the claim that matters: a part is
# offered as a shape on the sheet, so a part must be right. The sum runs ~2% under the
# undivided truth rather than the ~1% a single block does, and that is arithmetic, not drift
# -- reconstruction loses a sliver along every edge, and cutting one block into two adds two
# new edges. Asserting the sum alone would let a genuinely bad part hide behind a good one.
_PART_TRUTH_M2 = (140 * _K) * (420 * _K)
ck("each part is within 3% of the true area of the piece it traces",
   bool(_split_parts)
   and all(abs(r["area_m2"] - _PART_TRUTH_M2) / _PART_TRUTH_M2 < 0.03 for r in _split_parts),
   {"part_truth_m2": round(_PART_TRUTH_M2),
    "each": [round(r["area_m2"]) for r in _split_parts]})

# The fixture draws the two pieces the same size on purpose. Reconstruction may lose a sliver
# to every edge, but it must lose the SAME sliver to both -- two identical blocks measuring
# differently would mean the loss depends on where a piece sits, and a loose absolute
# tolerance would never show it.
ck("...and two identically-sized pieces measure the same, within 1%",
   len(_split_parts) == 2
   and abs(_split_parts[0]["area_m2"] - _split_parts[1]["area_m2"])
       / max(r["area_m2"] for r in _split_parts) < 0.01,
   {"each": [round(r["area_m2"], 1) for r in _split_parts]})

ck("the parts together account for the whole construction, within 3%",
   bool(_split_parts) and abs(_split_total - _TRUTH_M2) / _TRUTH_M2 < 0.03,
   {"truth_m2": round(_TRUTH_M2), "parts_m2": round(_split_total),
    "each": [round(r["area_m2"]) for r in _split_parts]})

ck("...and each part says what they all add up to, so including both is an informed choice",
   bool(_split_parts)
   and all(abs(r["part_total_m2"] - _split_total) < 1.0 for r in _split_parts),
   {"stated_total": _split_parts[0]["part_total_m2"] if _split_parts else None})

_split_rot = [r for r in _SPLIT[270]["rows"] if r.get("part_count") and "Container" in r["name"]]
ck("rotating the split sheet 270 degrees does not move the parts or their areas",
   len(_split_rot) == len(_split_parts)
   and all(abs(a["area_m2"] - b["area_m2"]) < 1.0
           for a, b in zip(sorted(_split_parts, key=lambda r: -r["area_m2"]),
                           sorted(_split_rot, key=lambda r: -r["area_m2"]))),
   {"upright": [round(r["area_m2"]) for r in _split_parts],
    "rotated": [round(r["area_m2"]) for r in _split_rot]})

# The portal found this and the unit tests did not: numbering the offer by output position
# made part 2 of one construction read as a different construction (surface-finish-6-part-1
# beside surface-finish-7-part-2). The region id has to be anchored to the legend row.
ck("both parts of one construction carry the SAME legend row ordinal",
   len(_split_parts) == 2
   and _split_parts[0]["row_ordinal"] == _split_parts[1]["row_ordinal"],
   {"ordinals": [r.get("row_ordinal") for r in _split_parts]})

ck("...and an unsplit sheet's ordinals are still its legend order, so existing ids do not move",
   [r["row_ordinal"] for r in _upright_offer["rows"]]
   == list(range(1, len(_upright_offer["rows"]) + 1)),
   {"ordinals": [r["row_ordinal"] for r in _upright_offer["rows"]]})

_whole = [r for r in _SPLIT[0]["rows"] if "HGV" in r["name"]]
ck("a construction that closes into ONE piece is still offered whole, not split",
   len(_whole) == 1 and not _whole[0].get("part_count") and _whole[0]["polygon_pts"],
   {"parts": _whole[0].get("part_count") if _whole else None})

_single = [r for r in _upright_offer["rows"] if r["area_m2"] is not None]
ck("the unsplit sheet is untouched by the multi-part path — no row gains parts",
   bool(_single) and not any(r.get("part_count") for r in _single),
   {"offered": len(_single)})


# Aryan, 15 Sep: the markup must follow the actual boundary, not the individual hatch strokes.
# The outline is only located to within the closing radius, but it was simplified at
# 0.001 x perimeter -- far finer -- so every wobble the stroke ends left in the mask survived
# into the polygon. On 0701's Container slab that was 97 vertices jagging in and out by about
# one hatch cell while the real edge ran straight alongside.
_SAW = np.zeros((400, 900), np.uint8)
_SAW[80:320, 60:840] = 1
for _i in range(60, 840, 20):                 # a sawtooth along the top edge, 12 px deep
    _SAW[80:92, _i:_i + 10] = 0
_saw_area = float(_SAW.sum()) * (_K / _S) ** 2
_fine_poly, _fine_holes, _fine_fid = hatch_legend_raster._outline_for(_SAW, _saw_area, _S, _K)
_coarse_poly, _coarse_holes, _coarse_fid = hatch_legend_raster._outline_for(
    _SAW, _saw_area, _S, _K, min_eps_px=24 * surface_finishes.OUTLINE_EPS_OF_RADIUS * 4)

ck("a sawtooth edge traces as many vertices when simplified finer than it",
   bool(_fine_poly) and len(_fine_poly) > 40, {"pts": len(_fine_poly or [])})
ck("...and collapses to the real rectangle once the tolerance matches the resolution",
   bool(_coarse_poly) and len(_coarse_poly) <= 12,
   {"pts": len(_coarse_poly or []), "was": len(_fine_poly or [])})
ck("...without the area walking off — the fidelity check still governs",
   bool(_coarse_poly) and _coarse_fid is not None and _coarse_fid < 0.05,
   {"fidelity": round(_coarse_fid, 4) if _coarse_fid is not None else None})

# The floor is OPT-IN. hatch_legend_raster._outline_for is on the shipped MJM/gold path, and a
# silently coarser outline there would move numbers this project treats as facts.
_default_poly, _d_holes, _d_fid = hatch_legend_raster._outline_for(_SAW, _saw_area, _S, _K)
ck("the default is byte-identical to the historical behaviour — gold paths cannot drift",
   _default_poly == _fine_poly)

# A thin strip must survive: the 225mm channelised edge is a real ring round the slab, and a
# tolerance near its own width would eat it rather than smooth it.
_STRIP = np.zeros((400, 900), np.uint8)
_STRIP[190:210, 60:840] = 1
_strip_area = float(_STRIP.sum()) * (_K / _S) ** 2
_strip_poly, _sh, _sfid = hatch_legend_raster._outline_for(
    _STRIP, _strip_area, _S, _K, min_eps_px=400)
ck("a thin strip is not destroyed by an oversized tolerance",
   _strip_poly is None or (_sfid is not None and _sfid < hatch_legend_raster.OUTLINE_FIDELITY_TOL),
   {"poly": None if _strip_poly is None else len(_strip_poly),
    "fidelity": None if _sfid is None else round(_sfid, 3)})

ck("the simplification tolerance is tied to the hatch's own radius, not the perimeter",
   0 < surface_finishes.OUTLINE_EPS_OF_RADIUS <= 0.5,
   {"OUTLINE_EPS_OF_RADIUS": surface_finishes.OUTLINE_EPS_OF_RADIUS})

# A construction drawn in pieces where only ONE clears the floor is not "part 1 of 1". That
# phrasing promises a part 2 that will never be offered, and the note read "drawn in 1 separate
# places". Not reachable on 0700/0701, so it is built here rather than waiting for a client
# sheet to expose it.
class _OnePieceOnly:
    """_outline_for that refuses the whole row but accepts a single piece."""
    def __init__(self, real, whole_mask):
        self._real, self._whole = real, whole_mask
    def __call__(self, mask, area_m2, S, k, min_eps_px=None):
        if mask.shape == self._whole.shape and int(mask.sum()) == int(self._whole.sum()):
            return None, [], 0.99
        return self._real(mask, area_m2, S, k, min_eps_px=min_eps_px)


_BIG = 260.0 / ((_K / _S) ** 2)            # comfortably over MIN_REGION_M2
_SMALL = 60.0 / ((_K / _S) ** 2)           # under it
_PIECES = np.zeros((300, 900), np.uint8)
_side = int(_BIG ** 0.5)
_PIECES[40:40 + _side, 40:40 + _side] = 1
_sside = int(_SMALL ** 0.5)
_PIECES[40:40 + _sside, 700:700 + _sside] = 1
_whole_kept, _whole_areas, _whole_parts = surface_finishes._row_regions(
    _PIECES, _PIECES, _S, _K)
ck("the fixture really is two pieces, one over the floor and one under",
   len(_whole_parts) == 2
   and max(a for _m, a in _whole_parts) > layer_surfaces.MIN_REGION_M2
   and min(a for _m, a in _whole_parts) < layer_surfaces.MIN_REGION_M2,
   {"areas": [round(a, 1) for _m, a in _whole_parts]})

_saved = hatch_legend_raster._outline_for
try:
    hatch_legend_raster._outline_for = _OnePieceOnly(_saved, _whole_kept.astype(np.uint8))
    _one_poly, _oh, _ofid = hatch_legend_raster._outline_for(
        _whole_kept.astype(np.uint8), sum(_whole_areas), _S, _K)
    _big_mask = max(_whole_parts, key=lambda p: p[1])[0]
    _big_poly, _bh, _bfid = hatch_legend_raster._outline_for(
        _big_mask.astype(np.uint8), max(a for _m, a in _whole_parts), _S, _K)
finally:
    hatch_legend_raster._outline_for = _saved

ck("the whole row refuses an outline while its largest piece traces one",
   _one_poly is None and bool(_big_poly),
   {"whole": _one_poly, "piece_pts": len(_big_poly or [])})
# Drive the real offer with a landmine in place: any mask holding more than one component
# refuses, so the whole row cannot trace but a single piece can. The entry must come back as
# ONE construction, never a lone "part 1 of 1".
_saved2 = hatch_legend_raster._outline_for
_one_doc = fitz.open(str(_SHEETS[0]))
try:
    def _only_single(mask, area_m2, S, k, min_eps_px=None, _real=_saved2):
        count, _lab, _st, _ct = cv2.connectedComponentsWithStats(
            (np.asarray(mask) > 0).astype(np.uint8), 8)
        if count - 1 > 1:
            return None, [], 0.99
        return _real(mask, area_m2, S, k, min_eps_px=min_eps_px)

    hatch_legend_raster._outline_for = _only_single
    _forced = surface_finishes.candidates(_one_doc[0], _K, S=_S)
finally:
    hatch_legend_raster._outline_for = _saved2
    _one_doc.close()

_forced_rows = [r for r in _forced["rows"] if r["area_m2"] is not None]
ck("with only single pieces traceable, every offer is a whole construction",
   bool(_forced_rows) and not any(r.get("part_count") for r in _forced_rows),
   {"offered": len(_forced_rows),
    "part_counts": [r.get("part_count") for r in _forced_rows]})
ck("...and none is labelled as a part",
   not any("(part " in (r.get("name") or "") for r in _forced_rows))

import shutil as _shutil
_shutil.rmtree(_DIR, ignore_errors=True)
