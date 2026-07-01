#!/usr/bin/env python3
"""
Plausibility guards — the last line of defence so an impossible number never ships silently.
Built from the 95,463 m² incident: a slab bigger than the whole site got emitted with no flag.
"""


def plausible(area_m2, site_m2=None, max_single_zone_m2=60000):
    """Return flags (empty == looks OK). Never raises — produces flags for the assessor."""
    flags = []
    if area_m2 is None:
        return flags
    if site_m2 and area_m2 > site_m2 * 1.02:
        flags.append(f"IMPOSSIBLE: slab {area_m2:,.0f} m² exceeds the site boundary {site_m2:,.0f} m² — blocked, re-check scale & region")
    if area_m2 > max_single_zone_m2:
        flags.append(f"IMPLAUSIBLE: {area_m2:,.0f} m² exceeds the single-zone bound {max_single_zone_m2:,} m² — likely scale/region error; verify")
    if area_m2 <= 0:
        flags.append("non-positive area")
    return flags


# ── Four-state measurement state machine ─────────────────────────────────────
# Every uploaded file must end up in exactly one of these — never a crash, never a
# silent wrong number. See MISSION in project instructions for the full contract.
MEASURED_VERIFIED   = "MEASURED_VERIFIED"     # area + scale verified + plausible -> approvable
MEASURED_UNVERIFIED = "MEASURED_UNVERIFIED"   # area exists but scale unverified/low-conf/implausible -> approve BLOCKED
UNMEASURED           = "UNMEASURED"            # no reliable number possible -> mandatory assessor trace
REJECTED             = "REJECTED"              # unreadable/unsupported input -> human-readable reason


def measurement_state(area_m2, scale_verified=None, confidence=None, site_m2=None,
                      max_single_zone_m2=60000, rejected_reason=None):
    """
    Decide the measurement_state for a takeoff result. Never raises.
    Returns (state, flags) — flags explain WHY, for the assessor / portal.

      area_m2         : measured area in m² (None if nothing could be measured)
      scale_verified   : True/False/None — was the scale cross-checked against a second source?
      confidence       : 'high'/'medium'/'low' from router.classify (low forces UNVERIFIED)
      site_m2          : known site boundary area, if any (for the impossible-area guard)
      rejected_reason   : if set, short-circuits straight to REJECTED with this reason
    """
    if rejected_reason:
        return REJECTED, [f"REJECTED: {rejected_reason}"]

    if area_m2 is None:
        return UNMEASURED, ["UNMEASURED: no reliable area — mandatory assessor trace"]

    plaus_flags = plausible(area_m2, site_m2=site_m2, max_single_zone_m2=max_single_zone_m2)
    if plaus_flags:
        return MEASURED_UNVERIFIED, plaus_flags + [
            "MEASURED_UNVERIFIED: implausible area — approve blocked until assessor confirms scale+extent"]

    if scale_verified is False:
        return MEASURED_UNVERIFIED, [
            "MEASURED_UNVERIFIED: scale not verified (single source / bar-title disagreement) — "
            "approve blocked until assessor confirms scale"]

    if confidence == "low":
        return MEASURED_UNVERIFIED, [
            "MEASURED_UNVERIFIED: low classifier confidence — approve blocked until assessor confirms"]

    return MEASURED_VERIFIED, []


if __name__ == "__main__":
    print("dev incident:", plausible(95463, site_m2=34329))
    print("correct:     ", plausible(26080, site_m2=34329))
    print("state (verified):  ", measurement_state(26080, scale_verified=True, confidence="high"))
    print("state (unverified):", measurement_state(26080, scale_verified=False))
    print("state (implausible):", measurement_state(95463, site_m2=34329))
    print("state (no area):   ", measurement_state(None))
    print("state (rejected):  ", measurement_state(None, rejected_reason="encrypted PDF"))
