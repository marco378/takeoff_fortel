#!/usr/bin/env python3
"""Client-supplied slab specification schema and field-level provenance.

The labels and applicability come from ``Brief_Spec.xlsx`` supplied by Fortel on
15 July 2026.  The workbook is a blank checklist: it supplies field names, but no
values or permitted-value lists.  Consequently this module never supplies a
default.  Effective values already used by costing may be shown, but they remain
explicitly provisional until an engineer drawing or assessor confirms them.
"""
import json


SCHEMA_VERSION = 1
NO_DETAILS = "ASSUMED / no details provided"

COMMON_FIELDS = ("depth_mm", "conc_mix", "mesh", "layers")

FIELD_LABELS = {
    "depth_mm": "Thickness of slab",
    "conc_mix": "Concrete Mix",
    "mesh": "Type of mesh",
    "layers": "Nr of mesh layers",
    "bay_sizes": "Bay sizes if joint layout available",
    "joint_details": (
        "Type and size of dowel bars- if joint details available + any special "
        "requirements for joints (thickening etc.)"
    ),
}

SLAB_SPEC_SCHEMA = {
    "external_yard": {
        "label": "External/Service Yard Slabs",
        "section": "External yard slabs",
        "fields": COMMON_FIELDS + ("bay_sizes", "joint_details"),
    },
    "dock": {
        "label": "Dock Slabs",
        "section": "Dock slabs",
        "fields": COMMON_FIELDS + ("bay_sizes", "joint_details"),
    },
    "ground_floor": {
        "label": "Ground Floor Slabs(Core Areas)",
        "section": "Ground floor slabs",
        "fields": COMMON_FIELDS + ("joint_details",),
    },
    "upper_floor": {
        "label": "Upper Floors",
        "section": "Upper floor slabs",
        "fields": COMMON_FIELDS,
    },
}

_ALIASES = {
    "external yard slabs": "external_yard",
    "external/service yard slabs": "external_yard",
    "external yard": "external_yard",
    "yard": "external_yard",
    "dock slabs": "dock",
    "dock": "dock",
    "ground floor slabs": "ground_floor",
    "ground floor slabs(core areas)": "ground_floor",
    "ground floor slabs (core areas)": "ground_floor",
    "ground floor slabs (ancillary areas)": "ground_floor",
    "ground floor": "ground_floor",
    "office": "ground_floor",
    "upper floor slabs": "upper_floor",
    "upper floors": "upper_floor",
    "upper floor": "upper_floor",
}


def normalise_slab_type(value=None, *, text=""):
    """Return a stable slab id, conservatively defaulting unknown drawings to yard."""
    probe = str(value or "").strip().casefold()
    if probe in SLAB_SPEC_SCHEMA:
        return probe
    if probe in _ALIASES:
        return _ALIASES[probe]

    label = f"{value or ''} {text or ''}".casefold().replace("_", "-")
    if "dock" in label:
        return "dock"
    if any(term in label for term in ("upper floor", "first floor", "mezzanine", "level 1")):
        return "upper_floor"
    if any(term in label for term in ("ground floor", "office", "transport", "internal slab")):
        return "ground_floor"
    return "external_yard"


def schema_definition():
    """JSON-safe schema for API/UI consumers; contains labels only, never values."""
    return {
        slab_type: {
            "label": definition["label"],
            "section": definition["section"],
            "fields": [
                {"key": key, "label": FIELD_LABELS[key]}
                for key in definition["fields"]
            ],
        }
        for slab_type, definition in SLAB_SPEC_SCHEMA.items()
    }


def coerce_field_value(key, value):
    """Validate one supplied value without imposing a client-unspecified choice list."""
    if key not in FIELD_LABELS:
        raise ValueError(f"unknown specification field: {key}")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if key in ("depth_mm", "layers"):
        number = int(value)
        if number <= 0:
            raise ValueError(f"{key} must be positive")
        return number
    return str(value).strip()


def empty_brief_spec(slab_type=None):
    """Return the exact applicable blank checklist; every blank is provisional."""
    slab_type = normalise_slab_type(slab_type)
    definition = SLAB_SPEC_SCHEMA[slab_type]
    return {
        "schema_version": SCHEMA_VERSION,
        "slab_type": slab_type,
        "slab_type_label": definition["label"],
        "section": definition["section"],
        "fields": {
            key: {
                "label": FIELD_LABELS[key],
                "value": None,
                "source": "not_provided",
                "provisional": True,
            }
            for key in definition["fields"]
        },
    }


def build_brief_spec(slab_type=None, *, effective_spec=None, confirmed=None,
                     source="assessor", existing=None, replace=False):
    """Build a checklist while preserving assumed-vs-confirmed field provenance.

    ``effective_spec`` is the already-computed costing specification.  Its values are
    useful to the assessor, but they are labelled ``assumed_default``.  Only values in
    ``confirmed`` become firm.  No value is generated here.
    """
    slab_type = normalise_slab_type(
        slab_type or (existing or {}).get("slab_type"),
        text=(existing or {}).get("section", ""),
    )
    result = empty_brief_spec(slab_type)
    applicable = set(result["fields"])

    for key, value in (effective_spec or {}).items():
        if key in applicable and value is not None:
            result["fields"][key].update({
                "value": value,
                "source": "assumed_default",
                "provisional": True,
            })

    if existing and not replace:
        for key, field in (existing.get("fields") or {}).items():
            if key not in applicable or not isinstance(field, dict):
                continue
            value = coerce_field_value(key, field.get("value"))
            if value is not None:
                result["fields"][key].update({
                    "value": value,
                    "source": str(field.get("source") or "not_provided"),
                    "provisional": bool(field.get("provisional", True)),
                })

    for key, raw_value in (confirmed or {}).items():
        if key not in applicable:
            raise ValueError(f"field {key!r} does not apply to {result['slab_type_label']}")
        value = coerce_field_value(key, raw_value)
        if value is None:
            # A cleared field remains visible as either the effective pricing assumption or
            # an unprovided blank.  It is never silently marked confirmed.
            continue
        result["fields"][key].update({
            "value": value,
            "source": source,
            "provisional": False,
        })
    return result


def confirmed_values(brief_spec):
    """Return only explicitly confirmed values from a stored checklist."""
    return {
        key: field.get("value")
        for key, field in ((brief_spec or {}).get("fields") or {}).items()
        if isinstance(field, dict) and not field.get("provisional", True)
        and field.get("value") is not None
    }


def brief_spec_signature(brief_spec):
    """Aggregation signature: values plus provisional state, excluding source filenames."""
    fields = (brief_spec or {}).get("fields") or {}
    payload = {
        "slab_type": (brief_spec or {}).get("slab_type"),
        "fields": {
            key: {"value": field.get("value"), "provisional": bool(field.get("provisional", True))}
            for key, field in fields.items() if isinstance(field, dict)
        },
    }
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def display_value(key, field):
    """Human-readable value that can never hide missing/provisional provenance."""
    value = (field or {}).get("value")
    if value is None:
        return NO_DETAILS
    text = f"{value} mm" if key == "depth_mm" else str(value)
    if (field or {}).get("provisional", True):
        return f"{text} — ASSUMED / provisional"
    return text


def display_lines(brief_spec):
    """Return exact client labels and visible values in schema order."""
    return [
        {
            "key": key,
            "label": field.get("label") or FIELD_LABELS.get(key, key),
            "value": display_value(key, field),
            "provisional": bool(field.get("provisional", True)),
        }
        for key, field in ((brief_spec or {}).get("fields") or {}).items()
        if isinstance(field, dict)
    ]
