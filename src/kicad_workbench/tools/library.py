"""Library tools: the symbol and footprint libraries of KiCad (read only)."""

import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastmcp.utilities.types import Image

from ..parsers.annotation import (
    UNANNOTATED_NOTE,
    UNANNOTATED_ROWS,
    AnnotationCheck,
    AnnotationError,
    AnnotationItem,
    check_annotation,
)
from ..parsers.board_netlist import NetlistComponent, NetlistPin, read_board_netlist
from ..parsers.kicad_versions import (
    KICAD8,
    KICAD9,
    KICAD10,
    KicadFileError,
    kicad_release,
    version_numbers,
    version_triple,
)
from ..parsers.lib_symbols import LibrarySymbol, read_library
from ..parsers.sch_geometry import mm_text
from ..parsers.sexpr import SExprError, parse
from ..parsers.symbol_info import SymbolInfo, SymbolPin, symbol_info
from ..server import mcp
from ..utils.kicad_cli import (
    decode_output,
    export_with_kicad_cli,
    get_kicad_version,
    run_kicad_cli_sync,
)
from ..utils.kicad_text import KiCadEncodingError, decode_as_kicad, read_as_kicad
from ..utils.library_footprint import (
    FootprintInfo,
    FootprintItems,
    FootprintPad,
    FootprintReadError,
    PadShape,
    read_footprints,
    read_library_footprint,
)
from ..utils.library_index import LibraryItem, library_items, row_problem, search
from ..utils.library_lookup import LibraryLookupError, find_kicad_library, find_library_symbol
from ..utils.library_tables import (
    LibraryRow,
    active_rows,
    kicad_environment,
    library_rows,
    project_variables,
)
from ..utils.model_paths import ModelFile, resolve_model
from ..utils.pin_pad_mapping import PinPadMapping, StackedPinError, map_pins, match_pads
from ..utils.sch_hierarchy import (
    AmbiguousRootError,
    schematic_project_file,
    top_level_sheets,
)
from ..utils.svg_image import (
    Plan,
    RegionError,
    SvgImageError,
    SvgPage,
    plan_images,
    read_svg,
    render_tile,
    required_scale,
)
from ..utils.tool_errors import error_answer

_KINDS = ("symbol", "footprint")
NO_KICAD_CLI = (
    "kicad-cli was not found, thus the KiCad version and its library tables are not known."
)
_SYMBOL_ID_ERROR = '❌ The library ID {!r} is not "<library>:<symbol>", for example "Device:R".'
_FOOTPRINT_ID_ERROR = (
    '❌ The library ID {!r} is not "<library>:<footprint>", for example '
    '"Resistor_SMD:R_0603_1608Metric".'
)


def project_file(project_path: str) -> Path | None:
    """Return the .kicad_pro file of a project path, or None for no project.

    The path can be the .kicad_pro file, the project directory with one
    .kicad_pro file, or a .kicad_sch or .kicad_pcb file. A schematic gives the
    project that KiCad uses for it (sch_hierarchy.schematic_project_file, also
    for a KiCad 10 project with more than one top-level sheet). A board gives
    the .kicad_pro file with the same name, else the one .kicad_pro file of its
    directory.

    Raises:
        ValueError: The path gives no project or more than one.
    """
    if not project_path:
        return None
    path = Path(project_path)
    if path.is_dir():
        directory = path
    elif path.suffix == ".kicad_pro" and path.is_file():
        return path
    elif path.suffix == ".kicad_sch" and path.is_file():
        try:
            return schematic_project_file(path)
        except AmbiguousRootError as e:
            raise ValueError(str(e)) from e
    elif path.suffix == ".kicad_pcb" and path.is_file():
        same_name = path.with_suffix(".kicad_pro")
        if same_name.is_file():
            return same_name
        directory = path.parent
    else:
        raise ValueError(f"No KiCad project at {path}.")
    projects = sorted(directory.glob("*.kicad_pro"))
    if len(projects) != 1:
        raise ValueError(
            f"The directory {directory} has {len(projects)} .kicad_pro files. "
            "Give the .kicad_pro file of the project."
        )
    return projects[0]


async def _rows(kind: str, project_path: str) -> tuple[list[LibraryRow], list[str]]:
    """Return the library rows of the installed KiCad and of the project."""
    if kind not in _KINDS:
        raise ValueError(f"The kind {kind!r} is not 'symbol' or 'footprint'.")
    version = await get_kicad_version()
    if version is None:
        raise ValueError(NO_KICAD_CLI)
    return await asyncio.to_thread(lambda: library_rows(kind, version, project_file(project_path)))


def cell(text: str) -> str:
    """Make a text safe for one cell of a Markdown table."""
    return " ".join(text.split()).replace("|", "\\|")


def _result_line(no_pad: int, no_number: int, no_pin: int, not_checked: int = 0) -> str | None:
    """The result line of a pin and pad check, or None when nothing is wrong.

    The counts are pin numbers, pins and pads. "Update PCB from Schematic"
    gives a message for each pin of the netlist, and the netlist can have a pin
    number more than one time (a pin of more than one unit).
    """
    parts = [
        (no_pad, "pin number with no pad", "pin numbers with no pad"),
        (no_number, "pin with no number", "pins with no number"),
        (not_checked, "symbol that the tool did not check", "symbols that the tool did not check"),
        (no_pin, "pad with no pin", "pads with no pin"),
    ]
    text = ", ".join(f"{n} {one if n == 1 else more}" for n, one, more in parts if n)
    if not text:
        return None
    return f"**Result**: {'❌' if no_pad or no_number or not_checked else '⚠️'} {text}"


def problem_lines(problems: list[str]) -> list[str]:
    if not problems:
        return []
    return ["", "## Problems", *(f"- {problem}" for problem in problems)]


def _plural(count: int) -> str:
    return f"{count} librar{'y' if count == 1 else 'ies'}"


def _state(row: LibraryRow, active: bool) -> str:
    """The state of a library row, as the library tables of KiCad give it."""
    if not active:
        return "not used: a later library has this name"
    if problem := row_problem(row):
        return problem
    return "hidden" if row.hidden else "ok"


async def _list(kind: str, project_path: str) -> str:
    rows, problems = await _rows(kind, project_path)
    active = {id(row) for row in active_rows(rows)}
    states = await asyncio.to_thread(lambda: [_state(row, id(row) in active) for row in rows])
    lines = [
        f"# {kind.capitalize()} libraries",
        f"Total: {len(rows)} rows, {_plural(len(active))}",
        "",
        "| Name | Scope | Type | State | Path |",
        "|------|-------|------|-------|------|",
    ]
    for row, state in zip(rows, states, strict=True):
        lines.append(
            f"| {cell(row.name)} | {row.scope} | {cell(row.type)} | {cell(state)} "
            f"| {cell(str(row.path or row.uri))} |"
        )
    return "\n".join(lines + problem_lines(problems))


@mcp.tool()
async def list_libraries(kind: str = "symbol", project_path: str = "") -> str:
    """List the symbol or footprint libraries of KiCad and of a project.

    The libraries come from the library tables: the global table of the
    installed KiCad version (with its nested tables), then the table of the
    project. When more than one row has the same library name, KiCad uses the
    last one: a project library replaces a global library.

    Args:
        kind: "symbol" or "footprint".
        project_path: Optional .kicad_pro, .kicad_sch or .kicad_pcb file, or
            the project directory.

    Returns:
        A table of the library rows: name, scope, type, state and path.
    """
    try:
        return await _list(kind, project_path)
    except Exception as e:
        return error_answer(e, "Error")


def _read_and_search(
    kind: str, rows: list[LibraryRow], query: str
) -> tuple[list[LibraryItem], list[str], int]:
    """Read the libraries (from the index when it is current) and search them.

    Returns:
        (results, notes, number of libraries searched).
    """
    items: list[LibraryItem] = []
    notes: list[str] = []
    searched = 0
    for row in rows:
        if row.type != "KiCad":
            notes.append(
                f"The library {row.name} has the type {row.type!r}. "
                "The search reads only libraries of the type 'KiCad'."
            )
            continue
        if row.problems:
            notes.extend(f"The library {row.name}: {problem}" for problem in row.problems)
            continue
        found, row_problems = library_items(kind, row)
        items.extend(found)
        notes.extend(row_problems)
        if found or not row_problems:
            searched += 1
    return search(items, query), notes, searched


async def _search(
    kind: str, query: str, project_path: str, library: str, limit: int, include_hidden: bool
) -> str:
    if not query.strip():
        return "❌ The query is empty."
    if limit < 1:
        return "❌ The limit must be 1 or more."
    rows, problems = await _rows(kind, project_path)
    rows = active_rows(rows)
    if library:
        # A library that the user names is searched also when it is hidden.
        selected = [row for row in rows if row.name == library]
        if not selected:
            return f"❌ No {kind} library {library!r}."
        if selected[0].disabled:
            return f"❌ The {kind} library {library!r} is disabled."
    else:
        # KiCad does not load a disabled library, and its chooser does not
        # show a hidden library.
        selected = [row for row in rows if not row.disabled and (include_hidden or not row.hidden)]

    results, notes, searched = await asyncio.to_thread(_read_and_search, kind, selected, query)
    lines = [
        f"# {kind.capitalize()} search: {query}",
        "",
        f"Found: {len(results)} in {_plural(searched)}"
        + (f" (the first {limit} below)" if len(results) > limit else ""),
    ]
    if results and kind == "symbol":
        lines += [
            "",
            "| Library ID | Description | Keywords | Footprint |",
            "|------------|-------------|----------|-----------|",
        ]
        for item in results[:limit]:
            lines.append(
                f"| {cell(item.lib_id)} | {cell(item.description)} "
                f"| {cell(item.keywords)} | {cell(item.footprint)} |"
            )
    elif results:
        lines += ["", "| Library ID | Description | Tags |", "|------------|-------------|------|"]
        for item in results[:limit]:
            lines.append(
                f"| {cell(item.lib_id)} | {cell(item.description)} | {cell(item.keywords)} |"
            )
    return "\n".join(lines + problem_lines(problems + notes))


async def _search_tool(*args) -> str:
    try:
        return await _search(*args)
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
async def search_library_symbols(
    query: str,
    project_path: str = "",
    library: str = "",
    limit: int = 50,
    include_hidden: bool = False,
) -> str:
    """Search the symbols of the KiCad libraries by name, description and keywords.

    Each word of the query must be in the library ID ("Device:R"), the
    description, the keywords or the default footprint. A derived symbol gets
    the empty fields from its parent. The order is that of the symbol chooser
    of KiCad: an exact name first, then the best score. The first search reads
    all libraries (some seconds); then an index in the user cache directory is
    used until a library file changes.

    Args:
        query: Words to find, for example "op amp rail-to-rail".
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries.
        library: Optional name of one library to search (also when it is hidden).
        limit: The maximum number of results in the table.
        include_hidden: Also search libraries that are hidden in KiCad.

    Returns:
        A table of the symbols: library ID, description, keywords and footprint.
    """
    return await _search_tool("symbol", query, project_path, library, limit, include_hidden)


@mcp.tool()
async def search_library_footprints(
    query: str,
    project_path: str = "",
    library: str = "",
    limit: int = 50,
    include_hidden: bool = False,
) -> str:
    """Search the footprints of the KiCad libraries by name, description and tags.

    Each word of the query must be in the library ID
    ("Resistor_SMD:R_0603_1608Metric"), the description or the tags. The order
    is that of the footprint chooser of KiCad: an exact name first, then the
    best score. The first search reads all libraries (some seconds); then an
    index in the user cache directory is used until a library file changes.

    Args:
        query: Words to find, for example "0603 resistor".
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries.
        library: Optional name of one library to search (also when it is hidden).
        limit: The maximum number of results in the table.
        include_hidden: Also search libraries that are hidden in KiCad.

    Returns:
        A table of the footprints: library ID, description and tags.
    """
    return await _search_tool("footprint", query, project_path, library, limit, include_hidden)


def _field_rules(kicad: str) -> int:
    """Return the field rules of the installed KiCad: 9 (KiCad 8 and 9) or 10.

    Raises:
        ValueError: The server has no rules for the version.
    """
    release = kicad_release(kicad)
    if release in (KICAD8, KICAD9):
        return 9
    # The nightly builds 10.99 (branch master) read the fields as KiCad 10
    # (kicad_fields; the server knows the rules of master of 2026-11-02).
    if release is KICAD10 or version_numbers(kicad) == (10, 99):
        return 10
    raise ValueError(
        f"The installed KiCad is {kicad}. This server knows the rules of KiCad 8.0, 9.0 "
        "and 10.0, and of the nightly builds 10.99."
    )


def _version_problem(symbol: LibrarySymbol, kicad: str) -> str | None:
    """Return why the server or the installed KiCad cannot read a library file, or None."""
    version = symbol.version
    if version is None:
        return f"The symbol library file {symbol.file} has no file version, (version N)."
    if version < KICAD8.symbol_library:
        return (
            f"The symbol library file {symbol.file} has the file version {version}, which "
            f"is older than the version of KiCad 8.0 ({KICAD8.symbol_library}). This server "
            "reads the libraries of KiCad 8 and later. Open the library in the symbol "
            "editor of KiCad 8 or later and save it."
        )
    if version > KICAD10.symbol_library:
        return (
            f"The symbol library file {symbol.file} has the file version {version}, which "
            f"is newer than the version of KiCad 10.0 ({KICAD10.symbol_library}). This "
            "server has no rules for a library of a nightly build."
        )
    release = kicad_release(kicad)
    if release is not None and version > release.symbol_library:
        return (
            f"The symbol library file {symbol.file} has the file version {version}, which "
            f"is newer than the version of KiCad {release.name} ({release.symbol_library}). "
            f"The installed KiCad {kicad} cannot read it."
        )
    return None


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _unit_text(info: SymbolInfo, unit: int) -> str:
    if unit == 0:
        return "all"
    if info.unit_count == 1:
        return str(unit)
    return f"{unit} ({info.unit_names.get(unit, '?')})"


def _style_text(info: SymbolInfo, style: int) -> str:
    if style == 0:
        return "all"
    if style <= len(info.body_styles):
        return f"{style} ({info.body_styles[style - 1]})"
    return str(style)


def _pin_row(info: SymbolInfo, pin: SymbolPin) -> str:
    alternates = "; ".join(f"{alt.name} ({alt.type}, {alt.shape})" for alt in pin.alternates)
    return (
        f"| {cell(pin.number)} | {cell(pin.name)} | {pin.type} | {pin.shape} "
        f"| {_unit_text(info, pin.unit)} | {_style_text(info, pin.body_style)} "
        f"| {mm_text(pin.x)} | {mm_text(pin.y)} | {pin.orientation} | {mm_text(pin.length)} "
        f"| {'yes' if pin.hidden else ''} | {cell(alternates)} |"
    )


def _symbol_text(
    lib_id: str, row: LibraryRow, chain: list[LibrarySymbol], info: SymbolInfo
) -> list[str]:
    symbol, root = chain[0], chain[-1]
    lines = [
        f"# Library symbol {lib_id}",
        "",
        f"**Library**: {row.name} ({row.scope}): {symbol.file}",
        f"**File version**: {symbol.version}",
    ]
    if len(chain) > 1:
        parents = " → ".join(parent.name for parent in chain[1:])
        lines.append(
            f"**Derived from**: {parents}. The units, the pins and the drawings come from "
            f"{root.name}" + (f" ({root.file})" if root.file != symbol.file else "") + "."
        )
    if info.power:
        lines.append(f"**Power symbol**: {info.power}")
    units = str(info.unit_count)
    if info.unit_count > 1:
        units += ", not interchangeable" if info.units_locked else ", interchangeable"
    lines.append(f"**Units**: {units}")
    lines.append(f"**Body styles**: {', '.join(info.body_styles) or '1'}")
    lines.append(
        f"**In BOM**: {_yes(info.in_bom)}, **On board**: {_yes(info.on_board)}, "
        f"**In position files**: {_yes(info.in_pos_files)}, "
        f"**Excluded from simulation**: {_yes(info.exclude_from_sim)}"
    )
    pin_names = (
        f"shown, offset {mm_text(info.pin_name_offset)} mm" if info.pin_names_shown else "hidden"
    )
    lines.append(
        f"**Pin names**: {pin_names}, **Pin numbers**: "
        + ("shown" if info.pin_numbers_shown else "hidden")
    )
    if info.duplicate_pins_are_jumpers:
        lines.append("**Duplicate pin numbers are jumpers**: yes")
    if info.jumper_pin_groups:
        groups = "; ".join(", ".join(group) for group in info.jumper_pin_groups)
        lines.append(f"**Jumper pin groups**: {cell(groups)}")
    lines.append(f"**Keywords**: {cell(info.keywords)}")
    lines.append(f"**Footprint filters**: {cell(' '.join(info.footprint_filters))}")

    lines += ["", "## Fields", "", "| Field | Value |", "|-------|-------|"]
    lines += [f"| {cell(name)} | {cell(value)} |" for name, value in info.fields]
    if info.unit_count > 1:
        lines += ["", "## Units", "", "| Unit | Name |", "|------|------|"]
        lines += [f"| {unit} | {cell(name)} |" for unit, name in info.unit_names.items()]
    lines += [
        "",
        f"## Pins ({len(info.pins)})",
        "",
        "| Number | Name | Type | Shape | Unit | Body style | X (mm) | Y (mm) | Orientation "
        "| Length (mm) | Hidden | Alternates |",
        "|--------|------|------|-------|------|------------|--------|--------|-------------"
        "|-------------|--------|------------|",
    ]
    lines += [_pin_row(info, pin) for pin in info.pins]
    lines += [
        "",
        "_The pin positions are in the coordinates of the library: the Y axis points up. "
        "The position is the end of the pin that connects._",
    ]
    return lines


def _read_symbol(
    library_name: str, symbol_name: str, kicad: str, project_path: str
) -> tuple[list[LibrarySymbol], LibraryRow, list[str], SymbolInfo]:
    """Read a library symbol and its parents as the installed KiCad reads them.

    Raises:
        LibraryLookupError: The symbol is not there, or the server or KiCad
            cannot read it.
        ValueError: The server has no rules for the installed KiCad.
    """
    rules = _field_rules(kicad)
    chain, row, problems = find_library_symbol(
        library_name, symbol_name, kicad, project_file(project_path)
    )
    for symbol in chain:
        if problem := _version_problem(symbol, kicad):
            raise LibraryLookupError(problem)
    try:
        return chain, row, problems, symbol_info(chain, rules)
    except KicadFileError as e:
        raise LibraryLookupError(str(e)) from e


async def _get_symbol(lib_id: str, project_path: str) -> str:
    library_name, _, symbol_name = lib_id.partition(":")
    if not library_name or not symbol_name:
        return _SYMBOL_ID_ERROR.format(lib_id)
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"

    try:
        chain, row, problems, info = await asyncio.to_thread(
            _read_symbol, library_name, symbol_name, kicad, project_path
        )
    except LibraryLookupError as e:
        return f"❌ Cannot read the symbol {lib_id}: {e}"
    return "\n".join(_symbol_text(lib_id, row, chain, info) + problem_lines(problems))


@mcp.tool()
async def get_library_symbol(lib_id: str, project_path: str = "") -> str:
    """Show the content of a symbol of a KiCad library: fields, units and pins.

    The library comes from the library tables of the installed KiCad and of
    the project, as in the symbol chooser of KiCad. A derived symbol (extends
    ...) is shown as KiCad places it: the fields of the symbol and of its
    parents, the flags of its parent, and the units and pins of its root symbol.

    Args:
        lib_id: The library ID, for example "Device:R" or "MCU_ST_STM32F4:STM32F405RGTx".
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries.

    Returns:
        The library file, the parents, the units, the body styles, the flags,
        the keywords, the footprint filters, the fields, and a table of the pins
        with their alternate functions.
    """
    try:
        return await _get_symbol(lib_id, project_path)
    except Exception as e:
        return error_answer(e, "Error")


def _nm(value: int) -> str:
    """Return a length in nanometers (the unit of pcbnew) as millimeters."""
    whole, part = divmod(abs(int(value)), 1_000_000)
    text = f"{whole}.{part:06d}".rstrip("0").rstrip(".")
    return f"-{text}" if value < 0 and text != "0" else text


def _number(value: float, digits: int = 6) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _xy(point: tuple[int, int]) -> str:
    return f"({_nm(point[0])}, {_nm(point[1])})"


def _size(size: tuple[int, int]) -> str:
    return f"{_nm(size[0])} × {_nm(size[1])}"


_PAD_TYPES = {"pth": "PTH", "smd": "SMD", "conn": "connector", "npth": "NPTH"}
# The bits of the chamfered corners (RECT_CHAMFER_POSITIONS of KiCad).
_CHAMFERS = ((1, "top left"), (2, "top right"), (4, "bottom left"), (8, "bottom right"))


def _shape_details(shape: PadShape) -> list[str]:
    """The values of a pad shape that its shape uses, other than the size."""
    details = []
    if shape.shape in ("roundrect", "chamfered_rect"):
        details.append(f"corner radius ratio {_number(shape.roundrect_ratio)}")
    if shape.shape == "chamfered_rect":
        corners = [name for bit, name in _CHAMFERS if shape.chamfer_positions & bit]
        details.append(
            f"chamfer ratio {_number(shape.chamfer_ratio)} ({', '.join(corners) or 'no corner'})"
        )
    if shape.shape == "trapezoid":
        details.append(f"delta {_xy(shape.delta)} mm")
    if shape.shape == "custom":
        details.append(f"anchor {shape.anchor_shape}")
    if any(shape.offset):
        details.append(f"offset {_xy(shape.offset)} mm")
    return details


def _layer_group(layers: tuple[str, ...]) -> str:
    if len(layers) > 3:
        return f"{layers[0]} to {layers[-1]} ({len(layers)} layers)"
    return ", ".join(layers)


def _local_settings(item: FootprintInfo | FootprintPad) -> list[str]:
    """The local clearance and zone settings of a footprint or a pad."""
    settings = []
    if item.clearance is not None:
        settings.append(f"clearance {_nm(item.clearance)} mm")
    if item.solder_mask_margin is not None:
        settings.append(f"solder mask margin {_nm(item.solder_mask_margin)} mm")
    if item.solder_paste_margin is not None:
        settings.append(f"solder paste margin {_nm(item.solder_paste_margin)} mm")
    if item.solder_paste_ratio is not None:
        settings.append(f"solder paste ratio {_number(item.solder_paste_ratio)}")
    if item.zone_connection != "inherited":
        settings.append(f"zone connection {item.zone_connection.replace('_', ' ')}")
    return settings


def _pad_row(pad: FootprintPad) -> str:
    details = _shape_details(pad.shape)
    if pad.padstack != "normal":
        shapes = "; ".join(
            f"{_layer_group(shape.layers)}: "
            + ", ".join([f"{shape.shape} {_size(shape.size)} mm", *_shape_details(shape)])
            for shape in pad.layer_shapes
        )
        details.append(f"padstack {pad.padstack.replace('_', '/')}: {shapes}")
    if pad.pin_function:
        details.append(f"pin function {pad.pin_function}")
    if pad.pin_type:
        details.append(f"pin type {pad.pin_type}")
    details += _local_settings(pad)
    drill = ""
    if any(pad.drill):
        drill = _nm(pad.drill[0]) if pad.drill_shape == "circle" else f"{_size(pad.drill)} oblong"
    return (
        f"| {cell(pad.number)} | {_PAD_TYPES.get(pad.type, pad.type)} | {pad.shape.shape} "
        f"| {_nm(pad.position[0])} | {_nm(pad.position[1])} | {_number(pad.orientation)} "
        f"| {_size(pad.shape.size)} | {drill} | {' '.join(pad.layers)} | {cell('; '.join(details))} |"
    )


def _courtyard_lines(info: FootprintInfo) -> list[str]:
    lines = [
        "",
        "## Courtyard",
        "",
        "| Layer | State | Outlines | Size (mm) | From (mm) | To (mm) | Area (mm²) |",
        "|-------|-------|----------|-----------|-----------|---------|------------|",
    ]
    for courtyard in info.courtyard:
        state = "malformed" if courtyard.malformed else "ok" if courtyard.outlines else "none"
        size = start = end = area = ""
        if courtyard.box is not None:
            x0, y0, x1, y1 = courtyard.box
            size, start, end = _size((x1 - x0, y1 - y0)), _xy((x0, y0)), _xy((x1, y1))
            area = _number(courtyard.area / 1e12)
        lines.append(
            f"| {courtyard.layer} | {state} | {courtyard.outlines} | {size} | {start} | {end} "
            f"| {area} |"
        )
    # DRC_TEST_PROVIDER_COURTYARD_CLEARANCE::testFootprintCourtyardDefinitions.
    if any(courtyard.malformed for courtyard in info.courtyard):
        drc = (
            "The DRC of KiCad reports a malformed courtyard: the shapes on a courtyard "
            "layer do not make closed outlines."
        )
    elif all(courtyard.outlines == 0 for courtyard in info.courtyard):
        # The default severity of DRCE_MISSING_COURTYARD is "ignore" in KiCad 8,
        # 9 and 10 (BOARD_DESIGN_SETTINGS).
        drc = (
            "The footprint has no courtyard. Its attribute allow_missing_courtyard lets the DRC "
            "of KiCad accept this, also when the test missing_courtyard is on."
            if "allow_missing_courtyard" in info.attributes
            else "The footprint has no courtyard. The DRC of KiCad reports this only when the "
            "project sets a severity for the test missing_courtyard: its default severity is "
            "ignore."
        )
    else:
        drc = ""
    lines += ["", f"_{drc}_"] if drc else []
    lines += [
        "",
        "_The courtyard is the polygon that the DRC of KiCad uses (BuildCourtyardCaches), "
        "not the drawn shapes._",
    ]
    return lines


def _model_row(model, found: ModelFile) -> str:
    state = f"{found.state}: {found.note}" if found.note else found.state

    def values(items: tuple[float, float, float]) -> str:
        return "(" + ", ".join(_number(item) for item in items) + ")"

    return (
        f"| {cell(model.file)} | {_yes(model.shown)} | {cell(state)} "
        f"| {cell(str(found.path) if found.path else '')} | {values(model.offset)} "
        f"| {values(model.scale)} | {values(model.rotation)} | {_number(model.opacity)} |"
    )


def _footprint_text(
    lib_id: str,
    row: LibraryRow,
    file: Path,
    version: str,
    info: FootprintInfo,
    models: list[ModelFile],
) -> list[str]:
    attributes = info.attributes
    if "smd" in attributes:
        kind = "SMD"
    elif "through_hole" in attributes:
        kind = "through-hole"
    else:
        kind = "unspecified"
    flags = [attribute for attribute in attributes if attribute not in ("smd", "through_hole")]
    lines = [
        f"# Library footprint {lib_id}",
        "",
        f"**Library**: {row.name} ({row.scope}): {row.path}",
        f"**File**: {file}",
        f"**File version**: {version}",
        f"**Read by**: pcbnew of KiCad {info.kicad}",
        f"**Description**: {cell(info.description)}",
        f"**Tags**: {cell(info.keywords)}",
        f"**Type**: {kind}",
        f"**Attributes**: {', '.join(flags) or 'none'}",
    ]
    if info.net_tie_pad_groups:
        lines.append(f"**Net tie pad groups**: {cell('; '.join(info.net_tie_pad_groups))}")
    if info.duplicate_pad_numbers_are_jumpers:
        lines.append("**Duplicate pad numbers are jumpers**: yes")
    if info.jumper_pad_groups:
        groups = "; ".join(", ".join(group) for group in info.jumper_pad_groups)
        lines.append(f"**Jumper pad groups**: {cell(groups)}")
    if info.private_layers:
        lines.append(f"**Private layers**: {', '.join(info.private_layers)}")
    if settings := _local_settings(info):
        lines.append(f"**Local settings**: {', '.join(settings)}")
    x0, y0, x1, y1 = info.box
    lines.append(
        f"**Size**: {_size((x1 - x0, y1 - y0))} mm, from {_xy((x0, y0))} to {_xy((x1, y1))} "
        "(all items, no text)"
    )
    if info.embedded_files:
        files = ", ".join(f"{name} ({kind})" for name, kind in info.embedded_files)
        lines.append(f"**Embedded files**: {cell(files)}")

    lines += _courtyard_lines(info)
    lines += [
        "",
        "## Fields",
        "",
        "| Field | Value | Visible | Layer |",
        "|-------|-------|---------|-------|",
    ]
    lines += [
        f"| {cell(field.name)} | {cell(field.text)} | {_yes(field.visible)} | {field.layer} |"
        for field in info.fields
    ]
    lines += [
        "",
        f"## Pads ({len(info.pads)})",
        "",
        "| Number | Type | Shape | X (mm) | Y (mm) | Angle (°) | Size (mm) | Drill (mm) | Layers "
        "| Details |",
        "|--------|------|-------|--------|--------|-----------|-----------|------------|--------"
        "|---------|",
    ]
    lines += [_pad_row(pad) for pad in info.pads]
    if info.models:
        lines += [
            "",
            f"## 3D models ({len(info.models)})",
            "",
            "| File | Shown | State | Path | Offset (mm) | Scale | Rotation (°) | Opacity |",
            "|------|-------|-------|------|-------------|-------|--------------|---------|",
        ]
        lines += [
            _model_row(model, found) for model, found in zip(info.models, models, strict=True)
        ]
    lines += [
        "",
        "_The positions are in the coordinates of the footprint: the Y axis points down, as in "
        "the board. A positive angle turns counterclockwise._",
    ]
    return lines


def _file_version(file: Path) -> str:
    """Return the (version N) of a footprint file, as text for the output.

    KiCad reads the version at each place in the footprint (parseFOOTPRINT).
    """
    try:
        root = parse(read_as_kicad(file))
    except (OSError, KiCadEncodingError, SExprError) as e:
        return f"not known (the server cannot read the file: {e})"
    node = root.first("version")
    return node.value(0, "none") if node is not None else "none"


def _find_footprint(
    library_name: str, footprint_name: str, kicad: str, project_path: str
) -> tuple[Path | None, LibraryRow, list[str], Path]:
    """Find the file of a library footprint as KiCad finds it.

    A schematic gives its project from the files of the hierarchy, thus call
    this function off the event loop.

    Returns:
        (the .kicad_pro file or None, the library row, the problems of the
        library tables, the .kicad_mod file).

    Raises:
        LibraryLookupError: The library or the footprint is not there.
        ValueError: The project path gives no project or more than one.
    """
    project = project_file(project_path)
    return project, *_footprint_file(library_name, footprint_name, kicad, project)


def _footprint_file(
    library_name: str, footprint_name: str, kicad: str, project: Path | None
) -> tuple[LibraryRow, list[str], Path]:
    """Find the .kicad_mod file of a footprint in the library tables of KiCad and of a project.

    Returns:
        (the library row, the problems of the library tables, the .kicad_mod file).

    Raises:
        LibraryLookupError: The library or the footprint is not there.
    """
    row, problems, files = _footprint_library(library_name, kicad, project)
    file_name = f"{footprint_name}.kicad_mod"
    # KiCad finds a footprint by the name of a file of the directory, with
    # the same case (FP_CACHE), also on a file system that ignores the case.
    # A name with a path separator is not such a name.
    if file_name not in files:
        raise LibraryLookupError(
            f'the library "{library_name}" ({row.path}) has no footprint "{footprint_name}".'
        )
    return row, problems, row.path / file_name


def _footprint_library(
    library_name: str,
    kicad: str,
    project: Path | None,
    rows: tuple[list[LibraryRow], list[str]] | None = None,
) -> tuple[LibraryRow, list[str], set[str]]:
    """Find a footprint library in the library tables of KiCad and of a project.

    rows: the result of library_rows("footprint", kicad, project), or None.

    Returns:
        (the library row, the problems of the library tables, the file names
        of the .pretty directory).

    Raises:
        LibraryLookupError: The library is not there, or it is not a directory.
    """
    row, problems = find_kicad_library("footprint", library_name, kicad, project, rows)
    if not row.path.is_dir():
        raise LibraryLookupError(
            f'the library "{library_name}" ({row.path}) is not a directory. A footprint '
            'library of the type "KiCad" is a .pretty directory.'
        )
    return row, problems, set(os.listdir(row.path))


async def _read_footprint(
    library_name: str, footprint_name: str, kicad: str, project_path: str
) -> tuple[Path | None, LibraryRow, list[str], Path, FootprintInfo]:
    """Find a library footprint as KiCad finds it, and read it with pcbnew.

    Returns:
        (the .kicad_pro file or None, the library row, the problems of the
        library tables, the .kicad_mod file, the footprint).

    Raises:
        LibraryLookupError: The footprint is not there, or KiCad cannot read it.
        ValueError: The project path gives no project or more than one.
    """
    project, row, problems, file = await asyncio.to_thread(
        _find_footprint, library_name, footprint_name, kicad, project_path
    )
    try:
        reader, info = await read_library_footprint(row.path, footprint_name)
    except FootprintReadError as e:
        raise LibraryLookupError(str(e)) from e
    if version_numbers(reader) != version_numbers(kicad):
        raise LibraryLookupError(
            f"kicad-cli is KiCad {kicad}, but the Python with pcbnew is KiCad {reader}. "
            "The library tables and the reader of the footprint must be the same KiCad. "
            f"Set KICAD_PYTHON to the Python of KiCad {kicad}."
        )
    if info is None:
        raise LibraryLookupError(
            f"KiCad {reader} cannot read the file {file}. pcbnew does not give the "
            "reason: open the footprint in the footprint editor of KiCad to see the error."
        )
    return project, row, problems, file, info


async def _get_footprint(lib_id: str, project_path: str) -> str:
    library_name, _, footprint_name = lib_id.partition(":")
    if not library_name or not footprint_name:
        return _FOOTPRINT_ID_ERROR.format(lib_id)
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"

    try:
        project, row, problems, file, info = await _read_footprint(
            library_name, footprint_name, kicad, project_path
        )
    except LibraryLookupError as e:
        return f"❌ Cannot read the footprint {lib_id}: {e}"

    def files() -> tuple[str, list[ModelFile]]:
        env = kicad_environment(kicad)
        variables = project_variables(project) if project is not None else {}
        major = version_numbers(kicad)[0]
        embedded = {name for name, _kind in info.embedded_files}
        found = [
            resolve_model(model.file, major, env, project, variables, row.path, embedded)
            for model in info.models
        ]
        return _file_version(file), found

    version, models = await asyncio.to_thread(files)
    text = _footprint_text(lib_id, row, file, version, info, models)
    return "\n".join(text + problem_lines(problems))


@mcp.tool()
async def get_library_footprint(lib_id: str, project_path: str = "") -> str:
    """Show the content of a footprint of a KiCad library: pads, courtyard, attributes, 3D models.

    The library comes from the library tables of the installed KiCad and of
    the project, as in the footprint chooser of KiCad. pcbnew of the installed
    KiCad reads the footprint, thus the values are those of KiCad. The tool
    finds the file of each 3D model as KiCad finds it.

    Args:
        lib_id: The library ID, for example "Resistor_SMD:R_0603_1608Metric".
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries and to set ${KIPRJMOD}
            for the 3D models.

    Returns:
        The library file, the type, the attributes, the local settings, the
        size, the courtyard, the fields, a table of the pads, and a table of
        the 3D models with the file that KiCad uses.
    """
    try:
        return await _get_footprint(lib_id, project_path)
    except Exception as e:
        return error_answer(e, "Error")


def _stacked_text(number: str) -> bool:
    """Return True for a pin number in brackets, which a stacked notation reads.

    KiCad 10.0 and branch master of 2026-11-04 read a number that does not
    start with "[" and end with "]" as one number (ExpandStackedPinNotation).
    """
    return number.startswith("[") and number.endswith("]")


def netlist_root(project: Path, kicad: str, output: str = "netlist") -> tuple[Path, int]:
    """Return the schematic that gives kicad-cli the netlist of the full project.

    kicad-cli uses the project file of the name of the schematic
    (EESCHEMA_HELPERS::LoadSchematic). From KiCad 10.0.7 it loads all
    top-level sheets of that project (top_level_sheets) when the schematic is
    one of them. KiCad 10.0.0 to 10.0.6 load only the schematic. The BOM
    export loads the schematic in the same way (JobExportBom).

    output: The name of the output in the error messages, for example "BOM".

    Returns:
        (the schematic, the number of top-level sheets of the project).

    Raises:
        ValueError: kicad-cli cannot write the output of the full project.
    """
    tops = top_level_sheets(project)
    named = Path(os.path.abspath(project.with_suffix(".kicad_sch")))
    if len(tops) == 1:
        if project.is_file() and tops[0].file.resolve() != named.resolve():
            raise ValueError(
                f"The top-level sheet of the project {project} is {tops[0].file.name}. "
                "kicad-cli uses the project file of the name of the schematic, thus it does "
                "not use the settings of this project (for example its text variables)."
            )
        return tops[0].file, 1
    numbers = version_triple(kicad)
    if numbers is None or numbers < (10, 0, 7):
        raise ValueError(
            f"The project {project} has {len(tops)} top-level sheets. kicad-cli of KiCad "
            f"{kicad} writes the {output} of one top-level sheet only. KiCad 10.0.7 and later "
            f"write the {output} of all top-level sheets."
        )
    if not named.is_file() or all(top.file.resolve() != named.resolve() for top in tops):
        raise ValueError(
            f"The project {project} has {len(tops)} top-level sheets, but {named.name} is "
            f"not one of them. kicad-cli writes the {output} of all top-level sheets only for "
            "the schematic with the name of the project."
        )
    return named, len(tops)


def _stacked_rules(kicad: str, unsure: list[str]) -> bool:
    """Return True when the installed KiCad expands the stacked pin numbers ("[1-4]").

    Args:
        unsure: The pin numbers that a different stacked notation can read
            in a different way.

    Raises:
        ValueError: The installed KiCad is a nightly build, and unsure is not
            empty.
    """
    release = kicad_release(kicad)
    if release is KICAD10:
        return True
    if release in (KICAD8, KICAD9):
        return False
    # _field_rules accepts only the nightly builds 10.99 as an other version.
    # Branch master of 2026-11-04 adds escapes to the stacked notation
    # (UnescapeStackedPinItem), and a nightly build can have an older or a
    # newer rule. A pin number with no bracket is one number in each version.
    if unsure:
        raise ValueError(
            f"The installed KiCad is the nightly build {kicad}. Its rules for stacked pin "
            "numbers are not the rules of KiCad 10.0, thus the server cannot check the pin "
            "numbers " + ", ".join(f'"{number}"' for number in dict.fromkeys(unsure)) + "."
        )
    return False


def _pin_rows(info: SymbolInfo, pins: list[SymbolPin], number: str | None = None) -> list[str]:
    """The rows of a pin table: the pin number (or the number of the netlist), name, type,
    unit and body style."""
    return [
        f"| {cell(pin.number if number is None else number)} | {cell(pin.name)} | {pin.type} "
        f"| {_unit_text(info, pin.unit)} | {_style_text(info, pin.body_style)} |"
        for pin in pins
    ]


_PIN_HEADER = [
    "| Pin number | Pin name | Type | Unit | Body style |",
    "|------------|----------|------|------|------------|",
]


def _mapping_text(
    symbol_id: str,
    row: LibraryRow,
    chain: list[LibrarySymbol],
    info: SymbolInfo,
    footprint_id: str,
    from_field: bool,
    footprint_row: LibraryRow,
    file: Path,
    footprint: FootprintInfo,
    kicad: str,
    stacked: bool,
    results: list[tuple[int, PinPadMapping]],
) -> list[str]:
    no_number = list(dict.fromkeys(pin for _style, result in results for pin in result.no_number))
    no_pad: dict[str, list[SymbolPin]] = {}
    for _style, result in results:
        for number, pins in result.no_pad:
            found = no_pad.setdefault(number, [])
            found += [pin for pin in pins if pin not in found]
    # The index of each pad with no pin, with the body styles that have no
    # pin for it.
    no_pin: dict[int, list[int]] = {}
    for style, result in results:
        for index in result.no_pin:
            no_pin.setdefault(index, []).append(style)
    expanded = list(dict.fromkeys(item for _style, result in results for item in result.stacked))
    invalid = list(dict.fromkeys(pin for _style, result in results for pin in result.invalid))

    numbered = [pad for pad in footprint.pads if pad.number]
    counts = list(dict.fromkeys(result.pin_numbers for _style, result in results))
    if len(counts) == 1:
        pin_numbers = str(counts[0])
    else:
        pin_numbers = ", ".join(
            f"{result.pin_numbers} ({_style_text(info, style)})" for style, result in results
        )
    result = _result_line(len(no_pad), len(no_number), len(no_pin))
    lines = [
        f"# Pins and pads of {symbol_id} and {footprint_id}",
        "",
        f"**Symbol**: {row.name} ({row.scope}): {chain[0].file}",
        f"**Footprint**: {footprint_row.name} ({footprint_row.scope}): {file}"
        + (" (the Footprint field of the symbol)" if from_field else ""),
        f'**Rules**: "Update PCB from Schematic" of KiCad {kicad}',
        f"**Pins**: {len(info.pins)}"
        + (" in all body styles" if len(results) > 1 else "")
        + f", with {pin_numbers} pin numbers"
        + (f" in {info.unit_count} units" if info.unit_count > 1 else ""),
        f"**Pads**: {len(numbered)} with a number, {len(footprint.pads) - len(numbered)} with no "
        "number (KiCad gives no net to a pad with no number. It also removes the number of an "
        "NPTH pad and of a pad with no copper layer.)",
    ]
    if not info.on_board:
        lines.append(
            '_The symbol has the flag "Exclude from board": "Update PCB from Schematic" puts '
            "no footprint of it on the board and gives none of these errors and warnings. The "
            "result is for the symbol with no such flag._"
        )
    lines.append(
        result or "**Result**: ✅ Each pin number has a pad, and each pad with a number has a pin."
    )
    if info.unit_count > 1:
        lines.append(
            "_The check uses the pins of all units. In a schematic, KiCad uses only the "
            "pins of the units that the schematic has._"
        )

    if no_pad:
        lines += [
            "",
            f"## Pin numbers with no pad ({len(no_pad)})",
            "",
            '_Error of "Update PCB from Schematic": "<reference> pad <number> not found in '
            '<footprint>". The pin has no connection on the board._',
            "",
            *_PIN_HEADER,
        ]
        for number, pins in no_pad.items():
            lines += _pin_rows(info, pins, number)
    if no_number:
        lines += [
            "",
            f"## Pins with no number ({len(no_number)})",
            "",
            '_Error of "Update PCB from Schematic": "Symbol <reference> has pins with no '
            'number". KiCad cannot match these pins to pads._',
            "",
            *_PIN_HEADER,
            *_pin_rows(info, no_number),
        ]
    if no_pin:
        styles = len(results) > 1
        lines += [
            "",
            f"## Pads with no pin ({len(no_pin)})",
            "",
            '_Warning of "Update PCB from Schematic": "No net found for component '
            '<reference> pad <number> (no pin <number> in symbol)". The pad gets no net._',
            "",
            "| Pad number | Type | Layers |" + (" Body styles |" if styles else ""),
            "|------------|------|--------|" + ("-------------|" if styles else ""),
        ]
        for index, pad_styles in sorted(no_pin.items()):
            pad = footprint.pads[index]
            row_text = f"| {cell(pad.number)} | {pad.type} | {_layer_group(pad.layers)} |"
            if styles:
                row_text += " " + ", ".join(_style_text(info, style) for style in pad_styles) + " |"
            lines.append(row_text)
    if expanded:
        lines += [
            "",
            f"## Stacked pin numbers ({len(expanded)})",
            "",
            "_KiCad 10 gives each of these pins the pad numbers of the list._",
            "",
            "| Pin number | Pin name | Unit | Pad numbers |",
            "|------------|----------|------|-------------|",
        ]
        lines += [
            f"| {cell(pin.number)} | {cell(pin.name)} | {_unit_text(info, pin.unit)} "
            f"| {cell(', '.join(numbers))} |"
            for pin, numbers in expanded
        ]
    if invalid:
        lines += [
            "",
            f"## Pin numbers with brackets that are not a stacked notation ({len(invalid)})",
            "",
            "_KiCad 10 uses the text as one pin number. Its ERC gives the warning "
            '"Pin name resembles stacked pin" (stacked_pin_name)._',
            "",
            *_PIN_HEADER,
            *_pin_rows(info, invalid),
        ]
    if not stacked and any("[" in pin.number or "]" in pin.number for pin in info.pins):
        lines += [
            "",
            f"_KiCad {kicad} uses a pin number with brackets as one number. KiCad 10 reads "
            '"[1-4]" or "[1,3]" as more than one number (stacked pins)._',
        ]
    return lines


async def _check_pin_pad_mapping(symbol_id: str, footprint_id: str, project_path: str) -> str:
    library_name, _, symbol_name = symbol_id.partition(":")
    if not library_name or not symbol_name:
        return _SYMBOL_ID_ERROR.format(symbol_id)
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"
    try:
        chain, row, problems, info = await asyncio.to_thread(
            _read_symbol, library_name, symbol_name, kicad, project_path
        )
    except LibraryLookupError as e:
        return f"❌ Cannot read the symbol {symbol_id}: {e}"
    if info.power:
        return (
            f"❌ {symbol_id} is a power symbol. KiCad puts no power symbol into the netlist, "
            "thus it has no footprint."
        )
    stacked = _stacked_rules(kicad, [pin.number for pin in info.pins if _stacked_text(pin.number)])
    from_field = not footprint_id
    if from_field:
        footprint_id = dict(info.fields).get("Footprint", "")
        if not footprint_id:
            return (
                f"❌ The Footprint field of the symbol {symbol_id} is empty. Give the "
                "footprint in footprint_lib_id."
            )
    footprint_library, _, footprint_name = footprint_id.partition(":")
    if not footprint_library or not footprint_name:
        return _FOOTPRINT_ID_ERROR.format(footprint_id) + (
            " It is the Footprint field of the symbol." if from_field else ""
        )
    try:
        _project, footprint_row, footprint_problems, file, footprint = await _read_footprint(
            footprint_library, footprint_name, kicad, project_path
        )
    except LibraryLookupError as e:
        return f"❌ Cannot read the footprint {footprint_id}: {e}"

    results = []
    for style in range(1, max(1, len(info.body_styles)) + 1):
        pins = [pin for pin in info.pins if pin.body_style in (0, style)]
        results.append((style, map_pins(pins, footprint.pads, stacked)))
    text = _mapping_text(
        symbol_id, row, chain, info, footprint_id, from_field, footprint_row, file, footprint,
        kicad, stacked, results,
    )  # fmt: skip
    return "\n".join(text + problem_lines(list(dict.fromkeys(problems + footprint_problems))))


@mcp.tool()
async def check_pin_pad_mapping(
    symbol_lib_id: str, footprint_lib_id: str = "", project_path: str = ""
) -> str:
    """Compare the pin numbers of a library symbol with the pad numbers of a footprint.

    The tool uses the rules of "Update PCB from Schematic" of the installed
    KiCad. A pin number that no pad has, and a pin with no number, are errors.
    A pad with a number that no pin has gets no net: this is a warning. KiCad
    10 reads a stacked pin number ("[1-4]") as more than one pad number. The
    check uses the pins of all units, for each body style.

    Args:
        symbol_lib_id: The library ID of the symbol, for example "Device:R".
        footprint_lib_id: Optional library ID of the footprint, for example
            "Resistor_SMD:R_0603_1608Metric". If empty, the tool uses the
            Footprint field of the symbol.
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries.

    Returns:
        The result, the pin numbers with no pad, the pins with no number, the
        pads with no pin, and the stacked pin numbers with their pad numbers.
    """
    try:
        return await _check_pin_pad_mapping(symbol_lib_id, footprint_lib_id, project_path)
    except Exception as e:
        return error_answer(e, "Error")


_NETLIST_TIMEOUT = 120
# kicad-cli writes this warning when SCH_REFERENCE_LIST::CheckAnnotation finds
# an error (JobExportNetlist, KiCad 8.0.9, 9.0.9 and 10.0.6).
_ANNOTATION_WARNING = "schematic has annotation errors"


# The most annotation errors that a check tool lists.
_ANNOTATION_ROWS = 20


async def _netlist_annotation(
    root: Path, kicad: str
) -> tuple[list[AnnotationError] | None, list[str], set[str] | None, list[AnnotationItem]]:
    """The annotation errors of the netlist job of kicad-cli for a schematic (view
    "cli", all symbols), or None when the check cannot run, the notes, the
    references that the errors change (annotation_references), and each symbol
    that is not annotated."""
    try:
        check = await asyncio.to_thread(
            check_annotation, root.with_suffix(".kicad_pro"), kicad, "cli", "all"
        )
    except (ValueError, KicadFileError, KiCadEncodingError, SExprError, OSError) as e:
        return None, [f"The server cannot list the annotation errors: {e}"], None, []
    return check.errors, check.problems, annotation_references(check), check.unannotated


async def _update_annotation(project: Path, kicad: str) -> tuple[bool | None, list[str]]:
    """True when "Update PCB from Schematic" finds annotation errors (view "update" of
    check_annotation), or None when the check cannot run, and the notes."""
    try:
        check = await asyncio.to_thread(check_annotation, project, kicad, "update", "all")
    except (ValueError, KicadFileError, KiCadEncodingError, SExprError, OSError) as e:
        return None, [f'The server cannot check the annotation of "Update PCB from Schematic": {e}']
    return bool(check.errors), check.problems


def annotation_references(check: AnnotationCheck) -> set[str]:
    """The references (lower case) of the netlist components that an annotation error
    changes.

    The netlist exporter (``NETLIST_EXPORTER_XML::makeSymbols``) sorts the symbols
    of a sheet by their reference with no case: two symbols of one sheet with one
    reference give one component, and the nets have the pins of the two. The
    symbols of two sheets give two components with one reference, and
    read_board_netlist uses the first component with the pins of the two. A unit
    that the library symbol does not have gives no pins. Thus these components can
    have the pins of more than one symbol, or not all pins:

    - each reference of a duplicate_reference error (also different unit counts);
    - each reference that is not annotated ("R?") and that more than one item has
      (CheckAnnotation does not compare different units of "U?");
    - each item with a unit larger than the unit count.

    A reference that is not annotated and that one item has gives an exact component.
    """
    references = {
        item.reference.lower()
        for error in check.errors
        if error.code == "duplicate_reference"
        for item in (error.first, error.second)
        if item is not None
    }
    unannotated: dict[str, int] = {}
    for item in check.unannotated:
        key = item.reference.lower()
        unannotated[key] = unannotated.get(key, 0) + 1
    references |= {reference for reference, count in unannotated.items() if count > 1}
    references |= {item.reference.lower() for item in check.extra_units}
    return references


def annotation_lines(found: "SchematicFootprints", kicad: str, effect: str) -> list[str]:
    """The Markdown lines about the annotation errors of the netlist of a check tool.

    effect: Why an annotation error can make the result of the tool wrong.
    """
    warning, errors = found.annotation, found.annotation_errors
    if not warning and not errors and not found.annotation_notes:
        return []
    lines = []
    if warning or errors:
        lines.append(f"⚠️ **Annotation**: {effect}")
    if warning:
        lines.append(f"kicad-cli (KiCad {kicad}) reports annotation errors in the netlist.")
    if errors:
        lines.append(
            "The annotation check of the server (the rules of the netlist job of kicad-cli) "
            "gives these errors:"
        )
        lines += [
            f"- {error.code}: {cell(error.message)} (sheet {error.first.sheet})"
            for error in errors[:_ANNOTATION_ROWS]
        ]
        if len(errors) > _ANNOTATION_ROWS:
            lines.append(f"- {len(errors) - _ANNOTATION_ROWS} more annotation errors")
    if found.annotation_unannotated:
        items = found.annotation_unannotated
        lines.append(f"Symbols that are not annotated ({len(items)}). {UNANNOTATED_NOTE}")
        lines += [f"- {cell(item.text)}" for item in items[:UNANNOTATED_ROWS]]
        if len(items) > UNANNOTATED_ROWS:
            lines.append(f"- {len(items) - UNANNOTATED_ROWS} more symbols")
    if warning and errors == []:
        lines.append(
            "❌ The annotation check of the server finds no error: the server cannot list "
            "the errors of kicad-cli."
        )
    elif errors and not warning:
        lines.append("❌ kicad-cli reports no annotation error: the list can be wrong.")
    lines += [f"⚠️ {note}" for note in found.annotation_notes]
    return lines


def _board_netlist(root: Path) -> tuple[str, str]:
    """Write the netlist that "Update PCB from Schematic" gives pcbnew, with kicad-cli.

    The format kicadsexpr is the netlist of NETLIST_EXPORTER_KICAD, the
    exporter of the update.

    Returns:
        (the netlist, the messages of kicad-cli).

    Raises:
        RuntimeError: kicad-cli did not write the netlist.
        KiCadEncodingError: The netlist is not UTF-8.
    """
    export = export_with_kicad_cli(
        ["sch", "export", "netlist", "--format", "kicadsexpr"],
        root,
        "board.net",
        _NETLIST_TIMEOUT,
    )
    return decode_as_kicad(export.data, Path("board.net")), "\n".join(export.messages)


@dataclass(frozen=True)
class _ComponentCheck:
    component: NetlistComponent
    reason: str  # Why the tool did not check the component, or "".
    no_pad: tuple[str, ...] = ()  # Pin numbers with no pad.
    no_number: tuple[NetlistPin, ...] = ()  # Pins with no number.
    no_pin: tuple[str, ...] = ()  # The number of each pad with no pin.


def _pin_cell(component: NetlistComponent, numbers: tuple[str, ...]) -> str:
    functions = {}
    for pin in component.pins:
        functions.setdefault(pin.number, pin.function)
    return cell(", ".join(f"{n} ({functions[n]})" if functions.get(n) else n for n in numbers))


def _pad_cell(numbers: tuple[str, ...]) -> str:
    counts = dict.fromkeys(numbers, 0)
    for number in numbers:
        counts[number] += 1
    return cell(", ".join(f"{n} ({c} pads)" if c > 1 else n for n, c in counts.items()))


def annotation_state(found: "SchematicFootprints") -> tuple[bool, bool]:
    """Return (the netlist has annotation errors, the server knows the components
    that they change: annotation_references).

    When kicad-cli reports errors and the server check finds none, or the check
    did not run, the server does not know the changed components.
    """
    errors = found.annotation_errors
    present = found.annotation or bool(errors)
    known = found.annotation_references is not None and not (found.annotation and not errors)
    return present, known


def _symbols(count: int, adjective: str = "") -> str:
    """For example "1 symbol" or "3 other symbols"."""
    words = [str(count), adjective, "symbol" if count == 1 else "symbols"]
    return " ".join(word for word in words if word)


_PASS = "Each pin number has a pad, and each pad with a number has a pin"
# SCH_EDIT_FRAME::ReadyToNetlist (KiCad 8.0.9, 9.0.9, 10.0.6).
UPDATE_BLOCKED = (
    '"Update PCB from Schematic" finds annotation errors: it opens the annotation dialog, '
    "and it runs only when no error stays."
)


def annotation_sentences(found: "SchematicFootprints", changed: str | None) -> list[str]:
    """The sentences of a check tool about the annotation errors; [] for none.

    changed: The sentence about the netlist components that the errors change
        (annotation_references), or None when no component changes.
    """
    present, known = annotation_state(found)
    notes = []
    if present and not known:
        notes.append(
            "The server does not know the netlist components that the annotation errors "
            "change: the result can be not complete."
        )
    elif changed:
        notes.append(changed)
    if found.update_blocked:
        notes.append(UPDATE_BLOCKED)
    elif found.update_blocked is None and present:
        notes.append('The server cannot check the annotation of "Update PCB from Schematic".')
    if present and not notes:
        notes.append("The netlist of kicad-cli has annotation errors.")
    return notes


def _annotation_result(
    result: str | None, others: int, changed: int, found: "SchematicFootprints"
) -> str | None:
    """The result line with the sentences about the annotation errors
    (annotation_sentences).
    With a note, the result has no pass mark.

    result: The result line of the counts of all checked symbols (_result_line).
    others: The symbols that the tool checked and no annotation error changes.
    changed: The symbols that an annotation error changes (annotation_references).
    """
    sentence = None
    if changed:
        sentence = (
            f"{_symbols(changed)} {'has' if changed == 1 else 'have'} an annotation error: the "
            f"result of {'this symbol' if changed == 1 else 'these symbols'} is not complete."
        )
    notes = annotation_sentences(found, sentence)
    if not notes:
        return result
    text = " ".join(notes)
    if result:
        return f"{result}. {text}"
    _present, known = annotation_state(found)
    if not others:
        return f"**Result**: ⚠️ {text}" if changed else f"**Result**: ⚠️ No symbol to check. {text}"
    if known and not changed:
        return f"**Result**: ⚠️ {_PASS}. {text}"
    other = "other" if changed else ""
    return f"**Result**: ⚠️ No error in the {_symbols(others, other)}. {text}"


def _mapping_table(checks: list[_ComponentCheck], empty: str = "") -> list[str]:
    """The table of the pin and pad errors and warnings of some symbols.

    empty: The text of a cell with no error.
    """
    lines = [
        "| Reference | Value | Footprint | Pin numbers with no pad | Pins with no number "
        "| Pads with no pin |",
        "|-----------|-------|-----------|-------------------------|---------------------"
        "|------------------|",
    ]
    for check in checks:
        component = check.component
        no_number = cell(", ".join(p.function or p.type for p in check.no_number))
        no_pad = _pin_cell(component, check.no_pad) or empty
        no_pin = _pad_cell(check.no_pin) or empty
        lines.append(
            f"| {cell(component.reference)} | {cell(component.value)} "
            f"| {cell(component.footprint)} | {no_pad} | {no_number or empty} | {no_pin} |"
        )
    return lines


def _schematic_mapping_text(
    path: Path, kicad: str, checks: list[_ComponentCheck], annotation: list[str],
    found: "SchematicFootprints",
) -> list[str]:  # fmt: skip
    root, tops, absent = found.root, found.tops, found.absent
    changed_references = found.annotation_references or set()
    problems, skipped, good, changed = [], [], [], []
    for check in checks:
        if check.reason:
            skipped.append(check)
        elif check.component.reference.lower() in changed_references:
            changed.append(check)
        elif check.no_pad or check.no_number or check.no_pin:
            problems.append(check)
        else:
            good.append(check)
    # The errors of a changed symbol are errors of the netlist too: they count.
    counted = problems + changed
    result = _annotation_result(
        _result_line(
            sum(len(c.no_pad) for c in counted),
            sum(len(c.no_number) for c in counted),
            sum(len(c.no_pin) for c in counted),
            len(skipped),
        ),
        len(problems) + len(good),
        len(changed),
        found,
    )
    lines = [
        f"# Pins and pads of the schematic {root.name}",
        "",
        f"**Schematic**: {root}"
        + (f", with {tops} top-level sheets" if tops > 1 else "")
        + (f" (the project of {path})" if root.resolve() != path.resolve() else ""),
        f'**Netlist**: the netlist that "Update PCB from Schematic" of KiCad {kicad} gives '
        "pcbnew (kicad-cli). It has no power symbol and no symbol that is excluded from the "
        "board. Each symbol has the pins of the units that the schematic has. The pads come "
        "from the library footprints, as a new footprint on the board gets them.",
        f"**Symbols**: {len(checks) - len(skipped)} checked, {len(skipped)} not checked",
    ]
    if result:
        lines.append(result)
    elif checks:
        lines.append(f"**Result**: ✅ {_PASS}.")
    else:
        lines.append("**Result**: the netlist has no symbol to check.")
    lines += annotation
    if absent:
        lines.append(
            f"_Not in the netlist: {', '.join(absent)} (a power symbol, a symbol that is "
            "excluded from the board, or no such reference)._"
        )
    if problems:
        lines += [
            "",
            f"## Symbols with errors or warnings ({len(problems)})",
            "",
            '_Errors and warnings of "Update PCB from Schematic": "<reference> pad <number> '
            'not found in <footprint>" for a pin number with no pad, "Symbol <reference> has '
            'pins with no number", and "No net found for component <reference> pad <number> '
            '(no pin <number> in symbol)" for a pad with no pin (the pad gets no net)._',
            "",
            *_mapping_table(problems),
        ]
    if changed:
        lines += [
            "",
            f"## Symbols with an annotation error ({len(changed)})",
            "",
            "_The netlist component of each reference can have the pins of more than one "
            "symbol (a duplicate reference, symbols that are not annotated), or not all pins "
            "(a unit that the library symbol does not have). Thus the result of these symbols "
            "is not complete. Repair the annotation, then run the check again._",
            "",
            *_mapping_table(changed, "none"),
        ]
    if skipped:
        lines += [
            "",
            f"## Symbols that the tool did not check ({len(skipped)})",
            "",
            '_"Update PCB from Schematic" gives an error for each of these symbols, and puts '
            "no footprint of it on the board._",
            "",
            "| Reason | Symbols |",
            "|--------|---------|",
        ]
        by_reason: dict[str, list[str]] = {}
        for check in skipped:
            by_reason.setdefault(check.reason, []).append(check.component.reference)
        lines += [
            f"| {cell(reason)} | {cell(', '.join(references))} |"
            for reason, references in by_reason.items()
        ]
    if good:
        lines += [
            "",
            f"## Symbols with no error and no warning ({len(good)})",
            "",
            cell(", ".join(c.component.reference for c in good)),
        ]
    return lines


@dataclass(frozen=True)
class SchematicFootprints:
    """The netlist of a schematic, and the footprints that KiCad finds for it."""

    path: Path  # The schematic that the caller gave.
    root: Path  # The schematic that kicad-cli read.
    tops: int  # The top-level sheets of the project.
    project: Path  # The .kicad_pro file (it can be missing).
    components: list[NetlistComponent]
    absent: list[str]  # The references that the caller gave and the netlist does not have.
    annotation: bool  # kicad-cli reports annotation errors.
    # The annotation errors of the netlist job (check_annotation, view "cli"); None
    # when the check did not run. The notes tell why the list can be incomplete.
    annotation_errors: list[AnnotationError] | None
    annotation_notes: list[str]
    # The references (lower case) whose netlist component can hold the pins of more
    # than one symbol, or not all pins of a symbol (annotation_references); None
    # when the check did not run.
    annotation_references: set[str] | None
    # Each symbol of the netlist job that is not annotated (check_annotation).
    annotation_unannotated: list[AnnotationItem]
    # "Update PCB from Schematic" finds annotation errors (view "update" of
    # check_annotation); None when the check did not run.
    update_blocked: bool | None
    reasons: dict[str, str]  # Footprint ID: why KiCad does not find or load it.
    libraries: dict[str, Path]  # Footprint ID: the .pretty directory.
    items: dict[str, FootprintItems]  # Footprint ID: the footprint that pcbnew loaded.
    problems: list[str]  # The problems of the library tables.


async def schematic_footprints(
    path: Path, kicad: str, references: list[str] | None = None, embedded: bool = False
) -> SchematicFootprints | str:
    """Read the netlist of "Update PCB from Schematic" and find the footprints as pcbnew does.

    embedded: True to read the names of the embedded files of the footprints.

    Returns:
        The netlist and the footprints, or the text of an error.

    Raises:
        ValueError: The server has no rules for the KiCad version, or
            kicad-cli cannot write the netlist of the full project.
    """
    # The server has rules (stacked pins, top-level sheets) only for KiCad 8,
    # 9, 10 and the nightly builds 10.99.
    _field_rules(kicad)
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    root, tops = await asyncio.to_thread(netlist_root, project, kicad)
    # The annotation checks do not need the netlist: they run at the same time.
    netlist, annotation, update = await asyncio.gather(
        asyncio.to_thread(_board_netlist, root),
        _netlist_annotation(root, kicad),
        _update_annotation(project, kicad),
        return_exceptions=True,
    )
    if isinstance(annotation, BaseException):
        raise annotation
    if isinstance(update, BaseException):
        raise update
    annotation_errors, notes, annotation_refs, unannotated = annotation
    update_blocked, update_notes = update
    try:
        if isinstance(netlist, BaseException):
            raise netlist
        text, output = netlist
        components = read_board_netlist(text)
    except (RuntimeError, KiCadEncodingError, SExprError) as e:
        return f"❌ kicad-cli did not give the netlist of {root}: {e}"
    absent: list[str] = []
    if references:
        by_reference = {component.reference: component for component in components}
        wanted = list(dict.fromkeys(references))
        absent = [reference for reference in wanted if reference not in by_reference]
        components = [by_reference[reference] for reference in wanted if reference in by_reference]

    # The footprint of each component, as LoadFootprints of pcbnew finds it.
    reasons: dict[str, str] = {}
    files: dict[str, tuple[Path, str]] = {}  # Footprint ID: (library, name).
    libraries: dict[str, tuple[LibraryRow, set[str]] | str] = {}  # Name: library or error.
    rows = await asyncio.to_thread(library_rows, "footprint", kicad, project)
    for component in components:
        footprint = component.footprint
        if not footprint or footprint in reasons or footprint in files:
            continue
        library, _, name = footprint.partition(":")
        if not library or not name:
            reasons[footprint] = f'The footprint ID "{footprint}" has no library name.'
            continue
        if library not in libraries:
            try:
                row, _problems, names = await asyncio.to_thread(
                    _footprint_library, library, kicad, project, rows
                )
            except LibraryLookupError as e:
                libraries[library] = str(e)
            else:
                libraries[library] = (row, names)
        library_found = libraries[library]
        if isinstance(library_found, str):
            reasons[footprint] = f'KiCad does not find the footprint "{footprint}": {library_found}'
        elif f"{name}.kicad_mod" not in library_found[1]:
            reasons[footprint] = (
                f'KiCad does not find the footprint "{footprint}": the library "{library}" '
                f'({library_found[0].path}) has no footprint "{name}".'
            )
        else:
            files[footprint] = (library_found[0].path, name)
    try:
        reader, loaded = await read_footprints(list(files.values()), embedded)
    except FootprintReadError as e:
        return f"❌ pcbnew cannot read the footprints: {e}"
    if files and version_numbers(reader) != version_numbers(kicad):
        return (
            f"❌ kicad-cli is KiCad {kicad}, but the Python with pcbnew is KiCad {reader}. "
            "The netlist, the library tables and the reader of the footprints must be the "
            f"same KiCad. Set KICAD_PYTHON to the Python of KiCad {kicad}."
        )
    items = {}
    for footprint, footprint_items in zip(files, loaded, strict=True):
        if footprint_items is None:
            reasons[footprint] = (
                f'KiCad cannot load the file of the footprint "{footprint}". Open it in the '
                "footprint editor to see the error."
            )
        else:
            items[footprint] = footprint_items
    return SchematicFootprints(
        path=path,
        root=root,
        tops=tops,
        project=project,
        components=components,
        absent=absent,
        annotation=_ANNOTATION_WARNING in output,
        annotation_errors=annotation_errors,
        annotation_notes=list(dict.fromkeys(notes + update_notes)),
        annotation_references=annotation_refs,
        annotation_unannotated=unannotated,
        update_blocked=update_blocked,
        reasons=reasons,
        libraries={footprint: files[footprint][0] for footprint in items},
        items=items,
        problems=list(dict.fromkeys(rows[1])),
    )


def not_found_reason(component: NetlistComponent, found: SchematicFootprints) -> str:
    """Why "Update PCB from Schematic" puts no footprint of a component on the board, or ""."""
    if not component.footprint:
        return 'No footprint ("No footprint defined for symbol").'
    return found.reasons.get(component.footprint, "")


async def _check_schematic(schematic_path: str, references: list[str] | None) -> str:
    path = Path(schematic_path)
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ No schematic file (.kicad_sch) at {schematic_path}."
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"
    found = await schematic_footprints(path, kicad, references)
    if isinstance(found, str):
        return found
    checked = [c for c in found.components if not not_found_reason(c, found)]
    stacked = _stacked_rules(
        kicad, [p.number for c in checked for p in c.pins if _stacked_text(p.number)]
    )
    checks = []
    for component in found.components:
        if reason := not_found_reason(component, found):
            checks.append(_ComponentCheck(component, reason))
            continue
        pads = found.items[component.footprint].pads
        numbers = [pin.number for pin in component.pins if pin.number]
        try:
            no_pad, no_pin = match_pads(numbers, pads, stacked)
        except StackedPinError as e:
            checks.append(_ComponentCheck(component, str(e)))
            continue
        checks.append(
            _ComponentCheck(
                component,
                "",
                tuple(no_pad),
                tuple(pin for pin in component.pins if not pin.number),
                tuple(pads[index].number for index in no_pin),
            )
        )
    annotation = annotation_lines(
        found,
        kicad,
        "With a duplicate reference, the netlist has the pins of two symbols in one "
        "component, thus a result can be wrong.",
    )
    text = _schematic_mapping_text(path, kicad, checks, annotation, found)
    return "\n".join(text + problem_lines(found.problems))


@mcp.tool()
async def check_schematic_pin_pad_mapping(
    schematic_path: str, references: list[str] | None = None
) -> str:
    """Compare the pins of each symbol of a schematic with the pads of its footprint.

    The tool uses the netlist that "Update PCB from Schematic" gives pcbnew
    (kicad-cli of the installed KiCad), and the rules of that update. A pin
    number that no pad has, a pin with no number, and a symbol whose footprint
    KiCad does not find are errors. A pad with a number that no pin has gets
    no net: this is a warning. A symbol has the pins of the units that the
    schematic has. The pads come from the library footprints, as a new
    footprint on the board gets them. For a board that has the footprints,
    run_drc with the schematic parity compares the footprints of the board.
    For a library symbol and a footprint, use check_pin_pad_mapping.
    When the netlist job finds annotation errors (a duplicate reference can
    join two symbols in one component), the tool lists them.

    Args:
        schematic_path: A schematic of the project (.kicad_sch). For a
            sub-sheet or an other top-level sheet, the tool uses the project.
        references: Optional references to check, for example ["U1", "J3"].
            All symbols of the netlist if empty.

    Returns:
        The result, the annotation errors, a table of the symbols with errors
        or warnings, the symbols that the tool did not check (with the
        reason), and the symbols with no problem.
    """
    try:
        return await _check_schematic(schematic_path, references)
    except Exception as e:
        return error_answer(e, "Error")


# kicad-cli draws with the built-in theme "KiCad Default" (user decision,
# 2026-11-04), not with the theme of an editor of the user. Its SVG has no
# background. The PNG gets the background of that theme: the same in KiCad
# 8.0.9, 9.0.9 and 10.0 (builtin_color_themes.h, LAYER_SCHEMATIC_BACKGROUND
# and LAYER_PCB_BACKGROUND).
_RENDER_THEME = "_builtin_default"
_BACKGROUNDS = {"symbol": "#F5F4EF", "footprint": "#001023"}
_LAYOUTS = ("tiles", "overview")
_IMAGE_LIMITS = range(64, 8193)
_RENDER_TIMEOUT = 120


def render_args(
    item_type: str, item_name: str, include_hidden: bool, output: Path, library: Path
) -> list[str]:
    """Return the kicad-cli arguments that draw one library item as SVG.

    kicad-cli 8, 9 and 10 read these arguments (spike/kicad-versions/
    render_check.py). The output directory must exist: kicad-cli 10 does not
    make it for a footprint, and makes only its last directory for a symbol.
    """
    if item_type == "symbol":
        args = ["sym", "export", "svg", "--symbol", item_name]
        if include_hidden:
            args += ["--include-hidden-pins", "--include-hidden-fields"]
    else:
        args = ["fp", "export", "svg", "--footprint", item_name]
    return args + ["--theme", _RENDER_THEME, "--output", str(output), str(library)]


def _natural_key(path: Path) -> list:
    """Sort "R_unit2" before "R_unit10"."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def _render_error(
    item_type: str,
    item_name: str,
    lib_id: str,
    kicad: str,
    row: LibraryRow,
    result: subprocess.CompletedProcess,
    svg_count: int,
) -> str:
    """Return the error when kicad-cli gave no SVG, or a wrong number of them."""
    output = (decode_output(result.stdout) + decode_output(result.stderr)).strip()
    if item_type == "symbol" and result.returncode != 0:
        # kicad-cli tells "There is no symbol selected to save." When the
        # server can read the library and finds no such symbol, it tells so.
        # Else the output of kicad-cli is the reason.
        try:
            names = read_library(row.path)
        except (OSError, KiCadEncodingError, SExprError, KicadFileError):
            names = None
        if names is not None and item_name not in names:
            return (
                f'❌ Cannot render the symbol {lib_id}: the library "{row.name}" ({row.path}) '
                f'has no symbol "{item_name}".'
            )
    return (
        f"❌ kicad-cli of KiCad {kicad} did not render the {item_type} {lib_id} "
        f"(exit code {result.returncode}, {svg_count} SVG files):\n\n"
        f"{output or '(no output)'}"
    )


def _page_count_warning(
    symbol: tuple[list[LibrarySymbol], SymbolInfo] | Exception, kicad: str, count: int
) -> str | None:
    """Compare the SVG files of kicad-cli with the units and body styles of the symbol.

    kicad-cli writes one SVG for each unit and body style. Returns the
    warning when the count is different, or when the server cannot read the
    symbol (symbol is the error), else None.
    """
    if isinstance(symbol, Exception):
        return (
            "⚠️ **Warning:** the server cannot read the symbol, thus it did not compare the "
            f"pages with the units and body styles of the symbol: {symbol}"
        )
    chain, info = symbol
    styles = max(1, len(info.body_styles))
    expected = info.unit_count * styles
    if count == expected:
        return None

    def number(value: int, noun: str) -> str:
        return f"{value} {noun}" + ("" if value == 1 else "s")

    warning = (
        f"⚠️ **Warning:** kicad-cli wrote {number(count, 'page')}, but the symbol has "
        f"{number(info.unit_count, 'unit')} and {number(styles, 'body style')} "
        f"({number(expected, 'page')})."
    )
    # LIB_SYMBOL::GetBodyStyleCount of a derived symbol did not use the root
    # symbol before KiCad 10.0.7 (commit 4134ea33fc of branch 10.0).
    numbers = version_triple(kicad)
    if len(chain) > 1 and styles > 1 and numbers is not None and (10, 0, 0) <= numbers < (10, 0, 7):
        warning += (
            " kicad-cli of KiCad 10.0.0 to 10.0.6 draws only the first body style of a "
            "derived symbol (KiCad issue 25209, repaired in KiCad 10.0.7)."
        )
    return warning


def _page_line(name: str, page: SvgPage) -> str:
    smallest = page.smallest_text
    text = f"smallest text {_number(smallest, 2)} mm" if smallest is not None else "no text"
    scale = required_scale(page)
    return (
        f"- {name}: {_number(page.width, 2)} × {_number(page.height, 2)} mm, {text}, legible from "
        f"{scale:.1f} px/mm ({scale * 25.4:.0f} DPI)"
    )


def _tile_label(name: str, page: SvgPage, plan: Plan, index: int) -> str:
    tile = plan.tiles[index]
    width, height = tile.pixels
    count = len(plan.tiles)
    part = f"tile {index + 1} of {count}, " if count > 1 else ""
    label = (
        f"{name}: {part}x {_number(tile.x, 2)} to {_number(tile.x + tile.width, 2)} mm, "
        f"y {_number(tile.y, 2)} to {_number(tile.y + tile.height, 2)} mm, {tile.scale:.1f} px/mm "
        f"({tile.scale * 25.4:.0f} DPI), {width} × {height} px"
    )
    smallest = page.smallest_text
    if smallest is not None:
        label += f", smallest text {smallest * tile.scale:.0f} px"
        if tile.scale < required_scale(page):
            label += " (can be not legible: use the layout tiles or a region)"
    return label


@dataclass(frozen=True)
class _Drawn:
    """The result of one render: the images, and the summary items of the thread."""

    # (page name, page, plan, the PNG of each tile)
    drawn: list[tuple[str, SvgPage, Plan, list[bytes]]]
    skipped: list[tuple[str, SvgPage]]  # pages that the region is not on
    warning: str | None
    files: list[str]  # the files written to output_dir


async def _render(
    lib_id: str,
    item_type: str,
    max_width_px: int,
    max_height_px: int,
    layout: str,
    region: list[float] | None,
    pages: list[str] | None,
    project_path: str,
    output_dir: str,
    include_hidden: bool,
    max_images: int,
) -> list[str | Image] | str:
    if item_type not in _KINDS:
        return f"❌ The item type {item_type!r} is not 'symbol' or 'footprint'."
    for name, value in (("max_width_px", max_width_px), ("max_height_px", max_height_px)):
        if value not in _IMAGE_LIMITS:
            return f"❌ {name} {value} is not from 64 to 8192 pixels."
    if layout not in _LAYOUTS:
        return f"❌ The layout {layout!r} is not 'tiles' or 'overview'."
    if region is not None and (len(region) != 4 or not all(map(math.isfinite, region))):
        return f"❌ The region {region} is not [x, y, width, height] in mm."
    if max_images < 1:
        return f"❌ max_images {max_images} is less than 1."
    if include_hidden and item_type == "footprint":
        return (
            "❌ include_hidden applies only to a symbol: kicad-cli draws no hidden footprint item."
        )
    library_name, _, item_name = lib_id.partition(":")
    if not library_name or not item_name:
        return (_SYMBOL_ID_ERROR if item_type == "symbol" else _FOOTPRINT_ID_ERROR).format(lib_id)
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"

    def find() -> tuple[
        LibraryRow, list[str], tuple[list[LibrarySymbol], SymbolInfo] | Exception | None
    ]:
        if item_type == "footprint":
            # kicad-cli 8, 9 and 10 give the exit code 0 when the footprint
            # is not there (PCBNEW_JOBS_HANDLER::JobExportFpSvg).
            _project, row, problems, _file = _find_footprint(
                library_name, item_name, kicad, project_path
            )
            return row, problems, None
        try:
            chain, row, problems, info = _read_symbol(library_name, item_name, kicad, project_path)
        except (LibraryLookupError, ValueError) as e:
            # kicad-cli reads the symbol library: it gives an error for a
            # symbol that is not there, or for a file that it cannot read.
            # A symbol that only the server cannot read gets a warning.
            row, problems = find_kicad_library(
                "symbol", library_name, kicad, project_file(project_path)
            )
            return row, problems, e
        return row, problems, (chain, info)

    try:
        row, problems, symbol = await asyncio.to_thread(find)
    except LibraryLookupError as e:
        return f"❌ Cannot render the {item_type} {lib_id}: {e}"

    target = Path(output_dir).absolute() if output_dir else None
    box = tuple(region) if region is not None else None
    region_text = ("[" + ", ".join(_number(value) for value in box) + "]") if box else ""

    def draw(directory: Path, result: subprocess.CompletedProcess) -> str | _Drawn:
        svgs = sorted(directory.glob("*.svg"), key=_natural_key)
        if result.returncode != 0 or not svgs or (item_type == "footprint" and len(svgs) != 1):
            return _render_error(item_type, item_name, lib_id, kicad, row, result, len(svgs))
        warning = None
        if symbol is not None:
            warning = _page_count_warning(symbol, kicad, len(svgs))
        if pages:
            # The names of the files that kicad-cli wrote: they differ
            # between KiCad 8, 9 and 10, thus the tool does not make them.
            names = [svg.stem for svg in svgs]
            if unknown := [name for name in pages if name not in names]:
                return (
                    f"❌ The {item_type} {lib_id} has no page "
                    f"{', '.join(map(repr, unknown))}. Its pages are: {', '.join(names)}."
                )
            svgs = [svg for svg in svgs if svg.stem in pages]
        plans = []
        skipped = []
        try:
            for svg in svgs:
                page = read_svg(svg.read_text(encoding="utf-8"))
                try:
                    plan = plan_images(page, layout, max_width_px, max_height_px, box)
                except RegionError:
                    # The pages of a symbol can have different sizes.
                    skipped.append((svg.stem, page))
                    continue
                plans.append((svg, page, plan))
        except SvgImageError as e:
            return f"❌ Cannot draw the {item_type} {lib_id}: {svg.name}: {e}"
        if not plans:
            lines = [_page_line(name, page) for name, page in skipped]
            return (
                f"❌ The region {region_text} mm is not on a page of the {item_type} {lib_id}:\n"
                + "\n".join(lines)
            )
        count = sum(len(plan.tiles) for _svg, _page, plan in plans)
        if count > max_images:
            lines = [_page_line(svg.stem, page) for svg, page, _plan in plans]
            return (
                f"❌ The {item_type} {lib_id} needs {count} images of at most "
                f"{max_width_px} × {max_height_px} px for legible text, more than "
                f"max_images ({max_images}):\n" + "\n".join(lines) + "\n\nSelect some "
                "pages, or use the layout overview, a region, a larger max_images, or "
                "larger images if your vision input reads them with no reduction."
            )
        drawn = []
        try:
            for svg, page, plan in plans:
                pngs = [render_tile(page, tile, _BACKGROUNDS[item_type]) for tile in plan.tiles]
                drawn.append((svg, page, plan, pngs))
        except SvgImageError as e:
            return f"❌ Cannot draw the {item_type} {lib_id}: {svg.name}: {e}"
        files = []
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
            for svg, _page, _plan, pngs in drawn:
                # The SVG as kicad-cli wrote it, byte for byte.
                shutil.copyfile(svg, target / svg.name)
                files.append(svg.name)
                for index, png in enumerate(pngs):
                    name = svg.stem if len(pngs) == 1 else f"{svg.stem}_tile{index + 1}"
                    (target / f"{name}.png").write_bytes(png)
                    files.append(f"{name}.png")
        named = [(svg.stem, page, plan, pngs) for svg, page, plan, pngs in drawn]
        return _Drawn(named, skipped, warning, files)

    def run() -> str | _Drawn:
        # The thread owns the temporary directory. A cancelled tool call does
        # not stop the thread or kicad-cli, thus only the thread removes it.
        directory = Path(tempfile.mkdtemp(prefix="kicad-workbench-render-"))
        try:
            args = render_args(item_type, item_name, include_hidden, directory, row.path)
            try:
                result = run_kicad_cli_sync(args, timeout=_RENDER_TIMEOUT, english=True)
            except subprocess.TimeoutExpired:
                return f"❌ kicad-cli of KiCad {kicad} did not stop in {_RENDER_TIMEOUT} s."
            except RuntimeError as e:
                return f"❌ {e}"
            return draw(directory, result)
        finally:
            shutil.rmtree(directory)

    outcome = await asyncio.to_thread(run)
    if isinstance(outcome, str):
        return outcome

    images = sum(len(pngs) for *_rest, pngs in outcome.drawn)
    what = "unit and body style" if item_type == "symbol" else "footprint"
    lines = [
        f"# Rendered {item_type} {lib_id}",
        "",
        f"**Library**: {row.name} ({row.scope}): {row.path}",
        f'**Rendered by**: kicad-cli of KiCad {kicad}, color theme "KiCad Default"',
        f"**Images**: {images} PNG, layout {layout}, at most {max_width_px} × {max_height_px} px",
        f"**Pages** (one for each {what}, the origin is the top left corner):",
        *(_page_line(name, page) for name, page, _plan, _pngs in outcome.drawn),
    ]
    if outcome.skipped:
        lines.append(f"**Not on the region {region_text} mm** (no image):")
        lines += [_page_line(name, page) for name, page in outcome.skipped]
    if outcome.warning is not None:
        lines.append(outcome.warning)
    overlaps = {
        round(plan.overlap, 2) for _name, _page, plan, _pngs in outcome.drawn if len(plan.tiles) > 1
    }
    if overlaps:
        lines.append(
            "_Tiles that touch overlap by at least "
            f"{', '.join(_number(o, 2) for o in sorted(overlaps))} mm (the labels give the "
            "region of each tile)._"
        )
    for name, _page, plan, _pngs in outcome.drawn:
        if plan.split_text is not None:
            lines.append(
                f"_{name}: a text longer than {_number(plan.split_text, 2)} mm can be split "
                "over two tiles._"
            )
    if target is not None:
        lines.append(f"**Files** in {target}: {', '.join(outcome.files)}")
    if item_type == "symbol" and not include_hidden:
        lines.append("_Hidden pins and hidden fields are not shown (include_hidden)._")
    content: list[str | Image] = ["\n".join(lines + problem_lines(problems))]
    for name, page, plan, pngs in outcome.drawn:
        for index, png in enumerate(pngs):
            content += [_tile_label(name, page, plan, index), Image(data=png, format="png")]
    return content


@mcp.tool()
async def render_library_item(
    lib_id: str,
    item_type: str,
    max_width_px: int,
    max_height_px: int,
    layout: str = "tiles",
    region: list[float] | None = None,
    pages: list[str] | None = None,
    project_path: str = "",
    output_dir: str = "",
    include_hidden: bool = False,
    max_images: int = 16,
) -> list[str | Image] | str:
    """Render a symbol or a footprint of a KiCad library as PNG images.

    Use the images for a visual check: the layout, the texts, the pin 1 mark,
    the silkscreen on the pads, items that overlap. get_library_symbol and
    get_library_footprint give the exact values.

    kicad-cli of the installed KiCad draws the item as SVG, with the color
    theme "KiCad Default". The tool draws each SVG as PNG images with the
    background of that theme. The library comes from the library tables of
    the installed KiCad and of the project, as in the chooser of KiCad.

    Args:
        lib_id: The library ID, for example "Device:R" or
            "Resistor_SMD:R_0603_1608Metric".
        item_type: "symbol" or "footprint".
        max_width_px: The largest image width that your vision input reads
            with no reduction, from 64 to 8192 pixels.
        max_height_px: The largest image height that your vision input reads
            with no reduction. If your input also has a limit of the pixel
            count, give a width and a height whose product is in that limit.
        layout: "tiles": the smallest text gets at least 15 pixels (the
            height of a 1.27 mm text at 300 DPI), never less than 300 DPI. An
            item that does not fit in one image at that scale is divided into
            tiles that overlap. An item that fits gets one image, as large as
            the limit permits. "overview": one image of the full item, as
            large as the limit permits; its text can be not legible.
        region: Optional [x, y, width, height] in mm, a part of the page to
            draw (the origin is the top left corner of the page; the summary
            gives the page sizes). Use it to see a part in more detail. The
            region applies to each page: a page that it is not on gets no
            image.
        pages: Optional names of the pages to draw, from the summary or from
            an error (a symbol has one page for each unit and body style, for
            example "74HC00_unit2"). All pages if empty.
        project_path: Optional project (.kicad_pro, .kicad_sch, .kicad_pcb or
            its directory), to include its libraries.
        output_dir: Optional directory to keep the SVG and PNG files. The
            tool replaces files with the same names, and does not delete
            other files. The summary gives the files that it wrote.
        include_hidden: Symbol only: also draw the hidden pins and the
            hidden fields.
        max_images: The tool gives an error, not images, when the item needs
            more images than this.

    Returns:
        A summary with the page of each unit and body style (symbol) or of
        the footprint, then the images. The summary gives a warning when
        kicad-cli wrote a different number of pages than the symbol has. The label of each image gives its
        part of the page in mm, its scale and the size of the smallest text
        in pixels.
    """
    try:
        return await _render(
            lib_id,
            item_type,
            max_width_px,
            max_height_px,
            layout,
            region,
            pages,
            project_path,
            output_dir,
            include_hidden,
            max_images,
        )
    except Exception as e:
        return error_answer(e, "Error")
