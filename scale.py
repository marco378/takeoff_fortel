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
    """Find a graphical scale bar: the line associated with a 'N m' label. -> (k_m_per_pt, info).

    Hardened against the #1 real-world miss: a slab edge or sheet BORDER rule that runs near the
    scale-bar label is longer than the bar, so the old `max(length)` grabbed it and under-scaled the
    drawing (e.g. a 700 pt border on an 842 pt sheet -> k 2.5x too small -> area ~6x too small).
    Two guards:
      1. Drop near-full-width lines (> BORDER_FRAC of the relevant page dimension) — those are
         borders/title rules, not scale bars (a real bar is a small fraction of the sheet).
      2. Among the remaining candidates, associate by PROXIMITY to the label's position (the 'N m'
         text sits at/just past the bar's end), not by raw length. Length is only the tiebreak.

    ROTATION-AGNOSTIC (real-sheet fix): every real Fortel A0/A1 sheet tested is landscape content
    stored in a portrait MediaBox with page /Rotate 90 or 270. PyMuPDF's get_drawings()/get_text
    both report RAW, PRE-ROTATION content-stream coordinates, so a visually-horizontal scale bar on
    those sheets is drawn as a stack of near-VERTICAL strokes in this function's coordinate frame.
    The old code only ever looked for `abs(a.y-b.y)<2` (horizontal) lines, so it silently never
    matched on any rotated real sheet. Fixed by testing EACH line for near-horizontal OR
    near-vertical orientation and doing every downstream check (border-frac, proximity, label y/x
    band) along that candidate's own dominant axis, symmetric between the two orientations.

    Also widened to accept a fused 'NNm' terminal-tick token (e.g. '25m') as BOTH the label anchor
    and its own numeric value — real Fortel bars segment as '0 5 10 15 20 25m' with PyMuPDF fusing
    the last tick to its unit, which the old bare-'m'-token / pure-digit-token regexes both rejected.

    SEGMENTED BARS (alternating-fill tick blocks): real Fortel bars are not one long line — they are
    several small rectangles (each drawn as 4 short line segments) stacked edge-to-edge, e.g. 4 blocks
    of ~28 pt each for a '0 5 10 15 20 25m' bar. No single segment is a long line, so candidates are
    first CLUSTERED: short same-axis segments whose cross-axis position agrees (within CLUSTER_TOL)
    are grouped, then the group's along-axis span is merged into contiguous runs (small gaps bridged,
    since adjacent alternating-fill blocks butt against each other) to recover the bar's true total
    length. This also still finds a plain single-line bar (the existing synthetic-fixture style) —
    it is just a cluster of one segment.

    Never crashes on a page with no usable scale-bar shape: falls through label candidates (instead
    of anchoring on text-extraction order via ms[0]) and returns (None, ...) cleanly if none pair to
    a line+digits, rather than raising on an empty max()/generator.
    """
    p = fitz.open(pdf)[page]
    page_w, page_h = p.rect.width, p.rect.height
    BORDER_FRAC = 0.70                       # lines wider than this fraction of the sheet are borders
    PROX_BAND = 60                           # pt: how close (cross-axis) a line must be to a label
    CLUSTER_TOL = 3                          # pt: same-axis segments this close in cross-pos are one bar
    GAP_TOL = 3                              # pt: along-axis gap this small between segments still merges

    words = p.get_text("words")
    bare_ms = [w for w in words if w[4].lower() in ("m", "metres", "meters")]
    fused_ms = [w for w in words if re.fullmatch(r"(\d{1,4})\s*(m|metres|meters)", w[4], re.I)]
    nums = [w for w in words if re.fullmatch(r"\d{1,4}", w[4])]

    # Label candidates: each is (x, y, forced_value_or_None). Fused 'NNm' tokens carry their own
    # terminal tick value directly (bypassing the separate nums/max() step for that candidate).
    # Fused tokens are tried FIRST and exclusively (Fortel's own bar-label convention — the terminal
    # tick fused to its unit) before falling back to bare 'm'/'metres'/'meters' tokens: a bare 'm' is
    # frequently an unrelated unit suffix on a dimension callout elsewhere on the sheet (e.g. "110m"
    # split into '110' + 'm' by the tokenizer) which can coincidentally have digit neighbours within
    # the proximity band and would otherwise out-rank the real bar by pure page position.
    fused_cands = [(w[0], w[1], int(re.match(r"\d{1,4}", w[4]).group())) for w in fused_ms]
    bare_cands = [(w[0], w[1], None) for w in bare_ms]
    label_cand_tiers = [fused_cands, bare_cands]
    if not (fused_cands or bare_cands):
        return None, "no scale-bar label"

    def line_axis(a, b):
        """-> ('h', length, cross_pos, span) or ('v', ...) or None if neither near-degenerate axis.
        No minimum length here (unlike the border/proximity guards below) — segmented bars are made
        of short pieces that must still enter the clustering pool."""
        if abs(a.y - b.y) < 2 and abs(a.x - b.x) > 1:
            return "h", abs(a.x - b.x), (a.y + b.y) / 2, (min(a.x, b.x), max(a.x, b.x))
        if abs(a.x - b.x) < 2 and abs(a.y - b.y) > 1:
            return "v", abs(a.y - b.y), (a.x + b.x) / 2, (min(a.y, b.y), max(a.y, b.y))
        return None

    def rect_axis(r):
        """A thin filled/stroked rectangle (a tick block) also counts as a bar segment along its
        LONG axis — some PDF producers (and reportlab) emit scale-bar blocks as 're' rectangle
        primitives rather than 4 exploded 'l' line segments. -> same shape as line_axis, or None
        if the rect isn't thin-and-long enough to read as a bar tick (i.e. it's square-ish)."""
        w, h = r.width, r.height
        if w <= 0 or h <= 0:
            return None
        if h >= w * 3:                       # tall & thin -> vertical bar segment
            return "v", h, (r.x0 + r.x1) / 2, (r.y0, r.y1)
        if w >= h * 3:                       # wide & short -> horizontal bar segment
            return "h", w, (r.y0 + r.y1) / 2, (r.x0, r.x1)
        return None

    all_lines = []
    for dr in p.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                res = line_axis(it[1], it[2])
                if res:
                    all_lines.append(res)
            elif it[0] == "re":
                res = rect_axis(it[1])
                if res:
                    all_lines.append(res)

    def merged_runs(segs):
        """segs: list of (lo, hi) spans sharing one axis/cross-pos cluster. -> list of merged (lo,hi),
        bridging gaps <= GAP_TOL (adjacent alternating-fill tick blocks butt together)."""
        runs = []
        for lo, hi in sorted(segs):
            if runs and lo <= runs[-1][1] + GAP_TOL:
                runs[-1] = (runs[-1][0], max(runs[-1][1], hi))
            else:
                runs.append((lo, hi))
        return runs

    # Try each label candidate in turn (deterministic order: bottom-right quadrant of the page
    # first, matching Fortel's title-block convention) instead of blindly anchoring on ms[0] /
    # text-extraction order — a label with no paired line/digits is skipped, never crashed on.
    # Tiers: fused 'NNm' tokens first and EXCLUSIVELY when any exist (Fortel's actual bar-label
    # convention), falling back to bare 'm' tokens only if no fused label pairs to anything — a
    # bare 'm' is usually a unit suffix on an unrelated dimension callout elsewhere on the sheet
    # and must never outrank a real fused terminal-tick label.
    def quadrant_key(lc):
        x, y, _ = lc
        # prefer labels further toward bottom-right (larger x+y) — title block convention
        return -(x + y)

    def ordered_labels(tier):
        """Order graphical-bar tick labels without mistaking an intermediate tick for the end.

        The Castle Donington Office GAs expose every graphical tick as a fused token on the
        same raw PDF axis (``1m``, ``5m``, ``10m``).  Sorting those solely by page position chose
        ``1m`` but paired it with the full 283 pt bar, producing a 10x scale error.  When two or
        more fused values are visibly aligned, try the largest/terminal value first.  A lone
        fused token and all bare ``m`` labels retain the existing title-block position order.
        """
        aligned_tol = 6.0

        def key(lc):
            x, y, value = lc
            if value is not None:
                aligned = [other for other in tier
                           if other[2] is not None
                           and (abs(other[0] - x) <= aligned_tol
                                or abs(other[1] - y) <= aligned_tol)]
                if len(aligned) >= 2:
                    terminal = max(other[2] for other in aligned)
                    return (0 if value == terminal else 1, -value, quadrant_key(lc))
            return (2, 0, quadrant_key(lc))

        return sorted(tier, key=key)

    any_found = False
    for tier in label_cand_tiers:
        if not tier:
            continue
        for mx, my, forced_val in ordered_labels(tier):
            label_cross_h = my                  # cross-axis coord to match against horizontal candidates
            label_cross_v = mx                  # cross-axis coord to match against vertical candidates
            label_along_h = mx                  # along-axis coord (position within a horizontal bar's run)
            label_along_v = my

            # Bucket same-axis segments near this label by cross-position (within CLUSTER_TOL), so a
            # stack of tick-block edges at (near-)identical cross-pos is treated as one bar.
            buckets = {}  # (axis, rounded_cross_pos) -> list of spans
            for axis, length, cross_pos, span in all_lines:
                bound = page_w if axis == "h" else page_h
                if length > BORDER_FRAC * bound:
                    continue                                   # guard 1: skip border / title-block rules
                label_cross = label_cross_h if axis == "h" else label_cross_v
                if abs(cross_pos - label_cross) >= PROX_BAND:
                    continue
                key = (axis, round(cross_pos / CLUSTER_TOL))
                buckets.setdefault(key, []).append(span)

            cands = []  # (barlen, dist_to_label, axis)
            for (axis, _), spans in buckets.items():
                label_along = label_along_h if axis == "h" else label_along_v
                for lo, hi in merged_runs(spans):
                    barlen = hi - lo
                    dist = 0.0 if lo <= label_along <= hi else min(abs(hi - label_along), abs(lo - label_along))
                    cands.append((barlen, dist, axis))
            if not cands:
                continue                                       # this label has no nearby bar line — try next
            any_found = True
            # guard 2: nearest to the label wins; longer bar breaks ties (more precise increment)
            barlen = min(cands, key=lambda c: (round(c[1], 1), -c[0]))[0]
            if forced_val is not None:
                label = forced_val
            else:
                nearby_nums = [int(w[4]) for w in nums if abs(w[1] - my) < PROX_BAND or abs(w[0] - mx) < PROX_BAND]
                if not nearby_nums:
                    continue                               # no digits near this label — try next candidate
                label = max(nearby_nums)
            if barlen <= 0:
                continue
            return label / barlen, f"{label} m / {barlen:.0f} pt"
        # A tier produced label(s) with a nearby line but no usable value pairing (or none at all) —
        # only fall through to the next (lower-priority) tier, don't mix candidates across tiers.

    return None, "label found but no bar line near it" if any_found else "no scale-bar label"


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
