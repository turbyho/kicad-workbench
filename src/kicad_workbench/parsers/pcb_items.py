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


def net_track_stats(tracks: list[PCBTrack]) -> dict[str, dict]:
    """The tracks of each net: total_length, segment_count, widths and layers (sorted).
    A track with no net is in the net "(unnamed)"."""
    stats: dict[str, dict] = {}
    for track in tracks:
        net = track.net or "(unnamed)"
        s = stats.setdefault(
            net, {"total_length": 0.0, "segment_count": 0, "widths": [], "layers": set()}
        )
        s["total_length"] += track.length
        s["segment_count"] += 1
        s["widths"].append(track.width)
        s["layers"].add(track.layer)
    for s in stats.values():
        s["layers"] = sorted(s["layers"])
    return stats


def via_stats(vias: list[PCBVia]) -> dict:
    """The vias: count, drill_sizes (most first), layer_spans and top_via_nets (the 20
    nets with the most vias). A via with no net is in the net "(unnamed)"."""
    if not vias:
        return {"count": 0}
    drill_counts: dict[str, int] = {}
    layer_span_counts: dict[str, int] = {}
    net_via_counts: dict[str, int] = {}
    for via in vias:
        drill = f"{via.drill:.3f}mm"
        drill_counts[drill] = drill_counts.get(drill, 0) + 1
        span = f"{via.top_layer} -> {via.bottom_layer}"
        layer_span_counts[span] = layer_span_counts.get(span, 0) + 1
        net = via.net or "(unnamed)"
        net_via_counts[net] = net_via_counts.get(net, 0) + 1
    return {
        "count": len(vias),
        "drill_sizes": dict(sorted(drill_counts.items(), key=lambda x: -x[1])),
        "layer_spans": layer_span_counts,
        "top_via_nets": dict(sorted(net_via_counts.items(), key=lambda x: -x[1])[:20]),
    }
