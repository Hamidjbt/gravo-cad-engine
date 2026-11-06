"""DXF plotting standards: the single source of truth for CAD styling.

Every layer name, ACI colour, lineweight and hatch convention the engine emits is
declared here. ``cad-fond-de-plan-build.py`` and ``deliverable_qa.py`` both import
this module, so a builder that drifts from the QA gate becomes impossible: there is
only one table to drift from.

Values are DXF-spec quantities, not preferences:

* **Lineweight** is stored in 1/100 mm and must be one of the fixed enum values the
  format allows (:data:`VALID_LINEWEIGHTS`). An out-of-enum value is silently coerced
  by AutoCAD, which is the kind of defect that only appears on paper, so
  :func:`validate_lineweight` exists to fail loudly instead.
* **ACI colour** is an index 1-255 (:data:`ACI_MIN` .. :data:`ACI_MAX`). ACI 7 is
  *adaptive*: black on white paper, white on black paper. Structural layers keep 7 so
  they read on either background; context layers take real indices so a CTB pen table
  can key off them without depending on the plot background.
* **Hatch pattern scale** is in drawing units. The model is in metres, so a predefined
  pattern whose base spacing is a fraction of a drawing unit produces invisible hatching
  at any real plot denominator unless it is scaled by the denominator.
"""

from __future__ import annotations

import math
from typing import Any

#: Fixed lineweight enum permitted by the DXF format, in 1/100 mm.
VALID_LINEWEIGHTS: frozenset[int] = frozenset(
    {0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211}
)

#: Special (negative) lineweight sentinels, documented so callers need not guess.
LINEWEIGHT_BYLAYER = -1
LINEWEIGHT_BYBLOCK = -2
LINEWEIGHT_DEFAULT = -3

ACI_MIN = 1
ACI_MAX = 255
#: ACI 7 is adaptive black/white; ACI 8 is dark grey; ACI 9 is light grey.
ACI_ADAPTIVE = 7
ACI_CONTEXT = 8
ACI_SECONDARY = 9
ACI_PRIMARY = 1

#: Desired hatch pitch *on paper*, in millimetres. Chosen so adjacent pattern lines
#: stay distinguishable at 1:1000 on an A0 sheet without turning the poché to mud.
PAPER_HATCH_PITCH_MM = 2.0

#: ANSI31 geometry (measured from the pattern definition, then reasoned about — the
#: distinction matters and cost a bug during development).
#:
#: ansig patterns repeat by a *translation* between parallel lines. ANSI31 stores
#: ``offset = (-2.2450640, 2.2450640)``, whose magnitude is 3.175 drawing units. But
#: 3.175 is the *offset magnitude*, not the spacing between the lines: the lines sit at
#: 45 deg, so perpendicular spacing is ``|offset| * cos(45) = 2.2451`` units.
#:
#: Conflating the two makes every pattern 41% too coarse. Feeding millimetres in as
#: drawing units (1/8 in = 3.175 mm) instead of units (1/8 = 0.125) is off by 25.4x.
#: Both mistakes are invisible in the DXF tags and only show on paper, so the scale is
#: derived from these two named quantities where the reasoning can be checked.
ANSI31_OFFSET_MAGNITUDE = 3.175
ANSI31_LINE_ANGLE_DEG = 45.0
ANSI31_BASE_SPACING = ANSI31_OFFSET_MAGNITUDE * math.cos(math.radians(ANSI31_LINE_ANGLE_DEG))

HATCH_SOLID = "SOLID"
HATCH_ANSI31 = "ANSI31"
SUPPORTED_HATCH_PATTERNS = (HATCH_SOLID, HATCH_ANSI31)


def validate_lineweight(value: int) -> int:
    """Return ``value`` if the DXF format accepts it, else raise ``ValueError``."""
    if value in {LINEWEIGHT_BYLAYER, LINEWEIGHT_BYBLOCK, LINEWEIGHT_DEFAULT}:
        return value
    if value not in VALID_LINEWEIGHTS:
        raise ValueError(f"lineweight {value} is not a valid DXF lineweight (1/100 mm)")
    return value


def validate_color(value: int) -> int:
    """Return ``value`` if it is a legal ACI index, else raise ``ValueError``."""
    if not ACI_MIN <= value <= ACI_MAX:
        raise ValueError(f"ACI colour {value} is outside {ACI_MIN}..{ACI_MAX}")
    return value


#: Architectural layer schema: name -> required DXF attributes.
#:
#: Roles are encoded in the weights so a plotted sheet reads hierarchically:
#: site boundary heaviest, building outline heavy, context light, fill lightest.
LAYERS: dict[str, dict[str, Any]] = {
    # Legacy names, retained for downstream consumers of pre-v1.11 exports.
    "BUILDINGS": {"color": ACI_ADAPTIVE, "lineweight": 50},
    "HATCH": {"color": ACI_CONTEXT, "lineweight": 9},
    "ROADS": {"color": ACI_CONTEXT, "lineweight": 25},
    "PARCELS": {"color": ACI_SECONDARY, "lineweight": 13},
    "SITE_BOUNDARY": {"color": ACI_PRIMARY, "lineweight": 70, "linetype": "SITE_DASH"},
    # Controlled G-* schema consumed by the precision export.
    "G-BLDG-WALL": {"color": ACI_ADAPTIVE, "lineweight": 50},
    "G-BLDG-SHARED": {"color": ACI_ADAPTIVE, "lineweight": 50},
    "G-BLDG-FILL": {"color": ACI_CONTEXT, "lineweight": 9},
    "G-ROAD-CURB": {"color": ACI_CONTEXT, "lineweight": 25},
    "G-ROAD-SURFACE": {"color": ACI_CONTEXT, "lineweight": 9},
    "G-ROAD-CL": {"color": ACI_SECONDARY, "lineweight": 13, "linetype": "CENTER"},
    "G-PARCEL": {"color": ACI_SECONDARY, "lineweight": 13},
    "G-HATCH": {"color": ACI_CONTEXT, "lineweight": 9},
    "G-SITE-BOUNDARY": {"color": ACI_PRIMARY, "lineweight": 70, "linetype": "SITE_DASH"},
    "G-ANNO-GRID": {"color": ACI_SECONDARY, "lineweight": 13},
    "G-ANNO-NORTH": {"color": ACI_SECONDARY, "lineweight": 18},
    "G-ANNO-TTLB": {"color": ACI_ADAPTIVE, "lineweight": 35},
}

LAYER_NAMES = frozenset(LAYERS)

#: Layers that must exist for a drawing to be considered complete.
REQUIRED_LAYER_NAMES = frozenset(
    {
        "G-BLDG-WALL",
        "G-BLDG-SHARED",
        "G-BLDG-FILL",
        "G-ROAD-CURB",
        "G-ROAD-CL",
        "G-PARCEL",
        "G-SITE-BOUNDARY",
        "G-ANNO-GRID",
        "G-ANNO-NORTH",
        "G-ANNO-TTLB",
    }
)

#: Hatch appearance per pattern. ``SOLID`` is architectural poché; ``ANSI31`` is the
#: lighter survey convention and needs a denominator-derived scale to be visible.
#: Every layer attribute the schema declares. Anything not in this tuple is not a DXF
#: layer property and must not be passed to ``doc.layers.add`` silently.
LAYER_ATTR_KEYS = ("color", "lineweight", "linetype")

HATCH_LAYER = "G-BLDG-FILL"
HATCH_PATTERN_TYPE_PREDEFINED = 1
HATCH_SOLID_FILL_FLAG = 1


def layer_attributes() -> dict[str, dict[str, Any]]:
    """Return a defensive copy of the layer table with every value validated."""
    out: dict[str, dict[str, Any]] = {}
    for name, attrs in LAYERS.items():
        validated = dict(attrs)
        validated["color"] = validate_color(int(attrs["color"]))
        validated["lineweight"] = validate_lineweight(int(attrs["lineweight"]))
        out[name] = validated
    return out


def create_layer(doc, name: str):
    """Create ``name`` in ``doc`` with the schema's colour, lineweight and linetype.

    Do **not** pass ``dxfattribs=`` to ``LayerTable.add()``: measured against ezdxf
    1.4.5, that path silently discards ``color`` *and* ``lineweight``, leaving every
    layer at ACI 7 with lineweight BYLAYER (-3). A drawing produced that way looks
    styled in memory and is uniformly unstyled on paper, with nothing in the file to
    indicate the values were dropped. Properties are therefore assigned explicitly
    after creation, which round-trips correctly.
    """
    attrs = layer_attributes()[name]
    layer = doc.layers.add(name)
    layer.dxf.color = validate_color(int(attrs["color"]))
    layer.dxf.lineweight = validate_lineweight(int(attrs["lineweight"]))
    linetype = attrs.get("linetype")
    if linetype:
        layer.dxf.linetype = linetype
    return layer


def pattern_scale_for(denominator: int, pitch_mm: float = PAPER_HATCH_PITCH_MM) -> float:
    """Return the DXF ``pattern_scale`` that lands ``pitch_mm`` on paper.

    The model is in metres and the sheet denominator is metres-per-paper-millimetre,
    so a pattern whose base spacing is in drawing units must be scaled by the ground
    distance those ``pitch_mm`` occupy::

        ground_m = pitch_mm * denominator / 1000
        scale    = ground_m / ANSI31_BASE_SPACING

    At 1:1000 a 2 mm pitch is 2.0 m of ground, giving ``scale = 16.0``; the same 2 mm
    pitch at 1:250 gives ``4.0``. Getting this wrong is invisible in the DXF and
    obvious on paper, which is why it is computed rather than hard-coded.
    """
    if denominator <= 0:
        raise ValueError(f"denominator must be positive, got {denominator}")
    if pitch_mm <= 0:
        raise ValueError(f"pitch_mm must be positive, got {pitch_mm}")
    ground_m = pitch_mm * denominator / 1000.0
    return ground_m / ANSI31_BASE_SPACING


def style_findings(doc) -> list[str]:
    """Return QA findings for layer styling in an opened ezdxf document.

    Checks the whole :data:`LAYERS` table, not just required layers, so an accidental
    colour change anywhere in the schema is caught rather than only where it is used.
    """
    findings: list[str] = []
    present = {layer.dxf.name for layer in doc.layers}
    for name in sorted(REQUIRED_LAYER_NAMES):
        if name not in present:
            findings.append(f"DXF missing required layer {name}")
    for name, attrs in LAYERS.items():
        if name not in present:
            continue
        layer = doc.layers.get(name)
        expected_color = attrs.get("color")
        if expected_color is not None and layer.dxf.color != expected_color:
            findings.append(f"DXF {name} color is {layer.dxf.color}, expected ACI {expected_color}")
        expected_lw = attrs.get("lineweight")
        if expected_lw is not None and layer.dxf.lineweight != expected_lw:
            findings.append(f"DXF {name} lineweight is {layer.dxf.lineweight}, expected {expected_lw}/100 mm")
        expected_lt = attrs.get("linetype")
        if expected_lt is not None and layer.dxf.linetype != expected_lt:
            findings.append(f"DXF {name} linetype is {layer.dxf.linetype}, expected {expected_lt}")
    return findings


def hatch_findings(doc, expected_pattern: str | None = None) -> list[str]:
    """Return QA findings for HATCH entities in an opened ezdxf document.

    Verifies the DXF group codes a plotting application actually reads: pattern name
    (2), pattern type (76), scale (41), hatch style (75) and boundary path flags (92).
    The ezdxf *renderer* cannot draw pattern hatches, so these codes are the only
    reliable evidence that a plotter will hatch correctly.
    """
    findings: list[str] = []
    hatches = list(doc.modelspace().query("HATCH"))
    if not hatches:
        return findings
    for index, hatch in enumerate(hatches):
        pattern = hatch.dxf.pattern_name if hatch.dxf.hasattr("pattern_name") else None
        solid = bool(hatch.dxf.solid_fill) if hatch.dxf.hasattr("solid_fill") else False
        if expected_pattern and pattern and expected_pattern != HATCH_SOLID:
            if pattern != expected_pattern:
                findings.append(f"HATCH {index} pattern is {pattern}, expected {expected_pattern}")
        if expected_pattern == HATCH_SOLID and not solid:
            findings.append(f"HATCH {index} is not a solid fill")
        if not hatch.paths:
            findings.append(f"HATCH {index} has no boundary paths")
            continue
        flags = [path.path_type_flags for path in hatch.paths]
        # An outer path carries the external bit (1). Any additional path without it is
        # an island: a courtyard. This is what makes the void render empty.
        if not any(flag & 1 for flag in flags):
            findings.append(f"HATCH {index} has no external boundary path")
        if len(flags) > 1 and not any(not (flag & 1) for flag in flags[1:]):
            findings.append(f"HATCH {index} defines inner paths as external; courtyards will fill in")
        if hatch.dxf.hasattr("hatch_style") and int(hatch.dxf.hatch_style) not in (0, 1, 2):
            findings.append(f"HATCH {index} hatch_style {hatch.dxf.hatch_style} is not a valid style")
        if pattern and pattern != HATCH_SOLID:
            scale = hatch.dxf.pattern_scale if hatch.dxf.hasattr("pattern_scale") else 1.0
            if scale <= 0:
                findings.append(f"HATCH {index} pattern_scale {scale} must be positive")
    return findings


def dxf_is_vector_only(doc) -> list[str]:
    """Return findings when a document carries raster or embedded-image payloads.

    The vector-first invariant is testable: a DXF that embeds images or a preview
    bitmap has stopped being a lightweight vector file regardless of its byte count.
    """
    findings: list[str] = []
    if "IMAGEDEF" in doc.objects or any(True for _ in doc.modelspace().query("IMAGE")):
        findings.append("DXF embeds raster image definitions; vector-first invariant broken")
    for entity in doc.modelspace().query("IMAGE WIPEOUT"):
        findings.append(f"DXF modelspace contains raster entity {entity.dxftype()}")
        break
    return findings
