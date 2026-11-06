"""Read a footprint of a KiCad library with pcbnew of the installed KiCad.

KiCad itself reads the footprint (``FootprintLoad``), thus the result is the
footprint as KiCad has it: the pads with their padstacks, the fields, the 3D
models, and the courtyard polygon of the DRC (``BuildCourtyardCaches``). The
script runs in KiCad 8, 9 and 10.

The SWIG API of pcbnew does not give the net tie pad groups, the jumper pad
groups and the embedded files. For these items the script gives the text that
the writer of KiCad makes from the footprint (``PCB_IO_KICAD_SEXPR::Format``).
This text has the attributes, the private layers and these lists in the
format of the installed KiCad.

When the footprint is not in the library or KiCad cannot read it,
``FootprintLoad`` gives no footprint and no reason. The other pcbnew functions
that read a footprint stop the process when the file has an error: their
exception handlers in the SWIG files of KiCad 8, 9 and 10 name the class
``PLUGIN``, which KiCad 8 renamed to ``PCB_IO``.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.board_models import embedded_files
from ..parsers.sexpr import Node, SExprError, parse
from .kicad_python import decode_python_output, result_line, run_kicad_python


class FootprintReadError(WorkbenchError):
    """pcbnew cannot be started, or the script did not give a result."""


# argv: the library directory, the footprint name. The script prints one line
# with the prefix, then a JSON object with the KiCad version, and with no other
# key when KiCad does not load the footprint. All lengths are in nanometers
# (the internal unit of pcbnew), the angles are in degrees.
_PREFIX = "FOOTPRINT_JSON="
_SCRIPT = """\
import json
import sys

import pcbnew

library, name = sys.argv[1:3]
footprint = pcbnew.FootprintLoad(library, name)
if footprint is None:
    print("FOOTPRINT_JSON=" + json.dumps({"kicad": pcbnew.GetMajorMinorPatchVersion()}))
    sys.exit(0)
# KiCad 8 builds the courtyard only for a footprint of a board.
board = pcbnew.BOARD()
board.Add(footprint)
# KiCad 9 and later: a pad has a padstack, with a shape for each copper layer.
# The shape of all layers is the shape of F.Cu (PADSTACK::ALL_LAYERS).
padstacks = hasattr(pcbnew, "PADSTACK")
ALL = (pcbnew.F_Cu,) if padstacks else ()
COPPER = len(pcbnew.LSET.AllCuMask().Seq())


def names(prefix, items):
    return {getattr(pcbnew, prefix + i): i.lower() for i in items if hasattr(pcbnew, prefix + i)}


SHAPES = names(
    "PAD_SHAPE_", ["CIRCLE", "RECTANGLE", "OVAL", "TRAPEZOID", "ROUNDRECT", "CHAMFERED_RECT", "CUSTOM"]
)
TYPES = names("PAD_ATTRIB_", ["PTH", "SMD", "CONN", "NPTH"])
DRILLS = names("PAD_DRILL_SHAPE_", ["CIRCLE", "OBLONG"])
ZONES = names("ZONE_CONNECTION_", ["INHERITED", "NONE", "THERMAL", "FULL", "THT_THERMAL"])
MODES = {}
if padstacks:
    MODES = {
        pcbnew.PADSTACK.MODE_NORMAL: "normal",
        pcbnew.PADSTACK.MODE_FRONT_INNER_BACK: "front_inner_back",
        pcbnew.PADSTACK.MODE_CUSTOM: "custom",
    }


def xy(vector):
    return [vector.x, vector.y]


def box(rect):
    return [rect.GetX(), rect.GetY(), rect.GetRight(), rect.GetBottom()]


def optional(value):
    # KiCad 8 gives 0 for a value that is not set, later versions None.
    return None if value is None or (not padstacks and value == 0) else value


def zone(item):
    get = getattr(item, "GetLocalZoneConnection", None) or item.GetZoneConnection
    return ZONES.get(get(), str(get()))


def layers(layer_set):
    ids = list(layer_set.Seq())
    copper = [layer for layer in ids if pcbnew.IsCopperLayer(layer)]
    other = [pcbnew.LayerName(layer) for layer in ids if not pcbnew.IsCopperLayer(layer)]
    if len(copper) == COPPER:
        return ["*.Cu"] + other
    return [pcbnew.LayerName(layer) for layer in copper] + other


def shape(pad, layer):
    value = pad.GetShape(*layer)
    return {
        "shape": SHAPES.get(value, str(value)),
        "size": xy(pad.GetSize(*layer)),
        "offset": xy(pad.GetOffset(*layer)),
        "roundrect_ratio": pad.GetRoundRectRadiusRatio(*layer),
        "chamfer_ratio": pad.GetChamferRectRatio(*layer),
        "chamfer_positions": pad.GetChamferPositions(*layer),
        "delta": xy(pad.GetDelta(*layer)),
        "anchor_shape": SHAPES.get(pad.GetAnchorPadShape(*layer), ""),
    }


def layer_shapes(pad):
    # The copper layers of a padstack with more than one shape, grouped by the
    # layer that holds their shape (PADSTACK::EffectiveLayerFor).
    if not padstacks or pad.Padstack().Mode() == pcbnew.PADSTACK.MODE_NORMAL:
        return []
    groups = {}
    for layer in pad.GetLayerSet().Seq():
        if pcbnew.IsCopperLayer(layer):
            effective = pad.Padstack().EffectiveLayerFor(layer)
            groups.setdefault(effective, []).append(pcbnew.LayerName(layer))
    return [dict(shape(pad, (layer,)), layers=group) for layer, group in groups.items()]


def pad_data(pad):
    data = shape(pad, ALL)
    data.update(
        number=pad.GetNumber(),
        type=TYPES.get(pad.GetAttribute(), str(pad.GetAttribute())),
        position=xy(pad.GetFPRelativePosition()),
        orientation=pad.GetFPRelativeOrientation().AsDegrees(),
        drill=xy(pad.GetDrillSize()),
        drill_shape=DRILLS.get(pad.GetDrillShape(), str(pad.GetDrillShape())),
        layers=layers(pad.GetLayerSet()),
        on_copper=pad.IsOnCopperLayer(),
        pin_function=pad.GetPinFunction(),
        pin_type=pad.GetPinType(),
        clearance=optional(pad.GetLocalClearance()),
        solder_mask_margin=optional(pad.GetLocalSolderMaskMargin()),
        solder_paste_margin=optional(pad.GetLocalSolderPasteMargin()),
        solder_paste_ratio=optional(pad.GetLocalSolderPasteMarginRatio()),
        zone_connection=zone(pad),
        padstack=MODES.get(pad.Padstack().Mode(), str(pad.Padstack().Mode()))
        if padstacks
        else "normal",
        layer_shapes=layer_shapes(pad),
    )
    return data


footprint.BuildCourtyardCaches()
courtyard = []
for layer, flag in (
    (pcbnew.F_CrtYd, pcbnew.MALFORMED_F_COURTYARD),
    (pcbnew.B_CrtYd, pcbnew.MALFORMED_B_COURTYARD),
):
    polygon = footprint.GetCourtyard(layer)
    count = polygon.OutlineCount()
    courtyard.append(
        {
            "layer": pcbnew.LayerName(layer),
            "outlines": count,
            "box": box(polygon.BBox()) if count else None,
            "area": polygon.Area() if count else 0.0,
            "malformed": bool(footprint.GetFlags() & flag),
        }
    )

writer = pcbnew.PCB_IO_KICAD_SEXPR()
writer.Format(footprint)
result = {
    "kicad": pcbnew.GetMajorMinorPatchVersion(),
    "description": footprint.GetLibDescription(),
    "keywords": footprint.GetKeywords(),
    "fields": [
        {"name": f.GetName(), "text": f.GetText(), "visible": f.IsVisible(), "layer": f.GetLayerName()}
        for f in footprint.GetFields()
    ],
    "clearance": optional(footprint.GetLocalClearance()),
    "solder_mask_margin": optional(footprint.GetLocalSolderMaskMargin()),
    "solder_paste_margin": optional(footprint.GetLocalSolderPasteMargin()),
    "solder_paste_ratio": optional(footprint.GetLocalSolderPasteMarginRatio()),
    "zone_connection": zone(footprint),
    "box": box(footprint.GetBoundingBox(False, False)),
    "courtyard": courtyard,
    "pads": [pad_data(pad) for pad in footprint.Pads()],
    "models": [
        {
            "file": m.m_Filename,
            "shown": m.m_Show,
            "opacity": m.m_Opacity,
            "offset": [m.m_Offset.x, m.m_Offset.y, m.m_Offset.z],
            "scale": [m.m_Scale.x, m.m_Scale.y, m.m_Scale.z],
            "rotation": [m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z],
        }
        for m in footprint.Models()
    ],
    "text": writer.GetStringOutput(True),
}
print("FOOTPRINT_JSON=" + json.dumps(result))
"""


@dataclass(frozen=True)
class PadShape:
    """The shape of a pad on one or more copper layers."""

    shape: str  # circle, rectangle, oval, trapezoid, roundrect, chamfered_rect, custom
    size: tuple[int, int]
    offset: tuple[int, int]
    roundrect_ratio: float
    chamfer_ratio: float
    chamfer_positions: int  # Bits: 1 top left, 2 top right, 4 bottom left, 8 bottom right
    delta: tuple[int, int]  # Of a trapezoid
    anchor_shape: str  # Of a custom pad
    layers: tuple[str, ...] = ()  # The copper layers of a padstack shape


@dataclass(frozen=True)
class FootprintPad:
    number: str
    type: str  # pth, smd, conn, npth
    shape: PadShape
    position: tuple[int, int]
    orientation: float
    drill: tuple[int, int]
    drill_shape: str  # circle, oblong
    layers: tuple[str, ...]
    # PAD::IsOnCopperLayer. "Update PCB from Schematic" gives a net only to a
    # pad on a copper layer.
    on_copper: bool
    pin_function: str
    pin_type: str
    clearance: int | None
    solder_mask_margin: int | None
    solder_paste_margin: int | None
    solder_paste_ratio: float | None
    zone_connection: str
    padstack: str  # normal, front_inner_back, custom
    layer_shapes: tuple[PadShape, ...]


@dataclass(frozen=True)
class FootprintField:
    name: str
    text: str
    visible: bool
    layer: str


@dataclass(frozen=True)
class Courtyard:
    layer: str
    outlines: int
    box: tuple[int, int, int, int] | None
    area: float  # nm²
    malformed: bool


@dataclass(frozen=True)
class FootprintModel:
    file: str
    shown: bool
    opacity: float
    offset: tuple[float, float, float]  # mm
    scale: tuple[float, float, float]
    rotation: tuple[float, float, float]  # degrees


@dataclass(frozen=True)
class FootprintInfo:
    """A footprint of a library, as the installed KiCad reads it."""

    kicad: str  # The version of pcbnew, for example "10.0.6"
    description: str
    keywords: str
    attributes: tuple[str, ...]  # The tokens of (attr ...), for example "smd", "dnp"
    private_layers: tuple[str, ...]
    net_tie_pad_groups: tuple[str, ...]
    duplicate_pad_numbers_are_jumpers: bool
    jumper_pad_groups: tuple[tuple[str, ...], ...]
    embedded_files: tuple[tuple[str, str], ...]  # (name, type)
    clearance: int | None
    solder_mask_margin: int | None
    solder_paste_margin: int | None
    solder_paste_ratio: float | None
    zone_connection: str
    box: tuple[int, int, int, int]  # All items with no text
    courtyard: tuple[Courtyard, ...]
    fields: tuple[FootprintField, ...]
    pads: tuple[FootprintPad, ...]
    models: tuple[FootprintModel, ...]


def _pad_shape(data: dict) -> PadShape:
    return PadShape(
        shape=data["shape"],
        size=tuple(data["size"]),
        offset=tuple(data["offset"]),
        roundrect_ratio=data["roundrect_ratio"],
        chamfer_ratio=data["chamfer_ratio"],
        chamfer_positions=data["chamfer_positions"],
        delta=tuple(data["delta"]),
        anchor_shape=data["anchor_shape"],
        layers=tuple(data.get("layers", ())),
    )


def _pad(data: dict) -> FootprintPad:
    return FootprintPad(
        number=data["number"],
        type=data["type"],
        shape=_pad_shape(data),
        position=tuple(data["position"]),
        orientation=data["orientation"],
        drill=tuple(data["drill"]),
        drill_shape=data["drill_shape"],
        layers=tuple(data["layers"]),
        on_copper=data["on_copper"],
        pin_function=data["pin_function"],
        pin_type=data["pin_type"],
        clearance=data["clearance"],
        solder_mask_margin=data["solder_mask_margin"],
        solder_paste_margin=data["solder_paste_margin"],
        solder_paste_ratio=data["solder_paste_ratio"],
        zone_connection=data["zone_connection"],
        padstack=data["padstack"],
        layer_shapes=tuple(_pad_shape(shape) for shape in data["layer_shapes"]),
    )


def _values(node: Node | None) -> tuple[str, ...]:
    return tuple(atom.value for atom in node.atoms()) if node is not None else ()


def footprint_info(data: dict) -> FootprintInfo:
    """Make the FootprintInfo from the JSON of the script.

    Raises:
        FootprintReadError: The text of the KiCad writer cannot be parsed.
    """
    try:
        root = parse(data["text"])
    except SExprError as e:
        raise FootprintReadError(f"The text that KiCad wrote cannot be parsed: {e}") from e
    jumpers = root.first("duplicate_pad_numbers_are_jumpers")
    groups = root.first("jumper_pad_groups")
    return FootprintInfo(
        kicad=data["kicad"],
        description=data["description"],
        keywords=data["keywords"],
        attributes=_values(root.first("attr")),
        private_layers=_values(root.first("private_layers")),
        net_tie_pad_groups=_values(root.first("net_tie_pad_groups")),
        duplicate_pad_numbers_are_jumpers=jumpers is not None and jumpers.value(0) == "yes",
        jumper_pad_groups=tuple(_values(group) for group in groups.children())
        if groups is not None
        else (),
        embedded_files=embedded_files(root),
        clearance=data["clearance"],
        solder_mask_margin=data["solder_mask_margin"],
        solder_paste_margin=data["solder_paste_margin"],
        solder_paste_ratio=data["solder_paste_ratio"],
        zone_connection=data["zone_connection"],
        box=tuple(data["box"]),
        courtyard=tuple(
            Courtyard(**{**c, "box": tuple(c["box"]) if c["box"] else None})
            for c in data["courtyard"]
        ),
        fields=tuple(FootprintField(**field) for field in data["fields"]),
        pads=tuple(_pad(pad) for pad in data["pads"]),
        models=tuple(
            FootprintModel(
                file=m["file"],
                shown=m["shown"],
                opacity=m["opacity"],
                offset=tuple(m["offset"]),
                scale=tuple(m["scale"]),
                rotation=tuple(m["rotation"]),
            )
            for m in data["models"]
        ),
    )


async def _script_result(args: list[str], prefix: str, input: bytes | None = None) -> dict:
    """Run a pcbnew script and return the JSON of its result line.

    Raises:
        FootprintReadError: No Python with pcbnew is found, or the script fails.
    """
    try:
        result = await run_kicad_python(args, timeout=120, input=input)
    except FileNotFoundError as e:
        raise FootprintReadError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise FootprintReadError("pcbnew did not stop in 120 s.") from e
    if result.returncode != 0:
        raise FootprintReadError(
            f"pcbnew stopped with the exit code {result.returncode}:\n"
            + decode_python_output(result.stderr).strip()
        )
    line = result_line(result.stdout, prefix)
    if line is None:
        raise FootprintReadError("The pcbnew script gave no result.")
    try:
        return json.loads(line)
    except ValueError as e:
        raise FootprintReadError(f"The pcbnew script gave a result that is not JSON: {e}") from e


async def read_library_footprint(library: Path, name: str) -> tuple[str, FootprintInfo | None]:
    """Read a footprint of a KiCad library (.pretty directory) with pcbnew.

    Returns:
        (the KiCad version of pcbnew, the footprint). The footprint is None when
        KiCad does not load it: the library has no such footprint, or KiCad
        cannot read its file.

    Raises:
        FootprintReadError: No Python with pcbnew is found, or the script fails.
    """
    data = await _script_result(["-c", _SCRIPT, str(library), name], _PREFIX)
    return data["kicad"], footprint_info(data) if "pads" in data else None


@dataclass(frozen=True)
class PadNumber:
    """The number of a pad, as pcbnew loaded the footprint."""

    number: str
    on_copper: bool  # PAD::IsOnCopperLayer


@dataclass(frozen=True)
class FootprintItems:
    """The pads, the 3D models and the embedded files of a footprint, as pcbnew loaded it."""

    pads: tuple[PadNumber, ...]
    models: tuple[tuple[str, bool], ...]  # (FP_3DMODEL::m_Filename, m_Show)
    # The names of the embedded files, or None when the caller did not ask for them.
    embedded_files: tuple[str, ...] | None


def embedded_file_names(text: str) -> tuple[str, ...]:
    """Return the names in the (embedded_files ...) of a text of the KiCad writer.

    Raises:
        FootprintReadError: The text cannot be parsed.
    """
    try:
        root = parse(text)
    except SExprError as e:
        raise FootprintReadError(f"The text that KiCad wrote cannot be parsed: {e}") from e
    return tuple(name for name, _kind in embedded_files(root))


_ITEMS_PREFIX = "FOOTPRINT_ITEMS_JSON="
# pcbnew.FootprintLoad makes a new plugin for each call (GetPluginForPath),
# and the plugin reads all files of the library into its cache (FP_CACHE::Load).
# 300 footprints of one library with KiCad 9.0.9: 15.5 s and 11.4 GB. Thus the
# script uses one plugin for each library: 0.1 s and 159 MB. All footprints of
# the 155 stock libraries of KiCad 10: 2.6 s. getFootprint ignores the errors
# of the cache (KiCad 8, 9 and 10), thus a footprint that KiCad cannot read
# gives None with each of the two methods. SWIG does not give the embedded
# files: the writer text of a footprint with a 3D model gives them.
# PCB_IO_KICAD_SEXPR() has the control flag CTL_FOR_BOARD (KiCad 8, 9 and 10),
# thus it writes the names of the embedded files of a footprint with no data
# (WriteEmbeddedFiles( ..., !( m_ctl & CTL_FOR_BOARD ) )).
_ITEMS_SCRIPT = (
    """\
import json
import sys

import pcbnew

request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
items, embedded = request["items"], request["embedded"]
result = [None] * len(items)
libraries = {}
for index, (library, name) in enumerate(items):
    libraries.setdefault(library, []).append((index, name))
writer = pcbnew.PCB_IO_KICAD_SEXPR()
for library, entries in libraries.items():
    plugin = pcbnew.GetPluginForPath(library)
    for index, name in entries:
        footprint = plugin.FootprintLoad(library, name)
        if footprint is None:
            continue
        models = [[m.m_Filename, m.m_Show] for m in footprint.Models()]
        text = ""
        if embedded and models:
            writer.Format(footprint)
            text = writer.GetStringOutput(True)
        result[index] = {
            "pads": [[pad.GetNumber(), pad.IsOnCopperLayer()] for pad in footprint.Pads()],
            "models": models,
            "text": text,
        }
    del plugin
data = {"kicad": pcbnew.GetMajorMinorPatchVersion(), "items": result}
print("""
    + repr(_ITEMS_PREFIX)
    + """ + json.dumps(data))
"""
)


async def read_footprints(
    items: list[tuple[Path, str]], embedded: bool = False
) -> tuple[str, list[FootprintItems | None]]:
    """Read the pads, the 3D models and the embedded files of footprints of KiCad libraries.

    Args:
        items: (the .pretty directory, the footprint name) of each footprint.
        embedded: True to read the names of the embedded files of each
            footprint with a 3D model (the KiCad writer formats the footprint).

    Returns:
        (the KiCad version of pcbnew, the items of each footprint). The items
        are None when KiCad does not load the footprint. With no footprints,
        the version is "".

    Raises:
        FootprintReadError: No Python with pcbnew is found, or the script fails.
    """
    if not items:
        return "", []
    # The list goes to stdin: one argument has at most 128 KiB on Linux.
    request = {"items": [[str(library), name] for library, name in items], "embedded": embedded}
    group = json.dumps(request).encode("utf-8")
    data = await _script_result(["-c", _ITEMS_SCRIPT], _ITEMS_PREFIX, group)
    footprints: list[FootprintItems | None] = []
    for found in data["items"]:
        if found is None:
            footprints.append(None)
            continue
        footprints.append(
            FootprintItems(
                pads=tuple(PadNumber(n, c) for n, c in found["pads"]),
                models=tuple((file, shown) for file, shown in found["models"]),
                embedded_files=(
                    (embedded_file_names(found["text"]) if found["text"] else ())
                    if embedded
                    else None
                ),
            )
        )
    return data["kicad"], footprints
