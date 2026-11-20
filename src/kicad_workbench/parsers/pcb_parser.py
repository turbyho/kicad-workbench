"""Text parser for KiCad PCB files (.kicad_pcb), without pcbnew."""

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..utils.file_handlers import validate_kicad_file
from ..utils.kicad_text import read_as_kicad
from .c_numbers import to_long
from .kicad_versions import check_board_version, header_version
from .pcb_items import PCBTrack, PCBVia, PCBZone
from .sexpr import Node, parse


@dataclass
class PCBFootprint:
    """Footprint from PCB file."""

    reference: str
    footprint_id: str
    value: str
    layer: str = "F.Cu"
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    pad_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PCBFootprint":
        """Create from parsed data structure."""
        at = data.get("at", {})
        position = (float(at.get("x", 0)), float(at.get("y", 0)))
        rotation = float(at.get("rotation", 0))

        return cls(
            reference=data.get("reference", ""),
            footprint_id=data.get("footprint_id", ""),
            value=data.get("value", ""),
            layer=data.get("layer", "F.Cu"),
            position=position,
            rotation=rotation,
            pad_count=data.get("pad_count", 0),
        )


# The standard names of the copper layers (LSET::Name), from the top.
_COPPER_NAMES = ("F.Cu", *(f"In{number}.Cu" for number in range(1, 31)), "B.Cu")
# The layer types of a copper layer in the layer table (LAYER::ParseType gives
# LT_UNDEFINED for each other type, for example "user").
_COPPER_TYPES = {"signal", "power", "mixed", "jumper", "auxiliary", "front", "back"}
# The names of the fixed map of LSET::NameToLayer that are not copper layers.
_NON_COPPER_NAMES = {
    "F.Adhes", "B.Adhes", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask",
    "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User", "Edge.Cuts", "Margin", "F.CrtYd",
    "B.CrtYd", "F.Fab", "B.Fab", "Rescue",
}  # fmt: skip


def _name_to_layer(name: str) -> str | None:
    """The layer of a name as LSET::NameToLayer (KiCad 10.0.6) gives it: the standard
    name of a copper layer, "" for a layer that is not copper, or None (-1).

    "In<n>" and any 3 more characters is In<n>.Cu: the function removes the last 3
    characters (wxString::RemoveLast, Truncate: no change for a text shorter than 3
    characters) and does not compare them with ".Cu". A number n above 30 gives a
    layer ID after In30_Cu, which is not a copper layer.
    """
    if name in ("F.Cu", "B.Cu"):
        return name
    if name in _NON_COPPER_NAMES:
        return ""
    if name.startswith("User."):
        offset = to_long(name[5:])
        if offset is not None and offset > 0:
            return ""
    if name.startswith("In"):
        rest = name[2:]
        offset = to_long(rest[:-3] if len(rest) >= 3 else rest)
        if offset is not None and offset > 0:
            return f"In{offset}.Cu" if offset <= 30 else ""
    return None


@dataclass
class _BoardLayers:
    """The copper layers of a board, as PCB_IO_KICAD_SEXPR_PARSER::parseLayers reads the
    layer table: enabled is the standard names of the copper layers of the board, from
    the top; names gives the layer of each copper name that an item can have."""

    enabled: list[str]
    names: dict[str, str]

    def item_copper(self, item: Node) -> set[str]:
        """The copper layers of the layer set of a zone or a track (also the layers that
        the board does not enable). parseZONE and parsePCB_TRACK read the items in their
        sequence: the last (layer ...) or (layers ...) gives the layers. (layers ...)
        has the masks of the parser for "*.Cu", "*In.Cu" and "F&B.Cu"
        (parseBoardItemLayersAsMask, LSET::AllCuMask); (layer X) has no masks
        (parseBoardItemLayer, lookUpLayer: "*.Cu" is not a layer name)."""
        tokens: list[str] = []
        masks = False
        for child in item.children():
            if child.head == "layer":
                tokens, masks = [child.value(0, "")], False
            elif child.head == "layers":
                tokens, masks = [atom.value for atom in child.atoms()], True
        copper: set[str] = set()
        for token in tokens:
            if not masks:
                if self.names.get(token):
                    copper.add(self.names[token])
            elif token == "*.Cu":
                copper.update(_COPPER_NAMES)
            elif token == "*In.Cu":
                copper.update(_COPPER_NAMES[1:-1])
            elif token == "F&B.Cu":
                copper.update(("F.Cu", "B.Cu"))
            elif self.names.get(token):
                copper.add(self.names[token])
        return copper


def _stack_index(name: str) -> int:
    """The position of a copper layer from the top (F.Cu is 0, B.Cu the last), as
    IsCopperLayerLowerThan compares them; a name that is not a standard copper name
    sorts after them."""
    return _COPPER_NAMES.index(name) if name in _COPPER_NAMES else len(_COPPER_NAMES)


def _board_layers(root: Node) -> _BoardLayers:
    """The copper layers of the layer table (PCB_IO_KICAD_SEXPR_PARSER::parseLayers).

    The copper entries are the entries at the start of the table, up to the first
    entry with an other type. KiCad does not use their numbers: the first is F.Cu, the
    last B.Cu, and an inner layer is the layer of its name when LSET::NameToLayer
    knows the name (KiCad 9 and 10: also a layer that is not copper, for example
    "User.1"), else In<position>.Cu. KiCad 8.0.9 uses the position only: the two rules
    give the same layers for each table that KiCad writes (the inner layers are In1.Cu
    to In<n>.Cu, in this sequence). The parser also knows each standard name
    (PCB_IO_KICAD_SEXPR_PARSER::init); the name of a copper entry of the table replaces
    it. The parser enables the layer of each later entry too: a later entry with a
    copper name enables that copper layer. names gives "" for a name of a layer that is
    not copper. A board with no layer table has the layers of a new BOARD: F.Cu and
    B.Cu.
    """
    names = {name: name for name in _COPPER_NAMES}
    table = root.first("layers")
    if table is None:
        return _BoardLayers(enabled=["F.Cu", "B.Cu"], names=names)
    entries = [entry.value(0, "") for entry in table.children()]
    types = [entry.value(1, "") for entry in table.children()]
    count = next(
        (index for index, kind in enumerate(types) if kind not in _COPPER_TYPES), len(types)
    )
    copper = entries[:count]
    enabled = []
    for position, name in enumerate(copper):
        if position == 0:
            layer = "F.Cu"
        elif position == len(copper) - 1:
            layer = "B.Cu"
        else:
            known = _name_to_layer(name)
            layer = f"In{position}.Cu" if known is None else known
        names[name] = layer
        if layer:
            enabled.append(layer)
    enabled += [names[name] for name in entries[count:] if names.get(name)]
    return _BoardLayers(enabled=[name for name in _COPPER_NAMES if name in enabled], names=names)


def _is_teardrop(zone: Node) -> bool:
    """True for a teardrop zone: parseZONE sets a teardrop type for (attr (teardrop
    (type padvia))) and (attr (teardrop (type track_end))). With no type, the zone keeps
    TD_NONE (a zone that is not a teardrop). An other type is a parse error of KiCad
    (Expecting): the text reader does not check it, and the zone is not a teardrop."""
    for attr in zone.children("attr"):
        for teardrop in attr.children("teardrop"):
            for kind in teardrop.children("type"):
                if kind.value(0, "") in ("padvia", "track_end"):
                    return True
    return False


def _net(item: Node, nets: dict[str, str]) -> str:
    """Return the net name of a track, a via or a zone.

    KiCad 10 writes the name, (net "GND"). KiCad 9 and earlier write the
    code of the net table, (net 1), and the table gives its name.
    """
    net = item.first("net")
    if net is None or not net.atoms():
        return ""
    atom = net.atoms()[0]
    if not atom.quoted and atom.value.isdigit():
        return nets.get(atom.value, "")
    return atom.value


def _field(footprint: Node, name: str) -> str:
    """Return a field of a footprint, for example "Reference".

    KiCad 8 and later write (property "Reference" "R1"). KiCad 6 and 7
    wrote (fp_text reference "R1").
    """
    for prop in footprint.children("property"):
        if prop.value(0) == name:
            return prop.value(1, "")
    for text in footprint.children("fp_text"):
        if text.value(0) == name.lower():
            return text.value(1, "")
    return ""


def _arc_length(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> float:
    """Return the length of the arc from start through mid to end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        # The three points are on a line.
        return math.dist(start, end)
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    radius = math.dist((ux, uy), start)

    def angle(p: tuple[float, float], q: tuple[float, float]) -> float:
        # The angle at the center between p and q, at most pi.
        vp = (p[0] - ux, p[1] - uy)
        vq = (q[0] - ux, q[1] - uy)
        cross = vp[0] * vq[1] - vp[1] * vq[0]
        dot = vp[0] * vq[0] + vp[1] * vq[1]
        return abs(math.atan2(cross, dot))

    # The mid point divides the arc, thus each half is at most pi.
    return radius * (angle(start, mid) + angle(mid, end))


def _point(item: Node | None) -> tuple[float, float]:
    return (item.number(0, 0.0), item.number(1, 0.0)) if item else (0.0, 0.0)


def _number(item: Node, head: str) -> float:
    child = item.first(head)
    return child.number(0, 0.0) if child else 0.0


def _text(item: Node, head: str) -> str:
    child = item.first(head)
    return child.value(0, "") if child else ""


class PCBParser:
    """Parser for KiCad PCB files (.kicad_pcb)."""

    def __init__(self, file_path: str) -> None:
        """Initialize parser with PCB file.

        Args:
            file_path: Path to .kicad_pcb file

        Raises:
            FileNotFoundError: If file doesn't exist
            InputError: If file is not a .kicad_pcb file
        """
        self.file_path = validate_kicad_file(file_path, ".kicad_pcb")
        self._data: dict[str, Any] | None = None

    def _parse_file(self) -> dict[str, Any]:
        """Parse the PCB file.

        Returns:
            Parsed data structure

        Raises:
            SExprError: The file is not a valid S-expression.
            KicadFileError: The board is not of KiCad 8 to 10.
        """
        if self._data is not None:
            return self._data

        text = read_as_kicad(self.file_path)
        # The version first: a board of an other KiCad needs no parse, and its error
        # is the version, not a syntax error of its format.
        check_board_version(header_version(text, "kicad_pcb"))
        root = parse(text)
        # The board items, by their head. A large board has some 100 000.
        items: dict[str, list[Node]] = defaultdict(list)
        for item in root.children():
            items[item.head].append(item)
            # The tracks in the order of the file, as BOARD::Tracks() (the parser of
            # KiCad adds each with ADD_MODE::BULK_APPEND).
            if item.head in ("segment", "arc"):
                items["track"].append(item)
        nets = {net.value(0): net.value(1, "") for net in items["net"]}
        layers = _board_layers(root)

        self._data = {
            "path": str(self.file_path),
            "general": self._parse_general(root, layers),
            "footprints": self._parse_footprints(items["footprint"]),
            "tracks": self._parse_tracks(items["track"], nets, layers),
            "vias": self._parse_vias(items["via"], nets, layers),
            "zones": self._parse_zones(items["zone"], nets, layers),
        }

        return self._data

    def _parse_general(self, root: Node, layers: _BoardLayers) -> dict[str, Any]:
        """Parse the board thickness and the number of copper layers."""
        general = root.first("general")
        thickness = general.first("thickness") if general else None

        return {
            "thickness": thickness.number(0, 1.6) if thickness else 1.6,
            "layers": len(layers.enabled) or 2,
        }

    def _parse_footprints(self, items: list[Node]) -> list[dict[str, Any]]:
        """Parse the footprints."""
        footprints = []
        for footprint in items:
            at = footprint.first("at")
            layer = footprint.first("layer")
            footprints.append({
                "footprint_id": footprint.value(0, ""),
                "reference": _field(footprint, "Reference"),
                "value": _field(footprint, "Value"),
                "layer": layer.value(0, "F.Cu") if layer else "F.Cu",
                "at": {
                    "x": at.number(0, 0.0) if at else 0.0,
                    "y": at.number(1, 0.0) if at else 0.0,
                    "rotation": at.number(2, 0.0) if at else 0.0,
                },
                "pad_count": len(footprint.children("pad")),
            })
        return footprints

    def _parse_tracks(
        self, items: list[Node], nets: dict[str, str], layers: _BoardLayers
    ) -> list[dict[str, Any]]:
        """Parse the tracks: the straight segments and the arcs, in their order.

        The length of an arc is the length along the arc, as pcbnew gives it. The layer
        is the copper layer of (layer X), or of (layers ...) with a solder mask layer
        ("F.Cu" "F.Mask": parseLayersForCuItemWithSoldermask). A track that is not on a
        copper layer is not on the board: KiCad 9 and 10 drop it in parsePCB_TRACK and
        parseARC, KiCad 8.0.9 in BOARD::Add. KiCad refuses a board with a track on more
        than one copper layer (Expecting "single copper layer"): the text reader does
        not check it, and leaves the track out.
        """
        tracks = []
        for track in items:
            copper = layers.item_copper(track)
            if len(copper) != 1:
                continue
            start = _point(track.first("start"))
            end = _point(track.first("end"))
            if track.head == "arc":
                length = _arc_length(start, _point(track.first("mid")), end)
            else:
                length = math.dist(start, end)
            tracks.append({
                "start": {"x": start[0], "y": start[1]},
                "end": {"x": end[0], "y": end[1]},
                "width": _number(track, "width"),
                "layer": copper.pop(),
                "net": _net(track, nets),
                "length": length,
            })
        return tracks

    def _parse_vias(
        self, items: list[Node], nets: dict[str, str], layers: _BoardLayers
    ) -> list[dict[str, Any]]:
        """Parse the vias, with the layer pair of PCB_VIA::SetLayerPair and
        SanitizeLayers (KiCad 8, 9 and 10): a through via (no type "blind", "buried" or
        "micro" after "via") is on F.Cu and B.Cu, also with an other pair or no (layers
        ...) (the constructor of PCB_VIA); an other via has its two layers from the top
        down. A layer name of the layer table is its standard name (_BoardLayers.names),
        as for the tracks."""
        vias = []
        for via in items:
            x, y = _point(via.first("at"))
            pair = via.first("layers")
            top = pair.value(0, "F.Cu") if pair else "F.Cu"
            bottom = pair.value(1, "B.Cu") if pair else "B.Cu"
            top, bottom = layers.names.get(top) or top, layers.names.get(bottom) or bottom
            if via.value(0, "") not in ("blind", "buried", "micro"):
                top, bottom = "F.Cu", "B.Cu"
            elif _stack_index(bottom) < _stack_index(top):
                top, bottom = bottom, top
            vias.append({
                "at": {"x": x, "y": y},
                "size": _number(via, "size"),
                "drill": _number(via, "drill"),
                "top_layer": top,
                "bottom_layer": bottom,
                "net": _net(via, nets),
            })
        return vias

    def _parse_zones(
        self, items: list[Node], nets: dict[str, str], layers: _BoardLayers
    ) -> list[dict[str, Any]]:
        """Parse the copper zones of the board.

        As in pcbnew, the zones in footprints are not board zones (BOARD::Zones()).
        The copper zones are the zones of the Zone Manager of KiCad (ZONE_SETTINGS_BAG):
        no rule area ((keepout ...) or (placement ...)), no teardrop (_is_teardrop), and
        only a zone on a copper layer. A zone with no net is a zone.
        "layers" is the copper layers of the zone on the board, from the top
        (_BoardLayers.item_copper).
        """
        zones = []
        for zone in items:
            if zone.first("keepout") is not None or zone.first("placement") is not None:
                continue
            if _is_teardrop(zone):
                continue
            # ZONE::IsOnCopperLayer: a copper layer of LSET::AllCuMask.
            copper = layers.item_copper(zone)
            if not copper:
                continue
            name = zone.first("net_name")
            zones.append({
                # KiCad 9 and earlier: (net 1) (net_name "GND").
                # KiCad 10: (net "GND").
                "net_name": name.value(0, "") if name else _net(zone, nets),
                # The layers that PCB_IO_KICAD_SEXPR::format(ZONE) saves: the
                # layers that the board enables, from the top.
                "layers": [layer for layer in layers.enabled if layer in copper],
            })
        return zones

    def get_tracks(self) -> list[PCBTrack]:
        """The tracks (segments and arcs) in the order of the file, as the items of
        the pcbnew reader."""
        return [
            PCBTrack(
                start=(t["start"]["x"], t["start"]["y"]),
                end=(t["end"]["x"], t["end"]["y"]),
                width=t["width"],
                layer=t["layer"],
                net=t["net"],
                length=t["length"],
            )
            for t in self._parse_file()["tracks"]
        ]

    def get_vias(self) -> list[PCBVia]:
        """The vias in the order of the file, as the items of the pcbnew reader."""
        return [
            PCBVia(
                position=(v["at"]["x"], v["at"]["y"]),
                size=v["size"],
                drill=v["drill"],
                top_layer=v["top_layer"],
                bottom_layer=v["bottom_layer"],
                net=v["net"],
            )
            for v in self._parse_file()["vias"]
        ]

    def get_zones(self) -> list[PCBZone]:
        """The copper zones as the items of the pcbnew reader, with no fill state (the
        text reader does not read it)."""
        return [PCBZone(z["net_name"], z["layers"], None) for z in self._parse_file()["zones"]]

    def get_footprints(self) -> list[PCBFootprint]:
        """Get all footprints from PCB.

        Returns:
            List of footprints
        """
        data = self._parse_file()
        return [PCBFootprint.from_dict(f) for f in data["footprints"]]

    def get_copper_layer_count(self) -> int:
        """The copper layers of the board, as pcbnew gives them: the copper layers of the
        layer table (PCB_IO_KICAD_SEXPR_PARSER::parseLayers, SetCopperLayerCount), 2 for a
        board with no layer table (the BOARD default)."""
        return self._parse_file()["general"]["layers"]

    def get_board_thickness(self) -> float:
        """The board thickness in mm, as pcbnew gives it: (general (thickness ...))
        (parseGeneralSection, SetBoardThickness), else DEFAULT_BOARD_THICKNESS_MM (1.6)."""
        return self._parse_file()["general"]["thickness"]
