#!/usr/bin/env python3
"""
Fortel AI Takeoff — Quotation Generator

Turns a pipeline result dict into a formatted quotation output:
  - Plain-text quotation body (for email / Word paste)
  - JSON quotation record (for the tracker / n8n)
  - HTML quotation (for email or browser view)
  - Editable Excel quotation (numeric inputs + live formulas)

The quotation matches how Fortel actually issues quotes:
  - Lists the drawing used and its discipline (engineer / architect)
  - States the area measured
  - Gives the rate build-up (depth, mesh, mix)
  - Declares ASSUMPTIONS when build-up is not from an engineer drawing
  - States "subject to confirmation" wherever assumptions were made

Usage:
  from quotation import generate_quotation, quotation_text, quotation_html, quotation_xlsx

  result = takeoff_pipeline.takeoff("drawings/D77.pdf")
  q = generate_quotation(result, project="Hemington D77 Hard Landscaping",
                         client="Fortel", ref="FTL-2026-D77")
  print(quotation_text(q))

Run standalone:
  python3 quotation.py          # self-test with synthetic data
"""
import datetime, html, json, uuid, io, contextlib, re
from collections import OrderedDict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from slab_spec import (brief_spec_signature, build_brief_spec,
                       display_lines as brief_spec_display_lines, normalise_slab_type)

with contextlib.redirect_stdout(io.StringIO()):
    from costing import rate_buildup, MESH_KG
from defaults import spec_with_defaults, assumption_note
from construction_pricing import (combined as combined_constructions,
                                  price_constructions, zone_constructions)

# Fortel company details for the quotation header
FORTEL_NAME    = "Fortel Group Limited"
FORTEL_ADDRESS = "Fortel House, Birmingham"
FORTEL_EMAIL   = "estimating@fortel.co.uk"
FORTEL_TEL     = "+44 (0)121 000 0000"

STANDARD_TERMS = (
    "This quotation is based on the drawings referenced below. "
    "Areas and build-ups are subject to confirmation upon receipt of the full construction specification. "
    "Rates are inclusive of supply, labour, plant and DPM. "
    "Excludes earthworks, drainage, kerbing and any items not listed above. "
    "Validity: 30 days from date of issue."
)

UNCLASSIFIED_SECTION = "Unclassified — assessor must classify"

SECTION_ORDER = (
    "External yard slabs",
    "Footpath slabs",
    "Dock slabs",
    "Ground floor slabs",
    "Upper floor slabs",
    "Prelims",
)
_SECTION_RANK = {section: index for index, section in enumerate(SECTION_ORDER)}
# Holding bucket for a section we could not classify. Deliberately NOT part of
# SECTION_ORDER (that is the client's canonical BOQ order) but ranked last so it
# sorts predictably and can never KeyError.
_SECTION_RANK[UNCLASSIFIED_SECTION] = len(SECTION_ORDER)
PROVISIONAL_LABEL = "PROVISIONAL — NO DETAILS PROVIDED"

# Short names for the reason string. FIELD_LABELS is the assessor's checklist wording ("Type
# and size of dowel bars- if joint details available…"); a priced row needs something that
# fits on one line beside the money.
_PROVISIONAL_FIELD_WORDS = {
    "depth_mm": "thickness", "conc_mix": "mix", "mesh": "mesh",
    "layers": "mesh layers", "bay_sizes": "bay sizes", "joint_details": "joint details",
}
# Sources that mean "this came off the drawing or from a human", as opposed to a default.
_CONFIRMED_SOURCES = {"engineer_drawing", "engineer", "architect", "assessor"}


def provisional_reason_for(brief_spec) -> str:
    """Say WHICH details are missing, when some of them are not.

    Aryan, 16 Sep: "the AI actually extracted the relevant details from the PDF, but those
    details are then being stored/classified under the 'Provisional – no details provided'
    flag." He was right. A Radlett row whose thickness was read off the legend, cited to the
    sheet and marked confirmed still printed "NO DETAILS PROVIDED" beside its price. The row
    IS provisional -- mesh and mix are assumed and the price rests on them -- but the reason
    given for it was false.

    A row with nothing confirmed keeps the original string exactly, so every job that had no
    extracted detail reads as it always did.
    """
    fields = (brief_spec or {}).get("fields") or {}
    confirmed, assumed = [], []
    for key, field in fields.items():
        if not isinstance(field, dict):
            continue
        word = _PROVISIONAL_FIELD_WORDS.get(key, key)
        if field.get("value") is None:
            assumed.append(word)
        elif not field.get("provisional", True) and str(
                field.get("source") or "") in _CONFIRMED_SOURCES:
            confirmed.append(word)
        else:
            assumed.append(word)
    if not confirmed:
        return PROVISIONAL_LABEL
    head = "PROVISIONAL — " + ", ".join(confirmed) + " from the drawing"
    return head + ("; " + ", ".join(assumed) + " assumed" if assumed else "")


# Flags that say we are not sure WHICH SURFACE we measured, as opposed to how big it is.
# These used to reach the portal and stop there: on 8 Sep 2026 the Indurent Park quotation
# exported £335,518.20 under the heading "EXTERNAL YARD SLABS", naming "Service Yard" four
# times, while the pipeline's own flags said the legend tint was never found and a different
# client's grey had been substituted. The measurement had in fact landed on the car park.
# The portal gate is not the deliverable: a caveat that does not survive export is not a caveat.
SURFACE_DOUBT_MARKERS = (
    "SURFACE NOT IDENTIFIED", "colour DISAGREE", "FELL BACK", "confirm region colour",
    "SGP grey convention", "grey-hatch heuristic", "not a plausible surface tint",
)
SURFACE_DOUBT_LABEL = "SURFACE IDENTITY UNCONFIRMED"
# ...unless the surface was identified by a stronger method than colour. When the engineer's
# own CAD layer names the surface, the colour disagreement that preceded it is history, not
# doubt: printing "SURFACE IDENTITY UNCONFIRMED" over a layer-identified yard would tell the
# client we are unsure of the one thing we are most sure of.
SURFACE_RESOLVED_MARKER = "SURFACE FROM CAD LAYER"

# What IS uncertain on a layer-identified surface is its boundary. The layer holds marks, not
# an outline, so the outline is those marks closed across blank ground, and the result is a
# minimum. That belongs on the client document for the same reason the identity caveat does:
# a caveat that does not survive export is not a caveat.
OUTLINE_ASSUMPTION_MARKERS = ("OUTLINE BRIDGED", "AREA IS A MINIMUM")
OUTLINE_ASSUMPTION_LABEL = "AREA IS A MINIMUM — OUTLINE RECONSTRUCTED"
# Column F, immediately right of VALUE — mirrors the REMEASURE caveat column in
# Fortel's own costing sheet rather than crowding the DESCRIPTION cell.
PROVISIONAL_COL = 6

ZONE_SECTION = {
    "external_yard": "External yard slabs",
    "dock": "Dock slabs",
    "ground_floor": "Ground floor slabs",
    "upper_floor": "Upper floor slabs",
}

FORTEL_MH_ROW = "E/O for MH details"
FORTEL_CHANNEL_ROW = "Pouring top of Channel Lengths"
FORTEL_TRANSITION_ROW = "E/O for Transition Details"
_FORTEL_EO_ORDER = {
    FORTEL_MH_ROW: 0,
    FORTEL_CHANNEL_ROW: 1,
    FORTEL_TRANSITION_ROW: 2,
}


def _unit_name(result: dict) -> str:
    """Return only a unit label proved by the drawing filename (never invent a plot code)."""
    filename = str(result.get("file") or Path(str(result.get("pdf_path") or "")).name)
    match = re.search(r"\bUnit[- _]?(\d+)\b", filename, re.I)
    return f"Unit-{match.group(1)}" if match else filename


def _fan_constructions(base_unit, constructions, parent, *, section_label,
                       construction_of, brief_spec_category):
    """Turn one measured area with a construction breakdown into one input per construction.

    Shared by the zone path and by the zone-less path a reviewed Surface Finishes Plan takes,
    so the two cannot drift -- they differ only in how the BOQ section was resolved.

    Only thickness varies between the pieces. Everything that belongs to the measured area
    ONCE -- its extras, its perimeter, its outline -- stays on the first piece alone, or the
    quotation would print a perimeter row per construction for a slab that has one perimeter.
    """
    costing = base_unit.get("costing") or {}
    priced = price_constructions(constructions, costing.get("spec") or {})
    extras_holder = costing.get("extras") or []
    once_only = ("perimeter_lm", "polygon_pts")
    pieces = []
    for c_index, entry in enumerate(priced):
        piece = dict(base_unit)
        piece["area_m2"] = float(entry["area_m2"])
        piece["unit_name"] = entry["name"]
        piece["area_label"] = entry["name"]
        piece["construction_name"] = entry["name"]
        # Groups are keyed per section; mark which measured area these came from so the
        # combined "Total ... Slab Area" row sums exactly these and nothing else, leaving
        # every job without constructions rendering as it does now.
        piece["construction_of"] = construction_of
        if brief_spec_category:
            piece["brief_spec"] = _construction_brief_spec(
                brief_spec_category, entry, base_unit.get("brief_spec"), parent)
        else:
            piece["brief_spec"] = _construction_brief_spec(
                normalise_slab_type(section_label, text=str(
                    parent.get("file") or Path(str(parent.get("pdf_path") or "")).name)),
                entry, base_unit.get("brief_spec"), parent)
        piece_costing = dict(costing)
        piece_costing.update({
            "area_m2": float(entry["area_m2"]),
            "rate": entry["rate"],
            "total_gbp": entry["total_gbp"],
            "spec": entry["spec"],
            "breakdown": entry["breakdown"],
            "extras": list(extras_holder) if c_index == 0 else [],
        })
        piece["costing"] = piece_costing
        if c_index:
            for key in once_only:
                piece.pop(key, None)
        piece_flags = list(parent.get("flags") or [])
        if entry["depth_assumed"]:
            piece_flags.append(
                f"THICKNESS NOT STATED FOR THIS CONSTRUCTION: the sheet's legend gives no "
                f"thickness for {entry['name']}, so its "
                f"{entry['area_m2']:,.1f} m² is MEASURED BUT NOT PRICED — the rate is left "
                f"blank for the assessor rather than taken from the drawing's general slab "
                f"specification. Supply the thickness (or the detail sheet it refers to) and "
                f"it prices like the others."
            )
        piece_declarations = []
        if entry["depth_assumed"]:
            # This needs its OWN declaration rather than the generic "ASSUMED" keyword filter
            # the results loop applies: the text deliberately does not say ASSUMED any more
            # (nothing is assumed — the rate is blank), so that filter would drop it and the
            # client document would carry a blank rate with no reason given for it.
            piece_declarations.append(piece_flags[-1])
        if c_index == 0 and len(priced) > 1:
            # Two counts are in play -- how many constructions were MEASURED and how many
            # could be PRICED -- and printing one of them alone in each output is what made
            # the sheet read "4" here and "5" there (Aryan, 16 Sep). State both, in one place.
            with_rate = sum(1 for e in priced if not e["depth_assumed"])
            counted = (f"are priced as {len(priced)} separate constructions"
                       if with_rate == len(priced) else
                       f"carry {len(priced)} separate constructions measured, of which "
                       f"{with_rate} {'is' if with_rate == 1 else 'are'} priced")
            piece_declarations.append(
                f"{section_label} {counted}, each at "
                "its own stated thickness with its own rate build-up; the combined area and "
                "value are the sum of them, not one slab at a single depth."
                + ("" if with_rate == len(priced) else
                   " The remaining construction is measured and carried at its own quantity "
                   "with the rate left blank for the assessor, so it is NOT in the total.")
            )
        piece["construction_declarations"] = piece_declarations
        piece["flags"] = piece_flags
        pieces.append(piece)
    return pieces


def _construction_brief_spec(category, entry, zone_brief_spec, parent):
    """The zone's checklist re-stated at one construction's own stated thickness.

    The thickness is a DRAWING fact -- the Surface Finishes Plan legend states it beside the
    construction's own hatch -- so it is recorded with ``engineer_drawing`` provenance and a
    citation, not as an assessor entry. Every other field keeps the zone's provenance
    untouched: this function confirms nothing it was not given.
    """
    if entry.get("depth_assumed") or not entry.get("depth_mm"):
        return zone_brief_spec
    evidence_text = "Surface Finishes Plan legend"
    if entry.get("detail_ref"):
        evidence_text += f" — {entry['name']} (refer to {entry['detail_ref']})"
    else:
        evidence_text += f" — {entry['name']}"
    evidence = {"text": evidence_text}
    drawing = parent.get("file") or Path(str(parent.get("pdf_path") or "")).name
    if drawing:
        evidence["file"] = drawing
    page = parent.get("page") or parent.get("page_number")
    if page:
        evidence["page"] = page
    try:
        return build_brief_spec(
            category,
            existing=zone_brief_spec,
            confirmed={"depth_mm": entry["depth_mm"]},
            source="engineer_drawing",
            evidence={"depth_mm": evidence},
        )
    except (TypeError, ValueError):
        return zone_brief_spec


# Fields the extractor marks when the DRAWING itself gave more than one answer. The note it
# writes says "nothing assumed, nothing priced" -- and until 16 Sep 2026 the price contradicted
# it. PLP Warwick Site A quoted 2,222.5 m2 at 190 mm for GBP 100,188 while its own flag read
# "SPEC CONFLICT - slab thickness: the drawing states 180 mm... and 200 mm... and 150 mm". The
# 190 came from DEFAULT_SPEC. The sheet does say 190 -- under "Ground Floor Slab Construction
# (By Others)", which is not Fortel's scope -- so the number was doubly not ours to use.
_DRAWING_CONFLICT_SOURCES = {"drawing_states_more_than_one"}


def depth_unresolved_on_drawing(brief_spec) -> bool:
    """True when the sheet states several slab thicknesses and none was chosen.

    This is the same rule the Rail Crossing already follows (``aba682a``): a thickness nobody
    stated for THIS surface must not become a rate. There it was a legend row with no
    thickness at all; here it is a sheet with several, which is the same absence of an answer.
    A single stated thickness, or an assessor's choice, is unaffected.
    """
    field = ((brief_spec or {}).get("fields") or {}).get("depth_mm")
    if not isinstance(field, dict) or field.get("value") is not None:
        return False
    return str(field.get("source") or "") in _DRAWING_CONFLICT_SOURCES


def _build_up_note(spec: dict, brief_spec: dict | None) -> str:
    """Fortel's standard build-up disclosure, minus the part of it that is not true.

    ``defaults.assumption_note`` says "no engineer construction-detail drawing supplied" for
    every provisional row. On a Surface Finishes Plan the slab thickness IS on the drawing --
    read off the legend beside its own hatch and cited to the sheet -- so that sentence sat in
    the notes directly under "200 mm slab", contradicting itself and the take-off above it
    (Aryan, 16 Sep). What is still assumed there is the mix and the mesh, and the note now
    says exactly that. ``defaults.py`` is untouched: the original wording is still what a row
    with nothing extracted gets, through ``assumption_note`` below.
    """
    fields = (brief_spec or {}).get("fields") or {}
    depth = fields.get("depth_mm") if isinstance(fields.get("depth_mm"), dict) else None
    depth_from_drawing = bool(
        depth and depth.get("value") is not None and not depth.get("provisional", True)
        and str(depth.get("source") or "") in _CONFIRMED_SOURCES)
    if not depth_from_drawing:
        return assumption_note(spec)
    return (
        f"NOTE — Build-up PART-ASSUMED: {spec['depth_mm']} mm slab thickness taken from the "
        f"drawing; {spec['mesh']} mesh and {spec.get('conc_mix', 'C32/40')} concrete are "
        f"ASSUMED, no engineer construction-detail drawing was supplied for those. "
        f"Rate subject to revision upon receipt of designer specification."
    )


def _expand_zone_results(results: list[dict]) -> list[dict]:
    """Fan a mixed marked drawing into unpriced BOQ-area inputs by proven zone category.

    A file-level rate/specification cannot be copied onto multiple zones: the client's real
    BOQ proves, for example, that Yard and Dock use different build-ups.  Explicit per-zone
    costing may be used when present; otherwise mixed-zone rate cells stay blank for the
    assessor.  Legacy/no-zone and genuinely single-zone results are unchanged.
    """
    expanded = []
    for parent in results:
        area_zones = [zone for zone in (parent.get("zones") or [])
                      if zone.get("area_m2") is not None]
        zones = [zone for zone in area_zones if zone.get("category") in ZONE_SECTION]
        if not zones:
            # A sheet that arrived UNMEASURED carries no zones at all: the assessor's Yard
            # region review is what measures it, and its per-construction breakdown sits on
            # the result rather than on a zone. That is the shape the real Radlett Surface
            # Finishes Plans take, so it must fan here too -- portal QA found it doing exactly
            # what this feature exists to stop, pricing four thicknesses at one default depth.
            #
            # The section is resolved exactly as it is today, from the drawing. Nothing is
            # invented here: a zone-less result with no constructions still passes through
            # whole, unchanged.
            loose = zone_constructions(parent)
            if loose:
                section_label = quotation_section(parent)
                base = dict(parent)
                base["brief_spec"] = parent.get("brief_spec") or build_brief_spec(
                    normalise_slab_type(section_label, text=str(
                        parent.get("file") or Path(str(parent.get("pdf_path") or "")).name)),
                    effective_spec=(parent.get("costing") or {}).get("spec") or {})
                expanded.extend(_fan_constructions(
                    base, loose, parent,
                    section_label=section_label,
                    construction_of=f"{_unit_name(parent)}::{section_label}",
                    brief_spec_category=None))
                continue
            # No recognised zone: the parent passes through whole, so its area is still counted.
            expanded.append(parent)
            continue
        # But once ANY zone is recognised, expansion REPLACES the parent — so a measured zone
        # whose category has no BOQ section used to be filtered out here and vanish from the
        # quotation without a word. Reported on the 27 Aug call as an element "created with the
        # wrong tool" that stopped contributing to the total and lost its thickness/mesh/finish;
        # the portal's classification dropdown collapsing to "other region" is how an assessor
        # lands here. Measured area must never disappear silently — that is the same contract as
        # never emitting a silent number, in the other direction.
        orphans = [zone for zone in area_zones if zone.get("category") not in ZONE_SECTION]

        categories = {zone["category"] for zone in zones}
        mixed = len(categories) > 1
        parent_costing = dict(parent.get("costing") or {})
        per_zone_costing = parent.get("zone_costings") or {}
        per_zone_specs = parent.get("brief_specs") or {}
        for index, zone in enumerate(zones):
            category = zone["category"]
            virtual = dict(parent)
            virtual["_zone_expanded"] = True
            virtual["zones"] = []
            virtual.pop("perimeter_lm", None)
            virtual.pop("polygon_pts", None)
            virtual["area_m2"] = float(zone["area_m2"])
            virtual["quotation_section"] = ZONE_SECTION[category]
            virtual["unit_name"] = _unit_name(parent)
            virtual["zone_category"] = category
            virtual["boq_scope"] = zone.get("boq_scope") or "main"
            virtual["scope_label"] = zone.get("scope_label")
            virtual["area_label"] = (zone.get("scope_label")
                                     or zone.get("unit_label")
                                     or ", ".join(zone.get("subjects") or []))
            virtual["brief_spec"] = per_zone_specs.get(category) or build_brief_spec(category)

            explicit_costing = per_zone_costing.get(category)
            if explicit_costing:
                costing = dict(explicit_costing)
                costing["area_m2"] = float(zone["area_m2"])
            elif mixed:
                # Preserve existing extras exactly once, but never inherit an aggregate rate,
                # build-up, or value into a different zone.
                costing = {
                    "area_m2": float(zone["area_m2"]), "rate": None, "total_gbp": None,
                    "assumed": True, "spec": {}, "breakdown": {},
                    "extras": list(parent_costing.get("extras") or []) if index == 0 else [],
                }
            else:
                costing = dict(parent_costing)
                costing["area_m2"] = float(zone["area_m2"])
            if index and costing.get("extras"):
                costing["extras"] = []
            virtual["costing"] = costing

            # A Yard zone an assessor reviewed carries its own per-construction breakdown.
            #
            # Aryan, 15 Sep: each construction gets its own quantity and its own rate
            # build-up, and they are summed only for a combined total at the end. The zone
            # keeps the total (nothing downstream that reads zone area_m2 changes), and the
            # breakdown fans into one input per construction here. Only THICKNESS varies --
            # everything else in the build-up stays the zone's single set of assumptions,
            # because the sheet does not vary them per construction.
            #
            # No new rendering is needed: the BOQ group key already includes the brief-spec
            # signature and the rate, so two constructions at different depths land in
            # different groups and get separate priced rows, separate specification blocks
            # and separate take-off totals on their own.
            constructions = zone_constructions(zone)
            if constructions:
                expanded.extend(_fan_constructions(
                    virtual, constructions, parent,
                    section_label=ZONE_SECTION[category],
                    construction_of=f"{_unit_name(parent)}::{category}",
                    brief_spec_category=category))
                continue
            expanded.append(virtual)

        # Carry the orphans through as their own unclassified, unpriced rows. No rate is
        # inherited or invented: the assessor classifies them and the quantity is visible
        # meanwhile, rather than the area quietly leaving the quotation.
        for zone in orphans:
            category = str(zone.get("category") or "").strip() or "unspecified"
            orphan = dict(parent)
            orphan["_zone_expanded"] = True
            orphan["zones"] = []
            orphan.pop("perimeter_lm", None)
            orphan.pop("polygon_pts", None)
            orphan["area_m2"] = float(zone["area_m2"])
            orphan["quotation_section"] = UNCLASSIFIED_SECTION
            orphan["unit_name"] = _unit_name(parent)
            orphan["zone_category"] = None
            orphan["boq_scope"] = zone.get("boq_scope") or "main"
            orphan["area_label"] = (zone.get("scope_label") or zone.get("unit_label")
                                    or ", ".join(zone.get("subjects") or []) or category)
            orphan["brief_spec"] = {}
            orphan["costing"] = {
                "area_m2": float(zone["area_m2"]), "rate": None, "total_gbp": None,
                "assumed": True, "spec": {}, "breakdown": {}, "extras": [],
            }
            orphan["flags"] = list(parent.get("flags") or []) + [
                f"UNCLASSIFIED ZONE CARRIED: a measured zone categorised '{category}' has no BOQ "
                f"section, so {float(zone['area_m2']):,.1f} m² is listed unpriced rather than "
                "dropped. Assessor must classify it before approval; its build-up (thickness, "
                "mesh, finish) cannot be stated until it is classified."
            ]
            expanded.append(orphan)
    return expanded


def quotation_section(result: dict) -> str:
    """Return the client's canonical BOQ section for a drawing/result."""
    # A measured/assessor-confirmed zone is stronger evidence than a drawing filename.  In
    # particular, one Office GA sheet may carry both ground-floor cores and upper floors.
    zone_category = str(result.get("zone_category") or "").strip().lower()
    if zone_category in ZONE_SECTION:
        return ZONE_SECTION[zone_category]
    explicit = str(result.get("quotation_section") or "").strip()
    if explicit:
        # UNCLASSIFIED_SECTION is deliberately outside SECTION_ORDER (it ranks last), but a
        # carried orphan zone asks for it by name and must resolve, not fall through to the
        # filename heuristics below and land in some unrelated section.
        for section in (*SECTION_ORDER, UNCLASSIFIED_SECTION):
            if explicit.casefold() == section.casefold():
                return section

    label = " ".join(str(result.get(key) or "") for key in (
        "file", "pdf", "pdf_path", "project_name", "name", "type"
    )).casefold().replace("_", "-")
    if "prelim" in label:
        return "Prelims"
    if "footpath" in label or "foot path" in label:
        return "Footpath slabs"
    if "dock" in label:
        return "Dock slabs"
    if any(term in label for term in (
            "upper floor", "first floor", "mezzanine", "level 1", "level-1")):
        return "Upper floor slabs"
    if any(term in label for term in (
            "ground floor", "ground-floor", "office", "transport", "internal slab")):
        return "Ground floor slabs"
    return "External yard slabs"


def _normalise_section(section, fallback="External yard slabs"):
    """Map a section name onto one of Fortel's BOQ sections.

    An UNRECOGNISED name must never be silently re-filed into a different priced section.
    On the 20 Aug call Inderjit classified a second area as dock slab on project 5.2 Longwell
    and it surfaced under External yard slabs — "where did this second area come from" — with
    the yard section's rows and none of the dock formulas ("it didn't pick up the correct
    sheet to mount the values on").  The cause was this function answering with the yard
    fallback for any spelling outside its alias table, e.g. "Dock Slab" or "dock_slab".

    Recognised names map as before.  An unrecognised, non-empty name is now surfaced for
    assessor classification instead of being mis-filed into a priced section: silently pricing
    a dock area as yard is the sectioning form of a silent number.  A blank/absent section
    still uses the caller's contextual fallback, which is a genuine default rather than a
    misclassification.
    """
    raw = str(section or "").strip()
    probe = raw.casefold().replace("_", " ").replace("-", " ")
    probe = " ".join(probe.split())
    if probe.endswith(" slab"):          # "dock slab" -> "dock slabs"
        probe += "s"
    aliases = {
        "yard": "External yard slabs", "external": "External yard slabs",
        "external yard": "External yard slabs", "external yard slabs": "External yard slabs",
        "footpath": "Footpath slabs", "foot path": "Footpath slabs",
        "footpath slab": "Footpath slabs", "footpath slabs": "Footpath slabs",
        "dock": "Dock slabs", "dock slabs": "Dock slabs",
        "ground": "Ground floor slabs", "ground floor": "Ground floor slabs",
        "ground floor slabs": "Ground floor slabs", "gf ancillary": "Ground floor slabs",
        "upper": "Upper floor slabs", "upper floors": "Upper floor slabs",
        "upper floor": "Upper floor slabs", "upper floor slabs": "Upper floor slabs",
        "upper floors slabs": "Upper floor slabs", "first floor": "Upper floor slabs",
        "ground floors": "Ground floor slabs", "ground slab": "Ground floor slabs",
        "external yard slab": "External yard slabs", "service yard": "External yard slabs",
        "yard slabs": "External yard slabs", "dock slab": "Dock slabs",
        "dock leveller": "Dock slabs", "footpaths": "Footpath slabs",
        "prelims": "Prelims", "preliminaries": "Prelims",
    }
    if probe in aliases:
        return aliases[probe]
    if not probe:
        return fallback
    return UNCLASSIFIED_SECTION


def _section_area_total_label(section: str) -> str:
    """Aryan's own wording for the combined area line at the foot of a fanned section."""
    if section == "External yard slabs":
        return "External/Service Yard Slab Area"
    return f"{section[:1].upper()}{section[1:]} Area"


def _spec_key(costing, brief_spec=None):
    # The client checklist's field-level provisional state is part of specification identity:
    # equal effective values cannot be collapsed when one is assumed and one is confirmed.
    if brief_spec:
        return brief_spec_signature(brief_spec)
    return json.dumps(costing.get("spec") or {}, sort_keys=True, default=str, separators=(",", ":"))


def _provisional_text(li):
    if not li.get("provisional"):
        return li["description"]
    return f"{li['description']} [{li.get('provisional_reason') or PROVISIONAL_LABEL}]"


def _unique_notes(notes):
    return list(dict.fromkeys(note for note in notes if note))


def _fortel_eo_description(description) -> str | None:
    """Map legacy/source wording onto Fortel's standard extra-over row labels."""
    text = str(description or "").strip()
    if text in _FORTEL_EO_ORDER:
        return text
    folded = text.casefold()
    if folded.startswith("e/o for mh details") or folded.startswith("manholes ("):
        return FORTEL_MH_ROW
    if folded.startswith("channel ") or folded.startswith("channel —"):
        return FORTEL_CHANNEL_ROW
    if folded in {"channel", "channel length"}:
        return FORTEL_CHANNEL_ROW
    if folded.startswith("transition ") or folded.startswith("transition —"):
        return FORTEL_TRANSITION_ROW
    if folded in {"transition", "transition length"}:
        return FORTEL_TRANSITION_ROW
    return None


def _fortel_concrete_description(spec: dict) -> str:
    """Build the concrete-row label only from specification values already in the job."""
    parts = []
    if spec.get("depth_mm") is not None:
        parts.append(f"{spec['depth_mm']}mm. th")
    parts.append("Concrete Slabs")
    if spec.get("conc_mix"):
        parts.append(str(spec["conc_mix"]))
    if spec.get("air_entrained") is True:
        parts.append("AE")
    if spec.get("aggregate_mm") is not None:
        parts.append(f"{spec['aggregate_mm']}mm agg.")
    return " ".join(parts)


def _fortel_mesh_description(mesh, layers) -> str:
    parts = [str(mesh)] if mesh else []
    parts.append("Mesh Fabric")
    if layers is not None:
        layer_text = "Single Layer" if int(layers) == 1 else f"{int(layers)} Layers"
        parts.append(f"x {layer_text}")
    return " ".join(parts)


def _bay_size_pair(brief_spec: dict):
    field = (brief_spec.get("fields") or {}).get("bay_sizes") or {}
    value = field.get("value") if isinstance(field, dict) else None
    if not value:
        return None
    numbers = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:m(?:etre)?s?)?", str(value), re.I)
    if len(numbers) < 2:
        return None

    def clean(number):
        return f"{float(number):g}"

    return clean(numbers[0]), clean(numbers[1])


def _fortel_joints_description(section: str, brief_spec: dict) -> str:
    if section in {"External yard slabs", "Footpath slabs"}:
        pair = _bay_size_pair(brief_spec)
        if pair:
            return (f"Joints (Excl. Mastic) - Based on {pair[0]}m wide x "
                    f"{pair[1]}m long bays")
        return "Joints (Excl. Mastic)"
    if section == "Ground floor slabs":
        return "Saw Cuts and Movement Joints (Excl. Mastic)"
    return "Saw Cuts and Movement Joints"


# ── Core generator ────────────────────────────────────────────────────────────

_UNIT_NO_RE = re.compile(r"unit[\s_\-]*(\d+)", re.I)
_DREF_RE    = re.compile(r"\b(D\d{2,4})\b", re.I)


def _unit_label_from_filename(drawing: str) -> str | None:
    """Human unit label from Fortel filename conventions, e.g.
    'External Markup Unit-1.pdf' + a D-ref -> 'Unit 1 (D77)'. Inderjit's BOQ keys its
    per-unit rows this way; falls back to None (caller keeps the raw filename)."""
    if not drawing:
        return None
    unit_m = _UNIT_NO_RE.search(drawing)
    if not unit_m:
        return None
    dref_m = _DREF_RE.search(drawing)
    label = f"Unit {unit_m.group(1)}"
    return f"{label} ({dref_m.group(1).upper()})" if dref_m else label


def generate_quotation(result: dict | list, project: str = "", client: str = "",
                       ref: str = None, extras: list = None, commercial: dict = None,
                       unmeasured: list = None, caveats: list = None) -> dict:
    """Build one structured quotation from one result or a project's result list.

    Units with the same canonical section, specification, existing rate and assumption
    provenance are aggregated into one quantity.  Differing specifications remain separate
    rows on the same quotation; no rate is recalculated here.
    """
    source_results = [r for r in (result if isinstance(result, (list, tuple)) else [result]) if r]
    if not source_results:
        source_results = [{}]
    results = _expand_zone_results(source_results)
    ref = ref or f"FTL-{datetime.date.today().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
    today = datetime.date.today().strftime("%-d %B %Y")

    groups = OrderedDict()
    extra_rows = []
    declarations = []
    pipeline_flags = []
    measurements_by_key = OrderedDict()
    drawings = []
    rates_versions = []
    rates_updated_at = []
    client_rate_fields = []
    commercial = dict(commercial or {})

    def add_assessor_eo(section, description, quantity, unit, *, drawing="",
                        provisional=False, basis=""):
        """Aggregate assessor quantities into Fortel's one-row-per-E/O workbook shape."""
        existing = next((item for item in extra_rows
                         if item.get("section") == section
                         and item.get("description") == description
                         and item.get("unit") == unit
                         and item.get("rate") is None), None)
        if existing is None:
            existing = {
                "section": section, "description": description, "qty": 0.0,
                "unit": unit, "rate": None, "value": None,
                "assessor_rate_required": True, "provisional": False,
                "provisional_reason": "", "assumption_basis": basis,
                "drawings": [],
            }
            extra_rows.append(existing)
        existing["qty"] = round(float(existing["qty"]) + float(quantity), 2)
        existing["provisional"] = bool(existing["provisional"] or provisional)
        existing["provisional_reason"] = (PROVISIONAL_LABEL
                                            if existing["provisional"] else "")
        if basis and not existing.get("assumption_basis"):
            existing["assumption_basis"] = basis
        if drawing and drawing not in existing["drawings"]:
            existing["drawings"].append(drawing)
        return existing

    def accumulate_group(unit, costing, spec, rate, section, drawing, brief_spec, area,
                         assumed):
        """Fold one measured quantity into its BOQ group.

        Shared by the main results loop and by separately named +Area elements, so an element
        an assessor classified gets the same specification block and build-up rows a zone
        does. Existing rate is part of the key, so stale/different priced results can never be
        silently collapsed under one arbitrary rate; matching specs/rates aggregate. The
        element id is in the key as well, so a separate area never merges into the main slab.
        """
        group_provisional = assumed or any(
            field.get("provisional", True)
            for field in (brief_spec.get("fields") or {}).values()
            if isinstance(field, dict)
        )
        # The sheet gave several thicknesses and none was chosen for this surface, so there is
        # no thickness to price at. Carry the quantity, leave the rate for the assessor. This
        # is the Rail Crossing rule applied to the whole-sheet path; without it the default
        # 190 mm silently became the price while the flag beside it said nothing was assumed.
        if depth_unresolved_on_drawing(brief_spec):
            rate = None
            # ...and the DESCRIPTION must not keep quoting the default either. A row reading
            # "190mm. th Concrete Slabs" beside a blank rate is the same false statement in a
            # different column, which is exactly what the Rail Crossing row used to do.
            spec = dict(spec or {}, depth_mm=None)
            costing = dict(costing or {}, rate=None, total_gbp=None,
                           spec=dict((costing or {}).get("spec") or {}, depth_mm=None))
        boq_scope = str(unit.get("boq_scope") or "main")
        key = (section, _spec_key(costing, brief_spec), rate,
               group_provisional, boq_scope, unit.get("area_element_id"),
               unit.get("construction_of"))
        group = groups.setdefault(key, {
            "section": section, "spec": spec, "brief_spec": brief_spec,
            "construction_of": unit.get("construction_of"),
            "rate": rate, "assumed": group_provisional,
            "area_element_id": unit.get("area_element_id"),
            "area_element_name": unit.get("area_element_name"),
            "area": 0.0, "drawings": [], "area_rows": [],
            "breakdown": costing.get("breakdown") or {},
            "boq_scope": boq_scope,
            "scope_label": unit.get("scope_label"),
        })
        group["area"] += float(area)
        if drawing and drawing not in group["drawings"]:
            group["drawings"].append(drawing)
        area_label = (unit.get("unit_name") or unit.get("area_label")
                      or _unit_label_from_filename(drawing) or drawing
                      or f"Measured area {len(group['area_rows']) + 1}")
        group["area_rows"].append({
            "description": str(area_label), "qty": float(area), "unit": "m²",
            "drawing": drawing,
        })

    element_units = []
    for unit in results:
        costing = unit.get("costing") or {}
        if costing.get("client_rates_applied"):
            if isinstance(costing.get("rates_version"), int):
                rates_versions.append(costing["rates_version"])
            if costing.get("rates_updated_at"):
                rates_updated_at.append(str(costing["rates_updated_at"]))
            client_rate_fields.extend(costing.get("client_rate_fields") or [])
        area = costing.get("area_m2") or unit.get("area_m2") or 0
        spec = costing.get("spec") or {}
        assumed = bool(costing.get("assumed", True))
        rate = costing.get("rate")
        section = quotation_section(unit)
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        if drawing and drawing not in drawings:
            drawings.append(drawing)
        flags = list(unit.get("flags") or [])
        pipeline_flags.extend(flags)

        stored_brief_spec = unit.get("brief_spec")
        if stored_brief_spec:
            brief_spec = stored_brief_spec
        else:
            # Legacy records carry only an effective pricing spec, not field provenance.
            # Show those values for context but keep every field visibly provisional.
            brief_spec = build_brief_spec(
                normalise_slab_type(section, text=drawing), effective_spec=spec,
            )

        if area:
            accumulate_group(unit, costing, spec, rate, section, drawing, brief_spec, area,
                             assumed)
        for note in unit.get("construction_declarations") or []:
            declarations.append(note)

        if assumed:
            if all(spec.get(key) is not None for key in ("depth_mm", "mesh")):
                declaration = _build_up_note(spec, brief_spec)
            else:
                declaration = "zone specification not provided; assessor must complete the slab checklist"
            declarations.append(f"{provisional_reason_for(brief_spec)}: {declaration}")
        if unit.get("source_discipline") == "architect":
            declarations.append(
                "Area measured from architect's hard-landscaping drawing — ±5% tolerance applies. "
                "No engineer construction-detail drawing found in the pack."
            )
        if unit.get("pending_approval"):
            declarations.append(
                f"{PROVISIONAL_LABEL}: {drawing or 'measured document'} — measured but not yet "
                "approved by the assessor; quantities provisional until approval."
            )
        declarations.extend(f for f in flags if (
            "ASSUMED" in f or "architect" in f.lower() or "tolerance" in f.lower()))
        # Surface-identity doubt must reach the client document, not just the assessor's screen.
        surface_resolved = any(SURFACE_RESOLVED_MARKER in f for f in flags)
        for flag in flags:
            if any(marker in flag for marker in OUTLINE_ASSUMPTION_MARKERS):
                declarations.append(f"{OUTLINE_ASSUMPTION_LABEL}: {flag}")
            elif not surface_resolved and any(m in flag for m in SURFACE_DOUBT_MARKERS):
                declarations.append(f"{SURFACE_DOUBT_LABEL}: {flag}")
        for exclusion in unit.get("exclusions") or []:
            quantity = (f" ({float(exclusion['area_m2']):g} m²)"
                        if isinstance(exclusion.get("area_m2"), (int, float)) else "")
            declarations.append(
                f"EXCLUDED FROM SLAB: {exclusion.get('label', 'recorded exclusion')}{quantity} "
                f"— {exclusion.get('rule', 'outside slab scope')}."
            )
        for prompt in unit.get("exclusion_prompts") or []:
            if prompt.get("status") != "assessor_confirmed":
                declarations.append(
                    f"{PROVISIONAL_LABEL}: EXCLUSION CHECK — "
                    f"{prompt.get('label', 'slab exclusion')}: "
                    f"{prompt.get('rule', 'assessor to confirm measured extent')}."
                )

        perimeter = unit.get("perimeter_lm")
        if perimeter is None and unit.get("polygon_pts") and unit.get("scale_k"):
            from geometry import polygon_perimeter_lm
            perimeter = polygon_perimeter_lm(unit["polygon_pts"], unit["scale_k"])
        if perimeter is not None:
            mkey = (section, "Slab perimeter", "Lm", False)
            measurement = measurements_by_key.setdefault(mkey, {
                "section": section, "description": "Slab perimeter", "qty": 0.0,
                "unit": "Lm", "provisional": False, "drawings": [],
                "quantity_rows": [], "assessor_rate_required": True,
            })
            measurement["qty"] += float(perimeter)
            measurement["quantity_rows"].append({
                "description": _unit_name(unit), "qty": float(perimeter), "unit": "Lm",
                "drawing": drawing,
            })
            if drawing and drawing not in measurement["drawings"]:
                measurement["drawings"].append(drawing)

        assumed_manhole_count = unit.get("manhole_count_assumed")
        if assumed_manhole_count is not None:
            mkey = (section, FORTEL_MH_ROW, "Nr", True)
            measurement = measurements_by_key.setdefault(mkey, {
                "section": section, "description": FORTEL_MH_ROW, "qty": 0,
                "unit": "Nr", "provisional": True, "drawings": [],
                "provisional_reason": PROVISIONAL_LABEL,
                "assessor_rate_required": True,
            })
            measurement["qty"] += int(assumed_manhole_count)
            if drawing and drawing not in measurement["drawings"]:
                measurement["drawings"].append(drawing)
            provenance = next((f for f in flags if "manhole_count_assumed=" in f), "")
            declarations.append(f"{PROVISIONAL_LABEL}: {provenance or 'assumed manhole quantity'}")

        if extras is None:
            for ex in costing.get("extras", []):
                provisional = bool(ex.get("estimate", False))
                item_rate = ex.get("rate")
                item_value = ex.get("value")
                if item_value is None and isinstance(item_rate, (int, float)):
                    item_value = round(float(ex.get("qty") or 0) * item_rate, 2)
                extra_rows.append({
                    "section": _normalise_section(ex.get("section"), section),
                    "description": ex["description"], "qty": ex["qty"], "unit": ex["unit"],
                    "rate": item_rate, "value": item_value,
                    "assessor_rate_required": (item_rate is None and not ex.get("value_status")),
                    "value_status": ex.get("value_status") or "",
                    "provisional": provisional,
                    "provisional_reason": PROVISIONAL_LABEL if provisional else "",
                    "drawings": [drawing] if drawing else [],
                })
                if provisional:
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: {ex['description']} is an estimate from existing "
                        "measurement provenance and must be confirmed before issue."
                    )

    # Marked-zone lengths are source quantities, never implicit prices.  Keep the per-unit
    # provenance so the workbook can expose editable source rows just like its area take-off.
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        for zone in unit.get("zones") or []:
            unit_label = zone.get("unit_label") or _unit_name(unit)
            category = zone.get("category")
            quantities = []
            if category in ("channel", "transition", "construction_joint") and zone.get("length_lm") is not None:
                quantities.append((
                    (ZONE_SECTION.get(zone.get("slab_category"), quotation_section(unit))
                     if category == "construction_joint" else "External yard slabs"),
                    (FORTEL_CHANNEL_ROW if category == "channel" else
                     FORTEL_TRANSITION_ROW if category == "transition" else
                     "Internal construction joint (CJ)"),
                    float(zone["length_lm"]),
                ))
                if category == "construction_joint":
                    declarations.append(
                        "CONSTRUCTION JOINT (CJ) — quantity only; rate left blank for assessor. "
                        + str(zone.get("joint_detail") or
                              "Detail requirements must be confirmed from the engineer drawing.")
                    )
            if category in ZONE_SECTION and zone.get("perimeter_lm") is not None:
                quantities.append((ZONE_SECTION[category], "Slab perimeter",
                                   float(zone["perimeter_lm"])))
            for section, description, quantity in quantities:
                mkey = (section, description, "Lm", False)
                measurement = measurements_by_key.setdefault(mkey, {
                    "section": section, "description": description, "qty": 0.0,
                    "unit": "Lm", "provisional": False, "drawings": [],
                    "quantity_rows": [], "assessor_rate_required": True,
                })
                measurement["qty"] += quantity
                measurement["quantity_rows"].append({
                    "description": unit_label, "qty": quantity, "unit": "Lm",
                    "drawing": drawing,
                })
                if drawing and drawing not in measurement["drawings"]:
                    measurement["drawings"].append(drawing)

    # Accepted channel proposals remain assumptions, but an assessor decision makes their
    # quantity quoteable as a provisional, blank-rate line.  Pending proposals are never
    # smuggled into totals: they produce an explicit declaration instead.
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        proposals = [proposal for proposal in (unit.get("channel_proposals") or [])
                     if isinstance(proposal, dict) and proposal.get("proposal_id")]
        decisions = unit.get("channel_proposal_decisions") or {}
        for proposal in proposals:
            proposal_id = proposal["proposal_id"]
            decision = decisions.get(proposal_id) if isinstance(decisions, dict) else None
            decision = decision if isinstance(decision, dict) else {}
            status = decision.get("decision")
            component = proposal.get("component")
            component_label = (
                "dock retaining-wall/loading-face run"
                if component == "dock_retaining_wall"
                else "yard retaining-wall-adjacent run"
                if component in {"yard_longest_contained_run", "yard_wall_adjacent_run"}
                else str(component or "channel run").replace("_", " ")
            )
            basis = str(proposal.get("basis") or "assessor-reviewed channel assumption")
            if status == "accepted":
                length_lm = decision.get("length_lm")
                if (isinstance(length_lm, (int, float)) and not isinstance(length_lm, bool)
                        and length_lm > 0):
                    edited = "assessor-edited" if decision.get("edited") else "assessor-accepted"
                    add_assessor_eo(
                        "External yard slabs", FORTEL_CHANNEL_ROW, length_lm, "Lm",
                        drawing=drawing, provisional=True, basis=basis,
                    )
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: {component_label} — {length_lm:g} Lm included "
                        f"as an {edited} assumption with rate left blank; basis: {basis}."
                    )
                else:
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: accepted channel proposal {proposal_id!r} has no "
                        "valid length and is not included; assessor must correct it before issue."
                    )
            elif status != "removed":
                declarations.append(
                    f"{PROVISIONAL_LABEL}: channel proposal {component_label} on "
                    f"{drawing or 'drawing'} has not been actioned; no channel quantity or "
                    "price is included."
                )

    # Transition candidates follow the same assessor-review lifecycle as channels.  They are
    # assumptions until explicitly accepted/edited, never measured zones or automatic prices.
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        candidates = [candidate for candidate in (unit.get("transition_candidates") or [])
                      if isinstance(candidate, dict) and candidate.get("candidate_id")]
        decisions = unit.get("transition_candidate_decisions") or {}
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            decision = decisions.get(candidate_id) if isinstance(decisions, dict) else None
            decision = decision if isinstance(decision, dict) else {}
            status = decision.get("decision")
            region_label = str(candidate.get("region_id") or "Yard entrance")
            basis = str(candidate.get("basis") or
                        "assessor-reviewed tarmac-to-concrete Yard entrance assumption")
            if status == "accepted":
                length_lm = decision.get("length_lm")
                if (isinstance(length_lm, (int, float)) and not isinstance(length_lm, bool)
                        and length_lm > 0):
                    edited = "assessor-edited" if decision.get("edited") else "assessor-accepted"
                    add_assessor_eo(
                        "External yard slabs", FORTEL_TRANSITION_ROW, length_lm, "Lm",
                        drawing=drawing, provisional=True, basis=basis,
                    )
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: Transition at {region_label} — {length_lm:g} Lm "
                        f"included as an {edited} assumption with rate left blank; basis: {basis}."
                    )
                else:
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: accepted Transition candidate {candidate_id!r} "
                        "has no valid length and is not included; assessor must correct it "
                        "before issue."
                    )
            elif status != "removed":
                declarations.append(
                    f"{PROVISIONAL_LABEL}: Transition candidate at {region_label} on "
                    f"{drawing or 'drawing'} has not been actioned; no Transition quantity "
                    "or price is included."
                )

    # User-drawn channels (red dotted lines added by the assessor)
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        user_channels = unit.get("user_channels") or []
        if not isinstance(user_channels, list):
            continue
        for idx, ch in enumerate(user_channels, 1):
            if not isinstance(ch, list) or len(ch) < 2:
                continue
            # Assessor geometry is an open polyline in canvas-point space. Sum every segment;
            # using only its first/last points would silently omit bends from the BOQ quantity.
            try:
                length_pts = sum(
                    ((float(ch[point_index][0]) - float(ch[point_index - 1][0])) ** 2
                     + (float(ch[point_index][1]) - float(ch[point_index - 1][1])) ** 2) ** 0.5
                    for point_index in range(1, len(ch))
                )
            except (TypeError, ValueError, IndexError):
                continue
            # Convert to metres using the job's scale
            scale_k = unit.get("scale_k") or (unit.get("result") or {}).get("scale_k")
            if not scale_k or scale_k <= 0:
                continue
            length_lm = round(length_pts * scale_k, 2)
            if length_lm <= 0:
                continue
            add_assessor_eo(
                "External yard slabs", FORTEL_CHANNEL_ROW, length_lm, "Lm",
                drawing=drawing, provisional=True,
                basis="assessor-drawn channel on portal",
            )
            declarations.append(
                f"{PROVISIONAL_LABEL}: assessor-drawn channel #{idx} — {length_lm:g} Lm "
                f"included with rate left blank; basis: assessor-drawn on portal."
            )

    # Cut-out regions (subtracted from measured area)
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        cutout_regions = unit.get("cutout_regions") or []
        if not isinstance(cutout_regions, list) or not cutout_regions:
            continue
        declarations.append(
            f"NOTE: {len(cutout_regions)} cut-out region(s) subtracted from measured area "
            f"on {drawing or 'drawing'} by assessor."
        )

    # ``+ Area`` elements are assessor-traced independent quantities.  They deliberately do
    # not enter the main slab group above: each client-entered name becomes one separate row
    # and inherits only an already-existing, unambiguous rate.  No rate is created here.
    for unit in source_results:
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        parent_costing = unit.get("costing") or {}
        parent_zone_categories = {
            str(zone.get("category") or "").strip().lower()
            for zone in (unit.get("zones") or []) if isinstance(zone, dict)
            and zone.get("area_m2") is not None
        }
        zone_costings = unit.get("zone_costings") or {}
        for element in unit.get("area_elements") or []:
            if not isinstance(element, dict) or not isinstance(
                    element.get("area_m2"), (int, float)):
                continue
            name = str(element.get("name") or "").strip()
            if not name:
                continue
            category = str(element.get("category") or "").strip().lower()
            section = ZONE_SECTION.get(category)
            section_unresolved = section is None
            if section_unresolved:
                # Drafts may be downloaded before approval. Keep the element visible with a
                # loud declaration; /approve hard-blocks this state, so it cannot silently
                # become a client-issued priced row in the wrong section.
                section = quotation_section(unit)
                declarations.append(
                    f"{PROVISIONAL_LABEL}: separately named area {name!r} has no confirmed "
                    "BOQ section; assessor must classify it before approval."
                )
            explicit_costing = (element.get("costing")
                                or zone_costings.get(category) or {})
            if explicit_costing:
                effective_costing = explicit_costing
            elif (len(parent_zone_categories) <= 1
                  and ZONE_SECTION.get(category) == quotation_section(unit)):
                # A single aggregate rate is reusable only inside its own BOQ section. Copying
                # a Yard build-up onto a separately named Dock/Office element would create a
                # plausible but wrong price; leave that editable workbook rate blank instead.
                effective_costing = parent_costing
            else:
                # One aggregate rate cannot safely be copied across a mixed Yard/Dock/Office
                # drawing. The editable workbook leaves the rate cell blank for the assessor.
                effective_costing = {}
            rate = effective_costing.get("rate")
            provisional = bool(effective_costing.get(
                "assumed", parent_costing.get("assumed", True))) or section_unresolved
            quantity = round(float(element["area_m2"]), 3)
            if not section_unresolved and category != "other":
                # Inderjit, 27 Aug: "It just extracted the figure and give me here just as a
                # one row... the other elements — what the thickness of this will be, what the
                # mesh will be in it, what would be the finish — all these sections here should
                # also come under this section as a completely separate element." A classified
                # +Area element now gets its own specification block and its own build-up rows,
                # keyed by element id so it can never merge into the main slab group.
                #
                # Its concrete row keeps EXACTLY the rate the single row carried before, and
                # the priced adders (trim, joints) are left for the assessor rather than
                # applied to a quantity nobody priced them against: no rate is created,
                # changed, or applied to new quantity by this change.
                element_brief_spec = (
                    (unit.get("brief_specs") or {}).get(category)
                    or build_brief_spec(normalise_slab_type(section, text=drawing),
                                        effective_spec=effective_costing.get("spec") or {})
                )
                element_unit = dict(
                    unit,
                    area_m2=quantity,
                    costing=dict(effective_costing, area_m2=quantity),
                    zones=[],
                    area_elements=[],
                    brief_spec=element_brief_spec,
                    quotation_section=section,
                    unit_name=name,
                    area_label=name,
                    scope_label=name,
                    area_element_id=element.get("element_id") or name,
                    area_element_name=name,
                    # The element carries its OWN BOQ scope. Inheriting the parent's would put
                    # a footpath under "Plant deck — " and into the wrong scoped section purely
                    # because the main slab happened to be a plant deck.
                    boq_scope=str(element.get("boq_scope") or "main"),
                )
                element_units.append(element_unit)
                declarations.append(
                    f"ASSESSOR-NAMED AREA — {name}: {quantity:g} m² measured separately from "
                    f"the main region on {drawing or 'the drawing'}, priced as its own "
                    f"{section.lower()} element."
                )
                continue
            if category == "other":
                # "Other / out of scope" is the escape hatch for an area that is genuinely not
                # a slab. It must not appear as a priced or provisional row at all — it is a
                # declared exclusion, the same shape the pipeline's own exclusions use.
                declarations.append(
                    f"EXCLUDED FROM SLAB: separately named area {name!r} "
                    f"({quantity:g} m²) — assessor classified it as out of scope."
                )
                continue
            extra_rows.append({
                "section": section,
                "description": name,
                "qty": quantity,
                "unit": "m²",
                "rate": rate,
                "value": (round(quantity * rate, 2)
                          if isinstance(rate, (int, float)) else None),
                "assessor_rate_required": rate is None,
                "provisional": provisional,
                "provisional_reason": PROVISIONAL_LABEL if provisional else "",
                "drawings": [drawing] if drawing else [],
                "assessor_named_area": True,
                "element_id": element.get("element_id"),
            })
            declarations.append(
                f"ASSESSOR-NAMED AREA — {name}: {quantity:g} m² measured separately from "
                f"the main region on {drawing or 'the drawing'}."
            )

    for element_unit in element_units:
        element_costing = element_unit.get("costing") or {}
        accumulate_group(
            element_unit, element_costing, element_costing.get("spec") or {},
            element_costing.get("rate"), element_unit["quotation_section"],
            element_unit.get("file") or Path(str(element_unit.get("pdf_path") or "")).name,
            element_unit["brief_spec"], float(element_unit["area_m2"]),
            bool(element_costing.get("assumed", True)),
        )

    line_items = []
    specifications = []
    for group_number, group in enumerate(
            sorted(groups.values(), key=lambda g: _SECTION_RANK[g["section"]]), 1):
        spec = group["spec"]
        area = round(group["area"], 3)
        depth_mm = spec.get("depth_mm")
        mesh = spec.get("mesh")
        mix = spec.get("conc_mix")
        layers = spec.get("layers")
        group_id = f"spec-{group_number}"
        specifications.append({
            "id": group_id,
            "section": group["section"],
            "slab_type": group["brief_spec"].get("slab_type"),
            "slab_type_label": ("Footpath Slabs" if group["section"] == "Footpath slabs"
                                else group["brief_spec"].get("slab_type_label")),
            "fields": group["brief_spec"].get("fields") or {},
            "display_lines": brief_spec_display_lines(group["brief_spec"]),
            "provisional": group["assumed"],
            "drawings": group["drawings"],
            "area_rows": group["area_rows"],
            "area_m2": area,
            "construction_of": group.get("construction_of"),
            "provisional_reason": (provisional_reason_for(group["brief_spec"])
                                   if group["assumed"] else ""),
            # The effective pricing spec this group was costed from. The XLSX rate build-up
            # used to look for a "spec" key on the line items, which has never existed, so
            # every build-up block fell back to its own hardcoded 190mm/120 defaults and
            # disagreed with the rate printed beside it.
            "spec": dict(spec),
            "rate": group["rate"],
        })
        common = {
            "section": group["section"], "qty": area, "unit": "m²",
            "drawings": group["drawings"], "specification_id": group_id,
        }
        scope_prefix = {
            "plant_deck": "Plant deck — ",
            "pod_first_floor": "POD first floor — ",
        }.get(group.get("boq_scope"), "")
        if group.get("area_element_name"):
            scope_prefix = f"{group['area_element_name']} — "
        elif group.get("construction_of"):
            # Name the construction on the priced line itself, not only in the take-off block
            # above it. Only when this group IS one construction: two constructions drawn at
            # the same thickness share one specification and therefore one priced row, and
            # labelling that row with either of their names would be false.
            names = {str(area_row.get("description") or "")
                     for area_row in group.get("area_rows") or []}
            if len(names) == 1 and names != {""}:
                scope_prefix = f"{names.pop()} — " 
        slab_desc = scope_prefix + _fortel_concrete_description(spec)
        if spec.get("depth_mm") is None and group.get("construction_of"):
            # _fortel_concrete_description already omits the thickness when there is none, but
            # a row reading "Concrete Slabs" beside a blank rate does not say WHY it is blank.
            slab_desc += " — thickness not stated on the drawing, rate for assessor"
        elif depth_unresolved_on_drawing(group["brief_spec"]):
            # Same requirement, different reason: here the sheet states SEVERAL thicknesses,
            # each for its own surface, and none has been chosen for this one.
            slab_desc += (" — the drawing states more than one thickness and none is "
                          "confirmed for this surface, rate for assessor")
        # Name the details that ARE from the drawing. "NO DETAILS PROVIDED" on a row whose
        # thickness was read off the legend and cited is simply untrue, and the client reads it.
        group_reason = provisional_reason_for(group["brief_spec"]) if group["assumed"] else ""
        line_items.append({
            **common, "description": slab_desc, "rate": group["rate"],
            "value": (round(area * group["rate"], 2)
                      if isinstance(group.get("rate"), (int, float)) else None),
            "assessor_rate_required": group.get("rate") is None,
            "provisional": group["assumed"],
            "provisional_reason": group_reason,
            "line_role": "concrete_slab",
        })

        # Fortel's standard sheet shows these components as included in the concrete slab
        # rate.  They are presentation rows only: no component rate is split out, derived or
        # copied from the supplied commercial workbook, so the quotation total is unchanged.
        included_rows = (
            (_fortel_mesh_description(mesh, layers), "mesh"),
            ("Curing Agent", "curing"),
            ("DPM 1200G (Excl. tapes and seals to laps)", "dpm"),
            (("Brush Finish" if group["section"] in {
                "External yard slabs", "Footpath slabs", "Dock slabs"
            } else "Eazifloat Finish"), "finish"),
        )
        for description, line_role in included_rows:
            line_items.append({
                **common, "description": description, "rate": None, "value": None,
                "value_status": "Incl.", "assessor_rate_required": False,
                "provisional": group["assumed"],
                "provisional_reason": group_reason,
                "line_role": line_role,
            })

        # Existing quotation adders are preserved unchanged; only their section/aggregated
        # quantity changes so all units share the client's requested one-tab structure.
        # A separately named area keeps the concrete rate it already had, but trim and joints
        # are NOT auto-applied to it: those are existing rates meeting a quantity nobody has
        # priced them against, so the assessor decides. The quotation total is unchanged by
        # giving the element its own group.
        priced_group = (isinstance(group.get("rate"), (int, float))
                        and not group.get("area_element_id"))
        adders = [
            ("Final Trimm +/-50mm dp. LP Only", area, "m²",
             1.40 if priced_group else None),
            (_fortel_joints_description(group["section"], group["brief_spec"]),
             area, "m²", 4.85 if priced_group else None),
        ]
        for desc, qty, unit_name, item_rate in adders:
            rate_missing = item_rate is None
            line_items.append({
                **common, "description": desc, "qty": qty, "unit": unit_name,
                "rate": item_rate,
                "value": (round(qty * item_rate, 2) if not rate_missing else None),
                "assessor_rate_required": rate_missing,
                "provisional": rate_missing,
                "provisional_reason": PROVISIONAL_LABEL if rate_missing else "",
            })

    if extras is not None:
        fallback_section = quotation_section(results[0])
        for ex in extras:
            if isinstance(ex, dict):
                desc, qty, unit_name = (ex["description"], ex["qty"], ex["unit"])
                item_rate = ex.get("rate")
                section = _normalise_section(ex.get("section"), fallback_section)
                provisional = bool(ex.get("estimate", False))
                value_status = ex.get("value_status") or ""
                item_value = ex.get("value")
            else:
                desc, qty, unit_name, item_rate = ex
                section = fallback_section
                # Use the result's existing provenance; do not infer from wording alone.
                provisional = bool(results[0].get("manhole_count_estimate") and "MH" in desc)
                value_status = ""
                item_value = None
            if item_value is None and isinstance(item_rate, (int, float)):
                item_value = round(qty * item_rate, 2)
            extra_rows.append({
                "section": section, "description": desc, "qty": qty, "unit": unit_name,
                "rate": item_rate, "value": item_value, "value_status": value_status,
                "assessor_rate_required": item_rate is None and not value_status,
                "provisional": provisional,
                "provisional_reason": PROVISIONAL_LABEL if provisional else "",
                "drawings": [],
            })
            if provisional:
                declarations.append(f"{PROVISIONAL_LABEL}: {desc} must be confirmed before issue.")

    line_items.extend(extra_rows)
    line_items.sort(key=lambda li: (
        _SECTION_RANK.get(li["section"], len(SECTION_ORDER)),
        1 if _fortel_eo_description(li.get("description")) else 0,
        _FORTEL_EO_ORDER.get(_fortel_eo_description(li.get("description")), 0),
    ))
    measurements = sorted(measurements_by_key.values(),
                          key=lambda item: _SECTION_RANK[item["section"]])
    subtotal = round(sum(float(li["value"]) for li in line_items
                         if isinstance(li.get("value"), (int, float))), 2)
    assumed = any(specification.get("provisional") for specification in specifications)
    for specification in specifications:
        provisional_labels = [
            line["label"] for line in specification["display_lines"] if line["provisional"]
        ]
        if provisional_labels:
            declarations.append(
                f"{specification.get('provisional_reason') or PROVISIONAL_LABEL}: "
                f"{specification['slab_type_label'] or specification['section']} "
                f"— {', '.join(provisional_labels)}."
            )

    if extras is None:
        declarations.append(
            "NOTE — Other extra-over items not explicitly listed in this quotation are not "
            "included; the assessor must add any further quantities before issue."
        )

    distinct_rate_versions = sorted(set(rates_versions))
    distinct_rate_fields = _unique_notes(client_rate_fields)
    if distinct_rate_versions:
        version_text = ", ".join(str(version) for version in distinct_rate_versions)
        field_text = ", ".join(distinct_rate_fields)
        declarations.append(
            f"CLIENT-EDITED RATES — rates version {version_text} applied to this pricing"
            + (f" ({field_text})." if field_text else ".")
        )

    # Documents in the case that have NO measured area yet (e.g. line/hatch office GA plans
    # refused to assessor-trace).  They must be visible in the quotation — a document that
    # silently vanishes from the case output reads as "skipped" (Aryan, 17 Jul).
    for note in (caveats or []):
        declarations.append(str(note))

    unmeasured_docs = [dict(d) for d in (unmeasured or []) if d]
    for doc in unmeasured_docs:
        declarations.append(
            f"NOT YET MEASURED — {doc.get('file', 'document')}: no area produced "
            f"({doc.get('state', 'UNMEASURED')}); awaiting assessor trace before it can be "
            "quantified and priced. Not included in the totals below."
        )

    first_costing = results[0].get("costing") or {}
    first_spec = first_costing.get("spec") or {}
    disciplines = _unique_notes([unit.get("source_discipline", "unknown") for unit in results])
    perimeter_measurements = [m for m in measurements if m["description"] == "Slab perimeter"]
    total_perimeter = (round(sum(float(m["qty"]) for m in perimeter_measurements), 1)
                       if perimeter_measurements else None)
    quotation = {
        "ref": ref, "date": today, "project": project, "client": client,
        "drawing": ", ".join(drawings), "drawings": drawings,
        "drawing_type": results[0].get("type", ""),
        "discipline": ", ".join(disciplines),
        "area_m2": round(sum(float((unit.get("costing") or {}).get("area_m2")
                                   or unit.get("area_m2") or 0) for unit in results), 3),
        "perimeter_lm": total_perimeter,
        "measurements": measurements,
        "spec": {
            "depth_mm": first_spec.get("depth_mm"),
            "mesh": first_spec.get("mesh"),
            "conc_mix": first_spec.get("conc_mix"),
            "layers": first_spec.get("layers"),
        },
        "specifications": specifications,
        "rate": first_costing.get("rate"), "breakdown": first_costing.get("breakdown") or {},
        "line_items": line_items, "section_order": list(SECTION_ORDER),
        "subtotal_gbp": subtotal, "total_gbp": subtotal,
        "assumed": assumed, "declarations": _unique_notes(declarations),
        "unmeasured": unmeasured_docs,
        "pipeline_flags": _unique_notes(pipeline_flags), "terms": STANDARD_TERMS,
        # The real BOQ locates these fields in the header/post-total block.  They are
        # project-specific and therefore optional; nothing is silently copied from one quote.
        "revision": commercial.get("revision") or ref,
        "measurement_basis": commercial.get("measurement_basis") or "",
        "joint_layout_note": commercial.get("joint_layout_note") or "",
        "joint_details_note": commercial.get("joint_details_note") or "",
        "market_warning": commercial.get("market_warning") or "",
        "commercial_terms": list(commercial.get("terms") or []),
    }
    if distinct_rate_versions:
        quotation.update({
            "rates_version": (distinct_rate_versions[0] if len(distinct_rate_versions) == 1
                              else distinct_rate_versions[-1]),
            "rates_versions": distinct_rate_versions,
            "rates_updated_at": max(rates_updated_at) if rates_updated_at else None,
            "client_rate_fields": distinct_rate_fields,
            "client_rates_applied": True,
        })
    return quotation


# ── Formatters ────────────────────────────────────────────────────────────────

def _display_qty(value) -> str:
    """Human quantity with at most two decimals; never round accepted Lm to an integer."""
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def quotation_text(q: dict) -> str:
    """Plain-text quotation — suitable for email body or Word paste."""
    lines = [
        f"{FORTEL_NAME}",
        f"Quotation Ref: {q['ref']}",
        f"Date: {q['date']}",
        "",
        f"Project : {q['project'] or '—'}",
        f"Client  : {q['client'] or '—'}",
        f"Drawing : {q['drawing']} ({q.get('drawing_type','')}, {q.get('discipline','')}'s drawing)",
        "",
    ]
    if q.get("specifications"):
        lines.append("SLAB SPECIFICATION CHECKLIST:")
        for specification in q["specifications"]:
            lines.append(
                f"  {specification['section']} — "
                f"{specification.get('slab_type_label') or 'Specification'}"
            )
            for field in specification.get("display_lines", []):
                citation = (f" (Source: {field['source_citation']})"
                            if field.get("source_citation") else "")
                lines.append(f"    {field['label']}: {field['value']}{citation}")
        lines.append("")
    lines += [
        "=" * 70,
        f"{'DESCRIPTION':<42}{'QTY':>8}{'UNIT':>6}{'RATE £':>9}{'VALUE £':>13}",
        "-" * 70,
    ]
    last_section = None
    for li in q["line_items"]:
        if li["section"] != last_section:
            lines.append(f"\n  {li['section'].upper()}")
            last_section = li["section"]
        description = _provisional_text(li)
        rate_text = f"{li['rate']:.2f}" if isinstance(li.get("rate"), (int, float)) else ""
        value_text = (str(li.get("value_status")) if li.get("value_status") else
                      (f"{li['value']:,.2f}" if isinstance(li.get("value"), (int, float)) else ""))
        qty_text = _display_qty(li["qty"])
        lines.append(
            f"  {description:<40}{qty_text:>8}{li['unit']:>6}"
            f"{rate_text:>9}{value_text:>13}"
        )
    lines += [
        "-" * 70,
        f"{'TOTAL NETT (excl. VAT)':<55}{q['total_gbp']:>15,.2f}",
        "=" * 70,
        "",
    ]
    if q.get("measurements"):
        lines.append("INFORMATIONAL MEASUREMENTS (NOT PRICED):")
        for measurement in q["measurements"]:
            description = _provisional_text(measurement)
            lines.append(
                f"  {measurement['section']} — {description}: "
                f"{measurement['qty']:,.1f} {measurement['unit']}"
            )
        lines.append("")
    if q["declarations"]:
        lines.append("NOTES / ASSUMPTIONS:")
        for d in q["declarations"]:
            lines.append(f"  • {d}")
        lines.append("")
    lines += [
        "STANDARD TERMS:",
        f"  {q['terms']}",
        "",
        f"Issued by: {FORTEL_NAME} · {FORTEL_EMAIL} · {FORTEL_TEL}",
    ]
    return "\n".join(lines)


def quotation_html(q: dict) -> str:
    """Self-contained HTML quotation — suitable for email or browser view."""
    def _h(value):
        return html.escape(str(value if value is not None else ""), quote=True)

    def _row(li):
        provisional = (" <strong style='color:#9a6500'>"
                       f"{_h(li.get('provisional_reason') or PROVISIONAL_LABEL)}</strong>"
                       if li.get("provisional") else "")
        rate = (f"£{li['rate']:.2f}" if isinstance(li.get("rate"), (int, float)) else "")
        value = (_h(li.get("value_status")) if li.get("value_status") else
                 (f"£{li['value']:,.2f}" if isinstance(li.get("value"), (int, float)) else ""))
        return (f"<tr><td>{_h(li['section'])}</td><td>{_h(li['description'])}{provisional}</td>"
                f"<td style='text-align:right'>{_display_qty(li['qty'])} {_h(li['unit'])}</td>"
                f"<td style='text-align:right'>{rate}</td>"
                f"<td style='text-align:right'>{value}</td></tr>")

    assumed_banner = ""
    if q["assumed"]:
        assumed_banner = (
            f"<div style='background:#fef9ec;border-left:4px solid #e67e22;"
            f"padding:10px 16px;margin:16px 0;font-size:13px;color:#7a5200'>"
            f"⚠ <b>Build-up assumed</b> — {_h(q['declarations'][0]) if q['declarations'] else ''}"
            f"</div>"
        )

    decl_html = ""
    if q["declarations"]:
        items = "".join(f"<li>{_h(d)}</li>" for d in q["declarations"])
        decl_html = f"<h3>Notes / Assumptions</h3><ul style='font-size:13px'>{items}</ul>"

    rows = "\n".join(_row(li) for li in q["line_items"])
    specification_html = ""
    if q.get("specifications"):
        spec_blocks = []
        for specification in q["specifications"]:
            fields = "".join(
                f"<tr><td>{_h(field['label'])}</td><td>{_h(field['value'])}"
                + (f"<br><small>Source: {_h(field['source_citation'])}</small>"
                   if field.get("source_citation") else "")
                + "</td></tr>"
                for field in specification.get("display_lines", [])
            )
            spec_blocks.append(
                f"<h4 style='margin:12px 0 4px'>{_h(specification['section'])} — "
                f"{_h(specification.get('slab_type_label') or 'Specification')}</h4>"
                f"<table style='margin:4px 0 12px'><tbody>{fields}</tbody></table>"
            )
        specification_html = "<h3>Slab specification checklist</h3>" + "".join(spec_blocks)
    measurement_html = ""
    if q.get("measurements"):
        measurement_rows = ""
        for measurement in q["measurements"]:
            marker = (" <strong style='color:#9a6500'>"
                      f"{_h(measurement.get('provisional_reason') or PROVISIONAL_LABEL)}"
                      "</strong>" if measurement.get("provisional") else "")
            measurement_rows += (
                f"<tr><td>{_h(measurement['section'])}</td>"
                f"<td>{_h(measurement['description'])}{marker}</td>"
                f"<td style='text-align:right'>{measurement['qty']:,.1f} "
                f"{_h(measurement['unit'])}</td></tr>"
            )
        measurement_html = (
            "<h3>Informational measurements <small>(not priced)</small></h3>"
            "<table><thead><tr><th>Section</th><th>Measurement</th>"
            "<th style='text-align:right'>Quantity</th></tr></thead>"
            f"<tbody>{measurement_rows}</tbody></table>"
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Quotation {_h(q['ref'])}</title>
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#111;max-width:760px;margin:32px auto;padding:0 16px}}
header{{background:#13294b;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0}}
header h1{{margin:0;font-size:22px}}
header p{{margin:4px 0 0 0;opacity:.75;font-size:13px}}
.meta{{display:flex;gap:40px;padding:14px 0;border-bottom:1px solid #ddd;font-size:13px;color:#555}}
.meta b{{color:#111}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th{{background:#f7f8fa;padding:7px 10px;text-align:left;font-size:12px;
    text-transform:uppercase;letter-spacing:.5px;color:#666;border-bottom:2px solid #ddd}}
td{{padding:6px 10px;border-bottom:1px solid #eee;font-size:13px}}
.total-row td{{font-weight:700;font-size:15px;border-top:2px solid #111;background:#f7f8fa}}
.terms{{font-size:12px;color:#888;margin-top:20px;line-height:1.6}}
footer{{font-size:11px;color:#bbb;margin-top:20px;padding-top:12px;border-top:1px solid #eee}}
</style></head><body>
<header>
  <h1>{_h(FORTEL_NAME)}</h1>
  <p>Quotation {_h(q['ref'])} &nbsp;·&nbsp; {_h(q['date'])}</p>
</header>
<div class="meta">
  <div><b>Project</b><br>{_h(q.get('project') or '—')}</div>
  <div><b>Client</b><br>{_h(q.get('client') or '—')}</div>
  <div><b>Drawing</b><br>{_h(q['drawing'])}</div>
  <div><b>Area</b><br><b style='font-size:18px;color:#13294b'>{q['area_m2']:,.0f} m²</b></div>
</div>
{assumed_banner}
{specification_html}
<table>
  <thead><tr>
    <th>Section</th><th>Description</th><th style='text-align:right'>Qty</th>
    <th style='text-align:right'>Rate</th><th style='text-align:right'>Value</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  <tfoot>
    <tr class='total-row'>
      <td colspan='4'>TOTAL NETT (excl. VAT)</td>
      <td style='text-align:right'>£{q['total_gbp']:,.2f}</td>
    </tr>
  </tfoot>
</table>
{measurement_html}
{decl_html}
<p class='terms'>{_h(q['terms'])}</p>
<footer>{_h(FORTEL_NAME)} &nbsp;·&nbsp; {_h(FORTEL_EMAIL)} &nbsp;·&nbsp; {_h(FORTEL_TEL)}</footer>
</body></html>"""


def quotation_json(q: dict) -> str:
    """JSON record — for the tracker / n8n."""
    return json.dumps(q, indent=2, default=str, ensure_ascii=False)


def _excel_text(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _excel_date(value):
    """Return a real Excel date when the quotation date is in a known format."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    for fmt in ("%d %B %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(value), fmt).date()
        except (TypeError, ValueError):
            pass
    return _excel_text(value)


def _excel_unit(value):
    """Match the unit spellings used in Fortel's editable BOQ template."""
    text = str(value or "")
    return {
        "m²": "m2", "m2": "m2",
        "m³": "m3", "m3": "m3",
        "lm": "Lm", "nr": "Nr", "item": "Item", "t": "T",
    }.get(text.casefold(), text)


def _excel_section_title(section, items):
    labels = {
        "External yard slabs": "External Yard Slabs",
        "Footpath slabs": "Footpath Slabs",
        "Dock slabs": "Dock Slabs",
        "Ground floor slabs": "Ground Floor Slabs",
        "Upper floor slabs": "Upper Floors",
        "Prelims": "Prelims",
    }
    title = labels.get(section, str(section))
    if section != "Prelims" and any(item.get("provisional") for item in items):
        # "(No Details)" is only true when nothing was extracted. On a Surface Finishes Plan
        # the thicknesses ARE on the drawing and cited, so the heading contradicted the rows
        # under it. Say provisional -- which is true, the mix and mesh are still assumed --
        # without claiming the drawing gave us nothing.
        extracted = any(item.get("provisional")
                        and (item.get("provisional_reason") or PROVISIONAL_LABEL)
                        != PROVISIONAL_LABEL for item in items)
        title += ("- Provisional Cost (Part Detailed)" if extracted
                  else "- Provisional Cost (No Details)")
    return title


# One rate build-up per priced construction, stacked down columns G-J beside the take-off.
# sr+0 header .. sr+16 the agreement check, then a blank separator row.
_BUILDUP_BLOCK_ROWS = 18


def _write_rate_buildup(ws, q: dict, anchors: dict | None = None):
    """Write one rate build-up per PRICED construction, in columns G onwards.

    This block is Fortel's own costing working, and the assessor edits its yellow inputs and
    expects the green cells to recalculate. It must therefore be an Excel transcription of
    ``costing.rate_buildup`` -- the function that produced the rate printed in column D -- and
    nothing else. The invariant is exact and tested: for every construction,
    ``TOTAL RATE/M2`` equals that construction's RATE cell.

    It used to be a transcription of the reference workbook's LAYOUT at fixed row numbers
    (External yard = 5, Footpath = 42, ...), written before a measured area could fan into
    several constructions. Every one of its cross-references into the take-off then pointed at
    whatever had since moved into that row (Aryan, 16 Sep, full formula audit):

      * ``Decarbonisation Charge`` and ``E/O RATE/M2`` divided by ``B12``, once the section's
        area row and now a blank cell -- the ``#DIV/0!`` beside the 180 mm rows, propagated up
        through ``Nett Total`` into ``TOTAL RATE/M2``.
      * ``Total Trimming`` subtracted ``D{sr+14}``, which had become the Joints rate, so
        trimming priced at a negative number.
      * ``Steel Rate/T`` was written as a LABEL with no value, so the mesh line multiplied by
        an empty cell and all steel cost read zero; the DPM line looked up the gauge in a
        table whose price column was ``=AQ9`` -- also empty -- so DPM read zero too.
      * The spec's ``dpm`` is Fortel's DPM cost in pounds per m2 (0.46). It was being written
        into a cell labelled "Gauge" and looked up as if it were 1200G sheeting.
      * A block was emitted for all five BOQ sections whether the job had them or not, each
        falling back to a second, divergent set of defaults (190 mm / GBP 120 / 2.5%) that
        does not match ``defaults.DEFAULT_SPEC``.

    Two rows are deliberately NOT carried over. ``Decarbonisation Charge`` and ``E/O RATE/M2``
    have no counterpart in ``rate_buildup``: including them would guarantee the block could
    not equal the rate beside it, and both were the source of the ``#DIV/0!``. They are left
    out rather than printed broken.
    """
    thin = Side(style="thin")
    medium = Side(style="medium")
    no_side = Side(style=None)
    font_s = Font(name="Arial", size=8)
    font_sb = Font(name="Arial", size=8, bold=True)

    fill_yellow = PatternFill("solid", fgColor="FFFF00")   # editable input
    fill_green = PatternFill("solid", fgColor="FF92D050")  # formula/result
    fill_green_dark = PatternFill("solid", fgColor="FF00B050")  # component totals

    def _cell(r, c, v, bold=False):
        cell = ws.cell(r, c, v)
        cell.font = font_sb if bold else font_s
        return cell

    def _border_gj(r):
        ws.cell(r, 7).border = Border(left=medium, right=no_side,
                                      top=no_side, bottom=no_side)
        ws.cell(r, 8).border = Border(left=no_side, right=medium,
                                      top=no_side, bottom=no_side)
        ws.cell(r, 9).border = Border(left=medium, right=no_side,
                                      top=no_side, bottom=no_side)
        ws.cell(r, 10).border = Border(left=no_side, right=medium,
                                       top=no_side, bottom=no_side)

    def _border_gj_header(r):
        for c in range(7, 11):
            ws.cell(r, c).border = Border(
                top=medium, bottom=thin,
                left=medium if c in (7, 9) else no_side,
                right=medium if c in (8, 10) else no_side)

    def _border_gj_totals(r):
        ws.cell(r, 7).border = Border(left=medium, bottom=medium)
        ws.cell(r, 8).border = Border(right=medium, bottom=medium)
        ws.cell(r, 9).border = Border(left=medium, bottom=medium)
        ws.cell(r, 10).border = Border(right=medium, bottom=medium)

    anchors = anchors or {}
    # Only a construction that HAS a rate gets a build-up. The Rail Crossing -- measured, no
    # stated thickness, rate left blank for the assessor -- must not acquire a priced build-up
    # here by the back door; that is the whole point of aba682a.
    blocks = [spec for spec in (q.get("specifications") or [])
              if isinstance(spec.get("rate"), (int, float)) and spec.get("spec")]
    if not blocks:
        return

    currency = '£#,##0.00'
    start_row = 5
    for block_index, specification in enumerate(blocks):
        sp = specification.get("spec") or {}
        anchor = anchors.get(specification.get("id")) or {}
        rate_row = anchor.get("rate_row")
        sr = start_row + block_index * _BUILDUP_BLOCK_ROWS

        depth = sp.get("depth_mm")
        conc_rate = sp.get("conc_rate")
        conc_wastage = sp.get("conc_wastage")
        mesh = sp.get("mesh")
        mesh_kg = MESH_KG.get(mesh)
        layers = sp.get("layers")
        steel_rate_t = sp.get("steel_rate_t")
        steel_wastage = sp.get("steel_wastage")
        lap_acc = sp.get("lap_acc")
        dpm = sp.get("dpm")
        curing = sp.get("curing")
        labour = sp.get("labour")
        trim = sp.get("trim")
        margin = sp.get("margin")
        # A block can only be written from the numbers the rate was actually built from. If
        # any is absent the rate did not come from rate_buildup, and a block reconstructed
        # around the gap would silently disagree with column D.
        if any(value is None for value in (
                depth, conc_rate, conc_wastage, mesh_kg, layers, steel_rate_t,
                steel_wastage, lap_acc, dpm, curing, labour, trim, margin)):
            continue

        # Name the CONSTRUCTION, not the BOQ section: four blocks all headed
        # "External/Service Yard Slabs" tell the assessor nothing about which is which.
        names = {str(area_row.get("description") or "")
                 for area_row in specification.get("area_rows") or []}
        title = (names.pop() if len(names) == 1 and names != {""}
                 else (specification.get("slab_type_label")
                       or specification.get("section") or "Slab"))
        # ── sr+0: what this build-up belongs to, and the depth that drives it ──────
        _cell(sr, 7, f"Rate build-up — {title}", True)
        _cell(sr, 9, "Slab Depth (mm)")
        _cell(sr, 10, depth)
        ws.cell(sr, 10).fill = fill_yellow
        _border_gj_header(sr)

        # ── sr+1..3: concrete ─────────────────────────────────────────────────────
        _cell(sr+1, 7, "Concrete/m2")
        _cell(sr+1, 8, f"=J{sr}/1000*J{sr+1}")
        ws.cell(sr+1, 8).fill = fill_green
        _cell(sr+1, 9, "Concrete Rate/m3")
        _cell(sr+1, 10, conc_rate)
        ws.cell(sr+1, 10).fill = fill_yellow
        _cell(sr+1, 12, sp.get("conc_mix") or "C32/40")
        _border_gj(sr+1)

        _cell(sr+2, 7, "Wastage/m2")
        _cell(sr+2, 8, f"=H{sr+1}*J{sr+2}")
        ws.cell(sr+2, 8).fill = fill_green
        _cell(sr+2, 9, "Concrete Wastage %")
        _cell(sr+2, 10, conc_wastage)
        ws.cell(sr+2, 10).fill = fill_yellow
        _border_gj(sr+2)

        _cell(sr+3, 7, "Total Concrete/m2", True)
        _cell(sr+3, 8, f"=SUM(H{sr+1}:H{sr+2})", True)
        ws.cell(sr+3, 7).fill = fill_green_dark
        ws.cell(sr+3, 8).fill = fill_green
        _border_gj(sr+3)

        # ── sr+4..7: steel ────────────────────────────────────────────────────────
        _cell(sr+4, 7, "Mesh Rate/m2")
        _cell(sr+4, 8, f"=J{sr+4}*J{sr+5}*J{sr+6}/1000")
        ws.cell(sr+4, 8).fill = fill_green
        _cell(sr+4, 9, f"Mesh {mesh} kg/m2")
        _cell(sr+4, 10, mesh_kg)
        ws.cell(sr+4, 10).fill = fill_yellow
        _border_gj(sr+4)

        _cell(sr+5, 7, "Wastage Rate/m2")
        _cell(sr+5, 8, f"=H{sr+4}*J{sr+7}")
        ws.cell(sr+5, 8).fill = fill_green
        _cell(sr+5, 9, "Nr of Layers")
        _cell(sr+5, 10, layers)
        ws.cell(sr+5, 10).fill = fill_yellow
        _border_gj(sr+5)

        _cell(sr+6, 7, "Lap + Accessories Rate/m2")
        _cell(sr+6, 8, f"=H{sr+4}*J{sr+8}")
        ws.cell(sr+6, 8).fill = fill_green
        # The input the old block never wrote: the label sat in column I with column J empty,
        # so the mesh line multiplied by a blank cell and every steel figure read zero.
        _cell(sr+6, 9, "Steel Rate/T")
        _cell(sr+6, 10, steel_rate_t)
        ws.cell(sr+6, 10).fill = fill_yellow
        _border_gj(sr+6)

        _cell(sr+7, 7, "Total Steel Rate/m2", True)
        _cell(sr+7, 8, f"=SUM(H{sr+4}:H{sr+6})", True)
        ws.cell(sr+7, 7).fill = fill_green_dark
        ws.cell(sr+7, 8).fill = fill_green
        _cell(sr+7, 9, "Steel Wastage %")
        _cell(sr+7, 10, steel_wastage)
        ws.cell(sr+7, 10).fill = fill_yellow
        _border_gj(sr+7)

        _cell(sr+8, 9, "Lap % + Accessories")
        _cell(sr+8, 10, lap_acc)
        ws.cell(sr+8, 10).fill = fill_yellow
        _border_gj(sr+8)

        # ── sr+9..12: the fixed per-m2 items, each priced as itself ───────────────
        for offset, (label, value) in enumerate((
                ("DPM Rate/m2", dpm), ("Curing Agent Rate/m2", curing),
                ("Labour Rate/m2", labour), ("Trimming Rate/m2", trim)), start=9):
            _cell(sr+offset, 7, label)
            _cell(sr+offset, 8, f"=J{sr+offset}")
            ws.cell(sr+offset, 8).fill = fill_green
            _cell(sr+offset, 9, label.replace("/m2", " (GBP/m2)"))
            _cell(sr+offset, 10, value)
            ws.cell(sr+offset, 10).fill = fill_yellow
            _border_gj(sr+offset)
        _cell(sr+9, 12, "DPM sheeting (excl. tapes and seals to laps)")

        # ── sr+13..15: nett, margin, the rate that must match column D ────────────
        _cell(sr+13, 7, "Nett Total", True)
        # NOT rounded per component: costing.rate_buildup rounds ONCE, at the end. Rounding
        # the concrete and steel subtotals first moved the 375 mm construction to 72.15
        # against its own priced rate of 72.14.
        _cell(sr+13, 8, f"=H{sr+3}+H{sr+7}+SUM(H{sr+9}:H{sr+12})", True)
        ws.cell(sr+13, 8).fill = fill_green
        _border_gj_totals(sr+13)

        _cell(sr+14, 7, "Margin")
        _cell(sr+14, 8, f"=H{sr+13}*J{sr+14}")
        ws.cell(sr+14, 8).fill = fill_green
        _cell(sr+14, 9, "Margin %")
        _cell(sr+14, 10, margin)
        ws.cell(sr+14, 10).fill = fill_yellow
        _border_gj(sr+14)

        _cell(sr+15, 7, "TOTAL RATE/M2", True)
        _cell(sr+15, 8, f"=ROUND(H{sr+13}*(1+J{sr+14}),2)", True)
        ws.cell(sr+15, 8).fill = fill_green
        ws.cell(sr+15, 8).number_format = currency
        _border_gj_totals(sr+15)

        if rate_row:
            # Say out loud which priced row this build-up is behind, and let the workbook
            # itself report a disagreement rather than leaving it to be found by eye.
            _cell(sr+16, 7, "Agrees with RATE in row " + str(rate_row))
            _cell(sr+16, 8,
                  f'=IF(ROUND(H{sr+15}-D{rate_row},2)=0,"OK","CHECK")')
            _border_gj(sr+16)



def quotation_xlsx(q: dict) -> bytes:
    """Editable one-sheet Excel quotation in Fortel's client-facing BOQ layout."""
    wb = Workbook()
    ws = wb.active
    ws.title = "REV_01"
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 130

    pale_gold = "FFF2CC"
    section_blue = "0070C0"
    black = "000000"
    white = "FFFFFF"
    red = "FF0000"
    thin_grey = Side(style="thin", color="D9D9D9")
    currency_format = ('_-[$£-809]* #,##0.00_-;\\-[$£-809]* #,##0.00_-;'
                       '_-[$£-809]* "-"??_-;_-@_-')

    def _date_text(value):
        parsed = _excel_date(value)
        if isinstance(parsed, (datetime.date, datetime.datetime)):
            return parsed.strftime("%d/%m/%Y")
        return str(parsed or "")

    # Exact header shape in the supplied Winvic BOQ.  Project-specific notices remain blank
    # unless explicitly provided; they are never copied into unrelated quotations.
    # specification_id -> the rows its priced rate and its measured area landed on. The rate
    # build-up blocks are written after the take-off and point at these, so no formula in the
    # workbook can reference a row number that was true for a different job.
    buildup_anchors: dict[str, dict] = {}
    ws["A1"] = _excel_text(f"Project: {q.get('project') or '—'}")
    ws["B1"] = _excel_text(q.get("measurement_basis") or "")
    ws["A2"] = _excel_text(f"Client: {q.get('client') or '—'}")
    ws["B2"] = _excel_text(q.get("joint_layout_note") or "")
    ws["A3"] = _excel_text(f"Date: {_date_text(q.get('date'))}")
    ws["B3"] = _excel_text(q.get("joint_details_note") or "")
    ws["A4"] = _excel_text(f"Rev: {q.get('revision') or q.get('ref') or '—'}")
    if q.get("client_rates_applied"):
        ws["D1"] = _excel_text(f"Client-edited rates version: {q.get('rates_version')}")
        ws["D2"] = _excel_text(f"Rates date: {q.get('rates_updated_at') or '—'}")
        ws["D1"].font = Font(name="Arial", size=9, bold=True)
        ws["D2"].font = Font(name="Arial", size=9)
    ws.merge_cells("D4:E4")
    drawings = q.get("drawings") or []
    ws["A5"] = _excel_text(
        "Drawing ref available at tender:" +
        (("\n" + "\n".join(str(item) for item in drawings)) if drawings else "")
    )
    ws["A5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[5].height = max(30, 15 * max(2, len(drawings) + 1))
    ws["A6"] = _excel_text(q.get("market_warning") or "")
    ws["A6"].font = Font(name="Arial", size=8, color=red)
    ws["A6"].alignment = Alignment(wrap_text=True, vertical="top")
    for row_index in range(1, 6):
        ws.cell(row_index, 1).font = Font(name="Arial", size=9, bold=row_index in (1, 2, 3, 4))
        ws.cell(row_index, 2).font = Font(name="Arial", size=9)

    sections = OrderedDict()
    for item in q.get("line_items", []):
        sections.setdefault(item.get("section") or "External yard slabs", []).append(item)
    assessor_measurements = [m for m in (q.get("measurements") or [])
                             if m.get("assessor_rate_required")]
    for measurement in assessor_measurements:
        sections.setdefault(measurement.get("section") or "External yard slabs", [])
    ordered_sections = [section for section in q.get("section_order", SECTION_ORDER) if section in sections]
    ordered_sections += [section for section in sections if section not in ordered_sections]

    specifications = {specification["id"]: specification
                      for specification in q.get("specifications", [])}
    row = 7
    section_fill = PatternFill("solid", fgColor=section_blue)
    provisional_fill = PatternFill("solid", fgColor=pale_gold)
    headings = ("DESCRIPTION", "QTY", "UNIT", "RATE", "VALUE")

    for col, heading in enumerate(headings, 1):
        cell = ws.cell(row, col, heading)
        cell.font = Font(name="Arial", bold=True, color=white, size=11)
        cell.fill = PatternFill("solid", fgColor=black)
        cell.alignment = Alignment(horizontal="right" if col in (2, 4, 5) else "left")
    ws.row_dimensions[row].height = 20
    row += 2  # the supplied workbook leaves row 8 blank

    for section in ordered_sections:
        section_items = sections[section]
        ws.cell(row, 1, _excel_section_title(section, section_items))
        for col in range(1, 6):
            ws.cell(row, col).fill = section_fill
            ws.cell(row, col).font = Font(name="Arial", bold=True, color=white, size=9)
        ws.cell(row, 1).alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 22
        row += 1

        group_ids = list(dict.fromkeys(
            item.get("specification_id") for item in section_items
            if item.get("specification_id")
        ))
        ungrouped = [item for item in section_items if not item.get("specification_id")]
        special_ungrouped = [item for item in ungrouped
                             if _fortel_eo_description(item.get("description"))]
        ungrouped = [item for item in ungrouped
                     if not _fortel_eo_description(item.get("description"))]

        def _write_item(item, qty_formula=None):
            nonlocal row
            if item.get("line_role") == "concrete_slab" and item.get("specification_id"):
                # Where the priced rate for this construction ends up. The rate build-up block
                # is checked against it, so it has to be a row number, not an assumption.
                buildup_anchors[item["specification_id"]] = {
                    "rate_row": row, "area_row": total_area_row_holder[0]}
            description = (_fortel_eo_description(item.get("description"))
                           or item["description"])
            # The marker used to be appended into DESCRIPTION with a newline, so every
            # provisional row rendered as two lines and looked untidy beside Fortel's own
            # template. Their sheet keeps tender caveats in a column to the right of VALUE, so
            # the marker goes there: still impossible to miss, no longer wrapping the label.
            ws.cell(row, 1, _excel_text(description))
            if item.get("provisional"):
                # The reason is computed per group from the brief-spec provenance. Writing the
                # PROVISIONAL_LABEL constant here made the XLSX -- the one output Aryan reads
                # -- print "NO DETAILS PROVIDED" beside a thickness taken off his own legend,
                # while the portal and the HTML said what was actually extracted.
                ws.cell(row, PROVISIONAL_COL, _excel_text(
                    item.get("provisional_reason") or PROVISIONAL_LABEL))
            ws.cell(row, 2, qty_formula or float(item["qty"]))
            ws.cell(row, 3, _excel_text(_excel_unit(item["unit"])))
            rate = item.get("rate")
            if isinstance(rate, (int, float)):
                ws.cell(row, 4, float(rate))
            value_status = item.get("value_status")
            if value_status:
                ws.cell(row, 5, _excel_text(value_status))
            elif isinstance(rate, (int, float)):
                ws.cell(row, 5, f"=ROUND(B{row}*D{row},2)")
            elif item.get("assessor_rate_required"):
                ws.cell(row, 5, f'=IF(D{row}="","",ROUND(B{row}*D{row},2))')
            elif isinstance(item.get("value"), (int, float)):
                ws.cell(row, 5, float(item["value"]))
            ws.cell(row, 2).number_format = '#,##0.##'
            ws.cell(row, 4).number_format = currency_format
            ws.cell(row, 5).number_format = currency_format
            for col in range(1, 6):
                ws.cell(row, col).border = Border(bottom=thin_grey)
                ws.cell(row, col).alignment = Alignment(
                    horizontal="right" if col in (2, 4, 5) else "left",
                    vertical="top", wrap_text=col == 1)
                ws.cell(row, col).font = Font(name="Arial", size=8)
                if item.get("provisional"):
                    ws.cell(row, col).fill = provisional_fill
            if item.get("provisional"):
                ws.row_dimensions[row].height = 30
            row += 1

        # Where one reviewed zone fanned into several constructions, each keeps its own
        # take-off block above and they are summed once at the end of the section, exactly as
        # Aryan asked: "Total External/Service Yard Slab Area". Keyed by the zone the
        # constructions came from, so a section that never had any renders unchanged.
        construction_total_rows = {}
        total_area_row_holder = [None]
        for group_id in group_ids:
            specification = specifications.get(group_id) or {}
            source_rows = specification.get("area_rows") or []
            first_source_row = row
            for source_row in source_rows:
                ws.cell(row, 1, _excel_text(source_row.get("description") or "Measured area"))
                ws.cell(row, 2, float(source_row.get("qty") or 0))
                ws.cell(row, 3, _excel_unit(source_row.get("unit") or "m²"))
                ws.cell(row, 2).number_format = '#,##0.##'
                for col in range(1, 6):
                    ws.cell(row, col).font = Font(name="Arial", size=8)
                row += 1
            if source_rows:
                total_area_row = row
                ws.cell(row, 1, "Total Area Take Off:")
                ws.cell(row, 1).font = Font(name="Arial", size=8, bold=True)
                ws.cell(row, 2, f"=SUM(B{first_source_row}:B{row - 1})")
                ws.cell(row, 2).number_format = '#,##0.##'
                ws.cell(row, 3, "m2")
                row += 1
            else:
                total_area_row = None
            total_area_row_holder[0] = total_area_row

            display_lines = specification.get("display_lines") or []
            if display_lines:
                ws.cell(row, 1, _excel_text(
                    f"Specification — {specification.get('slab_type_label') or section}\n" +
                    "\n".join(
                        f"{field['label']}: {field['value']}"
                        + (f" (Source: {field['source_citation']})"
                           if field.get("source_citation") else "")
                        for field in display_lines
                    )
                ))
                ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
                ws.cell(row, 1).font = Font(name="Arial", size=8,
                                            italic=bool(specification.get("provisional")),
                                            color=red if specification.get("provisional") else black)
                if specification.get("provisional"):
                    for col in range(1, 6):
                        ws.cell(row, col).fill = provisional_fill
                ws.row_dimensions[row].height = max(30, 12 * (len(display_lines) + 1))
                row += 1

            if total_area_row and specification.get("construction_of"):
                construction_total_rows.setdefault(
                    specification["construction_of"], []).append(total_area_row)

            qty_formula = f"=B{total_area_row}" if total_area_row else None
            for item in section_items:
                if item.get("specification_id") == group_id:
                    _write_item(item, qty_formula=qty_formula)

        # The combined figure sums the constructions' OWN take-off totals rather than
        # restating the zone's stored area: each construction is rounded to 0.1 m2
        # independently upstream, so the zone figure can differ from their sum by 0.1 and the
        # sheet would then contradict the rows printed directly above it.
        for source_rows_added in construction_total_rows.values():
            if len(source_rows_added) < 2:
                continue
            ws.cell(row, 1, _excel_text(f"Total {_section_area_total_label(section)}:"))
            ws.cell(row, 1).font = Font(name="Arial", size=8, bold=True)
            ws.cell(row, 2, "=" + "+".join(f"B{r}" for r in source_rows_added))
            ws.cell(row, 2).number_format = '#,##0.##'
            ws.cell(row, 3, "m2")
            for col in range(1, 6):
                ws.cell(row, col).font = Font(name="Arial", size=8, bold=col == 1)
                ws.cell(row, col).border = Border(bottom=thin_grey)
            row += 1

        for item in ungrouped:
            _write_item(item)

        def _write_measurement(measurement, *, show_sources=True):
            nonlocal row
            quantity_rows = ((measurement.get("quantity_rows") or [])
                             if show_sources else [])
            first_quantity_row = row
            for quantity_row in quantity_rows:
                ws.cell(row, 1, _excel_text(quantity_row.get("description") or "Measured length"))
                ws.cell(row, 2, float(quantity_row.get("qty") or 0))
                ws.cell(row, 3, _excel_unit(quantity_row.get("unit") or "Lm"))
                ws.cell(row, 2).number_format = '#,##0.##'
                for col in range(1, 6):
                    ws.cell(row, col).font = Font(name="Arial", size=8)
                row += 1
            quantity_formula = None
            if quantity_rows:
                total_quantity_row = row
                ws.cell(row, 1, f"Total {measurement['description']}:")
                ws.cell(row, 1).font = Font(name="Arial", size=8, bold=True)
                ws.cell(row, 2, f"=SUM(B{first_quantity_row}:B{row - 1})")
                ws.cell(row, 2).number_format = '#,##0.##'
                ws.cell(row, 3, _excel_unit(measurement.get("unit") or "Lm"))
                row += 1
                quantity_formula = f"=B{total_quantity_row}"
            _write_item({
                "description": (_fortel_eo_description(measurement["description"])
                                or measurement["description"]),
                "qty": measurement["qty"],
                "unit": measurement["unit"], "rate": None, "value": None,
                "provisional": measurement.get("provisional", False),
                "assessor_rate_required": True,
            }, qty_formula=quantity_formula)

        section_measurements = [m for m in assessor_measurements
                                if m["section"] == section]
        for measurement in section_measurements:
            if not _fortel_eo_description(measurement.get("description")):
                _write_measurement(measurement)

        # The supplied Fortel sheet places its extra-over rows after the slab/perimeter rows,
        # in MH -> Channel -> Transition order.  Legacy descriptions are normalised here so
        # old saved jobs also export into the current standard layout.
        special_entries = [
            (_FORTEL_EO_ORDER[_fortel_eo_description(item["description"])], index,
             "item", item)
            for index, item in enumerate(special_ungrouped)
        ]
        special_entries.extend(
            (_FORTEL_EO_ORDER[_fortel_eo_description(measurement["description"])],
             len(special_entries) + index, "measurement", measurement)
            for index, measurement in enumerate(section_measurements)
            if _fortel_eo_description(measurement.get("description"))
        )
        for _rank, _index, kind, record in sorted(special_entries):
            if kind == "item":
                _write_item(record)
            else:
                _write_measurement(record, show_sources=False)

        if section == "Dock slabs":
            ws.cell(row, 1, "Foundation thickenings directly underneath Dock Slab region")
            ws.cell(row, 1).font = Font(name="Arial", size=8, bold=True)
            ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
            row += 1

        row += 2

    total_row = row
    ws.cell(total_row, 1, "TOTAL NETT")
    ws.cell(total_row, 1).font = Font(name="Arial", bold=True, size=9)
    ws.cell(total_row, 5, f"=SUM(E7:E{total_row - 1})")
    ws.cell(total_row, 5).number_format = currency_format
    ws.cell(total_row, 5).font = Font(name="Arial", bold=True, size=9)

    row = total_row + 3
    measurements = [m for m in (q.get("measurements") or [])
                    if not m.get("assessor_rate_required")]
    if measurements:
        ws.cell(row, 1, "INFORMATIONAL MEASUREMENTS — NOT PRICED")
        for col in range(1, 6):
            ws.cell(row, col).font = Font(name="Arial", bold=True, color=white, size=9)
            ws.cell(row, col).fill = section_fill
        row += 1
        for measurement in measurements:
            description = f"{_excel_section_title(measurement['section'], [])} — {measurement['description']}"
            ws.cell(row, 1, description)
            if measurement.get("provisional"):
                ws.cell(row, PROVISIONAL_COL, _excel_text(
                    measurement.get("provisional_reason") or PROVISIONAL_LABEL))
            ws.cell(row, 2, float(measurement["qty"]))
            ws.cell(row, 3, _excel_unit(measurement["unit"]))
            ws.cell(row, 2).number_format = '#,##0.##'
            if measurement.get("provisional"):
                for col in range(1, 6):
                    ws.cell(row, col).fill = provisional_fill
                ws.row_dimensions[row].height = 30
            row += 1
        row += 1

    if q.get("declarations"):
        ws.cell(row, 1, "NOTES / ASSUMPTIONS")
        for col in range(1, 6):
            ws.cell(row, col).font = Font(name="Arial", bold=True, color=white, size=9)
            ws.cell(row, col).fill = section_fill
        row += 1
        for note in q["declarations"]:
            ws.cell(row, 1, f"• {note}")
            ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(row, 1).font = Font(name="Arial", size=8)
            ws.row_dimensions[row].height = 30
            row += 1

    ws.cell(row + 1, 1, f"STANDARD TERMS: {q['terms']}")
    ws.cell(row + 1, 1).font = Font(name="Arial", size=8)
    ws.cell(row + 1, 1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row + 1].height = 45
    row += 2
    for term in q.get("commercial_terms") or []:
        ws.cell(row, 1, _excel_text(term))
        ws.cell(row, 1).font = Font(name="Arial", size=8)
        ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
        row += 1

    widths = {"A": 82.43, "B": 19.43, "C": 10.29, "D": 12.71, "E": 21.14,
              "G": 22, "H": 14, "I": 18, "J": 12, "L": 30, "M": 12, "N": 22, "O": 14}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.print_title_rows = "1:7"
    ws.print_area = f"A1:O{row}"
    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.oddFooter.center.text = f"{FORTEL_NAME} · {FORTEL_EMAIL} · {FORTEL_TEL}"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    wb.calculation.calcMode = "auto"

    # ── Rate buildup section (columns G onwards) — mirrors the real Fortel costing sheet's
    #    detailed cost breakdown that sits to the right of the BOQ columns.
    _write_rate_buildup(ws, q, buildup_anchors)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def save_quotation(q: dict, out_dir: str = ".", file_stem: str | None = None) -> dict:
    """Save text, HTML, JSON and editable Excel versions to disk. Returns paths dict."""
    base = Path(out_dir) / (file_stem or q["ref"])
    paths = {}
    base.parent.mkdir(parents=True, exist_ok=True)
    (p := Path(f"{base}.txt")).write_text(quotation_text(q));  paths["txt"]  = str(p)
    (p := Path(f"{base}.html")).write_text(quotation_html(q)); paths["html"] = str(p)
    (p := Path(f"{base}.json")).write_text(quotation_json(q)); paths["json"] = str(p)
    (p := Path(f"{base}.xlsx")).write_bytes(quotation_xlsx(q)); paths["xlsx"] = str(p)
    return paths


# ── Standalone demo ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, io, contextlib

    # Simulate a pipeline result (as returned by takeoff_pipeline.takeoff)
    demo_result = {
        "file":             "SGP-D77-Hard-Landscaping.pdf",
        "type":             "UNMARKED vector",
        "confidence":       "medium",
        "source_discipline":"architect",
        "area_m2":          3172,
        "costing": {
            "area_m2":   3172,
            "rate":      44.89,
            "total_gbp": 142_391.08,
            "assumed":   True,
            "spec": {
                "depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40",
                "layers": 1, "conc_rate": 128,
            },
            "breakdown": {
                "concrete": 25.05, "steel": 4.30, "dpm": 0.46,
                "curing": 0.23, "labour": 10.00, "trim": 0.40,
                "nett": 40.44, "margin%": 11,
            },
        },
        "flags": [
            "BUILD-UP ASSUMED: 190mm / A252 / C32/40 — no engineer construction-detail found",
            "ARCHITECT drawing — build-up ASSUMED; no construction-detail sheet found.",
            "assessor: confirm extent + scale",
        ],
    }

    extras = [
        ("Manholes (inside concrete boundary)", 3, "Nr",  75.00),
        ("Slot drain channels (linear metres)", 48, "Lm", 17.50),
    ]

    q = generate_quotation(demo_result, project="Hemington D77 Hard Landscaping",
                           client="Winvic Construction", ref="FTL-2026-D77A",
                           extras=extras)

    print("── Plain text quotation ─────────────────────────────────────────────")
    print(quotation_text(q))
    print()

    paths = save_quotation(q, out_dir="quotations")
    print(f"Saved: {paths}")

    # Quick validation
    assert q["total_gbp"] > 0, "total must be positive"
    assert q["assumed"] is True, "should flag assumed build-up"
    assert any("ASSUMED" in d for d in q["declarations"]), "must declare assumption"
    assert len(q["line_items"]) >= 3, "at least slab + trim + joints"
    print("\nAll assertions passed ✅")
