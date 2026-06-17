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


if __name__ == "__main__":
    print("dev incident:", plausible(95463, site_m2=34329))
    print("correct:     ", plausible(26080, site_m2=34329))
