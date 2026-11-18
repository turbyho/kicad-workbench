"""PCB analysis tools for KiCad Workbench."""

from __future__ import annotations

import asyncio
import functools

from ..errors import WorkbenchError
from ..parsers.pcb_items import PCBTrack, PCBVia, PCBZone
from ..server import mcp
from ..utils import kicad_cli
from ..utils.file_versions import with_board_versions
from ..utils.kicad_python import KicadPythonError, check_same_kicad
from ..utils.pcbnew_thread import in_pcbnew_thread
from ..utils.tool_errors import error_answer
from .library import cell


class PcbnewImportError(WorkbenchError):
    """The server process finds pcbnew, but cannot import it."""


@functools.cache
def _pcbnew_importable() -> bool:
    """True when the server process can import pcbnew, False when it has no pcbnew
    (checked one time).

    Raises:
        PcbnewImportError: pcbnew is there, but its import fails (for example a
            build for a different wx or Python). The tools give the error: the text
            reader would hide it.
    """
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        return False
    except Exception as e:
        raise PcbnewImportError(f"The import of pcbnew failed: {type(e).__name__}: {e}") from e
    return True


TEXT_READER_WARNING = (
    "⚠️ pcbnew not available — text-based analysis: the tool reads the board file as "
    "text, not with pcbnew of KiCad"
)


def _check_pcbnew_version(kicad: str) -> None:
    """Make sure that the pcbnew of the server process is of the KiCad of kicad-cli
    (kicad_python.check_same_kicad). _pcbnew_importable imported it before.

    Raises:
        KicadPythonError: It is of an other KiCad.
    """
    import pcbnew

    version = pcbnew.GetMajorMinorPatchVersion()
    check_same_kicad(str(version), "the pcbnew of the server process", kicad)


def _board_tool(limits: str):
    """A board tool: with no pcbnew, its answer gets TEXT_READER_WARNING and the
    limits of the text reader for this tool (each answer that is not an error).

    The text reader is only for an installed KiCad 8 or later whose pcbnew the server
    process cannot import (for example the Python of KiCad on Windows). With no KiCad
    (kicad-cli gives no version), the tool gives the error of no_kicad_message: the
    tools work with the files as the installed KiCad does. The pcbnew of the server
    process must be of the KiCad of kicad-cli (_check_pcbnew_version).

    The board tool is a plain function, off the event loop: the import of pcbnew, the
    check of its version and the tool with pcbnew run in one call in the pcbnew thread
    (utils/pcbnew_thread.py: pcbnew is not thread-safe); the tool with the text reader
    runs in the pool of asyncio.to_thread.
    """

    def decorator(function):
        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            # The installed KiCad is the KiCad of kicad-cli (CLAUDE.md rule 4). First:
            # this error needs no pcbnew, thus no wait for the pcbnew thread.
            kicad = await kicad_cli.get_kicad_version()

            def with_pcbnew() -> str | None:
                """The answer of the tool with pcbnew, or None with no pcbnew."""
                try:
                    if not _pcbnew_importable():
                        return None
                except PcbnewImportError as e:
                    return f"❌ {e}"
                if problem := kicad_cli.installed_kicad_problem(kicad):
                    return f"❌ {problem}"
                try:
                    _check_pcbnew_version(kicad)
                except KicadPythonError as e:
                    return f"❌ {e}"
                except Exception as e:
                    return error_answer(e, "The version of the pcbnew of the server process")
                return function(*args, **kwargs)

            result = await in_pcbnew_thread(with_pcbnew)
            if result is not None:
                return result
            if problem := kicad_cli.installed_kicad_problem(kicad):
                return f"❌ {problem}"
            result = await asyncio.to_thread(function, *args, **kwargs)
            if not result.startswith("❌"):
                result += f"\n\n{TEXT_READER_WARNING}{limits}."
            return result

        return wrapper

    return decorator


def _get_parser(file_path: str):
    """The reader of the board: pcbnew when the server can import it, else the text
    reader (the tool then gives TEXT_READER_WARNING, see _board_tool).

    Raises:
        FileNotFoundError, InputError: The file is not there, or it is not a board.
        BoardLoadError: pcbnew cannot read the board. The tool gives the error: the
            text reader does not know the rules of a board that KiCad cannot read.
    """
    if not _pcbnew_importable():
        from ..parsers.pcb_parser import PCBParser

        return PCBParser(file_path)
    from ..parsers.pcb_parser_kicad import PCBParserKiCad

    return PCBParserKiCad(file_path)


def _is_pcbnew(parser) -> bool:
    """True for the pcbnew reader. _get_parser gives it when pcbnew is importable:
    the one source of this fact (also for the warning of _board_tool)."""
    return _pcbnew_importable()


@mcp.tool()
@with_board_versions("file_path")
@_board_tool("")
def list_pcb_footprints(
    file_path: str,
    filter_layer: str | None = None,
) -> str:
    """Give the footprints of a board.

    The layer of a footprint is the standard name of its copper layer, F.Cu or B.Cu,
    also when the layer table of the board has a user name for it ("Top"): KiCad shows
    the user name in its GUI.

    Args:
        file_path: The .kicad_pcb file.
        filter_layer: Optional layer of the footprints to give: "F.Cu" or "B.Cu" (a
            user name of the layer gives an error). Empty: all footprints.

    Returns:
        A table of the footprints: reference, value, library ID, layer, position,
        rotation and number of pads.
    """
    try:
        # A footprint is on F.Cu or on B.Cu (FOOTPRINT::Flip): an other filter finds no
        # footprint, also a user name of the layer.
        if filter_layer and filter_layer not in ("F.Cu", "B.Cu"):
            return (
                f"❌ filter_layer {filter_layer!r} is not F.Cu or B.Cu: a footprint is on "
                "one of these layers, with their standard names."
            )
        parser = _get_parser(file_path)
        # The two readers give the same fields: the library ID as the board file
        # has it (LIB_ID::Format), and the number of all pads (FOOTPRINT::GetPadCount).
        footprints = parser.get_footprints()
        if filter_layer:
            footprints = [f for f in footprints if f.layer == filter_layer]

        if not footprints:
            return "No footprints found."

        lines = [
            f"# Footprints in {file_path}",
            f"Total: {len(footprints)} footprint(s)",
            "",
            "| Reference | Value | Library ID | Layer | Position | Rotation | Pads |",
            "|-----------|-------|------------|-------|----------|----------|------|",
        ]
        for fp in footprints:
            pos_str = f"({fp.position[0]:.2f}, {fp.position[1]:.2f})"
            lines.append(
                f"| {cell(fp.reference)} | {cell(fp.value)} | {cell(fp.footprint_id)} "
                f"| {fp.layer} | {pos_str} "
                f"| {fp.rotation:.1f}° | {fp.pad_count} |"
            )
        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


@mcp.tool()
@with_board_versions("file_path")
@_board_tool(": no board outline, no design rules")
def get_pcb_statistics(file_path: str) -> str:
    """Give the size, the layers, the counts and the design rules of a board.

    The zone count is the count of the copper zones, as the Zone Manager of KiCad lists
    them: no rule area (keepout), no teardrop, and no zone on non-copper layers only.
    The size is the bounding box of the Edge.Cuts shapes of the board and of its
    footprints, with the line width (BOARD::ComputeBoundingBox): an outline of 100 x
    100 mm with lines of 0.15 mm gives 100.15 x 100.15 mm.

    Args:
        file_path: The .kicad_pcb file.

    Returns:
        The size of the board, the copper layers, the thickness, the numbers of
        footprints, tracks, vias, zones and nets, and the design rules.
    """
    try:
        parser = _get_parser(file_path)

        if _is_pcbnew(parser):
            info = parser.get_board_info()
            rules = parser.get_design_rules()

            lines = [
                f"# PCB Statistics: {file_path}",
                "",
                "## Board Information",
                f"**Dimensions:** {info['board_width_mm']:.2f} x {info['board_height_mm']:.2f} mm",
                f"**Copper Layers:** {rules['copper_layers']}",
                f"**Board Thickness:** {rules['board_thickness_mm']:.3f} mm",
                "",
                "## Elements",
                f"**Footprints:** {info['footprints_count']}",
                f"**Track Segments:** {info['tracks_count']}",
                f"**Vias:** {info['vias_count']}",
                f"**Copper Zones:** {info['zones_count']}",
                f"**Nets:** {info['nets_count']}",
                "",
                "## Design Rules",
                f"**Smallest Clearance:** {rules['smallest_clearance_mm']:.4f} mm",
                f"**Default Track Width:** {rules['current_track_width_mm']:.4f} mm",
                f"**Default Via Size/Drill:** {rules['current_via_size_mm']:.4f} / {rules['current_via_drill_mm']:.4f} mm",
                f"**Diff Pair Width/Gap:** {rules['current_diff_pair_width_mm']:.4f} / {rules['current_diff_pair_gap_mm']:.4f} mm",
            ]
            return "\n".join(lines)
        else:
            stats = parser.get_statistics()
            lines = [
                f"# PCB Statistics: {file_path}",
                "",
                "## Board Information",
                "**Dimensions:** not known (the text reader does not read the board outline)",
                f"**Layers:** {stats['layers']}",
                f"**Thickness:** {stats['thickness']:.2f} mm",
                "",
                "## Elements",
                f"**Footprints:** {stats['total_footprints']}",
                f"**Total Pads:** {stats['total_pads']}",
                f"**Track Segments:** {stats['total_tracks']}",
                f"**Vias:** {stats['total_vias']}",
                f"**Copper Zones:** {stats['total_zones']}",
                "",
                "## Averages",
                f"**Pads per Footprint:** {stats['total_pads'] / max(stats['total_footprints'], 1):.1f}",
            ]
            return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


@mcp.tool()
@with_board_versions("file_path")
@_board_tool(": no design rules")
def analyze_pcb_nets(file_path: str) -> str:
    """Give the routing of a board: track widths, vias, net lengths and layers.

    The tool gives the distribution of the track widths and of the via drills, the
    layer spans of the vias, the tracks of each layer, the nets by the length of their
    tracks, and a comparison with the design rules of the board. The zone count is the
    count of the copper zones, as the Zone Manager of KiCad lists them: no rule area
    (keepout), no teardrop, and no zone on non-copper layers only.

    The tool lists the 20 nets with the longest tracks. The warning about narrow
    tracks is a rule of the tool (a track narrower than half of the current track
    width of the board settings), not a DRC result: run_drc gives the checks of KiCad.

    Args:
        file_path: The .kicad_pcb file.

    Returns:
        The routing analysis of the board.
    """
    try:
        parser = _get_parser(file_path)

        if not _is_pcbnew(parser):
            data = parser._parse_file()
            tracks = data["tracks"]
            vias = data["vias"]
            zones = data["zones"]

            lines = [
                f"# PCB Routing Analysis: {file_path}",
                "",
                "## Overview",
                f"**Track Segments:** {len(tracks)}",
                f"**Vias:** {len(vias)}",
                f"**Copper Zones:** {len(zones)}",
            ]

            # Track width distribution
            width_counts: dict[str, int] = {}
            for t in tracks:
                key = f"{t['width']:.3f}mm"
                width_counts[key] = width_counts.get(key, 0) + 1
            if width_counts:
                lines += [
                    "",
                    "## Track Width Distribution",
                    "| Width | Count |",
                    "|-------|-------|",
                ]
                for w, c in sorted(width_counts.items(), key=lambda x: -x[1]):
                    lines.append(f"| {w} | {c}x |")

            # Via drill distribution
            drill_counts: dict[str, int] = {}
            for v in vias:
                key = f"{v['drill']:.3f}mm"
                drill_counts[key] = drill_counts.get(key, 0) + 1
            if drill_counts:
                lines += [
                    "",
                    "## Via Drill Distribution",
                    f"**Total vias:** {len(vias)}",
                    "",
                    "| Drill Size | Count |",
                    "|------------|-------|",
                ]
                for d, c in sorted(drill_counts.items(), key=lambda x: -x[1]):
                    lines.append(f"| {d} | {c}x |")

            # Per-net track stats
            net_stats: dict[str, dict] = {}
            for t in tracks:
                net = t.get("net", "(unnamed)")
                if net not in net_stats:
                    net_stats[net] = {"total_length": 0.0, "segment_count": 0, "widths": [], "layers": set()}
                s = net_stats[net]
                s["total_length"] += t.get("length", 0)
                s["segment_count"] += 1
                s["widths"].append(t["width"])
                s["layers"].add(t["layer"])

            top_nets = sorted(net_stats.items(), key=lambda x: -x[1]["total_length"])[:20]
            if top_nets:
                lines += [
                    "",
                    "## Top 20 Nets by Track Length",
                    "| Net | Length (mm) | Segments | Widths | Layers |",
                    "|-----|-------------|----------|--------|--------|",
                ]
                for name, s in top_nets:
                    unique_widths = sorted({f"{w:.3f}" for w in s["widths"]})
                    widths_str = ", ".join(unique_widths)
                    layers_str = ", ".join(sorted(s["layers"]))
                    lines.append(
                        f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | {widths_str} | {layers_str} |"
                    )

            return "\n".join(lines)

        net_stats = parser.get_net_track_stats()
        via_stats = parser.get_via_stats()
        rules = parser.get_design_rules()
        zones = parser.get_zones()
        info = parser.get_board_info()

        lines = [
            f"# PCB Routing Analysis: {file_path}",
            "",
            "## Overview",
            f"**Board:** {info['board_width_mm']:.2f} x {info['board_height_mm']:.2f} mm, "
            f"{rules['copper_layers']} copper layers",
            f"**Tracks:** {info['tracks_count']} segments across {len(net_stats)} nets",
            f"**Vias:** {info['vias_count']}",
            f"**Zones:** {len(zones)} copper zones",
        ]

        # Track width distribution
        width_counts: dict[str, int] = {}
        for s in net_stats.values():
            for w in s["widths"]:
                key = f"{w:.3f}mm"
                width_counts[key] = width_counts.get(key, 0) + 1

        if width_counts:
            lines += [
                "",
                "## Track Width Distribution",
                "| Width | Count |",
                "|-------|-------|",
            ]
            for w, c in sorted(width_counts.items(), key=lambda x: -x[1]):
                lines.append(f"| {w} | {c}x |")

        # Via drill distribution
        if via_stats["count"] > 0:
            lines += [
                "",
                "## Via Drill Distribution",
                f"**Total vias:** {via_stats['count']}",
                "",
                "| Drill Size | Count |",
                "|------------|-------|",
            ]
            for drill, count in via_stats["drill_sizes"].items():
                lines.append(f"| {drill} | {count}x |")

            lines += ["", "### Via Layer Spans"]
            for span, count in via_stats["layer_spans"].items():
                lines.append(f"- {span}: {count}x")

        # Layer coverage — count each track on its own layer. Previously this
        # added each net's TOTAL segment count to EVERY layer the net touches,
        # inflating multi-layer net totals by the layer fanout.
        layer_counts: dict[str, int] = {}
        for t in parser.get_tracks():
            layer_counts[t.layer] = layer_counts.get(t.layer, 0) + 1

        if layer_counts:
            lines += [
                "",
                "## Track Segments by Layer",
                "| Layer | Segments |",
                "|-------|----------|",
            ]
            for layer, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
                lines.append(f"| {layer} | {count} |")

        # Top nets by total track length
        top_nets = sorted(net_stats.items(), key=lambda x: -x[1]["total_length"])[:20]
        if top_nets:
            lines += [
                "",
                "## Top 20 Nets by Track Length",
                "| Net | Length (mm) | Segments | Widths | Layers |",
                "|-----|-------------|----------|--------|--------|",
            ]
            for name, s in top_nets:
                unique_widths = sorted({f"{w:.3f}" for w in s["widths"]})
                widths_str = ", ".join(unique_widths)
                layers_str = ", ".join(s["layers"])
                lines.append(
                    f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | {widths_str} | {layers_str} |"
                )

        # Design rule comparison
        if net_stats:
            all_widths = [w for s in net_stats.values() for w in s["widths"]]
            min_width = min(all_widths) if all_widths else 0
            rule_width = rules["current_track_width_mm"]
            lines += [
                "",
                "## Design Rule Comparison",
                f"**Min track width in design:** {min_width:.4f} mm",
                f"**Default track width (design rules):** {rule_width:.4f} mm",
                f"**Smallest clearance:** {rules['smallest_clearance_mm']:.4f} mm",
            ]
            if min_width < rule_width * 0.5:
                lines.append(f"⚠️ Some tracks are significantly narrower than the default ({min_width:.4f} vs {rule_width:.4f} mm)")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


@mcp.tool()
@with_board_versions("file_path")
@_board_tool(": no fill state of the zones")
def find_tracks_by_net(file_path: str, net_name: str) -> str:
    """Give the tracks and the vias of one net of a board, with their lengths and widths.

    The tool also gives the copper zones of the net (no rule area, no teardrop), each
    with all of its copper layers on the board, from the top. "Track Layers" is the
    layers of the tracks only. For a net with no tracks, vias or zones, the tool gives
    the nets whose name contains the name (no difference between upper and lower case).

    The layers are the standard names of the copper layers (F.Cu, In1.Cu, B.Cu), also
    when the layer table of the board has user names for them ("Top"): KiCad shows the
    user names in its GUI.

    Give the full net name of the board, case-sensitive (for example "/SDA" for a local
    label of the root sheet). The total length is the sum of the track segments, with
    no vias and no pads. The tables show 50 segments and 20 vias, in the order of the
    board file. The warning about different track widths comes from the tool, not from
    a DRC. With the text reader (no pcbnew), the zones have no fill state.

    Args:
        file_path: The .kicad_pcb file.
        net_name: The name of the net.

    Returns:
        The tracks, the vias and the copper zones of the net, and the length of the net.
    """
    try:
        parser = _get_parser(file_path)
        # The items of the two readers (pcb_items): the text reader gives no fill state.
        tracks, vias, zones = parser.get_tracks(), parser.get_vias(), parser.get_zones()
        net_tracks = [t for t in tracks if t.net == net_name]
        net_vias = [v for v in vias if v.net == net_name]
        net_zones = [z for z in zones if z.net_name == net_name]
        # A net with only zones (for example a GND pour) is a net with copper.
        if not net_tracks and not net_vias and not net_zones:
            return _similar_nets(
                net_name,
                [t.net for t in tracks],
                [v.net for v in vias],
                [z.net_name for z in zones],
            )
        return _net_items_answer(net_name, net_tracks, net_vias, net_zones)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


@mcp.tool()
@with_board_versions("file_path")
@_board_tool(": no design rules")
def analyze_pcb_signal_integrity(
    file_path: str,
    net_name: str = "",
) -> str:
    """Give the differential pairs, the RF tracks and the longest signal nets of a board (heuristic).

    The tool finds a differential pair from the ends of two net names, case-sensitive:
    _P/_N, _DP/_DN, _TXP/_TXN, _RXP/_RXN, _M/_S and _POS/_NEG (for example USB_DP and
    USB_DN). It does not find + and - (USB_D+ and USB_D-). The status of a pair uses
    fixed limits of the tool (OK below 0.127 mm, Marginal below 0.5 mm, else
    Mismatch), not the rules of the board. The length is the sum of the track
    segments, with no vias and no pads.

    An RF net has ANT, RF or FEED in its name, and not VDD or GND. The longest signal
    nets (15) are the nets whose name has no power word (VDD, VCC, GND, VSS, 3V3, 5V,
    1V8, PGND, VBUS). The tool also gives the 10 nets with the most vias.

    With the text reader (no pcbnew), the tool gives only the differential pairs and
    the longest signal nets: no RF tracks, no nets with vias, no design rules, and
    net_name has no effect.

    Args:
        file_path: The .kicad_pcb file.
        net_name: Optional name of one net to analyze in detail.

    Returns:
        The signal analysis of the board.
    """
    try:
        parser = _get_parser(file_path)

        if not _is_pcbnew(parser):
            data = parser._parse_file()
            tracks = data["tracks"]
            if not tracks:
                return "No tracks found in PCB file."

            # Build net stats from text parser data
            net_stats: dict[str, dict] = {}
            for t in tracks:
                net = t.get("net", "(unnamed)")
                if net not in net_stats:
                    net_stats[net] = {"total_length": 0.0, "segment_count": 0, "widths": [], "layers": set()}
                s = net_stats[net]
                s["total_length"] += t.get("length", 0)
                s["segment_count"] += 1
                s["widths"].append(t["width"])
                s["layers"].add(t["layer"])

            lines = [
                f"# Signal Integrity Analysis: {file_path}",
            ]

            # Auto-detect differential pairs
            diff_pairs = _detect_diff_pairs(set(net_stats.keys()))
            if diff_pairs:
                lines += [
                    "",
                    "## Differential Pair Analysis",
                    "| Pair | Net P | Net N | Length P | Length N | Delta | Status |",
                    "|------|-------|-------|----------|----------|--------|---------|",
                ]
                for pair_name, net_p, net_n in diff_pairs:
                    len_p = net_stats[net_p]["total_length"] if net_p in net_stats else 0
                    len_n = net_stats[net_n]["total_length"] if net_n in net_stats else 0
                    delta = abs(len_p - len_n)
                    if delta < 0.127:
                        status = "OK"
                    elif delta < 0.5:
                        status = "⚠️ Marginal"
                    else:
                        status = "❌ Mismatch"
                    lines.append(
                        f"| {pair_name} | {net_p} | {net_n} | {len_p:.3f} mm | "
                        f"{len_n:.3f} mm | {delta:.3f} mm | {status} |"
                    )

            # Longest signal nets
            power_keywords = ("VDD", "VCC", "GND", "VSS", "3V3", "5V", "1V8", "PGND", "VBUS")
            signal_nets = [
                (name, s) for name, s in net_stats.items()
                if not any(kw in name.upper() for kw in power_keywords)
            ]
            top_signals = sorted(signal_nets, key=lambda x: -x[1]["total_length"])[:15]
            if top_signals:
                lines += [
                    "",
                    "## Longest Signal Nets (Top 15)",
                    "| Net | Length (mm) | Segments | Layers |",
                    "|-----|-------------|----------|--------|",
                ]
                for name, s in top_signals:
                    layers = ", ".join(sorted(s["layers"]))
                    lines.append(f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | {layers} |")

            return "\n".join(lines)

        net_stats = parser.get_net_track_stats()
        rules = parser.get_design_rules()

        lines = [
            f"# Signal Integrity Analysis: {file_path}",
            "",
            f"**Diff Pair Design Rules:** width={rules['current_diff_pair_width_mm']:.4f} mm, "
            f"gap={rules['current_diff_pair_gap_mm']:.4f} mm",
        ]

        # If specific net requested, show detail
        if net_name:
            tracks = [t for t in parser.get_tracks() if t.net == net_name]
            if not tracks:
                return f"No tracks found for net '{net_name}'."
            total = sum(t.length for t in tracks)
            lines += [
                "",
                f"## Net: {net_name}",
                f"**Segments:** {len(tracks)}, **Total Length:** {total:.3f} mm",
            ]
            return "\n".join(lines)

        # Auto-detect differential pairs
        net_names = set(net_stats.keys())
        diff_pairs = _detect_diff_pairs(net_names)

        if diff_pairs:
            lines += [
                "",
                "## Differential Pair Analysis",
                "| Pair | Net P | Net N | Length P | Length N | Delta | Status |",
                "|------|-------|-------|----------|----------|--------|---------|",
            ]
            for pair_name, net_p, net_n in diff_pairs:
                len_p = net_stats[net_p]["total_length"] if net_p in net_stats else 0
                len_n = net_stats[net_n]["total_length"] if net_n in net_stats else 0
                delta = abs(len_p - len_n)
                # Length mismatch tolerance: 5 mil = 0.127 mm
                if delta < 0.127:
                    status = "OK"
                elif delta < 0.5:
                    status = "⚠️ Marginal"
                else:
                    status = "❌ Mismatch"
                lines.append(
                    f"| {pair_name} | {net_p} | {net_n} | {len_p:.3f} mm | "
                    f"{len_n:.3f} mm | {delta:.3f} mm | {status} |"
                )

        # RF traces (ANT, RF, antenna nets)
        rf_nets = [n for n in net_stats if any(
            kw in n.upper() for kw in ("ANT", "RF", "FEED")
        ) and "VDD" not in n.upper() and "GND" not in n.upper()]

        if rf_nets:
            lines += [
                "",
                "## RF Traces",
                "| Net | Length | Widths | Segments |",
                "|-----|--------|--------|----------|",
            ]
            for name in sorted(rf_nets):
                s = net_stats[name]
                widths = ", ".join(sorted({f"{w:.3f}" for w in s["widths"]}))
                lines.append(f"| {name} | {s['total_length']:.3f} mm | {widths} mm | {s['segment_count']} |")

        # Top signal nets by length (exclude power/GND)
        power_keywords = ("VDD", "VCC", "GND", "VSS", "3V3", "5V", "1V8", "PGND", "VBUS")
        signal_nets = [
            (name, s) for name, s in net_stats.items()
            if not any(kw in name.upper() for kw in power_keywords)
        ]
        top_signals = sorted(signal_nets, key=lambda x: -x[1]["total_length"])[:15]

        if top_signals:
            lines += [
                "",
                "## Longest Signal Nets (Top 15)",
                "| Net | Length (mm) | Segments | Layers |",
                "|-----|-------------|----------|--------|",
            ]
            for name, s in top_signals:
                layers = ", ".join(s["layers"])
                lines.append(f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | {layers} |")

        # Nets with most layer transitions (most vias = most transitions)
        via_stats = parser.get_via_stats()
        if via_stats["count"] > 0:
            top_via_nets = list(via_stats["top_via_nets"].items())[:10]
            lines += [
                "",
                "## Nets with Most Vias (Layer Transitions)",
                "| Net | Via Count |",
                "|-----|-----------|",
            ]
            for name, count in top_via_nets:
                lines.append(f"| {name} | {count} |")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


@mcp.tool()
@with_board_versions("file_path")
@_board_tool(": no fill state of the zones")
def analyze_pcb_power_integrity(file_path: str) -> str:
    """Give the copper zones of a board and the routing of its power nets (heuristic).

    The tool finds a power net and a ground net from its name (for example VCC, VDD,
    VBUS, GND), and gives the copper zones of each and the tracks of the power nets.

    The copper zones are the zones of the Zone Manager of KiCad: no rule area
    (keepout), no teardrop, and no zone on non-copper layers only. A zone can be on more
    than one copper layer: the tool gives all of its copper layers on the board, from
    the top, and counts each of them for the GND coverage. On a board with more than 2
    copper layers, a warning tells when the GND zones are not on each copper layer:
    this warning comes from the tool, not from a check of KiCad. "filled" is the fill
    state that the file has. With the text reader (no pcbnew), the words of the power
    nets are different (VDD, VCC, VBUS, LDO, DCDC; GND, PGND, AGND, DGND) and the
    zones have no fill state.

    Args:
        file_path: The .kicad_pcb file.

    Returns:
        The power analysis of the board.
    """
    try:
        parser = _get_parser(file_path)

        if not _is_pcbnew(parser):
            data = parser._parse_file()
            tracks = data["tracks"]
            zones = data["zones"]

            lines = [
                f"# Power Integrity Analysis: {file_path}",
            ]

            # Power zones
            power_keywords = ("VDD", "VCC", "VBUS", "LDO", "DCDC")
            gnd_keywords = ("GND", "PGND", "AGND", "DGND")

            power_zones = [z for z in zones if any(kw in z.get("net_name", "").upper() for kw in power_keywords)]
            gnd_zones = [z for z in zones if any(kw in z.get("net_name", "").upper() for kw in gnd_keywords)]

            if power_zones:
                lines += [
                    "",
                    "## Power Copper Zones",
                    "| Net | Layers |",
                    "|-----|--------|",
                ]
                for z in power_zones:
                    lines.append(f"| {z['net_name']} | {_zone_layers(z['layers'])} |")

            if gnd_zones:
                lines += [
                    "",
                    "## GND Copper Zones",
                    "| Net | Layers |",
                    "|-----|--------|",
                ]
                for z in gnd_zones:
                    lines.append(f"| {z['net_name']} | {_zone_layers(z['layers'])} |")
                gnd_layers = {layer for z in gnd_zones for layer in z["layers"]}
                lines.append(f"\n**GND Coverage:** {len(gnd_zones)} zones across {len(gnd_layers)} layers")
                lines += _gnd_layer_warning(gnd_layers, data["general"]["layers"])

            # Power net tracks
            net_stats: dict[str, dict] = {}
            for t in tracks:
                net = t.get("net", "")
                if not net:
                    continue
                if net not in net_stats:
                    net_stats[net] = {"total_length": 0.0, "segment_count": 0, "widths": []}
                net_stats[net]["total_length"] += t.get("length", 0)
                net_stats[net]["segment_count"] += 1
                net_stats[net]["widths"].append(t["width"])

            power_tracks = [(n, s) for n, s in net_stats.items() if any(kw in n.upper() for kw in power_keywords)]
            if power_tracks:
                lines += [
                    "",
                    "## Power Net Track Routing",
                    "| Net | Length (mm) | Segments | Min Width | Max Width |",
                    "|-----|-------------|----------|-----------|-----------|",
                ]
                for name, s in sorted(power_tracks, key=lambda x: -x[1]["total_length"]):
                    lines.append(
                        f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | "
                        f"{min(s['widths']):.4f} mm | {max(s['widths']):.4f} mm |"
                    )

            return "\n".join(lines)

        net_stats = parser.get_net_track_stats()
        zones = parser.get_zones()
        rules = parser.get_design_rules()

        lines = [
            f"# Power Integrity Analysis: {file_path}",
            "",
            f"**Board:** {rules['copper_layers']} layers, {rules['board_thickness_mm']:.3f} mm thick",
        ]

        # Identify power nets
        power_keywords = ("VDD", "VCC", "VSS", "3V3", "5V", "1V8", "VBUS", "LDO", "DCDC")
        gnd_keywords = ("GND", "PGND", "AGND", "DGND", "CHASSIS")

        power_zones = [z for z in zones if any(kw in z.net_name.upper() for kw in power_keywords)]
        gnd_zones = [z for z in zones if any(kw in z.net_name.upper() for kw in gnd_keywords)]

        # Power zones by net
        power_zone_by_net: dict[str, list[str]] = {}
        for z in power_zones:
            power_zone_by_net.setdefault(z.net_name, []).append(
                f"{_zone_layers(z.layers)} ({'filled' if z.filled else 'unfilled'})"
            )

        if power_zone_by_net:
            lines += [
                "",
                "## Power Copper Zones",
                "| Net | Zones | Layers |",
                "|-----|-------|--------|",
            ]
            for net, layer_info in sorted(power_zone_by_net.items()):
                layers_str = "; ".join(layer_info)
                lines.append(f"| {net} | {len(layer_info)} | {layers_str} |")

        # GND zones
        gnd_zone_by_net: dict[str, list[str]] = {}
        for z in gnd_zones:
            gnd_zone_by_net.setdefault(z.net_name, []).append(
                f"{_zone_layers(z.layers)} ({'filled' if z.filled else 'unfilled'})"
            )

        if gnd_zone_by_net:
            lines += [
                "",
                "## GND Copper Zones",
                "| Net | Zones | Layers |",
                "|-----|-------|--------|",
            ]
            for net, layer_info in sorted(gnd_zone_by_net.items()):
                layers_str = "; ".join(layer_info)
                lines.append(f"| {net} | {len(layer_info)} | {layers_str} |")

            total_gnd_zones = len(gnd_zones)
            filled_gnd_zones = sum(1 for z in gnd_zones if z.filled)
            gnd_layers = {layer for z in gnd_zones for layer in z.layers}
            lines += [
                "",
                f"**GND Coverage:** {total_gnd_zones} zones across {len(gnd_layers)} layers "
                f"({filled_gnd_zones} filled)",
            ]
            lines += _gnd_layer_warning(gnd_layers, rules["copper_layers"])

        # Power net track routing
        power_track_nets = [
            (name, s) for name, s in net_stats.items()
            if any(kw in name.upper() for kw in power_keywords)
        ]
        if power_track_nets:
            lines += [
                "",
                "## Power Net Track Routing",
                "| Net | Length (mm) | Segments | Min Width | Max Width |",
                "|-----|-------------|----------|-----------|-----------|",
            ]
            for name, s in sorted(power_track_nets, key=lambda x: -x[1]["total_length"]):
                min_w = min(s["widths"]) if s["widths"] else 0
                max_w = max(s["widths"]) if s["widths"] else 0
                lines.append(
                    f"| {name} | {s['total_length']:.2f} | {s['segment_count']} | "
                    f"{min_w:.4f} mm | {max_w:.4f} mm |"
                )

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing PCB")


# ── Helpers ──────────────────────────────────────────────────


# The warning of find_tracks_by_net for a net with tracks of different widths.
_MIXED_WIDTHS = (
    "⚠️ The tracks of the net have different widths. A manual change of the routing or "
    "of the design rules can be the cause. This warning comes from the tool, not from "
    "a DRC."
)


def _net_items_answer(
    net_name: str, tracks: list[PCBTrack], vias: list[PCBVia], zones: list[PCBZone]
) -> str:
    """The answer of find_tracks_by_net for the items of one net."""
    total_length = sum(t.length for t in tracks)
    widths = sorted({f"{t.width:.4f}" for t in tracks})
    layers = sorted({t.layer for t in tracks})
    lines = [
        f"# Track Analysis: {net_name}",
        "",
        f"**Track Segments:** {len(tracks)}",
        f"**Vias:** {len(vias)}",
        f"**Copper Zones:** {len(zones)}",
        f"**Total Track Length:** {total_length:.3f} mm",
        f"**Track Layers:** {', '.join(layers) if layers else 'N/A'}",
    ]
    if widths:
        lines.append(f"**Track Widths:** {', '.join(widths)} mm")
        if len(widths) > 1:
            lines.append(_MIXED_WIDTHS)
    if tracks:
        lines += [
            "",
            "## Track Segments",
            "| # | Start | End | Width | Layer | Length |",
            "|---|-------|-----|-------|-------|--------|",
        ]
        for i, t in enumerate(tracks[:50], 1):
            lines.append(
                f"| {i} | ({t.start[0]:.2f}, {t.start[1]:.2f}) | "
                f"({t.end[0]:.2f}, {t.end[1]:.2f}) | {t.width:.4f} | "
                f"{t.layer} | {t.length:.3f} |"
            )
        if len(tracks) > 50:
            lines.append(f"| ... | ({len(tracks) - 50} more segments) | | | | |")
    if vias:
        lines += [
            "",
            "## Vias",
            "| # | Position | Size | Drill | Span |",
            "|---|----------|------|-------|------|",
        ]
        for i, v in enumerate(vias[:20], 1):
            lines.append(
                f"| {i} | ({v.position[0]:.2f}, {v.position[1]:.2f}) | "
                f"{v.size:.3f} | {v.drill:.3f} | {v.top_layer} -> {v.bottom_layer} |"
            )
        if len(vias) > 20:
            lines.append(f"| ... | ({len(vias) - 20} more vias) | | | |")
    if zones:
        # One reader gives the fill state of each zone, or of none (the text reader).
        fill = zones[0].filled is not None
        lines += [
            "",
            "## Copper Zones",
            "| Net | Layers | Filled |" if fill else "| Net | Layers |",
            "|-----|--------|--------|" if fill else "|-----|--------|",
        ]
        for z in zones:
            row = f"| {z.net_name} | {_zone_layers(z.layers)} |"
            lines.append(row + (f" {'Yes' if z.filled else 'No'} |" if fill else ""))
    return "\n".join(lines)


def _zone_layers(layers: list[str]) -> str:
    """The copper layers of a zone (get_zones) for a table cell."""
    return ", ".join(layers) if layers else "no copper layer of the board"


def _gnd_layer_warning(gnd_layers: set[str], copper_layers: int) -> list[str]:
    """The warning of analyze_pcb_power_integrity for a board with more than 2 copper
    layers when the GND zones are not on each copper layer."""
    if copper_layers <= 2 or len(gnd_layers) >= copper_layers:
        return []
    return [
        f"⚠️ GND zones only on {len(gnd_layers)} of {copper_layers} copper layers. A GND "
        "zone on each inner layer can decrease the EMI of the board."
    ]


def _similar_nets(net_name: str, tracks: list[str], vias: list[str], zones: list[str]) -> str:
    """The answer of find_tracks_by_net for a net with no tracks, vias or copper zones:
    the nets whose name contains the name (no difference between upper and lower case),
    with the count of each item. The arguments are the nets of the items."""
    names = sorted({name for name in (*tracks, *vias, *zones) if name})
    similar = [name for name in names if net_name.lower() in name.lower()]
    if not similar:
        return f"No tracks, vias or copper zones found for net '{net_name}'."
    return (
        f"# Tracks for '{net_name}'\n\n"
        "Exact match not found. Similar nets:\n"
        + "\n".join(
            f"- `{name}` ({tracks.count(name)} segments, {vias.count(name)} vias, "
            f"{zones.count(name)} copper zones)"
            for name in similar
        )
    )


def _detect_diff_pairs(net_names: set[str]) -> list[tuple[str, str, str]]:
    """Auto-detect differential pairs from net names.

    Returns list of (pair_name, net_p, net_n) tuples.
    """
    pairs = []
    seen = set()

    # Common suffix patterns: _P/_N, _DP/_DN, _TXP/_TXN, etc.
    suffix_pairs = [
        ("_P", "_N"), ("_DP", "_DN"), ("_TXP", "_TXN"), ("_RXP", "_RXN"),
        ("_M", "_S"), ("_POS", "_NEG"),
    ]

    for suffix_p, suffix_n in suffix_pairs:
        for name in net_names:
            if name.endswith(suffix_p):
                candidate_n = name[:-len(suffix_p)] + suffix_n
                if candidate_n in net_names and name not in seen and candidate_n not in seen:
                    pair_name = name[:-len(suffix_p)]
                    pairs.append((pair_name, name, candidate_n))
                    seen.add(name)
                    seen.add(candidate_n)

    # Also check for common keywords like USB_C_N/USB_C_P
    keywords = ["USB", "DP", "HDMI", "CSI", "DSI", "SPI", "UART", "I2C"]
    for kw in keywords:
        p_candidates = [n for n in net_names if kw in n.upper() and n.endswith("_P") and n not in seen]
        for p in p_candidates:
            base = p[:-2]
            n_candidate = base + "_N"
            if n_candidate in net_names and n_candidate not in seen:
                pairs.append((base, p, n_candidate))
                seen.add(p)
                seen.add(n_candidate)

    return pairs
