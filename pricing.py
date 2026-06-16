#!/usr/bin/env python3
"""
Generalised pricing — price ANY project from measured slab zones + extra-over items.
The hardcoded Winvic BOQ in costing.py is one validated instance (total GBP 1,823,687.32);
this prices arbitrary projects from a takeoff.
"""
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    from costing import rate_buildup, MESH_KG


def slab_rate(z):
    """z: {depth_mm, conc_rate, mesh, layers, steel_rate_t, margin, +optional wastages/adders}.
    Returns (rate_per_m2 or None, flags)."""
    if z["mesh"] not in MESH_KG:
        return None, [f"unknown mesh '{z['mesh']}' — add to rate table"]
    if z["depth_mm"] <= 0 or z["conc_rate"] <= 0:
        return None, ["non-positive thickness/rate"]
    r, _ = rate_buildup(z["depth_mm"], z["conc_rate"], z.get("conc_wastage", 0.03), z["mesh"],
                        z["layers"], z["steel_rate_t"], z.get("steel_wastage", 0.10),
                        z.get("lap_acc", 0.18), z.get("dpm", 0.46), z.get("curing", 0.23),
                        z.get("labour", 10.0), z.get("trim", 0.40), z["margin"])
    return r, []


def price_boq(line_items):
    """line_items: (section, desc, qty, unit, rate). Returns (total, rows)."""
    rows, total = [], 0.0
    for sec, desc, qty, unit, rate in line_items:
        v = round(qty * rate, 2); total += v
        rows.append((sec, desc, qty, unit, rate, v))
    return round(total, 2), rows


def price_project(zones, extras=None):
    """zones: list of {name, area_m2, + slab_rate params}. extras: list of BOQ tuples.
    Returns (total, rows)."""
    items = []
    for z in zones:
        r, _ = slab_rate(z)
        if r is not None and z.get("area_m2", 0) > 0:
            items.append((z["name"], f"{z['depth_mm']}mm slab", z["area_m2"], "m2", r))
    items += (extras or [])
    return price_boq(items)
