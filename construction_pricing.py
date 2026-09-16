#!/usr/bin/env python3
"""Price one Yard zone's constructions individually, then sum them.

Aryan, 15 Sep 2026: each construction on a Surface Finishes Plan gets its own quantity and
its own rate build-up, and the constructions are summed only for a combined total at the end.
``review_yard_regions`` already produces that breakdown as ``zone["constructions"]``; this
module is the one place that turns it into money, so the assessor's portal card and the
client's quotation can never disagree about the arithmetic behind the same job.

The only per-construction input is THICKNESS, which the legend states. Everything else in the
build-up (concrete rate, mesh, layers, wastage, labour, margin) stays the zone's single set of
assumptions, because the drawing does not vary them per construction and this module invents
nothing. ``costing.rate_buildup`` already prices concrete as ``depth_mm / 1000 * conc_rate``,
so a stated thickness is sufficient to produce a genuine per-construction rate with no new
rate data -- no rate value is created, changed, or read from a different source here.

A construction whose thickness the legend did not state does NOT silently inherit another
construction's depth: it is priced at the zone depth and says so, so the quotation carries a
visible assumption rather than a plausible invented number.
"""
import contextlib, io

with contextlib.redirect_stdout(io.StringIO()):
    from costing import rate_buildup

# The exact keyword list costing.rate_buildup is called with everywhere else in this repo.
# Kept here as one tuple so a per-construction rate can never be built from a different set
# of inputs than the job-level rate it has to be comparable with.
RATE_FIELDS = ("depth_mm", "conc_rate", "conc_wastage", "mesh", "layers",
               "steel_rate_t", "steel_wastage", "lap_acc", "dpm", "curing",
               "labour", "trim", "margin")


def zone_constructions(zone) -> list[dict]:
    """Return a zone's per-construction breakdown, or [] for every other zone."""
    if not isinstance(zone, dict):
        return []
    constructions = zone.get("constructions")
    if not isinstance(constructions, list):
        return []
    return [c for c in constructions
            if isinstance(c, dict) and isinstance(c.get("area_m2"), (int, float))]


def results_constructions(result) -> list[tuple[dict, list[dict]]]:
    """Pair each zone of a result with its constructions; zones without them are skipped.

    A sheet that arrived UNMEASURED has no zones -- the assessor's region review is what
    measures it -- so its breakdown sits on the result itself and is returned against a zone
    of None. That is the shape the real Radlett Surface Finishes Plans take, so it is the
    shape that matters most.
    """
    if not isinstance(result, dict):
        return []
    pairs = []
    for zone in (result.get("zones") or []):
        constructions = zone_constructions(zone)
        if constructions:
            pairs.append((zone, constructions))
    if not pairs:
        loose = zone_constructions(result)
        if loose:
            pairs.append((None, loose))
    return pairs


def spec_for_construction(base_spec: dict, construction: dict) -> tuple[dict, bool]:
    """Return (spec at this construction's stated thickness, whether it was stated).

    When the legend states no thickness for this construction, the depth is BLANKED, not
    borrowed from the zone default. Borrowing produced the 16 Sep defect: Radlett's Rail
    Crossing priced 292.5 m2 at the 190 mm default -- GBP 13,182.98 on the client document at
    a thickness the drawing never gave -- directly beneath a specification block reading
    "nothing assumed, nothing priced". Refuse instead of guess: the quantity is real and is
    still shown, the rate is left for the assessor.
    """
    spec = dict(base_spec or {})
    depth = construction.get("depth_mm")
    if isinstance(depth, (int, float)) and depth > 0:
        spec["depth_mm"] = depth
        return spec, False
    spec["depth_mm"] = None
    return spec, True


def price_constructions(constructions, base_spec: dict) -> list[dict]:
    """Price every construction at its own thickness against one shared build-up.

    Returns one entry per construction. ``rate`` is None where the build-up could not be
    computed -- a blank rate cell for the assessor, never a guessed one, exactly as the mixed
    zone path already does.
    """
    priced = []
    for construction in constructions:
        spec, depth_assumed = spec_for_construction(base_spec, construction)
        area_m2 = round(float(construction.get("area_m2") or 0), 1)
        rate, parts = None, {}
        if not depth_assumed:
            try:
                rate, parts = rate_buildup(**{key: spec[key] for key in RATE_FIELDS})
            except Exception:
                rate, parts = None, {}
        priced.append({
            "name": str(construction.get("name") or "").strip() or "Construction",
            "depth_mm": spec.get("depth_mm"),
            "depth_stated": not depth_assumed,
            "depth_assumed": depth_assumed,
            "detail_ref": construction.get("detail_ref"),
            "region_ids": list(construction.get("region_ids") or []),
            "area_m2": area_m2,
            "rate": rate,
            "total_gbp": round(area_m2 * rate, 2) if isinstance(rate, (int, float)) else None,
            "spec": spec,
            "breakdown": parts,
        })
    return priced


def combined(priced) -> dict:
    """Sum priced constructions into the combined total, and the rate that total implies.

    ``area_m2`` is the SUM OF THE CONSTRUCTIONS, not the zone's own stored area: both are
    rounded to 0.1 independently, so taking the zone figure here would let a quotation
    disagree with the rows printed directly above it by 0.1 m2.

    ``rate`` is a reporting figure only -- the area-weighted average that the combined total
    implies. Nothing is priced with it; it exists so a screen that has room for one rate can
    show one that multiplies out to the real total instead of a single construction's.
    """
    area_m2 = round(sum(float(entry.get("area_m2") or 0) for entry in priced), 1)
    # A construction with no stated thickness is not priced, but it is still MEASURED. Summing
    # only the priced ones and reporting the rest separately is what lets the portal card and
    # the quotation agree on a sheet where some constructions price and some cannot -- the
    # alternative, returning None for the whole job, left the card showing a stale blended
    # total beside a quotation with a blank row.
    have_rate = [e for e in priced if isinstance(e.get("total_gbp"), (int, float))]
    no_rate = [e for e in priced if not isinstance(e.get("total_gbp"), (int, float))]
    priced_m2 = round(sum(float(e.get("area_m2") or 0) for e in have_rate), 1)
    total_gbp = round(sum(float(e["total_gbp"]) for e in have_rate), 2) if have_rate else None
    rate = round(total_gbp / priced_m2, 2) if total_gbp is not None and priced_m2 > 0 else None
    return {"area_m2": area_m2, "total_gbp": total_gbp, "rate": rate,
            "priced_area_m2": priced_m2,
            "unpriced_area_m2": round(sum(float(e.get("area_m2") or 0) for e in no_rate), 1),
            "unpriced_names": [e["name"] for e in no_rate],
            "constructions": priced}


def summary_line(entry: dict) -> str:
    """One human line for a priced construction, for flags and declarations."""
    text = f"{entry['name']} — {entry['area_m2']:,.1f} m²"
    if entry.get("depth_mm"):
        text += f" at {entry['depth_mm']:g} mm"
    if isinstance(entry.get("rate"), (int, float)):
        text += f" @ £{entry['rate']:,.2f}/m² = £{entry['total_gbp']:,.2f}"
    else:
        text += " — NO THICKNESS STATED for this construction on the sheet, so it is measured "
        text += "but not priced; rate left for the assessor"
    return text
