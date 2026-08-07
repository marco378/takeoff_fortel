"""Measurement rules for slab exclusions and review prompts.

Implements Inderjit's live-markup measurement rules from the 5 Aug call.
Identifies explicit exclusions (lift voids, risers, stair foundations) from
Bluebeam markups and generates review prompts for unresolved exclusions.
"""
import re


# ---------------------------------------------------------------------------
# Exclusion classification (Bluebeam markup subject → exclusion record)
# ---------------------------------------------------------------------------

_EXCLUSION_RULES = [
    {
        "pattern": re.compile(r"\blift\s*(?:shaft|void|pit)\b", re.I),
        "exclusion_id": "lift_void",
        "label": "Lift shaft / void",
        "rule": "lift void is not slab — separate lift subcontractor",
        "provenance": "explicit markup subject",
    },
    {
        "pattern": re.compile(r"\b(?:data|service)\s*riser\b", re.I),
        "exclusion_id": "service_data_riser",
        "label": "Service / data riser",
        "rule": "riser is not slab — services subcontractor",
        "provenance": "explicit markup subject",
    },
    {
        "pattern": re.compile(r"\bprecast\s*(?:stair(?:case)?)?\s*foundation\b", re.I),
        "exclusion_id": "precast_stair_foundation",
        "label": "Precast stair foundation",
        "rule": "stair foundation is separate work, not slab void pricing",
        "provenance": "explicit markup subject",
    },
]


def classify_exclusion(subject, content=""):
    """Return an exclusion record if *subject* matches a known exclusion, else None.

    Only explicit, labelled exclusions are classified.  A bare "Pit" or
    unlabeled polygon is never guessed to be a lift void or stair foundation.
    """
    probe = str(subject or "").strip()
    if not probe:
        return None
    for rule in _EXCLUSION_RULES:
        if rule["pattern"].search(probe):
            return {
                "exclusion_id": rule["exclusion_id"],
                "label": rule["label"],
                "rule": rule["rule"],
                "provenance": rule["provenance"],
            }
    return None


# ---------------------------------------------------------------------------
# Exclusion review prompts (drawing text → assessor review checklist)
# ---------------------------------------------------------------------------

# Category → list of (exclusion_id, label, rule) for that zone type
_CATEGORY_EXCLUSIONS = {
    "external_yard": [
        ("gatehouse", "Gatehouse",
         "colour segmentation cannot prove its footprint; assessor must trace the outline"),
        ("hub_office", "Hub Office",
         "colour segmentation cannot prove its footprint; assessor must trace the outline"),
    ],
    "ground_floor": [
        ("lift_void", "Lift shaft / void",
         "lift void is not slab — assessor must exclude it from the ground-floor trace"),
        ("service_data_riser", "Service / data riser",
         "riser is not slab — assessor must exclude it from the ground-floor trace"),
        ("precast_stair_foundation", "Precast stair foundation",
         "stair foundation is separate work, not slab void pricing"),
    ],
    "upper_floor": [
        ("lift_void", "Lift shaft / void",
         "lift void is not slab — assessor must exclude it from the upper-floor trace"),
        ("service_data_riser", "Service / data riser",
         "riser is not slab — assessor must exclude it from the upper-floor trace"),
    ],
}


def exclusion_review_prompts(categories, page_text=""):
    """Return assessor-review prompts for slab exclusions found or labelled on the drawing.

    *categories* is a list of zone category strings (e.g. ["external_yard", "dock"]).
    *page_text* is the full extracted text of the drawing page (to detect labelled
    exclusions whose outline was not resolved).

    Each prompt dict contains:
        exclusion_id, label, rule, status, requires_assessor_confirmation
    """
    text_lower = str(page_text or "").lower()
    seen = set()
    prompts = []

    for category in categories or []:
        for exclusion_id, label, rule in _CATEGORY_EXCLUSIONS.get(category, []):
            if exclusion_id in seen:
                continue
            seen.add(exclusion_id)
            # Check if the label appears in the drawing text
            label_words = label.lower().split()
            label_present = all(word in text_lower for word in label_words)
            if label_present:
                status = "outline_unresolved"
                requires = True
            else:
                status = "label_not_found"
                requires = False
            prompts.append({
                "exclusion_id": exclusion_id,
                "label": label,
                "rule": rule,
                "status": status,
                "requires_assessor_confirmation": requires,
            })

    return prompts


def unresolved_exclusion_detected(prompts):
    """Return True if any prompt has status 'outline_unresolved'."""
    return any(
        prompt.get("status") == "outline_unresolved"
        for prompt in (prompts or [])
    )
