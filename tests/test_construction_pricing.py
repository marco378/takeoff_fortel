#!/usr/bin/env python3
"""Per-construction pricing: one quote line per construction, summed at the end.

Sections in this module (printed in this order):
  - per-construction pricing

Imported by ci_tests.py, which owns the running total. Do not reorder.

The landmine these checks exist for: a Surface Finishes Plan names several constructions at
several thicknesses, and until 15 Sep 2026 the quotation priced their combined area at ONE
depth. The two things that must stay true are opposites, so both are checked end to end --
different thicknesses must produce different rates on separate rows, and constructions that
all share the zone's own thickness must produce EXACTLY what a job with no breakdown produces.
"""
import copy as _copy, io as _io
from openpyxl import load_workbook as _load_workbook

from tests import ck

print("per-construction pricing")
from quotation import generate_quotation, quotation_xlsx, quotation_text
import construction_pricing as _cp

_SPEC = {"depth_mm": 200, "conc_rate": 120.0, "conc_wastage": 0.05, "mesh": "A393",
         "layers": 2, "steel_rate_t": 900.0, "steel_wastage": 0.05, "lap_acc": 0.15,
         "dpm": 1.2, "curing": 0.8, "labour": 12.0, "trim": 1.5, "margin": 0.1}
_ZONE_RATE, _ = _cp.rate_buildup(**{k: _SPEC[k] for k in _cp.RATE_FIELDS})


def _job(constructions, area_m2=23234.0):
    zone = {"category": "external_yard", "area_m2": area_m2}
    if constructions is not None:
        zone["constructions"] = _copy.deepcopy(constructions)
    return {
        "file": "WP5-SFP.pdf", "pdf_path": "drawings/WP5-SFP.pdf", "area_m2": area_m2,
        "zones": [zone],
        "costing": {"area_m2": area_m2, "rate": _ZONE_RATE,
                    "total_gbp": round(area_m2 * _ZONE_RATE, 2), "spec": dict(_SPEC),
                    "assumed": True, "breakdown": {}},
    }


_THREE = [
    {"name": "HGV Slab Construction", "depth_mm": 200, "detail_ref": "DD-C-1010",
     "area_m2": 4000.0, "region_ids": ["surface-finish-1"]},
    {"name": "Container Slab Construction", "depth_mm": 375, "detail_ref": "DD-C-1020",
     "area_m2": 17689.3, "region_ids": ["surface-finish-2", "surface-finish-2-part-2"]},
    {"name": "Rail Crossing", "depth_mm": 225, "detail_ref": "DD-C-1030",
     "area_m2": 1544.7, "region_ids": ["surface-finish-3"]},
]


def _concrete_rows(q):
    return [(li["description"], li["qty"], li["rate"]) for li in q["line_items"]
            if li.get("line_role") == "concrete_slab"]


_q3 = generate_quotation(_job(_THREE), project="Radlett WP5", client="Fortel", ref="T-CP")
_rows3 = _concrete_rows(_q3)

ck("three constructions produce three priced slab rows", len(_rows3) == 3,
   f"-> {len(_rows3)}")
ck("each priced row carries its own rate", len({r for _d, _q, r in _rows3}) == 3,
   f"-> {sorted(r for _d, _q, r in _rows3)}")
ck("a thicker construction prices higher per m2",
   dict((d.split(' — ')[0], r) for d, _q, r in _rows3).get("Container Slab Construction", 0)
   > dict((d.split(' — ')[0], r) for d, _q, r in _rows3).get("HGV Slab Construction", 1e9))
ck("each row is named by its construction",
   all(any(c["name"] in d for c in _THREE) for d, _q, _r in _rows3),
   f"-> {[d for d, _q, _r in _rows3]}")
ck("each row states its own thickness",
   all(f"{int(c['depth_mm'])}mm" in " ".join(d for d, _q, _r in _rows3) for c in _THREE))
ck("quantities are the constructions' own areas",
   sorted(q for _d, q, _r in _rows3) == sorted(c["area_m2"] for c in _THREE))
ck("no construction area is lost or double counted",
   round(sum(q for _d, q, _r in _rows3), 1) == round(sum(c["area_m2"] for c in _THREE), 1))
ck("the combined value is the sum of the separately priced rows",
   round(sum(q * r for _d, q, r in _rows3), 2)
   == round(sum(_cp.price_constructions(_THREE, _SPEC)[i]["total_gbp"] for i in range(3)), 2))
ck("the quotation declares that the total is a sum of constructions",
   any("separate constructions" in d for d in _q3["declarations"]))

# The gold-path guard. Constructions that all share the zone's own thickness describe the
# SAME slab the old single-line quote described, so the document must be identical -- not
# merely close. If this fails, the change has moved a price on a job it had no business
# touching, which is exactly the failure this whole module is watching for.
_same = [{"name": "A", "depth_mm": 200, "area_m2": 13234.0, "region_ids": ["a"]},
         {"name": "B", "depth_mm": 200, "area_m2": 10000.0, "region_ids": ["b"]}]
_q_same = generate_quotation(_job(_same), project="P", client="C", ref="R")
_q_plain = generate_quotation(_job(None), project="P", client="C", ref="R")
ck("one thickness throughout prices exactly as an unfanned job",
   _q_same["subtotal_gbp"] == _q_plain["subtotal_gbp"],
   f"-> {_q_same['subtotal_gbp']} vs {_q_plain['subtotal_gbp']}")
ck("one thickness throughout keeps one priced slab row",
   _concrete_rows(_q_same) == _concrete_rows(_q_plain),
   f"-> {_concrete_rows(_q_same)}")
ck("a job with no construction breakdown is untouched",
   len(_concrete_rows(_q_plain)) == 1 and _concrete_rows(_q_plain)[0][1] == 23234.0)

# A thickness the legend did not state is never borrowed from a neighbouring construction.
_no_depth = [{"name": "HGV Slab Construction", "depth_mm": 375, "area_m2": 4000.0,
              "region_ids": ["a"]},
             {"name": "Unlabelled Apron", "depth_mm": None, "area_m2": 900.0,
              "region_ids": ["b"]}]
_q_nd = generate_quotation(_job(_no_depth, area_m2=4900.0),
                           project="P", client="C", ref="R")
_nd_priced = _cp.price_constructions(_no_depth, _SPEC)
ck("a construction with no stated thickness falls back to the zone spec, not a neighbour",
   _nd_priced[1]["depth_mm"] == _SPEC["depth_mm"] and _nd_priced[1]["depth_assumed"],
   f"-> {_nd_priced[1]['depth_mm']}")
ck("the assumed thickness is declared on the quotation",
   any("THICKNESS NOT STATED" in d for d in _q_nd["declarations"]),
   f"-> {[d for d in _q_nd['declarations'] if 'THICKNESS' in d]}")

# The legend thickness is a drawing fact, so it must not be recorded as an assessor entry.
_specs3 = {s["area_m2"]: s for s in _q3["specifications"]}
_container_spec = _specs3.get(17689.3, {})
_depth_field = (_container_spec.get("fields") or {}).get("depth_mm") or {}
ck("a legend thickness is recorded with engineer-drawing provenance",
   _depth_field.get("source") == "engineer_drawing", f"-> {_depth_field.get('source')}")
ck("the legend thickness is not marked provisional",
   _depth_field.get("provisional") is False)
ck("the construction's detail reference is cited with it",
   "DD-C-1020" in str((_depth_field.get("evidence") or {}).get("text") or ""),
   f"-> {(_depth_field.get('evidence') or {}).get('text')}")

# The take-off sheet: each construction totals separately, then once combined.
_wb = _load_workbook(_io.BytesIO(quotation_xlsx(_q3)), data_only=False)
_ws = _wb[_wb.sheetnames[0]]
_area_totals = [(_ws.cell(r, 1).value, _ws.cell(r, 2).value)
                for r in range(1, _ws.max_row + 1)
                if isinstance(_ws.cell(r, 1).value, str)
                and _ws.cell(r, 1).value.startswith("Total")
                and "Area" in _ws.cell(r, 1).value]
ck("each construction gets its own Total Area Take Off",
   sum(1 for label, _f in _area_totals if label == "Total Area Take Off:") == 3,
   f"-> {[l for l, _f in _area_totals]}")
ck("the sheet ends the section with Aryan's combined area line",
   any(label == "Total External/Service Yard Slab Area:" for label, _f in _area_totals))
_combined = next((f for label, f in _area_totals
                  if label == "Total External/Service Yard Slab Area:"), "")
ck("the combined line sums the constructions' own totals, not a restated zone area",
   str(_combined).startswith("=B") and str(_combined).count("+") == 2,
   f"-> {_combined}")

_wb_plain = _load_workbook(_io.BytesIO(quotation_xlsx(_q_plain)), data_only=False)
_ws_plain = _wb_plain[_wb_plain.sheetnames[0]]
ck("a job with no constructions gets no combined area line",
   not any(isinstance(_ws_plain.cell(r, 1).value, str)
           and "Yard Slab Area" in _ws_plain.cell(r, 1).value
           for r in range(1, _ws_plain.max_row + 1)))

# The portal card and the client document must not disagree about the same job.
from approval_server import _roll_up_constructions as _roll
_rolled = _roll(_job(_THREE)["costing"], _job(_THREE))
ck("the portal costing totals the same money as the quotation's slab rows",
   abs(_rolled["total_gbp"] - round(sum(q * r for _d, q, r in _rows3), 2)) < 0.05,
   f"-> {_rolled['total_gbp']}")
ck("the portal costing keeps the constructions' own areas",
   _rolled["constructions_area_m2"] == round(sum(c["area_m2"] for c in _THREE), 1))
ck("the portal rate is labelled as an implied average, not a priced rate",
   "not a rate anything was priced at" in _rolled.get("constructions_note", ""))
# Within half a penny per m2 -- the rate is rounded to 2dp for display, so this is the
# tightest the identity can be stated, and it is stated against the area rather than a
# flat currency figure so the check does not loosen as the job gets bigger.
ck("the implied average multiplies back out to the stated total",
   abs(_rolled["rate"] * _rolled["area_m2"] - _rolled["total_gbp"])
   <= 0.005 * _rolled["area_m2"],
   f"-> {_rolled['rate']} x {_rolled['area_m2']} vs {_rolled['total_gbp']}")
ck("a costing with no constructions is returned untouched",
   _roll(_job(None)["costing"], _job(None)) == _job(None)["costing"])
ck("a costing with no spec is returned untouched rather than guessed",
   _roll({"area_m2": 10.0, "rate": 5.0, "total_gbp": 50.0},
         _job(_THREE)) == {"area_m2": 10.0, "rate": 5.0, "total_gbp": 50.0})

# A mixed drawing: only the Yard fans; the Dock alongside it keeps its own single line.
_mixed = _job(_THREE)
_mixed["zones"].append({"category": "dock", "area_m2": 930.0})
_mixed["area_m2"] = 24164.0
_mixed["costing"]["area_m2"] = 24164.0
_q_mixed = generate_quotation(_mixed, project="P", client="C", ref="R")
_sections = [li["section"] for li in _q_mixed["line_items"]
             if li.get("line_role") == "concrete_slab"]
ck("a Dock zone beside a fanned Yard stays one row in its own section",
   _sections.count("Dock slabs") == 1 and _sections.count("External yard slabs") == 3,
   f"-> {_sections}")

_text = quotation_text(_q3)
ck("the plain-text quotation names every construction",
   all(c["name"] in _text for c in _THREE))
ck("the plain-text quotation does not repeat the sum declaration",
   _text.count("separate constructions") == 1, f"-> {_text.count('separate constructions')}")

# The shape production actually produces, and the one every fixture above got wrong.
#
# A Surface Finishes Plan arrives UNMEASURED with NO zones -- the assessor's Yard-region
# review is what measures it -- so its breakdown sits on the RESULT, not on a zone. Portal QA
# on the real Radlett sheet found four constructions (180/200/225/375 mm) being priced as one
# 25,730 m2 slab at the 190 mm default, with the per-construction flag printed above it. Every
# check above passed while that was true, because every fixture above put the constructions on
# a zone the live sheet does not have.
def _zoneless_job(constructions, area_m2=25730.7):
    job = {"file": "radlett_sfp.pdf", "pdf_path": "drawings/radlett_sfp.pdf",
           "area_m2": area_m2, "zones": [],
           "costing": {"area_m2": area_m2, "rate": _ZONE_RATE,
                       "total_gbp": round(area_m2 * _ZONE_RATE, 2), "spec": dict(_SPEC),
                       "assumed": True, "breakdown": {}},
           "perimeter_lm": 640.0}
    if constructions is not None:
        job["constructions"] = _copy.deepcopy(constructions)
    return job


_RADLETT = [
    {"name": "Intermodal Terminal HGV Slab Construction 200mm thick", "depth_mm": 200,
     "area_m2": 2134.9, "region_ids": ["surface-finish-3"]},
    {"name": "Intermodal Terminal HGV Slab Construction 225mm thick", "depth_mm": 225,
     "area_m2": 1564.6, "region_ids": ["surface-finish-4"]},
    {"name": "Intermodal Terminal HGV Slab Construction 180mm thick", "depth_mm": 180,
     "area_m2": 2908.6, "region_ids": ["surface-finish-5"]},
    {"name": "Intermodal Terminal Container Slab Construction 375mm thick", "depth_mm": 375,
     "area_m2": 19122.6, "region_ids": ["surface-finish-6"]},
]

_qz = generate_quotation(_zoneless_job(_RADLETT), project="Radlett", client="Fortel", ref="Z")
_rowsz = _concrete_rows(_qz)
ck("a zone-less reviewed sheet still prices one row per construction", len(_rowsz) == 4,
   f"-> {len(_rowsz)}")
ck("a zone-less sheet's rows carry four different rates",
   len({r for _d, _q, r in _rowsz}) == 4, f"-> {sorted(r for _d, _q, r in _rowsz)}")
ck("no zone-less row is priced at the default thickness the sheet never states",
   not any("190mm" in d for d, _q, _r in _rowsz), f"-> {[d for d, _q, _r in _rowsz]}")
ck("the zone-less quantities are the constructions' own",
   sorted(q for _d, q, _r in _rowsz) == sorted(c["area_m2"] for c in _RADLETT))
ck("a zone-less sheet gets the combined-area declaration",
   any("separate constructions" in d for d in _qz["declarations"]))

_wbz = _load_workbook(_io.BytesIO(quotation_xlsx(_qz)), data_only=False)
_wsz = _wbz[_wbz.sheetnames[0]]
ck("a zone-less sheet gets the combined area line in the workbook",
   any(isinstance(_wsz.cell(r, 1).value, str) and "Yard Slab Area" in _wsz.cell(r, 1).value
       for r in range(1, _wsz.max_row + 1)))

# One slab has one perimeter. Fanning must not multiply the perimeter row by the number of
# constructions, nor drop it: it is the same measured outline either way.
def _perimeter_rows(q):
    # Perimeter is a MEASUREMENT row, not a priced line item -- looking for it in
    # line_items finds nothing on every job and passes vacuously.
    return [m for m in (q.get("measurements") or [])
            if m.get("description") == "Slab perimeter"]


_qz_plain = generate_quotation(_zoneless_job(None), project="Radlett", client="Fortel", ref="Z")
ck("the fixture actually has a perimeter row to protect",
   len(_perimeter_rows(_qz_plain)) == 1, f"-> {len(_perimeter_rows(_qz_plain))}")
ck("fanning does not multiply the slab perimeter row",
   len(_perimeter_rows(_qz)) == len(_perimeter_rows(_qz_plain)),
   f"-> {len(_perimeter_rows(_qz))} vs {len(_perimeter_rows(_qz_plain))}")
ck("...and the perimeter quantity is unchanged by fanning",
   [m["qty"] for m in _perimeter_rows(_qz)] == [m["qty"] for m in _perimeter_rows(_qz_plain)]
   == [640.0],
   f"-> {[m['qty'] for m in _perimeter_rows(_qz)]} vs "
   f"{[m['qty'] for m in _perimeter_rows(_qz_plain)]}")

ck("a zone-less sheet with no constructions is untouched",
   len(_concrete_rows(_qz_plain)) == 1
   and _concrete_rows(_qz_plain)[0][1] == 25730.7)

_rolled_z = _roll(_zoneless_job(_RADLETT)["costing"], _zoneless_job(_RADLETT))
ck("the portal card rolls up a zone-less breakdown too",
   len(_rolled_z.get("constructions") or []) == 4,
   f"-> {len(_rolled_z.get('constructions') or [])}")
ck("the zone-less card total matches the quotation's slab rows",
   abs(_rolled_z["total_gbp"] - round(sum(q * r for _d, q, r in _rowsz), 2)) < 0.05,
   f"-> {_rolled_z['total_gbp']}")
ck("the zone-less card is not still the single blended default",
   _rolled_z["total_gbp"] != _zoneless_job(_RADLETT)["costing"]["total_gbp"])
