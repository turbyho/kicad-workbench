"""The tool assign_footprint: the footprints of placed symbols, as CvPcb of KiCad 10.0.6
assigns them (plans/roadmap.md, "Step 9"):

- The save of CvPcb (SCH_EDITOR_CONTROL::AssignFootprints) sets the text of the
  Footprint field of each unit of a reference, in the symbol (all instances of a shared
  sheet), not in a variant. A Footprint field that was empty and visible becomes hidden.
  symbol_fields plans the write (FootprintChange).
- CvPcb lists the components of the netlist of NETLIST_EXPORTER_KICAD: the same netlist
  as kicad-cli gives with --format kicadsexpr. It gives the pin count and the footprint
  filters of each component (board_netlist.read_cvpcb_parts). A symbol that is excluded
  from the board is not in it.
- The footprint must be in a library that KiCad loads (user decision, 2026-11-19: an
  error). The filters of CvPcb (footprint_filter.filter_matches) and the pin count
  filter (the pin count against FOOTPRINT::GetUniquePadCount(DO_NOT_INCLUDE_NPTH)) give
  a warning.
"""

import asyncio
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..errors import InputError
from ..parsers.board_netlist import CvpcbPart, read_cvpcb_parts
from ..parsers.sexpr import SExprError
from ..server import mcp
from ..utils.file_versions import with_library_versions, with_schematic_versions
from ..utils.footprint_filter import filter_matches
from ..utils.kicad_cli import KicadCliError
from ..utils.kicad_python import KicadPythonError, check_same_kicad
from ..utils.kicad_text import KiCadEncodingError
from ..utils.library_footprint import FootprintItems, FootprintReadError, read_footprints
from ..utils.library_lookup import LibraryLookupError
from ..utils.library_tables import library_rows
from ..utils.sch_hierarchy import netlist_root
from ..utils.tool_errors import error_answer
from .library import board_netlist_text, cell, checked_footprint_file
from .symbol_fields import FootprintChange, set_symbols


class FootprintAssignment(BaseModel):
    """The footprint of one reference."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(description="The reference of the symbol, for example U3.")
    footprint: str = Field(
        description=(
            'The library ID of the footprint, "<library>:<footprint>", for example '
            '"Package_SO:SOIC-8_3.9x4.9mm_P1.27mm". Empty: remove the footprint.'
        )
    )


def _footprint_files(
    footprints: list[str], kicad: str, project: Path
) -> tuple[dict[str, tuple[Path, str]], list[str]]:
    """Find the footprint files as KiCad finds them, with one read of the library tables.

    Returns:
        (footprint ID: (the .pretty directory, the name), the problems of the tables).

    Raises:
        InputError: KiCad does not find a footprint, or cannot read its file.
    """
    files: dict[str, tuple[Path, str]] = {}
    if not footprints:
        return files, []
    rows = library_rows("footprint", kicad, project)
    for footprint in footprints:
        library, _colon, name = footprint.partition(":")
        try:
            row, _problems, _file = checked_footprint_file(library, name, kicad, project, rows)
        except LibraryLookupError as e:
            raise InputError(f'KiCad does not find the footprint "{footprint}": {e}') from e
        files[footprint] = (row.path, name)
    return files, list(dict.fromkeys(rows[1]))


async def _pads(files: dict[str, tuple[Path, str]], kicad: str) -> dict[str, FootprintItems]:
    """Load the footprints with pcbnew.

    Raises:
        InputError: KiCad cannot load a footprint.
        FootprintReadError, KicadPythonError: pcbnew cannot run, or it is an other KiCad.
    """
    reader, loaded = await read_footprints(list(files.values()))
    if files:
        check_same_kicad(reader, "the Python with pcbnew", kicad)
    items: dict[str, FootprintItems] = {}
    for footprint, found in zip(files, loaded, strict=True):
        if found is None:
            directory, name = files[footprint]
            raise InputError(
                f'KiCad cannot load the footprint "{footprint}" ({directory / name}.kicad_mod). '
                "Open it in the footprint editor to see the error."
            )
        items[footprint] = found
    return items


async def _netlist_parts(project: Path, kicad: str) -> dict[str, CvpcbPart]:
    """The pin count and the filters of each component of the netlist of CvPcb.

    Raises:
        InputError: kicad-cli cannot give the netlist of the full project, or it fails.
    """
    root, _tops = await asyncio.to_thread(netlist_root, project, kicad)
    try:
        text, _messages = await asyncio.to_thread(board_netlist_text, root)
        return read_cvpcb_parts(text)
    except (KicadCliError, KiCadEncodingError, SExprError) as e:
        raise InputError(f"kicad-cli did not give the netlist of the project: {e}") from e


def _unknown_references(changes: list[FootprintChange], parts: dict[str, CvpcbPart]) -> str:
    """The error for the references that the netlist of CvPcb does not have, or ""."""
    missing = []
    for change in changes:
        if change.reference in parts:
            continue
        other = next((r for r in parts if r.upper() == change.reference.upper()), None)
        if other is not None:
            # AssignFootprints compares the references with the same case.
            return (
                f"Give the reference {other!r}, not {change.reference!r}: CvPcb compares the case."
            )
        missing.append(change.reference)
    if not missing:
        return ""
    return (
        f"The netlist of CvPcb has no component {', '.join(missing)}: CvPcb does not list a "
        "symbol that is excluded from the board, or that is in a sheet that is excluded from "
        "the board."
    )


async def _footprint_checks(
    changes: list[FootprintChange], project: Path, kicad: str
) -> tuple[list[str], list[str]] | str:
    """The checks of CvPcb for the assignments: each footprint is in a library that KiCad
    loads (an error), each reference is in the netlist of CvPcb (an error), and the
    footprint filters and the pin count (warnings).

    Returns:
        (the lines of the output, the warnings), or the text of an error.
    """
    footprints = list(dict.fromkeys(c.footprint for c in changes if c.footprint))
    try:
        files, problems = await asyncio.to_thread(_footprint_files, footprints, kicad, project)
        # pcbnew and kicad-cli do not depend on each other. Each one runs to its end: a
        # cancel does not stop a subprocess.
        found = await asyncio.gather(
            _pads(files, kicad), _netlist_parts(project, kicad), return_exceptions=True
        )
        for result in found:
            if isinstance(result, BaseException):
                raise result
        items, parts = found
    except (InputError, FootprintReadError, KicadPythonError) as e:
        return f"❌ {e} The tool wrote no file."
    if error := _unknown_references(changes, parts):
        return f"❌ {error} The tool wrote no file."
    lines = [
        "| Reference | Footprint | Footprint filters | Filters match | Pins | Pads |",
        "|---|---|---|---|---|---|",
    ]
    warnings = list(problems)
    for change in changes:
        if not change.footprint:
            continue
        part = parts[change.reference]
        footprint_items = items[change.footprint]
        nickname, _colon, name = change.footprint.partition(":")
        matches = filter_matches(part.filters, nickname, name)
        pads = footprint_items.unique_pad_count
        lines.append(
            "| "
            + " | ".join(
                cell(text)
                for text in (
                    change.reference,
                    change.footprint,
                    ", ".join(part.filters) or "(none)",
                    "yes" if matches else "no",
                    str(part.pin_count),
                    str(pads),
                )
            )
            + " |"
        )
        if not matches:
            warnings.append(
                f"The footprint filters of {change.reference} ({', '.join(part.filters)}) do not "
                f'match "{change.footprint}": CvPcb does not show it in the list of the filters.'
            )
        if part.pin_count != pads:
            warnings.append(
                f'{change.reference} has {part.pin_count} pins, and "{change.footprint}" has '
                f"{pads} pads: CvPcb does not show it in the list of the pin count."
            )
    if len(lines) == 2:
        return [], warnings
    lines.append("")
    lines.append(
        "The pins are the pins of the symbol in the netlist of CvPcb (all units, each number one "
        "time). The pads are the pad numbers of the footprint on copper, not NPTH, each number one "
        "time (FOOTPRINT::GetUniquePadCount)."
    )
    return lines, warnings


@mcp.tool()
@with_schematic_versions("schematic_path", write=True)
@with_library_versions
async def assign_footprint(schematic_path: str, assignments: list[FootprintAssignment]) -> str:
    """Assign footprints to placed symbols, as CvPcb ("Assign Footprints") of KiCad 10
    does it when it saves.

    The tool writes the footprint into the Footprint field of each unit of the
    reference, in the symbol, not in a variant. A variant with a Footprint of its own
    keeps it (the tool gives a warning; set_symbol_fields with the variant changes it).
    When the old footprint is empty and the field is visible, the field becomes
    hidden, as the save of CvPcb does ("Footprint field visible" in the table), also
    when the footprint stays empty. CvPcb hides such a field in each symbol of the
    netlist; the tool changes only the references of the call. An empty footprint
    removes the footprint.

    The tool checks each footprint as CvPcb has it:
    - The footprint must be in a library that KiCad loads (the global and the project
      footprint library tables, an enabled row of the type "KiCad"), with the same
      case: else an error.
    - The footprint filters of the symbol (anchored wildcards, with no difference of
      case; a filter with ":" compares "<library>:<footprint>") and the pin count (the
      pins of the symbol against the pads of the footprint) give a warning when they do
      not match. CvPcb then does not show the footprint in that filtered list. The
      tool still writes the footprint.

    The tool gets the pin count and the filters from the netlist of kicad-cli (the
    netlist that CvPcb reads). It refuses a reference that the netlist does not have
    (a symbol that is excluded from the board, or in a sheet that is excluded from the
    board), a power symbol (a reference that starts with "#"), a reference that is not
    annotated (U?), a duplicate reference, a symbol with no Footprint field, a Footprint
    field with a text variable (KiCad compares the resolved text), a footprint ID with
    no library name, and a library of an other type than "KiCad". With kicad-cli
    10.0.0 to 10.0.6, a project with more than one top-level sheet is an error.

    A symbol in a sheet that the hierarchy uses more than one time has one Footprint
    field for all its references (U201 and U301): give the same footprint for each of
    these references. CvPcb changes all of them; the tool refuses a call that gives
    only one.

    The tool checks all assignments and all files first, and writes no file when a
    check fails. It writes all files or no file. KiCad must not have the files open. A
    schematic file of the project with an older version than the installed KiCad
    writes is an error: the user converts it in KiCad first. The installed KiCad must
    be KiCad 10.0.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool finds the
            references in the full project.
        assignments: One item for each reference: the reference and the footprint
            ("<library>:<footprint>", or empty to remove it).

    Returns:
        A table of the Footprint fields before and after the write, the files, and a
        table of the filters, the pin count and the pad count of each footprint.
    """
    if not assignments:
        return "❌ assignments is empty. Give the references and their footprints."
    try:
        changes = [
            FootprintChange(reference=a.reference, footprint=a.footprint) for a in assignments
        ]
        return await set_symbols(
            schematic_path,
            changes,
            FootprintChange.what,
            partial(_footprint_checks, changes),
        )
    except Exception as e:
        return error_answer(e, "Error")
