#!/usr/bin/env python3
"""
Standard build-up assumptions for Fortel AI Takeoff.

Used when NO engineer drawing / construction-detail sheet is available (architect-only pack,
or ~60% of enquiries that arrive without proper specs — per Inderjit, standup 20 Jun 2026).

Rule (from Inderjit verbatim):
  "We have to assume the thickness of slab, what type of mesh we will be using. And then
   we mention in our quotation that these assumptions has been made ... if the client came
   back to us saying this should be the specifications then we change it accordingly."

THESE DEFAULTS MUST BE STATED IN EVERY QUOTATION THAT USES THEM.
Use ASSUMPTION_NOTE to generate the declaration string.
"""

# ---- Standard industrial service-yard build-up (no spec available) ----
DEFAULT_SPEC = {
    # Slab
    "depth_mm":       190,        # standard for HGV service yards
    "conc_mix":       "C32/40",   # standard external industrial
    "conc_rate":      128,        # £/m³ (validated from Winvic California Drive BOQ)
    "conc_wastage":   0.03,       # 3% standard
    # Reinforcement
    "mesh":           "A252",     # mid-range; conservative for service yard
    "layers":         1,
    "steel_rate_t":   850,        # £/tonne (6-month fixed rate from supplier)
    "steel_wastage":  0.15,       # 15% — assumed from 4.9×8.8 m joint-bay calc
                                  # (Amarvir standup 24 Jun 2026; 10% only when
                                  #  actual joint layout is available)
    "lap_acc":        0.18,       # 18% laps + accessories allowance
    # Fixed items (from the costing template — constant for every job)
    "dpm":            0.46,       # £/m² DPM
    "curing":         0.23,       # £/m² curing compound
    "labour":         10.00,      # £/m² supply & lay labour
    "trim":           0.40,       # £/m² final trim
    "margin":         0.11,       # 11% margin
    # ── Concrete supplier inquiry fields (always specified for externals) ──────
    # Source: Amarvir screen-share, standup 24 Jun 2026
    "cement_type":    "CEM I",    # standard Portland cement
    "air_entrained":  True,       # always air-entrained for external slabs
    "aggregate_mm":   20,         # 20mm crushed aggregate (unless spec differs)
    "wc_ratio":       0.45,       # water/cement ratio (always 0.45 external)
    "slump_class":    "S3",       # target slump — S3 for 99% of jobs; S4 = WinVIC only
}

# Thicker slab option — used when dock areas or HGV turning are in scope
HEAVY_DUTY_OVERRIDES = {
    "depth_mm": 250,
    "mesh":     "A393",
    "layers":   1,
}

# Two-layer option — rarely used; only when designer specifies explicitly
TWO_LAYER_OVERRIDES = {
    "mesh":   "A393",
    "layers": 2,
}


def spec_with_defaults(engineer_spec: dict | None = None) -> tuple[dict, bool]:
    """
    Merge any known engineer spec values over the defaults.
    Returns (spec_dict, assumed: bool).
    `assumed` is True when at least one key came from defaults (not the engineer drawing).
    """
    spec = dict(DEFAULT_SPEC)
    assumed = True
    if engineer_spec:
        assumed = False
        for k, v in engineer_spec.items():
            if k in spec and v is not None:
                spec[k] = v
            else:
                assumed = True   # at least one field still defaulted
    return spec, assumed


def assumption_note(spec: dict) -> str:
    """
    Generate the disclosure text to include in every quotation where defaults were used.
    Matches Fortel's standard wording exactly.
    """
    return (
        f"NOTE — Build-up ASSUMED (no engineer construction-detail drawing supplied): "
        f"{spec['depth_mm']} mm slab, {spec['mesh']} mesh ({spec.get('conc_mix','C32/40')} concrete). "
        f"Rate subject to revision upon receipt of designer specification."
    )


def flag_assumed(spec: dict, assumed: bool) -> list[str]:
    """Return pipeline flags (empty list if fully specified by engineer drawings)."""
    if not assumed:
        return []
    return [
        f"BUILD-UP ASSUMED: {spec['depth_mm']}mm / {spec['mesh']} / {spec.get('conc_mix','C32/40')} "
        f"— no engineer construction-detail found; stated in quotation as indicative"
    ]


# ---- Quick sanity check ----
if __name__ == "__main__":
    s, assumed = spec_with_defaults()
    print("Default spec (no engineer drawing):")
    for k, v in s.items():
        print(f"  {k:20} {v}")
    print()
    print("Assumption note:")
    print(" ", assumption_note(s))
    print()
    # Partial override (engineer gave depth + mesh, but not rates)
    s2, assumed2 = spec_with_defaults({"depth_mm": 200, "mesh": "A393", "layers": 1})
    print("Partial override (200mm / A393):", s2["depth_mm"], s2["mesh"], "| still assumed:", assumed2)
    print("Flags:", flag_assumed(s2, assumed2))
