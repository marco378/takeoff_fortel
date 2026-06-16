#!/usr/bin/env python3
"""
Scale + sheet handling (adversarial round 3). Scale is the #1 risk — read it per-viewport,
never hardcode. Breaks this addresses:
  - scale bar mis-detected (a slab edge is a longer horizontal line than the bar)
        -> associate the bar with its 'N m' label by proximity
  - PDF /UserUnit ignored (large drawings) -> area under-sized by UserUnit^2
  - multi-page tender packs -> never assume page 0; route every page
"""
import re, fitz


def detect_scale_bar(pdf, page=0):
    """Find a graphical scale bar: the horizontal line nearest a 'N m' label. -> (k_m_per_pt, info)."""
    p = fitz.open(pdf)[page]
    words = p.get_text("words")
    ms = [w for w in words if w[4].lower() in ("m", "metres", "meters")]
    nums = [w for w in words if re.fullmatch(r"\d{1,4}", w[4])]
    if not (ms and nums):
        return None, "no scale-bar label"
    my = ms[0][1]
    hl = []
    for dr in p.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 2 and abs(a.x - b.x) > 40 and abs(a.y - my) < 60:
                    hl.append(abs(a.x - b.x))
    if not hl:
        return None, "label found but no bar line near it"
    barlen = max(hl)
    label = max(int(w[4]) for w in nums if abs(w[1] - my) < 60)
    return label / barlen, f"{label} m / {barlen:.0f} pt"


def user_unit(pdf, page=0):
    """PDF /UserUnit multiplier (1.0 if absent). Ignoring it under-sizes area by UserUnit^2."""
    d = fitz.open(pdf)
    t, v = d.xref_get_key(d[page].xref, "UserUnit")
    return float(v) if t != "null" else 1.0


def pages(pdf):
    """Multi-page tender packs: page indices to classify+route (never assume page 0)."""
    return list(range(fitz.open(pdf).page_count))


def scale_for(pdf, page=0):
    """Best-effort per-viewport scale: scale bar (verified) x UserUnit, else None+flag."""
    k, info = detect_scale_bar(pdf, page)
    if k is None:
        return None, [f"no verifiable scale ({info}) — assessor must confirm; title-block is unreliable"]
    return k * user_unit(pdf, page), [f"scale bar: {info}; UserUnit={user_unit(pdf, page)}"]
