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


def scale_consensus(refs, tol=0.10):
    """refs: list of (real_metres, span_units) from a scale bar / dimensions. -> (k, flags).
    If references DISAGREE beyond tol the sheet is MIXED-SCALE -> return None + flag (never emit).
    This is the direct fix for the 95,463 m² incident: the flow auto-picked one dimension
    (257.2 m @ 1:500) to scale a slab drawn at 1:306, a different viewport — 2.67x too big."""
    ks = [m / s for m, s in refs if (s and s > 0 and m is not None and m > 0)]
    if not ks:
        return None, ["no usable scale reference"]
    lo, hi = min(ks), max(ks)
    if hi / lo - 1 > tol:
        return None, [f"scale references DISAGREE ({lo:.4f}..{hi:.4f} m/unit, {hi/lo:.2f}x spread) -> "
                      "MIXED-SCALE sheet; use the slab's OWN viewport (scale bar) or assessor confirms. "
                      "DO NOT auto-pick a dimension."]
    return sum(ks) / len(ks), [f"scale consensus k={sum(ks)/len(ks):.4f} ({len(ks)} refs agree within {int(tol*100)}%)"]


# ---- Fortel's method (from the estimation call): VERIFY scale against a known real-world feature ----
UK_PARKING_BAY_M = 2.5   # UK standard car-parking bay width — Fortel's go-to scale check
PT_PER_M = 0.0254 / 72   # 1 PDF point in metres of paper


def title_block_k(denominator):
    """k (m/pt) implied by a stated drawing scale 1:N. Fortel enter '1 mm = N/1000 m'."""
    return denominator * PT_PER_M if denominator else None


def scale_from_bay(bay_width_pt, bay_m=UK_PARKING_BAY_M):
    """Calibrate m/pt from a measured car-parking bay (UK standard 2.5 m)."""
    return bay_m / bay_width_pt if bay_width_pt else None


def verify_against_feature(k, span_pt, real_m, tol=0.05):
    """Check scale k against a known feature (bay 2.5 m / printed dimension / scale bar).
    Returns flags ([] == verified). THIS is the step that would have caught 95,463 m²."""
    if not (k and span_pt and real_m):
        return ["cannot verify — no known feature measured"]
    got = k * span_pt
    if abs(got - real_m) / real_m > tol:
        return [f"SCALE UNVERIFIED: a {real_m} m feature reads {got:.2f} m ({got/real_m:.2f}x) — "
                "scale is wrong (likely a stale title-block scale); recalibrate from the feature"]
    return []


def calibrate_verified(title_denominator=None, bay_width_pt=None, dim_span_pt=None, dim_m=None):
    """Fortel's method: take the title-block scale, then VERIFY against a known feature; the feature
    WINS on conflict (the PDF is often not at its stated scale). Returns (k_m_per_pt, flags)."""
    k_title = title_block_k(title_denominator)
    k_feat, feat = None, None
    if bay_width_pt:
        k_feat, feat = scale_from_bay(bay_width_pt), f"parking bay 2.5 m / {bay_width_pt:.0f} pt"
    elif dim_span_pt and dim_m:
        k_feat, feat = dim_m / dim_span_pt, f"dimension {dim_m} m / {dim_span_pt:.0f} pt"
    if k_feat is None:
        return k_title, (["scale from title block ONLY — UNVERIFIED; measure a parking bay (2.5 m) "
                          "or a printed dimension before trusting the area"] if k_title else ["no scale reference"])
    flags = [f"scale VERIFIED from {feat}: k={k_feat:.4f}"]
    if k_title and abs(k_title - k_feat) / k_feat > 0.05:
        flags.insert(0, f"title-block scale (k={k_title:.4f}) DISAGREES with the verified feature "
                        f"(k={k_feat:.4f}) -> PDF not at its stated scale; using the feature scale")
    return k_feat, flags
