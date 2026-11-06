"""Text parser for KiCad PCB files (.kicad_pcb), without pcbnew."""

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..utils.file_handlers import validate_kicad_file
from ..utils.kicad_text import read_as_kicad
from .kicad_versions import check_board_version, header_version
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
            pad_count=len(data.get("pads", [])),
        )


_COPPER_LAYER = re.compile(r"F\.Cu|B\.Cu|In\d+\.Cu")

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
            ValueError: If file is not a .kicad_pcb file
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
        nets = {net.value(0): net.value(1, "") for net in items["net"]}

        self._data = {
            "path": str(self.file_path),
            "general": self._parse_general(root),
            "footprints": self._parse_footprints(items["footprint"]),
            "tracks": self._parse_tracks(items["segment"], items["arc"], nets),
            "vias": self._parse_vias(items["via"], nets),
            "zones": self._parse_zones(items["zone"], nets),
        }

        return self._data

    def _parse_general(self, root: Node) -> dict[str, Any]:
        """Parse the board thickness and the number of copper layers."""
        general = root.first("general")
        thickness = general.first("thickness") if general else None

        # The layer table: (layers (0 "F.Cu" signal) (2 "B.Cu" signal) ...).
        # Only F.Cu, B.Cu and In<N>.Cu are copper. F.SilkS, F.Mask and the
        # other "F." layers are not.
        table = root.first("layers")
        names = {layer.value(0) for layer in table.children()} if table else set()
        copper = [name for name in names if name and _COPPER_LAYER.fullmatch(name)]

        return {
            "thickness": thickness.number(0, 1.6) if thickness else 1.6,
            "layers": len(copper) or 2,
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
                "pads": [],
                "pad_count": len(footprint.children("pad")),
            })
        return footprints

    def _parse_tracks(
        self, segments: list[Node], arcs: list[Node], nets: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Parse the tracks: the straight segments, then the arcs.

        The length of an arc is the length along the arc, as pcbnew gives it.
        """
        tracks = []
        for track in segments + arcs:
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
                "layer": _text(track, "layer"),
                "net": _net(track, nets),
                "length": length,
            })
        return tracks

    def _parse_vias(self, items: list[Node], nets: dict[str, str]) -> list[dict[str, Any]]:
        """Parse the vias."""
        vias = []
        for via in items:
            x, y = _point(via.first("at"))
            layers = via.first("layers")
            vias.append({
                "at": {"x": x, "y": y},
                "size": _number(via, "size"),
                "drill": _number(via, "drill"),
                "top_layer": layers.value(0, "") if layers else "",
                "bottom_layer": layers.value(1, "") if layers else "",
                "net": _net(via, nets),
            })
        return vias

    def _parse_zones(self, items: list[Node], nets: dict[str, str]) -> list[dict[str, Any]]:
        """Parse the copper zones of the board.

        As in pcbnew (BOARD::Zones()), the zones in footprints are not board
        zones, and a zone with no net is a zone. A rule area (keepout) is not
        a copper zone, thus it is not in the list.
        """
        zones = []
        for zone in items:
            if zone.first("keepout") is not None:
                continue
            name = zone.first("net_name")
            layers = zone.first("layers")
            layer = _text(zone, "layer")
            if not layer and layers:
                layer = " ".join(atom.value for atom in layers.atoms())
            zones.append({
                # KiCad 9 and earlier: (net 1) (net_name "GND").
                # KiCad 10: (net "GND").
                "net_name": name.value(0, "") if name else _net(zone, nets),
                "layer": layer,
            })
        return zones

    def get_footprints(self) -> list[PCBFootprint]:
        """Get all footprints from PCB.

        Returns:
            List of footprints
        """
        data = self._parse_file()
        return [PCBFootprint.from_dict(f) for f in data["footprints"]]

    def get_statistics(self) -> dict[str, Any]:
        """Get PCB statistics.

        Returns:
            Dictionary with statistics
        """
        data = self._parse_file()

        # Calculate total pads from pad_count field
        total_pads = sum(f.get("pad_count", 0) for f in data["footprints"])

        return {
            "total_footprints": len(data["footprints"]),
            "total_pads": total_pads,
            "total_tracks": len(data["tracks"]),
            "total_vias": len(data["vias"]),
            "total_zones": len(data["zones"]),
            "layers": data["general"]["layers"],
            "thickness": data["general"]["thickness"],
        }
