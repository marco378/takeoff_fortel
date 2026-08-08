"""Evidence-gated slab exclusions from Inderjit's 5 August live markup.

The functions in this module classify explicit annotation text or create an assessor
checklist. They never manufacture an exclusion polygon or subtract an unproved area.
"""
from __future__ import annotations

import re


EXCLUSION_RULES = (
    {
        "exclusion_id": "gatehouse",
        "label": "Gatehouse",
        "categories": {"external_yard"},
        "patterns": (re.compile(r"\bgate\s*house\b", re.I),),
        "rule": "exclude from the external/service Yard slab area",
    },
    {
        "exclusion_id": "hub_office",
        "label": "Hub office",
        "categories": {"external_yard"},
        "patterns": (re.compile(r"\bhub\s+office\b", re.I),),
        "rule": "exclude from the external/service Yard slab area",
    },
    {
        "exclusion_id": "lift_void",
        "label": "Lift shaft / lift pit",
        "categories": {"ground_floor", "upper_floor"},
        # The transcript repeatedly renders "lift pit" as "lift bit".  Require the feature
        # noun: a lift lobby is slab circulation space and a bare "Lift" is not enough evidence
        # to subtract anything.  A bare "Pit" is likewise not enough.
        "patterns": (
            re.compile(r"\blift\s+(?:shaft|pit|bit|void)\b", re.I),
        ),
        "rule": "exclude from the office slab area",
    },
    {
        "exclusion_id": "service_data_riser",
        "label": "Service / data riser",
        "categories": {"ground_floor", "upper_floor"},
        "patterns": (
            re.compile(r"\b(?:service|data)\s+(?:riser|raiser)\b", re.I),
            re.compile(r"\b(?:riser|raiser)\s+(?:service|data)\b", re.I),
        ),
        "rule": "exclude from the office slab area",
    },
    {
        "exclusion_id": "precast_stair_foundation",
        "label": "Precast staircase foundation",
        "categories": {"ground_floor"},
        # A generic Pit or Foundation is deliberately insufficient. The feature must carry
        # explicit stair/foundation semantics before it can be excluded automatically.
        "patterns": (
            re.compile(
                r"\bpre[- ]?cast\s+(?:concrete\s+)?stair(?:case)?\s+foundation\b",
                re.I,
            ),
            re.compile(r"\bstair(?:case)?\s+foundation\b", re.I),
        ),
        "rule": (
            "exclude from the slab quotation; price separately only if the client asks "
            "(detail evidence: 300 / 345 / 600 mm)"
        ),
    },
)

# Preserve Aryan's module-level API shape for any external diagnostic/import code.
_EXCLUSION_RULES = [
    {
        "pattern": pattern,
        "exclusion_id": rule["exclusion_id"],
        "label": rule["label"],
        "rule": rule["rule"],
        "provenance": "explicit markup subject/content",
    }
    for rule in EXCLUSION_RULES
    for pattern in rule["patterns"]
]
_CATEGORY_EXCLUSIONS = {
    category: [
        (rule["exclusion_id"], rule["label"], rule["rule"])
        for rule in EXCLUSION_RULES
        if category in rule["categories"]
    ]
    for category in ("external_yard", "ground_floor", "upper_floor")
}


def _normalise(value) -> str:
    return " ".join(str(value or "").strip().split())


def classify_exclusion(subject, content=""):
    """Classify only semantically explicit annotation subject/content evidence."""
    evidence = _normalise(f"{subject or ''} {content or ''}")
    if not evidence:
        return None
    for rule in EXCLUSION_RULES:
        if any(pattern.search(evidence) for pattern in rule["patterns"]):
            return {
                "exclusion_id": rule["exclusion_id"],
                "label": rule["label"],
                "rule": rule["rule"],
                "provenance": "explicit markup subject/content",
                "evidence": evidence,
                "categories": sorted(rule["categories"]),
            }
    return None


def exclusion_review_prompts(categories, page_text=""):
    """Return the applicable checklist and identify labels with unresolved outlines.

    Absence of searchable text does not prove absence of a lift, riser, foundation,
    Gatehouse, or Hub office. Such rows remain visible assessor checks, but only an actual
    text hit creates the approval-blocking ``outline_unresolved`` state.
    """
    category_set = {str(category or "").strip().lower() for category in categories or []}
    text = _normalise(page_text)
    prompts = []
    for rule in EXCLUSION_RULES:
        if not (rule["categories"] & category_set):
            continue
        match = next((pattern.search(text) for pattern in rule["patterns"]
                      if pattern.search(text)), None)
        prompts.append({
            "exclusion_id": rule["exclusion_id"],
            "label": rule["label"],
            "rule": rule["rule"],
            "status": "outline_unresolved" if match else "assessor_check",
            "drawing_text_evidence": match.group(0) if match else None,
            "requires_assessor_confirmation": True,
            "assumed": True,
            "basis": (
                f"drawing text labels '{match.group(0)}', but no reliable exclusion "
                "outline was resolved"
                if match else
                "client exclusion checklist; no reliable automatic feature outline was resolved"
            ),
        })
    return prompts


def unresolved_exclusion_detected(prompts):
    """Return whether labelled evidence exists without a resolved exclusion outline."""
    return any(prompt.get("status") == "outline_unresolved" for prompt in prompts or [])


__all__ = [
    "EXCLUSION_RULES", "classify_exclusion", "exclusion_review_prompts",
    "unresolved_exclusion_detected",
]
