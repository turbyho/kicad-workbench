"""The tracks, the vias and the copper zones of a board: the items that the two board
readers give (pcb_parser_kicad.PCBParserKiCad through pcbnew, pcb_parser.PCBParser
with no pcbnew). The layer names are the standard names of the copper layers (F.Cu,
In1.Cu, B.Cu), not the user names of the layer table."""

from dataclasses import dataclass


@dataclass
class PCBTrack:
    """Track (segment) from PCB file."""

    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net: str = ""
    length: float = 0.0


@dataclass
class PCBVia:
    """Via from PCB file."""

    position: tuple[float, float]
    size: float
    drill: float
    top_layer: str
    bottom_layer: str
    net: str = ""


@dataclass
class PCBZone:
    """A copper zone of the board."""

    net_name: str
    layers: list[str]  # the copper layers of the zone, from the top (F.Cu, In1.Cu, B.Cu)
    filled: bool | None  # None: the reader does not read the fill state (the text reader)
