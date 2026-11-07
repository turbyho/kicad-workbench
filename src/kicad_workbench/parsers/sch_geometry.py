"""Positions of the connection points of a schematic sheet, as KiCad finds them.

KiCad keeps a schematic position as an integer number of internal units
(IU): 100 nm (``schIUScale``). The parser reads a length in mm and rounds it
(``parseInternalUnits``: KiROUND of mm * 10000). The functions here use IU,
thus two positions are equal when KiCad finds them equal.

A pin of a placed symbol is at the position of the symbol plus the pin
position of the definition, transformed (KiCad 8.0.9, 9.0.9 and 10.0.6,
``parseSchematicSymbol`` and ``SCH_SYMBOL::SetOrientation``):

1. The library has its Y axis up, the sheet down: (x, y) becomes (x, -y).
2. The rotation of (at x y angle): 90 gives (y, -x), 180 gives (-x, -y),
   270 gives (-y, x).
3. The mirror after the rotation: (mirror x) gives (x, -y), (mirror y) gives
   (-x, y).
"""

import math
from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import InputError
from .lib_symbols import LibraryPin, library_pins
from .sexpr import Node
from .sub_symbols import sub_symbols

IU_PER_MM = 10000
IU_PER_MIL = 254

Point = tuple[int, int]

# The labels of all types. KiCad reads "netclass_flag" as a directive label.
LABEL_HEADS = ("label", "global_label", "hierarchical_label", "directive_label", "netclass_flag")


def kiround(value: float) -> int:
    """Return the value rounded to an integer, half away from zero (KiROUND).

    Raises:
        InputError: The value is not a finite number.
    """
    if not math.isfinite(value):
        raise InputError(f"{value} is not a finite number")
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def iu(mm: float) -> int:
    """Return a length in mm in IU, as the KiCad parser rounds it."""
    return kiround(mm * IU_PER_MM)


def to_mm(value: int) -> float:
    """Return a length in IU in mm."""
    return value / IU_PER_MM


def mm_text(value: int) -> str:
    """Return a length in IU as KiCad writes it in mm: 4 decimals at most."""
    text = f"{to_mm(value):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def on_grid(point: Point, grid: int) -> bool:
    """Return True if a point is on the grid (ERC_TESTER::TestOffGridEndpoints)."""
    return point[0] % grid == 0 and point[1] % grid == 0


def align(value: int, grid: int, offset: int = 0) -> int:
    """Return the nearest value v with (v - offset) on the grid (GRID_HELPER::Align)."""
    distance = value - offset
    steps, remainder = divmod(distance, grid)  # remainder >= 0
    # KiROUND of distance / grid: half away from zero.
    if 2 * remainder > grid or (2 * remainder == grid and distance >= 0):
        steps += 1
    return offset + steps * grid


@dataclass(frozen=True)
class Placement:
    """The position (IU), the rotation and the mirror of a placed symbol."""

    x: int
    y: int
    angle: int = 0  # 0, 90, 180 or 270.
    mirror: str = ""  # "", "x" or "y".

    def sheet_position(self, pin: LibraryPin) -> Point:
        """Return the position of a pin of the definition in the sheet, in IU."""
        x, y = iu(pin.x), -iu(pin.y)
        if self.angle == 90:
            x, y = y, -x
        elif self.angle == 180:
            x, y = -x, -y
        elif self.angle == 270:
            x, y = -y, x
        if self.mirror == "x":
            y = -y
        elif self.mirror == "y":
            x = -x
        return self.x + x, self.y + y


def placement(symbol: Node) -> Placement:
    """Return the placement of a (symbol (lib_id ...) ...) of a sheet.

    KiCad reads the angle as an integer (static_cast<int>), and only 0, 90,
    180 and 270, and the mirror x or y (parseSchematicSymbol).

    Raises:
        InputError: The (at ...) is not a number, or KiCad cannot read the
            angle or the mirror.
    """
    at = symbol.first("at")
    angle = int(at.number(2, 0.0)) if at else 0
    if angle not in (0, 90, 180, 270):
        raise InputError(f"the symbol angle {angle} is not 0, 90, 180 or 270")
    mirror = symbol.first("mirror")
    axis = mirror.value(0, "") if mirror else ""
    if mirror is not None and axis not in ("x", "y"):
        raise InputError(f"the symbol mirror {axis!r} is not x or y")
    return Placement(
        iu(at.number(0, 0.0)) if at else 0,
        iu(at.number(1, 0.0)) if at else 0,
        angle,
        axis,
    )


def symbol_unit(symbol: Node) -> int:
    """Return the (unit N) of a placed symbol (default 1).

    Raises:
        InputError: The unit is not a number.
    """
    unit = symbol.first("unit")
    return int(unit.number(0, 1)) if unit else 1


def placed_units(symbol: Node) -> set[int]:
    """Return the units of a placed symbol: its (unit N) and the units of its instances.

    A sheet that the hierarchy uses more than one time can show a different
    unit for each instance.

    Raises:
        InputError: A unit is not a number.
    """
    units = {symbol_unit(symbol)}
    instances = symbol.first("instances")
    if instances is not None:
        for path in instances.walk("path"):
            if (instance_unit := path.first("unit")) is not None:
                units.add(int(instance_unit.number(0, 1)))
    return units


def body_style(symbol: Node) -> int:
    """Return the body style of a placed symbol: (body_style N), or (convert N) of KiCad 8 and 9.

    Raises:
        InputError: The body style is not a number.
    """
    node = symbol.first("body_style") or symbol.first("convert")
    return int(node.number(0, 1)) if node else 1


def definitions(root: Node) -> dict[str, Node]:
    """Return the symbol definitions of the lib_symbols section by lib_id.

    KiCad reads each definition when it loads the file (parseLibSymbol): a
    sub-symbol name that it cannot read stops the load, also for a definition that
    no placed symbol uses.

    Raises:
        KicadParseError: KiCad cannot read the name of a sub-symbol.
        LocaleNumberError: A number of such a name depends on the locale of KiCad.
    """
    lib_symbols = root.first("lib_symbols")
    if lib_symbols is None:
        return {}
    symbols = lib_symbols.children("symbol")
    for symbol in symbols:
        sub_symbols(symbol)
    return {symbol.value(0, ""): symbol for symbol in symbols}


def definition_key(symbol: Node) -> str:
    """Return the key of the definition of a placed symbol in lib_symbols.

    KiCad writes (lib_name "GND_1") when the definition in the sheet is not
    the same as the library symbol (lib_id). Then lib_name is the key, when it
    is not empty (SCH_SYMBOL::GetSchSymbolLibraryName).
    """
    for head in ("lib_name", "lib_id"):
        node = symbol.first(head)
        if node is not None and node.value(0, ""):
            return node.value(0, "")
    return ""


def placed_symbols(root: Node) -> Iterator[Node]:
    """Yield the placed symbols of a sheet: (symbol (lib_id ...) ...)."""
    for symbol in root.children("symbol"):
        if symbol.first("lib_id") is not None:
            yield symbol


def placed_pins(
    symbol: Node, definition: Node, units: set[int] | None = None
) -> list[tuple[LibraryPin, Point]]:
    """Return the pins of a placed symbol with their positions in the sheet.

    Args:
        units: The units that show the pins. None: the units of placed_units.

    Raises:
        InputError: A position, a unit or the body style is not a number.
    """
    where = placement(symbol)
    if units is None:
        units = placed_units(symbol)
    pins = library_pins(definition, body_style(symbol), units)
    return [(pin, where.sheet_position(pin)) for pin in pins]


def node_point(node: Node) -> Point:
    """Return the point (IU) of an (at x y ...), (xy x y) or (size x y)."""
    return iu(node.number(0, 0.0)), iu(node.number(1, 0.0))


def on_segment(point: Point, segment: tuple[Point, Point]) -> bool:
    """Return True if the point is on the segment, also at an end (IsPointOnSegment)."""
    (x1, y1), (x2, y2) = segment
    x, y = point
    if (x2 - x1) * (y - y1) != (y2 - y1) * (x - x1):
        return False
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


@dataclass
class ConnectionPoints:
    """The points of a sheet where a new item connects."""

    points: set[Point]
    # The wires (and for a label the buses): a new item connects in their middle.
    segments: list[tuple[Point, Point]]

    def segments_at(self, point: Point) -> list[tuple[Point, Point]]:
        """Return the segments that hold the point (also at an end)."""
        return [segment for segment in self.segments if on_segment(point, segment)]

    def __contains__(self, point: Point) -> bool:
        return point in self.points or bool(self.segments_at(point))
