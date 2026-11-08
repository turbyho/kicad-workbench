"""Schematic editing tools for KiCad Workbench."""

import asyncio
import dataclasses
import itertools
import math
import threading
import uuid
from pathlib import Path

from ..errors import InputError
from ..parsers.bus_names import is_bus_label
from ..parsers.kicad_fields import rules_for_version
from ..parsers.kicad_versions import (
    KICAD8,
    RELEASES,
    KicadFileError,
    is_release_schematic,
    kicad_release,
    release_of_schematic,
    release_of_symbol_library,
    symbol_library_problem,
)
from ..parsers.lib_symbols import (
    LibraryPin,
    PlacedField,
    flatten,
    library_pins,
    placed_fields,
    symbol_pins,
    unit_count,
)
from ..parsers.sch_geometry import (
    ConnectionPoints,
    Placement,
    Point,
    align,
    definition_key,
    definitions,
    iu,
    on_grid,
    on_segment,
    placed_pins,
    placed_symbols,
    placement,
    symbol_unit,
    to_mm,
)
from ..parsers.sch_junctions import (
    Line,
    Sheet,
    UnknownJunctionError,
    finish_wire,
    junction_symbol,
    place_symbol,
    read_sheet,
)
from ..parsers.sexpr import (
    Node,
    edited,
    indent,
    parse,
    property_atoms,
    property_field,
    quote,
)
from ..parsers.sheet_list import kiid, kiid_path
from ..server import mcp
from ..utils.file_stamp import FileStamp, read_for_write_stamped
from ..utils.kicad_cli import kicad_cli_version_sync, no_kicad_message
from ..utils.kicad_lock import writable_schematic_version, write_schematic, write_with_warning
from ..utils.kicad_project import (
    PRO_SUFFIX,
)
from ..utils.library_lookup import LibraryLookupError, find_library_symbol
from ..utils.sch_hierarchy import (
    AmbiguousRootError,
    SheetPath,
    schematic_project_file,
    sheet_paths,
)
from ..utils.schematic_settings import (
    SchematicSettings,
    Severity,
    off_grid_severity,
    schematic_settings,
)
from ..utils.schematic_versions import with_schematic_versions
from ..utils.tool_errors import error_answer


def _get_date_string() -> str:
    """Get current date in ISO format."""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def _child_symbol(parent: Node, name: str) -> Node | None:
    """Return the (symbol "<name>" ...) list in a list, or None."""
    for node in parent.children("symbol"):
        if node.value(0) == name:
            return node
    return None


# The items that SCH_IO_KICAD_SEXPR::Format writes before (lib_symbols ...), in
# this order (KiCad 8, 9 and 10).
_HEADER_HEADS = ("version", "generator", "generator_version", "uuid", "paper", "title_block")


def _insert_symbol_into_lib_symbols(content: str, symbol_text: str) -> str:
    """Insert a symbol definition into the lib_symbols section of a schematic.

    KiCad writes the section in each file, also when it is empty, after the
    header items (_HEADER_HEADS; SCH_IO_KICAD_SEXPR::Format): each symbol on
    its own lines, and the ")" of a section with symbols on its own line. A file
    with no section gets it after the header items at the start of the file.
    The parser of KiCad reads the section at each place. The caller has checked
    the head "kicad_sch" (writable_schematic_version).
    """
    root = parse(content)
    section = root.first("lib_symbols")
    if section is not None:
        symbols = section.children()
        if symbols:
            last = symbols[-1].end
            return content[:last] + "\n" + symbol_text + content[last:]
        # "(lib_symbols)": the ")" goes on its own line, with the indent of the section.
        line_start = content.rfind("\n", 0, section.start) + 1
        lead = content[line_start : section.start]
        lead = lead if not lead.strip() else ""
        head_end = section.items[0].end
        return content[:head_end] + "\n" + symbol_text + "\n" + lead + content[section.end - 1 :]
    position = root.items[0].end
    for item in root.items[1:]:
        if not isinstance(item, Node) or item.head not in _HEADER_HEADS:
            break
        position = item.end
    section_text = f"\n\t(lib_symbols\n{symbol_text}\n\t)"
    return content[:position] + section_text + content[position:]


# ---------------------------------------------------------------------------
# Pin anchor geometry (for label placement)
# ---------------------------------------------------------------------------


def _lib_symbols_block(content: str) -> str:
    """Text of the (lib_symbols ...) section, or ''."""
    node = parse(content).first("lib_symbols")
    return content[node.start : node.end] if node else ""


def _symbol_def_block(lib_block: str, lib_id: str) -> str | None:
    """Text of the (symbol "<lib_id>" ...) definition in a lib_symbols section, or None."""
    if not lib_block:
        return None
    node = _child_symbol(parse(lib_block), lib_id)
    return lib_block[node.start : node.end] if node else None


def _pin_anchor(content: str, reference: str, pin: str) -> tuple[Point, int] | None:
    """Return the position (IU) of a pin of a placed symbol, and the angle of a label on it.

    The symbols are the symbols with this Reference field: each unit of a
    part is a symbol of its own. The pin is the first pin with the number in
    the unit and the body style of a symbol, with its rotation and mirror
    (sch_geometry). The angle (0, 90, 180 or 270) points away from the
    symbol position, thus the labels of the pins point out of the body.
    None: no symbol has the pin, or the definition is not in lib_symbols.
    """
    root = parse(content)
    defs = definitions(root)
    for symbol in placed_symbols(root):
        fields = dict(
            field for prop in symbol.children("property") if (field := property_field(prop))
        )
        definition = defs.get(definition_key(symbol))
        if fields.get("Reference") != reference or definition is None:
            continue
        where = placement(symbol)
        for lib_pin, (x, y) in placed_pins(symbol, definition, {symbol_unit(symbol)}):
            if lib_pin.number != pin:
                continue
            sx, sy = x - where.x, y - where.y
            if abs(sx) >= abs(sy):
                angle = 0 if sx >= 0 else 180
            else:
                angle = 90 if sy < 0 else 270
            return (x, y), angle
    return None


def _append_top_level(content: str, entry: str) -> str:
    """Add items at the end of the root list (kicad_sch), before its ")".

    The parser gives the ")" of the root list: a text after it (a comment) stays
    after it.
    """
    end = parse(content).end - 1
    return content[:end] + entry + "\n" + content[end:]


_LABEL_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")

_JUSTIFY = {0: "left", 180: "right", 90: "bottom", 270: "top"}


def _multiline_label_error(name: str) -> str | None:
    """Return an error if the label name has a line break, else None.

    A KiCad label holds one line of text (``SetMultilineAllowed( false )``
    in ``eeschema/sch_label.cpp``).
    """
    if "\n" in name or "\r" in name:
        return f"❌ The label name {name!r} has a line break. A KiCad label holds one line of text."
    return None


def _field_error(field: str, value: str) -> str | None:
    """Return an error if KiCad does not accept the value of a symbol field, else None.

    A symbol field cannot hold a line break or a tab (``FIELD_VALIDATOR`` in
    ``common/validators.cpp``).
    """
    if any(c in value for c in "\r\n\t"):
        return (
            f"❌ The {field} {value!r} has a line break or a tab. "
            f"A KiCad symbol field cannot hold these characters."
        )
    return None


def _library_error(
    library: Path, symbol: Node, library_version: int | None, version: int
) -> str | None:
    """Return an error if a symbol of a library cannot go into a schematic, else None."""
    problem = symbol_library_problem(symbol, library_version, version)
    if problem is None:
        return None
    newest = RELEASES[-1]
    if is_release_schematic(version):
        own = f"a symbol library of KiCad {release_of_schematic(version).name}"
    else:
        own = "a symbol library of the nightly build of KiCad that saved the schematic"
    # Only a newer KiCad can save the schematic in the format of the library.
    if library_version is not None and library_version > version:
        if library_version > newest.symbol_library:
            newer = "the nightly build of KiCad that saved the library"
        else:
            newer = f"KiCad {release_of_symbol_library(library_version).name}"
        advice = f"Open the schematic in {newer} and save it, or use {own}."
    else:
        advice = f"Use {own}."
    return f"❌ Cannot add component from {library}: {problem} {advice}"


def _library_definition(
    project: Path, library_name: str, symbol_name: str, kicad: str, version: int
) -> tuple[str, Path, list[str]] | str:
    """Return the lib_symbols definition of a library symbol, its library and the
    problems of the library tables, or the error.

    The library comes from the library tables of the installed KiCad and of
    the project of the schematic, as in the symbol chooser of KiCad. A derived
    symbol gets the items of its parents (lib_symbols.flatten).
    """
    lib_id = f"{library_name}:{symbol_name}"
    error = f"❌ Cannot add component {lib_id}:"
    try:
        chain, row, problems = find_library_symbol(library_name, symbol_name, kicad, project)
    except LibraryLookupError as e:
        return f"{error} {e}"
    # Each symbol of the chain goes into the schematic in the format of its
    # library file. KiCad must read it there in the same way.
    try:
        for symbol in chain:
            if library_error := _library_error(symbol.file, symbol.node, symbol.version, version):
                return library_error
        text = flatten(chain, lib_id, rules_for_version(version).kicad)
    except KicadFileError as e:
        return f"{error} {e}"
    return indent(text, 2), row.path, problems


def _same_path(wanted: str, sheet: SheetPath) -> bool:
    """Return True if a text gives the sheet path: its names or its UUID path.

    The slashes at the start and the end are optional ("Power", "/Power/").
    A KiCad sheet name cannot hold "/". The UUIDs are compared as KiCad reads them
    (KIID_PATH): with or without the dashes, in upper or lower case.
    """
    names = tuple(name for name in wanted.split("/") if name)
    if sheet.uuids is not None and kiid_path(wanted) == tuple(kiid(u) for u in sheet.uuids):
        return True
    return SheetPath(sheet.file, None, names).shown == sheet.shown


def _sheet_path(path: Path, project: Path, wanted: str) -> tuple[SheetPath, int] | str:
    """Return the sheet path that gets the reference of a new symbol and the number
    of sheet paths of the file, or the error.

    KiCad gives a new symbol the reference of the sheet that it shows. A sheet
    file that the hierarchy uses more than one time thus needs the sheet path.
    A KiCad sheet name cannot hold "/".
    """
    paths, problems = sheet_paths(project)
    target = path.resolve()
    mine = [sheet for sheet in paths if sheet.file is not None and sheet.file.resolve() == target]
    if not mine:
        return f"the schematic is not in the sheet hierarchy of the project {project}." + "".join(
            f" {problem}" for problem in problems
        )

    def listed(sheet: SheetPath) -> str:
        # KiCad gives a file or a sheet on the path a new UUID at each load.
        return f"{sheet.shown} ({sheet.path if sheet.uuids else 'no fixed UUID path'})"

    if wanted:
        found = [sheet for sheet in mine if _same_path(wanted, sheet)]
        if len(found) != 1:
            state = "is not a sheet path" if not found else "gives more than one sheet"
            names = ", ".join(listed(sheet) for sheet in mine)
            return f"{wanted!r} {state} of this schematic. The sheet paths are: {names}."
        chosen = found[0]
    elif len(mine) > 1:
        return (
            f"the hierarchy uses this schematic {len(mine)} times. Give sheet_path, the "
            f"sheet that gets the reference: {', '.join(listed(sheet) for sheet in mine)}."
        )
    else:
        chosen = mine[0]
    if chosen.uuids is None:
        return (
            f"KiCad has no fixed sheet path for the sheet {chosen.shown} of this schematic. "
            + " ".join(problems)
        )
    return chosen, len(mine)


def _mm(value: float) -> str:
    """Return a length in mm as KiCad writes it: 4 decimals at most (100 nm)."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


# ---------------------------------------------------------------------------
# The connection grid
# ---------------------------------------------------------------------------


def _xy(point: Point) -> str:
    """Return a point in IU as the text "x y" in mm, for the file."""
    return f"{_mm(to_mm(point[0]))} {_mm(to_mm(point[1]))}"


def _shown(point: Point) -> str:
    """Return a point in IU as the text "(x, y)" in mm, for the output."""
    return f"({_mm(to_mm(point[0]))}, {_mm(to_mm(point[1]))})"


@dataclasses.dataclass(frozen=True)
class _NoGrid:
    """The connection grid of a schematic is not known: the reason, and the range of
    the wire widths of the net classes of all its projects (sch_junctions)."""

    reason: str
    wire_widths: tuple[int, int]


def _project_settings(path: Path) -> SchematicSettings | _NoGrid:
    """Return the settings of the project of a schematic, or the error.

    A schematic in more than one project has the settings of these projects
    when they have the same connection grid. Else its connection grid is not
    known. off_grid_severity reads the ERC severities of each project.
    """
    try:
        return schematic_settings(schematic_project_file(path))
    except AmbiguousRootError as e:
        found = [schematic_settings(root.with_suffix(PRO_SUFFIX)) for root in e.roots]
        # The wire widths of the net classes give a range (sch_junctions), thus the
        # range of all the projects applies.
        widths = (
            min(item.wire_widths[0] for item in found),
            max(item.wire_widths[1] for item in found),
        )
        if len({item.connection_grid for item in found}) > 1:
            return _NoGrid(
                f"{e} These projects have different connection grids, thus the connection "
                "grid of the schematic is not known. With snap_to_grid=False the tool "
                "writes the positions as they are.",
                widths,
            )
        problems = dict.fromkeys(item.problem for item in found if item.problem)
        return SchematicSettings(
            connection_grid=found[0].connection_grid,
            problem=" ".join(problems) or None,
            wire_widths=widths,
            erc_severities=tuple(pair for item in found for pair in item.erc_severities),
        )


def _installed_off_grid(settings: SchematicSettings, kicad: str | None, version: int) -> Severity:
    """Return the severity of endpoint_off_grid in the KiCad that runs ERC.

    The key order of the installed KiCad applies when it can open the file.
    Get the severity before the write of the file: an error of kicad-cli then
    changes no file. The function blocks (it can run kicad-cli).

    Args:
        kicad: The installed KiCad version, when the caller has it. Else the tool
            runs kicad-cli for it, but only when the order changes the result. An
            error of kicad-cli goes into the reason: the edit does not need KiCad.
        version: The file version of the schematic.
    """
    severity = off_grid_severity(settings, None)
    if severity.value is not None:
        return severity  # The same in each order.
    if kicad is None:
        try:
            kicad = kicad_cli_version_sync()
        except OSError as e:
            return off_grid_severity(settings, None, f"the installed KiCad (kicad-cli: {e})")
    release = kicad_release(kicad) if kicad else None
    if release is not None and release.schematic < version:
        # KiCad does not open a file of a newer version: a newer KiCad runs ERC.
        return off_grid_severity(
            settings,
            None,
            f"the KiCad that opens the file (the installed KiCad {kicad} cannot open "
            f"version {version})",
        )
    return off_grid_severity(settings, kicad)


def _locked_axes(point: Point, existing: ConnectionPoints) -> set[int]:
    """Return the axes (0: X, 1: Y) on which a point cannot move to the connection grid.

    A point on a connection point of the sheet cannot move: a new item there
    connects to it. A point in the middle of a wire (or for a label of a bus)
    also connects there, thus it moves only along it: on a horizontal one only
    on X, on a vertical one only on Y, on a slanted one not at all.
    """
    if point in existing.points:
        return {0, 1}
    locked: set[int] = set()
    for (x1, y1), (x2, y2) in existing.segments_at(point):
        locked.update({1} if y1 == y2 else {0} if x1 == x2 else {0, 1})
    return locked


def _snapped_label(point: Point, grid: int, existing: ConnectionPoints) -> Point:
    """Return the nearest point of the connection grid for a label (GRID_HELPER::Align).

    The label moves only on the axes that are not locked (_locked_axes). It
    stays when the move takes it away from the item where it connects.
    """
    locked = _locked_axes(point, existing)
    new = tuple(point[axis] if axis in locked else align(point[axis], grid) for axis in (0, 1))
    segments = existing.segments_at(point)
    if segments and not any(on_segment(new, segment) for segment in segments):
        return point
    return new


def _snapped_wire(points: list[Point], grid: int, existing: ConnectionPoints) -> list[Point]:
    """Return the points of a wire on the connection grid (GRID_HELPER::Align).

    Each point moves on the axes that are not locked (_locked_axes).
    Consecutive points with the same X (a vertical segment) or the same Y (a
    horizontal segment) keep the same X or Y: when one of them cannot move on
    that axis, the others get its X or Y. Thus a horizontal or vertical segment
    stays horizontal or vertical. A point that the move takes away from the
    wire where it connects stays where it is.
    """
    locked = [_locked_axes(point, existing) for point in points]
    while True:
        result = [list(point) for point in points]
        for axis in (0, 1):
            start = 0
            while start < len(points):
                end = start + 1
                while end < len(points) and points[end][axis] == points[start][axis]:
                    end += 1
                value = points[start][axis]
                if not any(axis in locked[index] for index in range(start, end)):
                    value = align(value, grid)
                for index in range(start, end):
                    result[index][axis] = value
                start = end
        final = [(x, y) for x, y in result]
        lost = [
            index
            for index, point in enumerate(points)
            if locked[index] != {0, 1}
            and existing.segments_at(point)
            and not any(on_segment(final[index], seg) for seg in existing.segments_at(point))
        ]
        if not lost:
            return final
        for index in lost:
            locked[index] = {0, 1}


def _zero_segment(points: list[Point], when: str) -> str | None:
    """Return the error for a wire segment of length 0, or None."""
    for start, end in itertools.pairwise(points):
        if start == end:
            return (
                f"❌ The wire has a segment of length 0 at {_shown(start)}{when}. "
                "Remove one of the two points."
            )
    return None


def _keeps(old: Point, new: Point, existing: ConnectionPoints) -> bool:
    """Return True if a point that moves from old to new keeps its connection.

    A point on a connection point of the sheet must stay there. A point in the
    middle of a wire must stay on that wire.
    """
    if old == new or old not in existing:
        return True
    if old in existing.points:
        return False
    return any(on_segment(new, segment) for segment in existing.segments_at(old))


def _new_connections(moves: list[tuple[Point, Point]], existing: ConnectionPoints) -> list[Point]:
    """Return the moved points that the move puts on a connection point of the sheet."""
    return [new for old, new in moves if new != old and new in existing and old not in existing]


# ---------------------------------------------------------------------------
# Junctions
# ---------------------------------------------------------------------------


def _junction_rules(version: int) -> int:
    """Return the KiCad version (8, 9 or 10) whose junction rules a file version uses.

    A file of a nightly build gets the release before the build, as for the
    other rules of the file version (release_of_schematic).
    """
    return int(release_of_schematic(version).name.split(".")[0])


def _line_text(line: Line) -> str:
    head = "bus" if line.bus else "wire"
    return (
        f"\t({head} (pts (xy {_xy(line.start)}) (xy {_xy(line.end)}))\n"
        f"\t\t{line.stroke_text}\n"
        f'\t\t(uuid "{line.uuid}")\n'
        "\t)"
    )


def _apply_sheet(content: str, sheet: Sheet) -> str:
    """Write the changes of a sheet (sch_junctions) into the text that it comes from.

    A changed line of the file gets its new ends, a removed line goes, and the
    new lines and junctions go to the end of the sheet.
    """
    edits = []
    for line in sheet.lines:
        if line.edited_in_file:
            pts = line.node.first("pts")
            edits.append((pts.start, pts.end, f"(pts (xy {_xy(line.start)}) (xy {_xy(line.end)}))"))
    for node in [line.node for line in sheet.removed] + sheet.removed_junctions:
        start = content.rfind("\n", 0, node.start) + 1
        if content[start : node.start].strip():
            start = node.start
        end = node.end
        if content[end : end + 1] == "\n":
            end += 1
        edits.append((start, end, ""))
    # KiCad 10: the members of the groups (SCH_ITEM::Duplicate, SCH_COMMIT).
    members_of: dict[int, tuple] = {}
    for members, member, add in sheet.group_changes:
        names = members_of.setdefault(id(members), (members, [a.value for a in members.atoms()]))[1]
        if add:
            names.append(member)
        elif member in names:
            names.remove(member)
    for members, names in members_of.values():
        text = "(members" + "".join(f" {quote(name)}" for name in names) + ")"
        edits.append((members.start, members.end, text))
    for start, end, text in sorted(edits, reverse=True):
        content = content[:start] + text + content[end:]
    entries = [_line_text(line) for line in sheet.lines if line.node is None]
    entries += [
        f"\t(junction (at {_xy(point)}) (diameter 0) (color 0 0 0 0)\n"
        f'\t\t(uuid "{uuid.uuid4()}")\n'
        "\t)"
        for point in sheet.added_junctions
    ]
    return _append_top_level(content, "\n".join(entries)) if entries else content


def _junction_report(sheet: Sheet) -> str:
    """Return the output lines about the junctions and the changed wires of the sheet."""
    text = ""
    if sheet.added_junctions:
        text += (
            "\n**Junctions added** (as the schematic editor of KiCad adds them): "
            + ", ".join(_shown(point) for point in sheet.added_junctions)
            + ".\n"
        )
    if sheet.removed_junctions:
        text += (
            f"\n**Junctions removed:** {len(sheet.removed_junctions)} junctions where fewer "
            "than three wire directions meet (KiCad: CleanUp).\n"
        )
    changed = [line for line in sheet.lines if line.edited_in_file]
    pieces = [line for line in sheet.lines if line.node is None and not line.new]
    parts = []
    if changed:
        parts.append(
            "changed: " + ", ".join(f"{_shown(ln.start)} to {_shown(ln.end)}" for ln in changed)
        )
    if pieces:
        parts.append(
            "new parts: " + ", ".join(f"{_shown(ln.start)} to {_shown(ln.end)}" for ln in pieces)
        )
    if sheet.removed:
        parts.append(f"{len(sheet.removed)} removed")
    if parts:
        text += (
            "\n**Wires of the sheet** (KiCad: BreakSegments, CleanUp, TrimWire): "
            f"{'; '.join(parts)}.\n"
        )
    return text


def _symbol_position(requested: Placement, pins: list[LibraryPin], grid: int) -> Placement:
    """Return the nearest position of a new symbol that puts its pins on the connection grid.

    For each axis, the pins can be on the grid when each pin offset has the
    same remainder on the grid. When the offsets have different remainders,
    the pins without the type no_connect decide (KiCad 9 and later do not
    check these pins). Else, and for a symbol with no pins, the position of
    the symbol goes on the grid on that axis, as in the symbol placement of
    KiCad.
    """
    origin = Placement(0, 0)
    offsets = [(pin, origin.sheet_position(pin)) for pin in pins]
    connected = [(pin, offset) for pin, offset in offsets if pin.type != "no_connect"]
    position = []
    for axis, value in ((0, requested.x), (1, requested.y)):
        remainder = 0
        for group in (offsets, connected):
            found = {-offset[axis] % grid for _pin, offset in group}
            if len(found) == 1:
                remainder = found.pop()
                break
        position.append(align(value, grid, remainder))
    return Placement(position[0], position[1])


def _off_grid_text(items: list[str], settings: SchematicSettings, installed: Severity) -> str:
    """Return the warning about pins or wire ends off the connection grid, or "".

    ERC finds them (endpoint_off_grid), except when the project ignores this
    check.

    Args:
        installed: The severity in the KiCad that runs ERC (_installed_off_grid).
    """
    if not items or installed.value == "ignore":
        return ""
    projects = "projects" if len(settings.erc_severities) > 1 else "project"
    return (
        f"\n⚠️ **Warning:** these points are not on the connection grid of "
        f"{_mm(to_mm(settings.connection_grid))} mm: {', '.join(items)}. ERC of the "
        f"{projects} {_gives(installed)}.\n"
    )


def _no_connect_text(no_connect: list[str], settings: SchematicSettings, version: int) -> str:
    """Return the warning about the pins of the type no_connect off the connection grid,
    or "".

    Only KiCad 8 checks these pins, and only a file of KiCad 8 opens in KiCad 8
    (the file version, not the installed KiCad).
    """
    if not no_connect or version > KICAD8.schematic:
        return ""
    kicad8 = off_grid_severity(settings, KICAD8.name)
    if kicad8.value == "ignore":
        return ""
    return (
        f"\n⚠️ **Warning:** these pins of the type no_connect are not on the connection "
        f"grid of {_mm(to_mm(settings.connection_grid))} mm: {', '.join(no_connect)}. ERC "
        f"of KiCad 8 {_gives(kicad8)}. KiCad 9 and later do not check these pins.\n"
    )


def _gives(severity: Severity) -> str:
    """Return what ERC gives for the points off the grid, with a severity that is not
    "ignore"."""
    if severity.value is not None:
        return f"gives endpoint_off_grid ({severity.value})"
    known = f"its severity is not known: {severity.reason}"
    given = " or ".join(value for value in severity.possible if value != "ignore")
    if "ignore" in severity.possible:
        return f"can give endpoint_off_grid ({given}, or no violation; {known})"
    return f"gives endpoint_off_grid ({given}; {known})"


def _grid_note(settings: SchematicSettings | _NoGrid) -> str:
    """Return the note when the settings of the project are not known, or KiCad uses
    the default settings because the project file is bad."""
    if isinstance(settings, _NoGrid):
        return f"\n**Note:** {settings.reason} The tool cannot tell the points off the grid.\n"
    return f"\n**Note:** {settings.problem}\n" if settings.problem else ""


# The mandatory fields that LIB_SYMBOL shows by default. The others are hidden.
_SHOWN_FIELDS = ("Reference", "Value")


def _placed_property(field: PlacedField, x: float, y: float, text: str | None) -> str:
    """Return the (property ...) of a placed symbol at (x, y).

    KiCad copies the field of the definition and puts it at the position of
    the symbol plus the position in the library (SCH_SYMBOL::UpdateFields).
    The library has its Y axis up, the sheet down.

    Args:
        text: The new text. None keeps the text of the definition.

    Raises:
        InputError: The (at ...) of the field is not a number.
    """
    if text is None:
        text = field.text
    node = field.node
    if node is None:
        # A field with the default values of LIB_SYMBOL (the text size of
        # EDA_TEXT is the default of the parser).
        hide = "" if field.name in _SHOWN_FIELDS else " (effects (hide yes))"
        return f"(property {quote(field.name)} {quote(text or '')} (at {_mm(x)} {_mm(y)} 0){hide})"
    atoms = property_atoms(node)
    edits: list[tuple[int, int, str]] = []
    if atoms[0].value != field.name:
        edits.append((atoms[0].start, atoms[0].end, quote(field.name)))
    if text is not None:
        if len(atoms) > 1:
            edits.append((atoms[1].start, atoms[1].end, quote(text)))
        else:
            edits.append((atoms[0].end, atoms[0].end, " " + quote(text)))
    at = node.first("at")
    lx, ly = (at.number(0, 0.0), at.number(1, 0.0)) if at else (0.0, 0.0)
    angle = at.value(2, "0") if at else "0"
    new_at = f"(at {_mm(x + lx)} {_mm(y - ly)} {angle})"
    if at is not None:
        edits.append((at.start, at.end, new_at))
    else:
        after = atoms[1].end if len(atoms) > 1 else atoms[0].end
        edits.append((after, after, " " + new_at))
    return edited(node, edits)


# The flags of a placed symbol, and their values when the library symbol has
# no such flag (the defaults of LIB_SYMBOL). KiCad 10 also writes
# in_pos_files: it is written only when the definition has it.
_PLACED_FLAGS = (("exclude_from_sim", "no"), ("in_bom", "yes"), ("on_board", "yes"))


def _placed_symbol(
    definition: Node,
    fields: list[PlacedField],
    lib_id: str,
    texts: dict[str, str | None],
    x: float,
    y: float,
    unit: int,
    pins: list[str],
    instance: tuple[str, str],
) -> str:
    """Return the (symbol ...) of a placed symbol, as KiCad places a library symbol.

    The SCH_SYMBOL constructor copies the fields (UpdateFields) and the flags
    of the definition. The instance holds the reference of the sheet path.

    Args:
        texts: The new text of a field by name. None keeps the library text.
        pins: The pin numbers (lib_symbols.symbol_pins).
        instance: (project name, sheet UUID path).
    """
    flags = []
    for head, default in _PLACED_FLAGS:
        nodes = definition.children(head)
        flags.append(f"({head} {nodes[-1].value(0, default) if nodes else default})")
    pos_files = definition.children("in_pos_files")
    if pos_files:
        flags.append(f"(in_pos_files {pos_files[-1].value(0, 'yes')})")
    properties = [
        _placed_property(field, x, y, texts.get(field.name)) for field in fields
    ]
    pin_lines = [f'(pin {quote(pin)} (uuid "{uuid.uuid4()}"))' for pin in pins]
    project, sheet = instance
    lines = [
        f"(symbol (lib_id {quote(lib_id)}) (at {_mm(x)} {_mm(y)} 0) (unit {unit})",
        " ".join(flags) + " (dnp no)",
        f'(uuid "{uuid.uuid4()}")',
        *properties,
        *pin_lines,
        f"(instances (project {quote(project)} (path {quote(sheet)} "
        f"(reference {quote(texts['Reference'])}) (unit {unit}))))",
        ")",
    ]
    return indent("\n".join(lines), 1)


def _label_position(
    path: Path, content: str, x: float, y: float, snap_to_grid: bool
) -> tuple[Point, str, str] | str:
    """Return the position (IU) of a label at x, y, the text that tells it and the
    note about the project settings (_grid_note), or the error.

    With snap_to_grid, the label goes to the nearest point of the connection
    grid of the project, except on a connection point of the sheet (_snapped).
    A label on a bus names the bus. ERC does not check the position of a
    label, thus without snap_to_grid the tool needs no project settings.
    """
    if not (math.isfinite(x) and math.isfinite(y)):
        return f"❌ The position ({x}, {y}) is not a position in mm."
    requested = (iu(x), iu(y))
    if not snap_to_grid:
        return requested, f"at {_shown(requested)}", ""
    settings = _project_settings(path)
    if isinstance(settings, _NoGrid):
        return f"❌ Cannot add the label: {settings.reason}"
    grid = settings.connection_grid
    try:
        existing = read_sheet(parse(content), settings.wire_widths).connection_points(label=True)
    except (InputError, KicadFileError) as e:
        return f"❌ Cannot add the label: the sheet has an item that KiCad cannot read: {e}."
    point = _snapped_label(requested, grid, existing)
    text = f"at {_shown(point)}"
    if point != requested:
        text += f", moved to the connection grid of {_mm(to_mm(grid))} mm from {_shown(requested)}"
    if _new_connections([(requested, point)], existing):
        text += ", where it connects to an item of the sheet"
    return point, text, _grid_note(settings)


@dataclasses.dataclass(frozen=True)
class _LabelTarget:
    """The schematic of a new label, and the place of the label."""

    content: str
    stamp: FileStamp  # of the content (read_for_write_stamped)
    point: Point  # IU
    angle: int
    where: str  # the text of the position
    note: str  # about the project settings


def _resolve_label_target(
    path: Path,
    reference: str,
    pin: str,
    x: float | None,
    y: float | None,
    snap_to_grid: bool,
) -> _LabelTarget | str:
    """Return the schematic and the place of a label, or the error.

    The label goes on the pin of the reference, or at explicit x, y.
    """
    content, stamp = read_for_write_stamped(path)
    if x is not None and y is not None:
        found = _label_position(path, content, x, y, snap_to_grid)
        if isinstance(found, str):
            return found
        return _LabelTarget(content, stamp, found[0], 0, found[1], found[2])
    try:
        anchor = _pin_anchor(content, reference, pin)
    except (InputError, KicadFileError) as e:
        return f"❌ Cannot add the label: the sheet has an item that KiCad cannot read: {e}."
    if anchor is None:
        return (
            f"❌ Pin '{pin}' not found on component '{reference}' — place the "
            f"component first, or pass explicit x/y coordinates."
        )
    point, angle = anchor
    where = f"anchored on {reference}.{pin} at {_shown(point)}"
    return _LabelTarget(content, stamp, point, angle, where, "")


async def _run_edit(work, *args) -> str:
    """Run the work of an edit tool off the event loop (file reads, the parse, kicad-cli,
    the save).

    A cancel of the tool does not stop the thread. A cancel before the save stops the
    work at the save (_save): the file does not change, as with the awaits of the
    tool before the work went into one thread. A cancel during the save cannot stop
    it.
    """
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(work, cancelled, *args)
    except asyncio.CancelledError:
        cancelled.set()
        raise


def _save(
    cancelled: threading.Event, path: Path, content: str, stamp: FileStamp
) -> tuple[str | None, str]:
    """write_with_warning(write_schematic), but not after a cancel of the tool (_run_edit)."""
    if cancelled.is_set():
        return "❌ The tool call was cancelled: the tool did not change the file.", ""
    return write_with_warning(write_schematic, path, content, stamp)


def _add_pin_label_sync(
    cancelled: threading.Event,
    kind: str,
    file_path: str,
    reference: str,
    pin: str,
    name: str,
    shape: str,
    x: float | None,
    y: float | None,
    snap_to_grid: bool,
) -> str:
    """The work of add_hierarchical_label and add_global_label (kind: "hierarchical_label"
    or "global_label"). The function blocks: the tool runs it with _run_edit."""
    path = Path(file_path)
    if not path.exists():
        return f"❌ The file {file_path} does not exist."
    target = _resolve_label_target(path, reference, pin, x, y, snap_to_grid)
    if isinstance(target, str):
        return target
    content, stamp, point = target.content, target.stamp, target.point
    angle, where, note = target.angle, target.where, target.note

    justify = _JUSTIFY.get(angle, "left")
    # A global label has the field of the references to the other sheets.
    references = (
        '\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}" (at 0 0 0)\n'
        "\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
        "\t\t)\n"
        if kind == "global_label"
        else ""
    )
    entry = (
        f"\t({kind} {quote(name)} (shape {shape}) (at {_xy(point)} {angle})\n"
        f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
        f'\t\t(uuid "{uuid.uuid4()}")\n'
        f"{references}"
        f"\t)\n"
    )
    open_error, saved = _save(cancelled, path, _append_top_level(content, entry), stamp)
    if open_error:
        return open_error
    word = "Global label" if kind == "global_label" else "Hierarchical label"
    return f"✅ {word} '{name}' ({shape}) {where} angle {angle}°{note}{saved}"


@mcp.tool()
@with_schematic_versions("file_path", write=True)
async def add_hierarchical_label(
    file_path: str,
    reference: str,
    pin: str,
    label_name: str = "",
    shape: str = "input",
    x: float | None = None,
    y: float | None = None,
    snap_to_grid: bool = True,
) -> str:
    """Add a hierarchical label on a component pin (cross-sheet connection).

    Hierarchical labels are how signals cross sheet boundaries in KiCad: a
    label placed in a sub-sheet appears as a same-named pin on the parent
    sheet's sheet symbol, connecting the two. Placing them by hand for
    every cross-sheet signal is the classic tedium this automates.

    The label anchors EXACTLY on the pin (computed from the embedded
    symbol geometry incl. rotation and the library-vs-sheet Y flip), so
    the connection is electrically valid — verify with generate_netlist.

    Args:
        file_path: Path to the .kicad_sch file (the SUB-sheet for signals
            going up to its parent).
        reference: Component reference (e.g. 'U1').
        pin: Pin number.
        label_name: Label text; defaults to the reference+pin when empty.
        shape: Electrical direction: input, output, bidirectional,
            tri_state or passive (ERC uses this).
        x/y: Explicit anchor coordinates (overrides reference/pin).
        snap_to_grid: Only for x/y. True: move the label to the nearest point
            of the connection grid of the project (default 1.27 mm), except on
            a connection point of the sheet (a pin, a wire, a label).

    Returns:
        Confirmation with the exact anchor position.
    """
    if shape not in _LABEL_SHAPES:
        return f"❌ Invalid shape '{shape}'. Options: {', '.join(_LABEL_SHAPES)}"
    name = label_name or f"{reference}.{pin}"
    if error := _multiline_label_error(name):
        return error
    try:
        return await _run_edit(
            _add_pin_label_sync,
            "hierarchical_label",
            file_path,
            reference,
            pin,
            name,
            shape,
            x,
            y,
            snap_to_grid,
        )
    except Exception as e:
        return error_answer(e, "Error adding hierarchical label")


@mcp.tool()
@with_schematic_versions("file_path", write=True)
async def add_global_label(
    file_path: str,
    reference: str,
    pin: str,
    label_name: str,
    shape: str = "input",
    x: float | None = None,
    y: float | None = None,
    snap_to_grid: bool = True,
) -> str:
    """Add a global label on a component pin (project-wide connection).

    Global labels connect same-named signals anywhere in the whole
    hierarchy regardless of sheets — useful for power-style rails and
    cross-cutting signals where a hierarchical parent/child chain would
    be awkward.

    Args:
        file_path: Path to the .kicad_sch file.
        reference: Component reference (e.g. 'U1').
        pin: Pin number.
        label_name: Global net name (required — globals are named nets).
        shape: Electrical direction: input, output, bidirectional,
            tri_state or passive.
        x/y: Explicit anchor coordinates (overrides reference/pin).
        snap_to_grid: Only for x/y. True: move the label to the nearest point
            of the connection grid of the project (default 1.27 mm), except on
            a connection point of the sheet (a pin, a wire, a label).

    Returns:
        Confirmation with the exact anchor position.
    """
    if shape not in _LABEL_SHAPES:
        return f"❌ Invalid shape '{shape}'. Options: {', '.join(_LABEL_SHAPES)}"
    if error := _multiline_label_error(label_name):
        return error
    try:
        return await _run_edit(
            _add_pin_label_sync,
            "global_label",
            file_path,
            reference,
            pin,
            label_name,
            shape,
            x,
            y,
            snap_to_grid,
        )
    except Exception as e:
        return error_answer(e, "Error adding global label")


def _add_component_from_library_sync(
    cancelled: threading.Event,
    file_path: str,
    library_name: str,
    symbol_name: str,
    reference: str,
    value: str,
    footprint: str,
    x: float,
    y: float,
    unit: int,
    sheet_path: str,
    snap_to_grid: bool,
) -> str:
    """The work of add_component_from_library. The function blocks (file reads, the parse, the
    save): the tool runs it with _run_edit."""
    path = Path(file_path)
    if not path.exists():
        return f"❌ The file {file_path} does not exist."
    content, stamp = read_for_write_stamped(path)
    version = writable_schematic_version(path, content)
    if isinstance(version, str):
        return version
    lib_id = f"{library_name}:{symbol_name}"
    try:
        project = schematic_project_file(path)
    except AmbiguousRootError as e:
        return f"❌ Cannot add component {lib_id}: {e}"

    # A definition in lib_symbols is in the format of the schematic: use it.
    symbol_block = _symbol_def_block(_lib_symbols_block(content), lib_id)
    kicad = None
    if symbol_block is not None:
        definition = "already in lib_symbols"
    else:
        kicad = kicad_cli_version_sync()
        if kicad is None:
            return f"❌ Cannot add component {lib_id}: {no_kicad_message()}"
        found = _library_definition(project, library_name, symbol_name, kicad, version)
        if isinstance(found, str):
            return found
        symbol_block, library_path, problems = found
        content = _insert_symbol_into_lib_symbols(content, symbol_block)
        definition = f"inserted into lib_symbols from {library_path}"
        if problems:
            # KiCad uses no row of these tables, as the tool, and shows the problems.
            definition += "\n**Library table problems (KiCad shows them too):** " + " ".join(
                problems
            )

    definition_node = parse(symbol_block)
    try:
        units = unit_count(definition_node)
    except KicadFileError as e:
        return f"❌ Cannot add component {lib_id}: {e}"
    if not 1 <= unit <= units:
        return (
            f"❌ Cannot add component {lib_id}: the symbol has {units} "
            f"unit{'s' if units > 1 else ''}, thus the unit must be from 1 to {units}, "
            f"not {unit}."
        )
    placed = _sheet_path(path, project, sheet_path)
    if isinstance(placed, str):
        return f"❌ Cannot add component {lib_id}: {placed}"
    sheet, sheet_count = placed
    settings = schematic_settings(project)
    grid = settings.connection_grid
    try:
        items = read_sheet(parse(content), settings.wire_widths)
    except (InputError, KicadFileError) as e:
        return (
            f"❌ Cannot add component {lib_id}: the sheet has an item that KiCad cannot read: {e}."
        )
    try:
        pins = symbol_pins(definition_node)
        unit_pins = library_pins(definition_node, 1, {unit})
        requested = Placement(iu(x), iu(y))
        where = requested
        touches = False
        connects = []
        if snap_to_grid:
            existing = items.connection_points()
            candidate = _symbol_position(requested, unit_pins, grid)
            # A pin on a connection point of the sheet must stay connected there.
            touches = any(
                not _keeps(requested.sheet_position(pin), candidate.sheet_position(pin), existing)
                for pin in unit_pins
            )
            if not touches:
                where = candidate
                connects = [
                    f"pin {pin.number} at {_shown(where.sheet_position(pin))}"
                    for pin in unit_pins
                    if _new_connections(
                        [(requested.sheet_position(pin), where.sheet_position(pin))], existing
                    )
                ]
        fields = placed_fields(definition_node, rules_for_version(version).kicad)
        component_entry = _placed_symbol(
            definition_node,
            fields,
            lib_id,
            {"Reference": reference, "Value": value, "Footprint": footprint or None},
            to_mm(where.x),
            to_mm(where.y),
            unit,
            pins,
            (project.stem, sheet.path),
        )
    except (KicadFileError, InputError) as e:
        return f"❌ Cannot add component {lib_id}: {e}"
    try:
        new_pins = junction_symbol(parse(component_entry), definition_node)
    except (InputError, KicadFileError) as e:
        return f"❌ Cannot add component {lib_id}: the library symbol: {e}."
    try:
        place_symbol(items, new_pins, _junction_rules(version), is_bus_label)
    except UnknownJunctionError as e:
        return f"❌ Cannot add component {lib_id}: {e}"
    content = _apply_sheet(content, items)
    content = _append_top_level(content, "\n" + component_entry)

    off_grid, off_grid_nc = [], []
    for pin in unit_pins:
        position = where.sheet_position(pin)
        if not on_grid(position, grid):
            group = off_grid_nc if pin.type == "no_connect" else off_grid
            group.append(f"pin {pin.number} at {_shown(position)}")
    position = _shown((where.x, where.y))
    if where != requested:
        position += (
            f", moved to the connection grid of {_mm(to_mm(grid))} mm from "
            f"{_shown((requested.x, requested.y))}"
        )
    if touches:
        position += (
            ", not moved: the move to the connection grid takes a pin away from a "
            "connection point of the sheet"
        )
    elif snap_to_grid and off_grid:
        position += ", the pins of the symbol cannot all be on the connection grid"
    if connects:
        position += (
            "\n**Note:** after the move to the connection grid these pins are on a "
            f"connection point of the sheet, thus they connect there: {', '.join(connects)}."
        )

    off_grid_text = ""
    if off_grid:
        installed = _installed_off_grid(settings, kicad, version)
        off_grid_text = _off_grid_text(off_grid, settings, installed)
    off_grid_text += _no_connect_text(off_grid_nc, settings, version)

    open_error, saved = _save(cancelled, path, content, stamp)
    if open_error:
        return open_error

    shared = ""
    if not project.is_file():
        # sch_hierarchy cannot find a project outside the directory tree.
        shared = (
            f"\n**Note:** The project file {project.name} does not exist. The reference is "
            f"for the sheet {sheet.shown} of {project.with_suffix('.kicad_sch').name} as the "
            "schematic editor opens it with no project. If the schematic is a sub-sheet of a "
            "project in an other directory tree, KiCad shows the Reference field there "
            "until the user annotates the schematic.\n"
        )
    if sheet_count > 1:
        # SCH_SYMBOL::GetRef uses the Reference field for a path with no instance.
        shared += (
            f"\n**Note:** The hierarchy uses this schematic {sheet_count} times. Only "
            f"{sheet.shown} has the reference {reference}. KiCad shows the Reference field, "
            f"{reference}, also for the other sheets until the user annotates the schematic.\n"
        )
    return f"""✅ Component added successfully!

**File:** {file_path}
**Library:** {library_name}
**Symbol:** {symbol_name}
**Reference:** {reference}
**Value:** {value}
**Position:** {position}
**Lib ID:** {lib_id}
**Sheet:** {sheet.shown}
**Symbol definition:** {definition}
**Pins:** {len(pins)} pins detected
{_junction_report(items)}{off_grid_text}{_grid_note(settings)}{shared}{saved}
"""


@mcp.tool()
@with_schematic_versions("file_path", write=True)
async def add_component_from_library(
    file_path: str,
    library_name: str,
    symbol_name: str,
    reference: str,
    value: str,
    footprint: str = "",
    x: float = 100,
    y: float = 100,
    unit: int = 1,
    sheet_path: str = "",
    snap_to_grid: bool = True,
) -> str:
    """Add a symbol of a KiCad symbol library to the schematic.

    The library comes from the library tables of the installed KiCad and of the
    project, as in the symbol chooser of KiCad: a library of the project table
    replaces a global library with the same name. A derived symbol gets the
    units, the pins and the drawings of its parents. When the schematic has the
    symbol in its lib_symbols section, the tool uses that definition. The tool
    gets the version of the installed KiCad from kicad-cli, to find the global
    library table.

    The placed symbol gets the fields of the library symbol (Datasheet,
    Description, user fields) at their library positions, the flags
    exclude_from_sim, in_bom, on_board and in_pos_files of the library symbol,
    and the reference of its sheet path, as KiCad places a symbol. The tool
    does not move the fields (KiCad: autoplace).

    The connection grid is schematic.connection_grid_size of the project file
    (default 1.27 mm). ERC finds a pin off this grid (endpoint_off_grid).

    As the schematic editor of KiCad, the tool removes the part of a wire
    between two pins of the new symbol, and adds a junction where a pin is in
    the middle of a wire. The output tells each junction and each changed wire.

    Args:
        file_path: The .kicad_sch file.
        library_name: The library name of the library tables.
        symbol_name: The symbol name in the library.
        reference: The reference, for example "R1".
        value: The text of the Value field.
        footprint: The footprint. Empty: the footprint of the library symbol.
        x, y: The position in mm.
        unit: The unit, from 1 to the number of units of the symbol.
        sheet_path: Only for a sheet file that the hierarchy uses more than
            one time: the sheet that gets the reference, as the sheet names
            ("/Power/") or the UUID path. The error lists the sheet paths.
        snap_to_grid: True: move the symbol to the nearest position that puts
            its pins on the connection grid. When the move takes a pin away
            from a connection point of the sheet (a pin, a wire, a label), the
            symbol stays at x, y. False: use x, y as they are. The output
            tells the pins off the grid.
    """
    for field, text in (("Reference", reference), ("Value", value), ("Footprint", footprint)):
        if error := _field_error(field, text):
            return error
    if not (math.isfinite(x) and math.isfinite(y)):
        return f"❌ The position ({x}, {y}) is not a position in mm."
    try:
        return await _run_edit(
            _add_component_from_library_sync,
            file_path,
            library_name,
            symbol_name,
            reference,
            value,
            footprint,
            x,
            y,
            unit,
            sheet_path,
            snap_to_grid,
        )
    except Exception as e:
        return error_answer(e, "Error adding component")


def _add_wire_sync(
    cancelled: threading.Event,
    file_path: str,
    points: list[tuple[float, float]],
    snap_to_grid: bool,
) -> str:
    """The work of add_wire. The function blocks (file reads, the parse, the
    save): the tool runs it with _run_edit."""
    if len(points) < 2:
        return "❌ A wire needs two or more points."
    for point in points:
        if not all(math.isfinite(value) for value in point):
            return f"❌ The point {tuple(point)} is not a position in mm."
    requested = [(iu(x), iu(y)) for x, y in points]
    if error := _zero_segment(requested, ""):
        return error

    path = Path(file_path)
    if not path.exists():
        return f"❌ The file {file_path} does not exist."
    content, stamp = read_for_write_stamped(path)
    version = writable_schematic_version(path, content)
    if isinstance(version, str):
        return version
    settings = _project_settings(path)
    if isinstance(settings, _NoGrid) and snap_to_grid:
        return f"❌ Cannot add the wire: {settings.reason}"
    try:
        root = parse(content)
        sheet = read_sheet(root, settings.wire_widths)
    except (InputError, KicadFileError) as e:
        return f"❌ Cannot add the wire: the sheet has an item that KiCad cannot read: {e}."
    final = requested
    if snap_to_grid:
        existing = sheet.connection_points()
        final = _snapped_wire(requested, settings.connection_grid, existing)
        if error := _zero_segment(final, " after the move to the connection grid"):
            return error

    segments = [
        Line(start, end, False, (0, "solid", (0.0, 0.0, 0.0, 0.0)), str(uuid.uuid4()), new=True)
        for start, end in itertools.pairwise(final)
    ]
    junctions_before = list(sheet.junctions)
    try:
        finish_wire(sheet, segments, _junction_rules(version), is_bus_label)
    except UnknownJunctionError as e:
        return f"❌ Cannot add the wire: {e}"
    added = [line for line in sheet.lines if line.new and line.node is None]
    # A wire of the file that the new wire extends (a wire inside it: no change), or
    # that a new wire takes in (CleanUp keeps the first line of the pair).
    merged = [line for line in sheet.lines if line.new and line.node is not None and line.moved]
    merged += [
        line
        for line in added
        if any(
            on_segment(old.original[0], (line.start, line.end))
            and on_segment(old.original[1], (line.start, line.end))
            and old.bus == line.bus
            for old in sheet.removed
        )
    ]
    file_changes = [line for line in sheet.lines if line.edited_in_file]
    if not (
        added or file_changes or sheet.removed or sheet.added_junctions or sheet.removed_junctions
    ):
        return (
            "✅ No change: the sheet has this wire already (KiCad: CleanUp removes a "
            "wire with the ends of an other wire)."
        )
    content = _apply_sheet(content, sheet)
    off_grid_text = ""
    if not isinstance(settings, _NoGrid):
        off_grid = [
            _shown(point)
            for point in dict.fromkeys(final)
            if not on_grid(point, settings.connection_grid)
        ]
        if off_grid:
            installed = _installed_off_grid(settings, None, version)
            off_grid_text = _off_grid_text(off_grid, settings, installed)
    open_error, saved = _save(cancelled, path, content, stamp)
    if open_error:
        return open_error

    lines = "".join(f"\n- {_shown(line.start)} to {_shown(line.end)}" for line in added)
    text = (
        f"✅ Wire added: {len(added)} segment{'s' if len(added) != 1 else ''}{lines}\n"
        if added
        else "✅ Wire added.\n"
    )
    if merged:
        text += (
            "\nThe wire is merged with a wire of the sheet on the same straight line "
            "(KiCad: CleanUp): "
            + ", ".join(f"{_shown(line.start)} to {_shown(line.end)}" for line in merged)
            + ".\n"
        )
    text += _junction_report(sheet)
    if isinstance(settings, _NoGrid):
        return text + _grid_note(settings) + saved
    grid = settings.connection_grid
    moves = [
        f"{_shown(old)} to {_shown(new)}"
        for old, new in zip(requested, final, strict=True)
        if old != new
    ]
    if moves:
        text += (
            f"\nPoints moved to the connection grid of {_mm(to_mm(grid))} mm: {', '.join(moves)}.\n"
        )
    connects = []
    if snap_to_grid:
        connects = _new_connections(list(zip(requested, final, strict=True)), existing)
        # A label connects anywhere on a wire, also without a junction.
        # A label or a junction connects in the middle of a wire, also without a new
        # junction.
        written = [(line.start, line.end) for line in sheet.lines if line.new]
        old_parts = [
            line.original
            for line in [*sheet.lines, *sheet.removed]
            if line.original and (line.new or line in sheet.removed)
        ]
        middles = [label.position for label in sheet.labels] + [
            point
            for point in junctions_before
            if point in sheet.junctions and point not in sheet.added_junctions
        ]
        for point in dict.fromkeys(middles):
            after = any(on_segment(point, segment) for segment in written)
            # Not new: on the requested wire, or on the part of the file of a wire
            # that the new wire extends.
            before = any(
                on_segment(point, segment)
                for segment in [*itertools.pairwise(requested), *old_parts]
            )
            if after and not before and point not in connects:
                connects.append(point)
    if connects:
        text += (
            "\n**Note:** after the move to the connection grid these points are on a "
            "connection point of the sheet, thus the wire connects there: "
            f"{', '.join(_shown(point) for point in connects)}.\n"
        )
    return text + off_grid_text + _grid_note(settings) + saved


@mcp.tool()
@with_schematic_versions("file_path", write=True)
async def add_wire(
    file_path: str,
    points: list[tuple[float, float]],
    snap_to_grid: bool = True,
) -> str:
    """Add a wire to the schematic: a line through two or more points.

    A KiCad wire has two points (SCH_LINE). The tool adds one wire for each
    segment, as KiCad saves a line with corners.

    As the schematic editor of KiCad, the tool merges the segments that are
    on one straight line, merges a new wire with a wire of the sheet on the
    same straight line that it touches, removes the part of a new wire
    between the two pins of a symbol with two pins, and adds a junction where
    three or more wire directions meet (a wire end or a pin in the middle of
    a wire). The output tells each junction and each changed wire.

    The connection grid is schematic.connection_grid_size of the project file
    (default 1.27 mm). ERC finds a wire end off this grid (endpoint_off_grid).

    Args:
        file_path: The .kicad_sch file.
        points: The points (x, y) in mm. Each point after the first ends a segment.
        snap_to_grid: True: move each point to the nearest point of the
            connection grid. A point on a connection point of the sheet (a
            pin, a wire end, a junction, a label) stays. A point in the middle
            of a wire moves only along the wire. A horizontal or vertical
            segment stays horizontal or vertical. False: use the points as
            they are. The output tells the points off the grid.
    """
    try:
        return await _run_edit(_add_wire_sync, file_path, points, snap_to_grid)
    except Exception as e:
        return error_answer(e, "Error adding wire")


def _add_label_sync(
    cancelled: threading.Event,
    file_path: str,
    text: str,
    x: float,
    y: float,
    orientation: float,
    snap_to_grid: bool,
) -> str:
    """The work of add_label. The function blocks (file reads, the parse, the
    save): the tool runs it with _run_edit."""
    path = Path(file_path)
    if not path.exists():
        return f"❌ The file {file_path} does not exist."
    content, stamp = read_for_write_stamped(path)
    found = _label_position(path, content, x, y, snap_to_grid)
    if isinstance(found, str):
        return found
    point, where, note = found
    label_entry = (
        f"\t(label {quote(text)} (at {_xy(point)} {orientation})\n"
        "\t\t(effects (font (size 1.27 1.27)) (justify left))\n"
        f'\t\t(uuid "{uuid.uuid4()}")\n'
        "\t)\n"
    )
    content = _append_top_level(content, label_entry)

    open_error, saved = _save(cancelled, path, content, stamp)
    if open_error:
        return open_error
    return f"✅ Label '{text}' added {where}{note}{saved}"


@mcp.tool()
@with_schematic_versions("file_path", write=True)
async def add_label(
    file_path: str,
    text: str,
    x: float,
    y: float,
    orientation: float = 0,
    snap_to_grid: bool = True,
) -> str:
    """Add a local label (text label) to the schematic.

    Args:
        file_path: The .kicad_sch file.
        text: The label text.
        x, y: The position in mm.
        orientation: The angle of the label.
        snap_to_grid: True: move the label to the nearest point of the
            connection grid of the project (default 1.27 mm), except on a
            connection point of the sheet (a pin, a wire, a label).
    """
    if error := _multiline_label_error(text):
        return error
    try:
        return await _run_edit(_add_label_sync, file_path, text, x, y, orientation, snap_to_grid)
    except Exception as e:
        return error_answer(e, "Error adding label")
