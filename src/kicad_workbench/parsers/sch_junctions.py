"""Junctions as the schematic editor of KiCad adds them for a new wire or symbol.

A wire end or a pin in the middle of a wire connects to the wire only through a
junction. The schematic editor adds the junctions after a wire tool run
(``SCH_LINE_WIRE_BUS_TOOL::finishSegments``) and after the placement of a
symbol (``SCH_DRAWING_TOOLS::PlaceSymbol``). The rules here are those of KiCad
8.0.9, 9.0.9 and 10.0.6:

- The candidate points: the ends of the new segments and each connection point
  of the sheet on a new segment, or the pins of a new symbol.
- A point needs a junction when at least three directions of wires (or of
  buses) meet there (``SCH_SCREEN::doIsJunction`` of KiCad 8,
  ``JUNCTION_HELPERS::AnalyzePoint`` of KiCad 9 and 10).
- ``AddJunction`` adds the junction and splits each wire and bus that has the
  point in its middle (``BreakSegments``).

The editor also changes the new wires before the decision, with steps that
this module does for the new items only: it merges the consecutive collinear
segments of the run (``simplifyWireList``), merges a new segment with a
collinear wire that it touches or overlaps (``CleanUp``), and removes the part
of a new wire between the two pins of a symbol with at most two pins
(``TrimWire``). For a new symbol it removes the part of a wire between two of
its pins (``TrimOverLappingWires``).

Two inputs of KiCad are not in the file: the box of a symbol (KiCad finds a
hidden pin only when the pin is in the box of the visible items) and the pen
width of a bus entry from its net class (KiCad 10 uses the box of a bus
entry). The module computes the decision with and without these items. When
the two decisions are not the same, it raises UnknownJunctionError.
"""

import itertools
import math
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..errors import InputError
from .lib_symbols import LibraryPin, body_points, library_pins
from .sch_geometry import (
    IU_PER_MIL,
    LABEL_HEADS,
    ConnectionPoints,
    Point,
    body_style,
    definition_key,
    definitions,
    iu,
    mm_text,
    node_point,
    on_segment,
    placed_symbols,
    placed_units,
    placement,
)
from .sexpr import Node

# The unique angles of pins and bus entries start above all real angles.
_UNIQUE_ANGLE = 10000


class UnknownJunctionError(InputError):
    """The decision depends on data that is not in the file."""


# ---------------------------------------------------------------------------
# Geometry (libs/kimath/src/trigo.cpp, eeschema/sch_line.cpp)
# ---------------------------------------------------------------------------


def _where(point: Point) -> str:
    return f"({mm_text(point[0])}, {mm_text(point[1])})"


def _hit(start: Point, end: Point, point: Point) -> bool:
    """SCH_LINE::HitTest(point, -1): a tolerance of 1 IU, without the pen width."""
    if point in (start, end):
        return True
    distance = 1
    x, y = point
    xmin, xmax = sorted((start[0], end[0]))
    ymin, ymax = sorted((start[1], end[1]))
    if ymin - y > distance or y - ymax > distance:
        return False
    if xmin - x > distance or x - xmax > distance:
        return False
    if start[0] == end[0] and ymin < y < ymax:
        return abs(start[0] - x) <= distance
    if start[1] == end[1] and xmin < x < xmax:
        return abs(start[1] - y) <= distance
    return _squared_distance(start, end, point) < (distance + 1) ** 2


def _squared_distance(start: Point, end: Point, point: Point) -> float:
    """SEG::SquaredDistance of a point."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    px, py = point[0] - start[0], point[1] - start[1]
    length = dx * dx + dy * dy
    if length == 0:
        return px * px + py * py
    t = (px * dx + py * dy) / length
    if t <= 0:
        return px * px + py * py
    if t >= 1:
        qx, qy = point[0] - end[0], point[1] - end[1]
        return qx * qx + qy * qy
    nx, ny = start[0] + t * dx - point[0], start[1] + t * dy - point[1]
    return nx * nx + ny * ny


def _angle(vector: tuple[int, int]) -> int:
    """KiROUND(EDA_ANGLE(vector).AsDegrees()), with the Y axis down."""
    x, y = vector
    if x == 0 and y == 0:
        degrees = 0.0
    elif y == 0:
        degrees = 0.0 if x >= 0 else -180.0
    elif x == 0:
        degrees = 90.0 if y >= 0 else -90.0
    elif x == y:
        degrees = 45.0 if x >= 0 else -135.0
    elif x == -y:
        degrees = -45.0 if x >= 0 else 135.0
    else:
        degrees = math.degrees(math.atan2(y, x))
    return int(math.copysign(math.floor(abs(degrees) + 0.5), degrees))


@dataclass
class Line:
    """A wire or a bus of a sheet."""

    start: Point
    end: Point
    bus: bool
    stroke: tuple  # (width, style, color) for IsStrokeEquivalent.
    uuid: str
    new: bool = False  # A line of the call.
    node: Node | None = None  # The (wire ...) or (bus ...) of the file.
    changed: bool = False
    stroke_text: str = "(stroke (width 0) (type default))"
    original: tuple[Point, Point] | None = None  # The ends in the file.

    @property
    def edited_in_file(self) -> bool:
        """True for a line of the file that gets new ends."""
        return self.node is not None and self.changed and self.moved

    @property
    def moved(self) -> bool:
        """True if the ends are not the ends of the file (or the line is not of the file)."""
        return self.original is None or {self.start, self.end} != set(self.original)

    def angle_from(self, point: Point) -> int:
        """SCH_LINE::GetAngleFrom."""
        other = self.end if point == self.start else self.start
        return _angle((other[0] - point[0], other[1] - point[1]))

    def reverse_angle_from(self, point: Point) -> int:
        """SCH_LINE::GetReverseAngleFrom."""
        other = self.start if point == self.end else self.end
        return _angle((other[0] - point[0], other[1] - point[1]))

    @property
    def null(self) -> bool:
        return self.start == self.end


def _less(a: Point, b: Point) -> bool:
    return a[0] < b[0] or (a[0] == b[0] and a[1] < b[1])


def _sorted_ends(line: Line) -> tuple[Point, Point]:
    return (line.end, line.start) if _less(line.end, line.start) else (line.start, line.end)


def merge_overlap(
    this: Line, other: Line, is_junction: Callable[[Point], bool] | None = None
) -> Line | None:
    """SCH_LINE::MergeOverlap(screen, other, checkJunctions): the merged line or None.

    The result is a copy of other (its UUID) from the lowest to the highest
    end of the two lines. is_junction: the junction test of a touch point
    (checkJunctions), or None.
    """
    if this is other or this.bus != other.bus:
        return None
    left = _sorted_ends(other)
    right = _sorted_ends(this)
    if _less(right[0], left[0]):
        left, right = right, left
    first = right
    if _less(right[1], left[1]):
        right = left
    if _less(left[1], first[0]):
        return None
    if left == first:
        return replace(other, start=left[0], end=left[1], changed=True)
    (lx1, ly1), (lx2, ly2) = left
    (ox1, oy1), (ox2, oy2) = first
    if ly1 == ly2 and oy1 == oy2:  # Two horizontal lines.
        collinear = ly1 == oy1
    elif lx1 == lx2 and ox1 == ox2:  # Two vertical lines.
        collinear = lx1 == ox1
    else:
        dx, dy = lx2 - lx1, ly2 - ly1
        collinear = (ox1 - lx1) * dy - (oy1 - ly1) * dx == 0 and (ox2 - lx1) * dy - (
            oy2 - ly1
        ) * dx == 0
    if not collinear:
        return None
    if left[1] == first[0] and is_junction is not None and is_junction(left[1]):
        return None
    return replace(other, start=left[0], end=right[1], changed=True)


def _parallel(a: Line, b: Line) -> bool:
    """SCH_LINE::IsParallel: the cross product of the directions is zero."""
    ax, ay = a.end[0] - a.start[0], a.end[1] - a.start[1]
    bx, by = b.end[0] - b.start[0], b.end[1] - b.start[1]
    return ax * by - ay * bx == 0


# ---------------------------------------------------------------------------
# The items of a sheet
# ---------------------------------------------------------------------------


@dataclass
class SymbolPins:
    """The pins of a placed symbol for the junction rules.

    A sheet that the hierarchy uses more than one time can show an other unit
    for each instance, and the file does not tell which instance the user
    edits. KiCad finds a hidden pin only when it is in the box of the symbol.
    """

    # GetConnectionPoints for each unit that the sheet can show: all pins, also
    # hidden and no_connect pins.
    unit_points: dict[int, list[Point]]
    # doIsConnected is true there for each unit, and KiCad finds the symbol there.
    connected: set[Point]
    # doIsConnected is true there only for some units, or KiCad finds the
    # symbol only when its box holds a hidden pin there.
    uncertain: set[Point]
    # The hidden pins (all types) outside the box of the body and the visible pins.
    hidden_outside: set[Point] = field(default_factory=set)

    @property
    def points(self) -> list[Point]:
        """The connection points of all units, in their order."""
        return list(dict.fromkeys(p for points in self.unit_points.values() for p in points))


@dataclass
class Label:
    kind: str  # "label", "global_label", "hierarchical_label" or "directive_label"
    position: Point
    text: str


@dataclass
class Entry:
    """A bus wire entry: its two ends and its stroke width in IU (0: of the net class)."""

    position: Point
    end: Point
    width: int


@dataclass
class Sheet:
    """The items of a sheet that the junction rules use."""

    lines: list[Line]
    junctions: list[Point]
    entries: list[Entry]
    symbols: list[SymbolPins]
    sheet_pins: list[list[Point]]
    labels: list[Label]
    no_connects: list[Point]
    graphic_ends: list[Point]  # The ends of graphic lines (GetConnectionPoints, no connection).
    # The lowest and the highest wire width of the net classes of the project.
    wire_widths: tuple[int, int] = (6 * IU_PER_MIL, 6 * IU_PER_MIL)
    added_junctions: list[Point] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)
    # The (junction ...) of the file by position, and those that the changes remove.
    junction_nodes: dict[Point, list[Node]] = field(default_factory=dict)
    removed_junctions: list[Node] = field(default_factory=list)
    # KiCad 10: the (members ...) of the (group ...) of each grouped line by UUID, and
    # the UUIDs that the changes add to or remove from it.
    groups: dict[str, Node] = field(default_factory=dict)
    group_changes: list[tuple[Node, str, bool]] = field(default_factory=list)

    def connection_points(self, label: bool = False) -> ConnectionPoints:
        """Return the points where a new wire, symbol or label connects.

        KiCad 8.0.9, 9.0.9 and 10.0.6 connect a pin or a wire end in the middle
        of a wire only through a junction, which the tools add as the schematic
        editor does. A label connects anywhere on a wire or a bus. A wire or a
        pin connects to a bus only through a bus entry. The ends of graphic
        lines do not connect.

        Args:
            label: The points for a new label: also the ends and the segments of
                the buses.
        """
        points: set[Point] = set()
        segments = []
        for line in self.lines:
            if line.bus and not label:
                continue
            points.update((line.start, line.end))
            segments.append((line.start, line.end))
        points.update(self.junctions)
        for entry in self.entries:
            points.update((entry.position, entry.end))
        for symbol in self.symbols:
            points.update(symbol.points)
        for pins in self.sheet_pins:
            points.update(pins)
        points.update(label.position for label in self.labels)
        points.update(self.no_connects)
        return ConnectionPoints(points, segments)

    def connections(self) -> list[Point]:
        """SCH_SCREEN::GetConnections: the connection points of all items, sorted, unique:
        the points of connection_points with the buses, and the ends of graphic lines."""
        return sorted(self.connection_points(label=True).points | set(self.graphic_ends))


def _stroke(node: Node) -> tuple:
    stroke = node.first("stroke")
    width = stroke.first("width") if stroke else None
    style = stroke.first("type") if stroke else None
    color = stroke.first("color") if stroke else None
    style_name = style.value(0, "default") if style else "default"
    # No color is COLOR4D::UNSPECIFIED, (0, 0, 0, 0).
    rgba = tuple(color.number(i, 0.0) for i in range(4)) if color else (0.0, 0.0, 0.0, 0.0)
    return (
        iu(width.number(0, 0.0)) if width else 0,
        "solid" if style_name in ("default", "solid") else style_name,
        rgba,
    )


def _ends(node: Node) -> list[Point]:
    pts = node.first("pts")
    return [node_point(xy) for xy in pts.children("xy")] if pts else []


def read_sheet(root: Node, wire_widths: tuple[int, int] | None = None) -> Sheet:
    """Return the items of a sheet (the root of a .kicad_sch file).

    Raises:
        InputError: A position is not a number, or KiCad cannot read a symbol.
        KicadParseError: KiCad cannot read the name of a sub-symbol.
        LocaleNumberError: A number of such a name depends on the locale of KiCad.
    """
    lines = []
    for head in ("wire", "bus"):
        for node in root.children(head):
            ends = _ends(node)
            if len(ends) == 2:
                uuid_node = node.first("uuid")
                stroke = node.first("stroke")
                lines.append(
                    Line(
                        ends[0],
                        ends[1],
                        head == "bus",
                        _stroke(node),
                        uuid_node.value(0, "") if uuid_node else "",
                        node=node,
                        original=(ends[0], ends[1]),
                        stroke_text=node.source[stroke.start : stroke.end]
                        if stroke
                        else Line.stroke_text,
                    )
                )
    no_connects = [
        node_point(at) for item in root.children("no_connect") if (at := item.first("at"))
    ]
    graphic_ends = []
    for node in root.children("polyline"):
        ends = _ends(node)
        if len(ends) == 2:  # A graphic SCH_LINE.
            graphic_ends.extend(ends)
    entries = []
    for node in root.children("bus_entry"):
        at, size = node.first("at"), node.first("size")
        if at is None:
            continue
        x, y = node_point(at)
        dx, dy = node_point(size) if size is not None else (0, 0)
        entries.append(Entry((x, y), (x + dx, y + dy), _stroke(node)[0]))
    defs = definitions(root)
    symbols = []
    for symbol in placed_symbols(root):
        definition = defs.get(definition_key(symbol))
        if definition is not None:
            symbols.append(junction_symbol(symbol, definition))
    sheet_pins = [
        [node_point(at) for pin in sheet.children("pin") if (at := pin.first("at"))]
        for sheet in root.children("sheet")
    ]
    labels = []
    for kind in LABEL_HEADS:
        for node in root.children(kind):
            if (at := node.first("at")) is not None:
                name = "directive_label" if kind == "netclass_flag" else kind
                labels.append(Label(name, node_point(at), node.value(0, "")))
    junctions = []
    junction_nodes: dict[Point, list[Node]] = {}
    for item in root.children("junction"):
        if (at := item.first("at")) is not None:
            junctions.append(node_point(at))
            junction_nodes.setdefault(node_point(at), []).append(item)
    sheet = Sheet(lines, junctions, entries, symbols, sheet_pins, labels, no_connects, graphic_ends)
    sheet.junction_nodes = junction_nodes
    for group in root.children("group"):
        members = group.first("members")
        for member in members.atoms() if members is not None else []:
            sheet.groups[member.value] = members
    if wire_widths is not None:
        sheet.wire_widths = wire_widths
    return sheet


def _box(points: list[Point]) -> tuple[int, int, int, int] | None:
    if not points:
        return None
    xs = [x for x, _y in points]
    ys = [y for _x, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(box: tuple[int, int, int, int] | None, point: Point) -> bool:
    return box is not None and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def junction_symbol(symbol: Node, definition: Node) -> SymbolPins:
    """Return the pins of a placed symbol for the junction rules.

    For each unit of the symbol (placed_units), the box of the body graphics
    and of the visible pins is inside the box that KiCad uses for the R-tree
    query (lib_symbols.body_points). A hidden pin in that box is found, a
    hidden pin outside it is uncertain.

    Raises:
        InputError: A position is not a number, or KiCad cannot read the symbol.
        KicadParseError: KiCad cannot read the name of a sub-symbol.
        LocaleNumberError: A number of such a name depends on the locale of KiCad.
    """
    where = placement(symbol)
    style = body_style(symbol)
    unit_points = {}
    found, possible = [], []
    hidden_outside: set[Point] = set()
    for unit in sorted(placed_units(symbol)):
        pins = library_pins(definition, style, {unit})
        positions = [where.sheet_position(pin) for pin in pins]
        unit_points[unit] = positions
        body = [
            where.sheet_position(LibraryPin("", "", x, y))
            for x, y in body_points(definition, style, {unit})
        ]
        box = _box(body + [pos for pin, pos in zip(pins, positions, strict=True) if not pin.hidden])
        certain, maybe = set(), set()
        for pin, pos in zip(pins, positions, strict=True):
            if pin.hidden and not _inside(box, pos):
                hidden_outside.add(pos)
            if pin.type == "no_connect":
                continue
            (certain if not pin.hidden or _inside(box, pos) else maybe).add(pos)
        found.append(certain)
        possible.append(certain | maybe)
    connected = set.intersection(*found) if found else set()
    uncertain = (set.union(*possible) if possible else set()) - connected
    return SymbolPins(unit_points, connected, uncertain, hidden_outside)


# ---------------------------------------------------------------------------
# The decision for one point
# ---------------------------------------------------------------------------


@dataclass
class _Info:
    is_junction: bool = False
    has_dot: bool = False
    has_bus_entry: bool = False
    to_multiple_wires: bool = False
    to_multiple_buses: bool = False


def _in_box(point: Point, entry: Entry, inflate: int) -> bool:
    x1, x2 = sorted((entry.position[0], entry.end[0]))
    y1, y2 = sorted((entry.position[1], entry.end[1]))
    return x1 - inflate <= point[0] <= x2 + inflate and y1 - inflate <= point[1] <= y2 + inflate


def _entry_in_query(point: Point, entry: Entry, widths: tuple[int, int]) -> bool | None:
    """Return True if the R-tree query of KiCad 10 at the point finds the bus entry.

    The box is the entry rectangle, inflated by the pen width / 2 + 1, and by
    the pen width again in the R-tree. None: the pen width of the net class
    decides, and the file does not tell the net class.
    """
    if point in (entry.position, entry.end):
        return True
    if entry.width > 0:
        return _in_box(point, entry, entry.width // 2 + 1 + entry.width)
    low, high = widths
    if _in_box(point, entry, low // 2 + 1 + low):
        return True
    if not _in_box(point, entry, high // 2 + 1 + high):
        return False
    return None


def _analyze(
    sheet: Sheet,
    point: Point,
    kicad: int,
    is_bus_label: Callable[[str], bool],
    symbols_found: bool,
    entries_found: bool,
) -> _Info:
    """doIsJunction (KiCad 8) or AnalyzePoint (KiCad 9 and 10) with breakCrossings false.

    symbols_found: KiCad finds the symbols that are uncertain at the point.
    entries_found: KiCad 10 finds the bus entries whose box is not decided.
    """
    info = _Info()
    wires, buses = 0, 1
    break_lines = [False, False]
    exits: list[set[int]] = [set(), set()]
    mid_lines: list[list[Line]] = [[], []]
    unique = itertools.count(_UNIQUE_ANGLE)
    lines = [line for line in sheet.lines if _hit(line.start, line.end, point)]
    bus_entry_bus_angles = 0
    bus_end_segments = 0

    if point in sheet.junctions:
        info.has_dot = True
    symbols = [
        symbol
        for symbol in sheet.symbols
        if point in symbol.connected or (symbols_found and point in symbol.uncertain)
    ]
    if kicad >= 10:
        found_entries = []
        for entry in sheet.entries:
            in_query = _entry_in_query(point, entry, sheet.wire_widths)
            if in_query or (in_query is None and entries_found):
                info.has_bus_entry = True
                found_entries.append(entry)
        labels = [label for label in sheet.labels if label.position == point]
        sheets = [pins for pins in sheet.sheet_pins if point in pins]
        # AnalyzePoint returns early with fewer than two items at the point. Fewer
        # than two items give fewer than three exits, thus the result is the same.
        stub = [set(), set()]
        for line in lines:
            if not line.null and point in (line.start, line.end):
                stub[buses if line.bus else wires].add(line.angle_from(point))
        keep_stub = len(stub[wires]) >= 3 or len(stub[buses]) >= 3
        if not (info.has_dot or keep_stub):
            merged = list(lines)
            while True:
                for i, j in itertools.combinations(range(len(merged)), 2):
                    result = merge_overlap(merged[i], merged[j])
                    if result is not None:
                        merged[i] = result
                        del merged[j]
                        break
                else:
                    break
            lines = merged
    else:
        found_entries = [e for e in sheet.entries if point in (e.position, e.end)]
        labels = []
        sheets = [pins for pins in sheet.sheet_pins if point in pins]

    for line in lines:
        if line.null:
            continue
        layer = buses if line.bus else wires
        if point in (line.start, line.end):
            break_lines[layer] = True
            exits[layer].add(line.angle_from(point))
            if layer == buses:
                bus_end_segments += 1
        elif _hit(line.start, line.end, point):
            mid_lines[layer].append(line)
    for entry in found_entries:
        if point in (entry.position, entry.end):
            break_lines[buses] = break_lines[wires] = True
            exits[buses].add(next(unique))
            exits[wires].add(next(unique))
            bus_entry_bus_angles += 1
            info.has_bus_entry = True
    for _symbol in symbols:
        break_lines[wires] = True
        exits[wires].add(next(unique))
    for _pins in sheets:
        break_lines[wires] = True
        exits[wires].add(next(unique))
    for label in labels:
        if label.kind == "label" and is_bus_label(label.text):
            break_lines[buses] = True
        elif label.kind != "directive_label":
            break_lines[wires] = True
    for layer in (wires, buses):
        if break_lines[layer]:
            for line in mid_lines[layer]:
                exits[layer].add(line.angle_from(point))
                exits[layer].add(line.reverse_angle_from(point))
    if info.has_bus_entry and kicad >= 9:
        info.to_multiple_wires = len(exits[wires]) > 2 and len(exits[buses]) == 1
        if kicad >= 10:
            real_buses = len(exits[buses]) - bus_entry_bus_angles
            info.to_multiple_buses = real_buses >= 3 and bus_end_segments >= 1
    info.is_junction = len(exits[wires]) >= 3 or len(exits[buses]) >= 3
    return info


def _explicit(info: _Info, kicad: int) -> bool:
    """IsExplicitJunction (KiCad 10: AllowsExplicitJunction)."""
    if kicad <= 8:
        return info.is_junction and not info.has_bus_entry
    return info.is_junction and (
        not info.has_bus_entry or info.to_multiple_wires or info.to_multiple_buses
    )


def _needed(info: _Info, kicad: int) -> bool:
    """IsExplicitJunctionNeeded."""
    return _explicit(info, kicad) and not info.has_dot


def _decide(
    sheet: Sheet,
    point: Point,
    kicad: int,
    is_bus_label: Callable[[str], bool],
    test: Callable[[_Info, int], bool],
) -> bool:
    """Return the test of the analysis at the point, for each value of the unknown inputs.

    Raises:
        UnknownJunctionError: The test depends on a symbol that is uncertain at the
            point (SymbolPins), or on the pen width of a bus entry.
    """
    symbols = any(point in symbol.uncertain for symbol in sheet.symbols)
    entries = kicad >= 10 and any(
        _entry_in_query(point, entry, sheet.wire_widths) is None for entry in sheet.entries
    )

    def results(found_values: tuple[bool, ...], entry_values: tuple[bool, ...]) -> set[bool]:
        return {
            test(_analyze(sheet, point, kicad, is_bus_label, found, entry), kicad)
            for found in found_values
            for entry in entry_values
        }

    symbol_values = (False, True) if symbols else (False,)
    entry_values = (False, True) if entries else (False,)
    found = results(symbol_values, entry_values)
    if len(found) > 1:
        reasons = []
        if any(len(results(symbol_values, (entry,))) > 1 for entry in entry_values):
            reasons.append(
                "a symbol there (the sheet can show an other unit for each instance of the "
                "sheet, or the pin is hidden and KiCad uses the box of the symbol)"
            )
        if any(len(results((value,), entry_values)) > 1 for value in symbol_values):
            reasons.append("the pen width of a bus entry from its net class")
        x, y = point
        raise UnknownJunctionError(
            f"the junction rule of KiCad at {_where((x, y))} depends on "
            f"{' and on '.join(reasons)}. The file does not give this."
        )
    return found.pop()


def junction_needed(
    sheet: Sheet, point: Point, kicad: int, is_bus_label: Callable[[str], bool]
) -> bool:
    """SCH_SCREEN::IsExplicitJunctionNeeded of the KiCad version (8, 9 or 10)."""
    return _decide(sheet, point, kicad, is_bus_label, _needed)


def is_junction(
    sheet: Sheet, point: Point, kicad: int, is_bus_label: Callable[[str], bool]
) -> bool:
    """SCH_SCREEN::IsJunction of the KiCad version (8, 9 or 10)."""
    return _decide(sheet, point, kicad, is_bus_label, lambda info, _kicad: info.is_junction)


# ---------------------------------------------------------------------------
# The changes: junctions, breaks, merges, trims
# ---------------------------------------------------------------------------


def _new_uuid() -> str:
    return str(uuidlib.uuid4())


def break_segments(sheet: Sheet, point: Point) -> None:
    """BreakSegments: split each wire and bus that has the point in its middle
    (SCH_LINE::BreakAt)."""
    for line in list(sheet.lines):
        if point not in (line.start, line.end) and on_segment(point, (line.start, line.end)):
            _break_at(sheet, line, point)


def add_junction(sheet: Sheet, point: Point) -> None:
    """AddJunction: the junction, then BreakSegments."""
    sheet.junctions.append(point)
    sheet.added_junctions.append(point)
    break_segments(sheet, point)


def simplify_wire_list(segments: list[Line]) -> list[Line]:
    """simplifyWireList: one pass that merges each segment with the next collinear one."""
    result = list(segments)
    index = 0
    while index < len(result):
        line = result[index]
        if line.null:
            del result[index]
            continue
        if index + 1 < len(result):
            merged = merge_overlap(line, result[index + 1])
            if merged is not None:
                result[index : index + 2] = [merged]
        index += 1
    return result


def _remove(sheet: Sheet, line: Line) -> None:
    """Remove a line from the sheet. A line of the file goes from the file."""
    sheet.lines.remove(line)
    if line.node is not None:
        sheet.removed.append(line)
    # The commit removes the line from its group (SCH_COMMIT::pushSchEdit).
    if line.uuid in sheet.groups:
        sheet.group_changes.append((sheet.groups.pop(line.uuid), line.uuid, False))


def _can_merge(first: Line, second: Line) -> bool:
    """The tests of CleanUp before MergeOverlap: first is before second in the order of
    the lowest X, the two lines are on the same layer, have the same stroke
    (IsStrokeEquivalent), are parallel, and their X and Y ranges overlap."""
    if first.bus != second.bus or first.stroke != second.stroke:
        return False
    if first.null or second.null or not _parallel(first, second):
        return False
    first_x = sorted((first.start[0], first.end[0]))
    second_x = sorted((second.start[0], second.end[0]))
    # KiCad sorts by the lowest X (std::sort, not stable). With the same lowest X
    # the order is not decided: then the line of the file is first and keeps its UUID.
    if first_x[0] > second_x[0] or (first_x[0] == second_x[0] and first.new and not second.new):
        return False
    if second_x[0] > first_x[1]:
        return False
    first_y = sorted((first.start[1], first.end[1]))
    second_y = sorted((second.start[1], second.end[1]))
    return not (first_y[1] < second_y[0] or second_y[1] < first_y[0])


def merge_lines(
    sheet: Sheet,
    kicad: int,
    is_bus_label: Callable[[str], bool],
    active: Callable[[Line], bool],
) -> None:
    """CleanUp step d for the active lines: merge each active line with a line on the
    same straight line that it touches or overlaps.

    A line with the two ends of an other line goes. A touching merge happens
    only when the touch point is not a junction (MergeOverlap with
    checkJunctions). The merged line is a copy of the first line of the pair.

    Raises:
        UnknownJunctionError: The junction test of a touch point depends on data
            that is not in the file.
    """

    def junction(point: Point) -> bool:
        return is_junction(sheet, point, kicad, is_bus_label)

    while True:
        for line in [line for line in sheet.lines if active(line)]:
            if _merge_one(sheet, line, junction):
                break
        else:
            return


def _merge_one(sheet: Sheet, line: Line, junction: Callable[[Point], bool]) -> bool:
    """Merge the line with the first line that CleanUp merges it with; True if it did."""
    for other in sheet.lines:
        if other is line:
            continue
        for first, second in ((line, other), (other, line)):
            if not _can_merge(first, second):
                continue
            if {second.start, second.end} <= {first.start, first.end}:
                _remove(sheet, second)
                return True
            merged = merge_overlap(second, first, junction)
            if merged is not None:
                if first.uuid in sheet.groups or second.uuid in sheet.groups:
                    raise UnknownJunctionError(
                        f"the wire merges with a wire of a group at {_where(first.start)}. "
                        "KiCad keeps a copy of the merged wire, and its source does not "
                        "decide if the copy stays in the group."
                    )
                merged.new = first.new or second.new
                sheet.lines[sheet.lines.index(first)] = merged
                _remove(sheet, second)
                return True
    return False


def merge_new_lines(sheet: Sheet, kicad: int, is_bus_label: Callable[[str], bool]) -> None:
    """CleanUp step d for the new lines (merge_lines)."""
    merge_lines(sheet, kicad, is_bus_label, lambda line: line.new)


def break_new_lines_at_junctions(sheet: Sheet) -> None:
    """CleanUp step a of KiCad 8 and 9 for the new lines (BreakSegmentsOnJunctions):
    split each new line at each junction and each bus entry end in its middle."""
    points = set(sheet.junctions)
    for entry in sheet.entries:
        points.update((entry.position, entry.end))
    for point in sorted(points):
        for line in [line for line in sheet.lines if line.new]:
            if point not in (line.start, line.end) and on_segment(point, (line.start, line.end)):
                _break_at(sheet, line, point)


def remove_junctions_on_new_lines(
    sheet: Sheet, kicad: int, is_bus_label: Callable[[str], bool]
) -> None:
    """CleanUp step b for the junctions on the new lines: remove a junction that is not
    an explicit junction (IsExplicitJunction).

    Raises:
        UnknownJunctionError: The test depends on data that is not in the file.
    """
    new_lines = [(line.start, line.end) for line in sheet.lines if line.new]
    for point in list(dict.fromkeys(sheet.junctions)):
        if not any(on_segment(point, segment) for segment in new_lines):
            continue
        _remove_if_not_explicit(sheet, point, kicad, is_bus_label)


def _remove_if_not_explicit(
    sheet: Sheet, point: Point, kicad: int, is_bus_label: Callable[[str], bool]
) -> bool:
    """CleanUp step b at one point: remove the junctions there when the point is not an
    explicit junction (IsExplicitJunction). True if it removed them.

    Raises:
        UnknownJunctionError: The test depends on data that is not in the file.
    """
    if point not in sheet.junctions or _decide(sheet, point, kicad, is_bus_label, _explicit):
        return False
    while point in sheet.junctions:
        sheet.junctions.remove(point)
    while point in sheet.added_junctions:
        sheet.added_junctions.remove(point)
    sheet.removed_junctions += sheet.junction_nodes.pop(point, [])
    return True


def _break_at(sheet: Sheet, line: Line, point: Point) -> Line:
    """SCH_LINE::BreakAt: the line goes to the point, the new piece (a new UUID) from it."""
    piece = replace(line, start=point, uuid=_new_uuid(), node=None, changed=True, original=None)
    line.end = point
    line.changed = True
    sheet.lines.insert(sheet.lines.index(line) + 1, piece)
    # KiCad 10: the piece joins the group of the line (SCH_ITEM::Duplicate).
    if line.uuid in sheet.groups:
        sheet.groups[piece.uuid] = sheet.groups[line.uuid]
        sheet.group_changes.append((sheet.groups[line.uuid], piece.uuid, True))
    return piece


def trim_wire(sheet: Sheet, a: Point, b: Point, only_new: bool) -> bool:
    """SCH_EDIT_FRAME::TrimWire: remove the part between a and b of the first wire that
    holds both, when the wire is not exactly [a, b].

    The wire breaks at a, the piece that holds b breaks at b, and the piece
    that holds a goes. A piece of length 0 also goes (the CleanUp of the
    commit removes it). only_new: only a new wire.
    """
    if a == b:
        return False
    for line in list(sheet.lines):
        if line.bus or (only_new and not line.new):
            continue
        if not (on_segment(a, (line.start, line.end)) and on_segment(b, (line.start, line.end))):
            continue
        if {line.start, line.end} == {a, b}:
            continue
        piece = _break_at(sheet, line, a)
        holder = line if on_segment(b, (line.start, line.end)) else piece
        other = _break_at(sheet, holder, b)
        middle = holder if on_segment(a, (holder.start, holder.end)) else other
        _remove(sheet, middle)
        for part in (line, piece, other):
            if part in sheet.lines and part.null:
                _remove(sheet, part)
        return True
    return False


_OLD_LINES = (
    "the sheet has wires at {where} that the schematic editor of KiCad changes before it "
    "adds the junctions ({step}). The tool does this step only for the new wire. Change "
    "these wires in KiCad first."
)


def _check_old_merges(
    sheet: Sheet, points: list[Point], kicad: int, is_bus_label: Callable[[str], bool]
) -> None:
    """Raise when CleanUp, which the tool does only for the new lines, would merge two
    old lines at a point of the decision.

    The schematic editor does CleanUp and TrimWire for all the lines of the
    sheet before it decides on the junctions. Two old lines that CleanUp
    merges change the decision only at the points that they hold.

    Raises:
        UnknownJunctionError: Such old lines are at a point of the decision.
    """

    def junction(point: Point) -> bool:
        return is_junction(sheet, point, kicad, is_bus_label)

    old = [line for line in sheet.lines if not line.new]
    for point in dict.fromkeys(points):
        here = [line for line in old if _hit(line.start, line.end, point)]
        for first, second in itertools.permutations(here, 2):
            if _can_merge(first, second) and (
                {second.start, second.end} <= {first.start, first.end}
                or merge_overlap(second, first, junction) is not None
            ):
                x, y = point
                raise UnknownJunctionError(_OLD_LINES.format(where=_where((x, y)), step="CleanUp"))


def _trim_pairs(symbol: SymbolPins) -> dict[int, set[tuple[Point, Point]]]:
    """Return the pin pairs of TrimWire for each unit: the units with at most two
    connection points, without a pair of two pins at one point (TrimWire does
    nothing for them)."""
    return {
        unit: {pair for pair in itertools.combinations(points, 2) if pair[0] != pair[1]}
        if len(points) <= 2
        else set()
        for unit, points in symbol.unit_points.items()
    }


def _check_old_trims(sheet: Sheet, points: list[Point]) -> None:
    """Raise when TrimWire, which the tool does only for the new wires, would cut an old
    wire at a point of the decision, or could cut an old wire in place of a new one.

    Raises:
        UnknownJunctionError: Such an old wire is there.
    """
    for symbol in sheet.symbols:
        for a, b in set().union(*_trim_pairs(symbol).values()):
            holds = [
                line
                for line in sheet.lines
                if not line.bus
                and on_segment(a, (line.start, line.end))
                and on_segment(b, (line.start, line.end))
                and {line.start, line.end} != {a, b}
            ]
            old_holds = [line for line in holds if not line.new]
            if not old_holds:
                continue
            touched = any(
                _hit(line.start, line.end, point) for line in old_holds for point in points
            )
            if touched or any(line.new for line in holds):
                raise UnknownJunctionError(
                    _OLD_LINES.format(
                        where=f"the pins of a symbol at {_where(a)}",
                        step="TrimWire",
                    )
                )


def _clean_up_at(
    sheet: Sheet, points: list[Point], kicad: int, is_bus_label: Callable[[str], bool]
) -> None:
    """CleanUp of the commit for the points of a change: remove a junction there that is
    not an explicit junction (step b), then merge the lines with an end at such a
    point (step d)."""
    removed = [
        point
        for point in dict.fromkeys(points)
        if _remove_if_not_explicit(sheet, point, kicad, is_bus_label)
    ]
    if removed:
        merge_lines(
            sheet, kicad, is_bus_label, lambda line: line.start in removed or line.end in removed
        )


# ---------------------------------------------------------------------------
# The two flows
# ---------------------------------------------------------------------------


def finish_wire(
    sheet: Sheet, segments: list[Line], kicad: int, is_bus_label: Callable[[str], bool]
) -> None:
    """finishSegments for new segments: add them to the sheet with the changes of KiCad.

    Raises:
        UnknownJunctionError: The decision depends on data that is not in the file.
    """
    segments = simplify_wire_list(segments)
    connections = sheet.connections()
    candidates = []
    for segment in segments:
        candidates += [segment.start, segment.end]
        candidates += [p for p in connections if on_segment(p, (segment.start, segment.end))]
    sheet.lines.extend(segments)
    _check_old_merges(sheet, candidates, kicad, is_bus_label)
    # CleanUp (SCHEMATIC::CleanUp, SCH_EDIT_FRAME::SchematicCleanUp of KiCad 8).
    if kicad <= 9:
        break_new_lines_at_junctions(sheet)
    remove_junctions_on_new_lines(sheet, kicad, is_bus_label)
    merge_new_lines(sheet, kicad, is_bus_label)
    _check_old_trims(sheet, candidates)
    # TrimWire for each symbol with at most two connection points.
    common_pairs: dict[int, set[tuple[Point, Point]]] = {}
    for symbol in sheet.symbols:
        pairs = _trim_pairs(symbol)
        common = set.intersection(*pairs.values()) if pairs else set()
        for a, b in set().union(*pairs.values()) - common:
            if any(
                on_segment(a, (ln.start, ln.end)) and on_segment(b, (ln.start, ln.end))
                for ln in sheet.lines
                if ln.new and not ln.bus
            ):
                raise UnknownJunctionError(
                    "a new wire goes through two pins of a symbol that shows an other unit "
                    "for an other instance of the sheet. KiCad removes the wire between the "
                    "pins only for a unit with two pins, and the file does not tell the unit."
                )
        for a, b in sorted(common):
            trim_wire(sheet, a, b, only_new=True)
        common_pairs[id(symbol)] = common
    for point in candidates:
        if junction_needed(sheet, point, kicad, is_bus_label):
            add_junction(sheet, point)
    # The CleanUp of the commit, for the points of the new wire and of the trims.
    trims = [
        p for symbol in sheet.symbols for pair in common_pairs.get(id(symbol), ()) for p in pair
    ]
    _clean_up_at(sheet, candidates + trims, kicad, is_bus_label)


def place_symbol(
    sheet: Sheet, symbol: SymbolPins, kicad: int, is_bus_label: Callable[[str], bool]
) -> None:
    """PlaceSymbol for a new symbol: TrimOverLappingWires, then AddJunctionsIfNeeded.

    Raises:
        UnknownJunctionError: The decision depends on data that is not in the file.
    """
    sheet.symbols.append(symbol)
    # GetConnectionPoints of the new symbol: two pins at one point count two times.
    points = [p for unit_points in symbol.unit_points.values() for p in unit_points]
    for line in list(sheet.lines):
        on = [p for p in points if on_segment(p, (line.start, line.end))][:3]
        if len(on) == 2 and not line.bus:  # TrimWire cuts only wires.
            # KiCad tests only the lines in the box of the symbol.
            if all(p in symbol.hidden_outside for p in on):
                raise UnknownJunctionError(
                    "two hidden pins of the new symbol are on a wire. KiCad removes the wire "
                    "between them only when the wire is in the box of the symbol, which the "
                    "file does not give."
                )
            trim_wire(sheet, on[0], on[1], only_new=False)
    needed = [
        p for p in sorted(set(symbol.points)) if junction_needed(sheet, p, kicad, is_bus_label)
    ]
    for point in needed:
        add_junction(sheet, point)
    # The CleanUp of the commit, for the pins of the symbol.
    _clean_up_at(sheet, symbol.points, kicad, is_bus_label)
