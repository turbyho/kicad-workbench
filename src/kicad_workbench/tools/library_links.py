"""The tool check_library_links: the links of a project to its libraries and 3D models.

KiCad checks some links itself (KiCad 8.0.9, 9.0.9 and 10.0.6):

- ERC: lib_symbol_issues (a symbol whose library or library symbol KiCad
  does not find) and lib_symbol_mismatch (a symbol that is not equal to its
  library symbol, LIB_SYMBOL::Compare). KiCad 8 gives the two as
  lib_symbol_issues.
- DRC: lib_footprint_issues and lib_footprint_mismatch, the same for the
  footprints of the board.

The tool runs kicad-cli for these checks. KiCad does not check the 3D models:
the tool finds them as the STEP exporter does (model_paths.resolve_model),
with the library of the footprint and its embedded files, and for a board
also the embedded files of the board.
"""

import asyncio
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..parsers.board_models import board_models
from ..parsers.check_report_parser import parse_drc_report, parse_erc_report
from ..parsers.kicad_versions import version_numbers
from ..parsers.sexpr import SExprError
from ..server import mcp
from ..utils.kicad_cli import decode_output, get_kicad_version, run_kicad_cli_sync
from ..utils.kicad_text import KiCadEncodingError, read_as_kicad
from ..utils.library_tables import (
    LibraryTableError,
    find_library,
    kicad_environment,
    library_rows,
    project_variables,
)
from ..utils.model_paths import ModelFile, resolve_model
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.schematic_settings import rule_severities, unknown_severity_reason
from ..utils.tool_errors import error_answer
from .library import (
    NO_KICAD_CLI,
    SchematicFootprints,
    annotation_lines,
    annotation_sentences,
    cell,
    netlist_root,
    not_found_reason,
    problem_lines,
    project_file,
    schematic_footprints,
)

_CHECK_TIMEOUT = 600
# The types of the ERC and DRC checks, and their keys in the project file.
_ERC_TYPES = ("lib_symbol_issues", "lib_symbol_mismatch")
_DRC_TYPES = ("lib_footprint_issues", "lib_footprint_mismatch")
_ERC_KEYS = ("erc", "rule_severities")
_DRC_KEYS = ("board", "design_settings", "rule_severities")
# The default severity of the four checks in KiCad 8, 9 and 10.
_DEFAULT_SEVERITY = "warning"


# The flags of the result (the value 1): the result gives them with no number.
_FLAGS = {
    "schematic not checked",
    "schematic symbols not checked",
    "schematic footprints not checked",
    "schematic 3D models not checked",
    "board footprints not checked",
    "board 3D models not checked",
    "annotation errors (the server does not know the number)",
}
# The singular form of a count of the result.
_SINGULAR = {
    "symbols with a library problem": "symbol with a library problem",
    "annotation errors": "annotation error",
    "symbols whose footprint KiCad does not find": "symbol whose footprint KiCad does not find",
    "3D models of the schematic footprints not found": (
        "3D model of the schematic footprints not found"
    ),
    "board footprints with a library problem": "board footprint with a library problem",
    "3D models of the board footprints not found": "3D model of the board footprints not found",
}


def _count_text(name: str, count: int) -> str:
    """A count of the result, for example "1 annotation error" or "schematic not checked"."""
    if name in _FLAGS:
        return name
    return f"{count} {_SINGULAR.get(name, name) if count == 1 else name}"


def _run_check(args: list[str], source: Path) -> dict:
    """Run "kicad-cli sch erc" or "kicad-cli pcb drc" and return the JSON report.

    The thread that runs this function owns the temporary directory.

    Raises:
        RuntimeError: kicad-cli did not write the report.
    """
    directory = Path(tempfile.mkdtemp(prefix="kicad-workbench-check-"))
    try:
        report = directory / "report.json"
        command = [*args, "--format", "json", "--severity-all", "--output", str(report)]
        try:
            result = run_kicad_cli_sync(
                [*command, str(source)], timeout=_CHECK_TIMEOUT, english=True
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"kicad-cli did not stop in {_CHECK_TIMEOUT} s.") from e
        if result.returncode != 0 or not report.is_file():
            output = (decode_output(result.stdout) + decode_output(result.stderr)).strip()
            raise RuntimeError(
                f"kicad-cli stopped with the exit code {result.returncode}: {output}"
            )
        try:
            return json.loads(report.read_text(encoding="utf-8"))
        except ValueError as e:
            raise RuntimeError(f"the report of kicad-cli is not JSON: {e}") from e
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _check_lines(
    title: str,
    errors: list,
    types: tuple[str, ...],
    project: Path,
    keys: tuple[str, ...],
    kind: str,
    kicad: str,
) -> tuple[list[str], int]:
    """The section of the ERC or DRC violations of some types, and their count (not excluded).

    kind is "erc" or "drc", kicad the installed KiCad version (rule_severities).
    """
    lines = ["", f"## {title}", ""]
    defaults = dict.fromkeys(types, _DEFAULT_SEVERITY)
    unknown = []
    for name, severity in rule_severities(project, keys, defaults, kind, kicad).items():
        if severity == "ignore":
            lines.append(f"_The project ignores the check {name}: KiCad does not run it._")
        elif severity is None:
            unknown.append(name)
    if unknown:
        if len(unknown) > 1:
            checks = f"The severities of the checks {', '.join(unknown)} are"
        else:
            checks = f"The severity of the check {unknown[0]} is"
        lines.append(f"_{checks} not known: {unknown_severity_reason(kicad)}._")
    found = [error for error in errors if error.type in types]
    if not found:
        lines.append("✅ KiCad reports no problem.")
        return lines, 0
    # One row for each problem text, with the items of its violations.
    groups: dict[tuple[str, str, str], list[str]] = {}
    for error in found:
        severity = error.severity + (" (excluded)" if error.excluded else "")
        items = groups.setdefault((error.type, severity, error.description), [])
        items += [item.reference or item.description for item in error.items]
    lines += ["| Type | Severity | Problem | Items |", "|------|----------|---------|-------|"]
    lines += [
        f"| {kind} | {severity} | {cell(text)} | {cell(', '.join(dict.fromkeys(items)))} |"
        for (kind, severity, text), items in groups.items()
    ]
    return lines, sum(not error.excluded for error in found)


@dataclass(frozen=True)
class _Model:
    owner: str  # The footprint ID (schematic) or the reference (board).
    footprint: str
    file: str
    shown: bool
    found: ModelFile


_JOINED = (
    "The annotation errors change the netlist (one component for more than one symbol, "
    "or a unit that the library symbol does not have): the {item} of a symbol can be not "
    "checked."
)


def _annotation_note(found: SchematicFootprints, item: str) -> str | None:
    """The note about the annotation errors for the footprints or the 3D models
    ("footprint" or "3D model"), or None when there is no annotation error."""
    changed = _JOINED.format(item=item) if found.annotation_references else None
    notes = annotation_sentences(found, changed)
    return " ".join(notes) if notes else None


def _model_lines(
    title: str, models: list[_Model], owner: str, users: dict[str, str], note: str | None = None
) -> list[str]:
    """The section of the 3D models that KiCad does not find.

    note: The note about the annotation errors of the netlist (_annotation_note).
    """
    lines = ["", f"## {title}", ""]
    if not models:
        lines.append("_The footprints have no 3D model._")
        return lines
    states: dict[str, int] = {}
    for model in models:
        states[model.found.state] = states.get(model.found.state, 0) + 1
    lines.append("Models: " + ", ".join(f"{count} {state}" for state, count in states.items()))
    bad = [model for model in models if model.found.state in ("not found", "no file name")]
    if not bad:
        if note:
            lines += ["", f"⚠️ KiCad finds the file of each 3D model. {note}"]
        else:
            lines += ["", "✅ KiCad finds the file of each 3D model."]
        return lines
    if note:
        lines += ["", f"⚠️ {note}"]
    lines += [
        "",
        "_The 3D viewer and the STEP export show no model for these files. A model that is "
        "not shown (hide) is not in the 3D view and not in the STEP file, thus the result "
        "does not count it._",
        "",
        f"| {owner} | Footprint | Model | Shown | State | Note |",
        f"|{'-' * (len(owner) + 2)}|-----------|-------|-------|-------|------|",
    ]
    # One row for each footprint and model, with the symbols or the references.
    groups: dict[tuple[str, str, bool, str, str], list[str]] = {}
    for model in bad:
        key = (model.footprint, model.file, model.shown, model.found.state, model.found.note)
        groups.setdefault(key, []).append(users.get(model.owner, model.owner))
    for (footprint, file, shown, state, note), owners in groups.items():
        lines.append(
            f"| {cell(', '.join(owners))} | {cell(footprint)} | {cell(file)} "
            f"| {'yes' if shown else 'no'} | {state} | {cell(note)} |"
        )
    return lines


def _schematic_models(found: SchematicFootprints, kicad: str) -> list[_Model]:
    env = kicad_environment(kicad)
    variables = project_variables(found.project)
    major = version_numbers(kicad)[0]
    return [
        _Model(
            footprint,
            footprint,
            file,
            shown,
            resolve_model(
                file,
                major,
                env,
                found.project,
                variables,
                found.libraries[footprint],
                set(items.embedded_files),
            ),
        )  # fmt: skip
        for footprint, items in found.items.items()
        for file, shown in items.models
    ]


def _board_models(board: Path, project: Path, kicad: str) -> tuple[list[_Model], list[str]]:
    """The 3D models of the footprints of a board, as the STEP exporter finds them.

    The exporter uses the library of the footprint ID in the library tables as
    the footprint library path, and the embedded files of the footprint, then
    of the board (EXPORTER_STEP::buildFootprint3DShapes).

    Raises:
        KiCadEncodingError, SExprError: The board file cannot be read.
        LibraryTableError: The library tables cannot be read.
    """
    footprints, board_embedded = board_models(read_as_kicad(board))
    env = kicad_environment(kicad)
    variables = project_variables(project)
    major = version_numbers(kicad)[0]
    rows = library_rows("footprint", kicad, project)
    libraries: dict[str, Path | None] = {}
    models = []
    for footprint in footprints:
        # LIB_ID: an ID with no ":" has no library nickname.
        nickname = footprint.lib_id.partition(":")[0] if ":" in footprint.lib_id else ""
        if nickname not in libraries:
            row = None
            if nickname:
                row, _problems = find_library("footprint", nickname, kicad, project, rows)
            libraries[nickname] = row.path if row is not None else None
        for file, shown in footprint.models:
            found = resolve_model(
                file, major, env, project, variables, libraries[nickname],
                set(footprint.embedded_files), set(board_embedded),
            )  # fmt: skip
            models.append(_Model(footprint.reference, footprint.lib_id, file, shown, found))
    return models, list(rows[1])


def _footprint_lines(found: SchematicFootprints) -> tuple[list[str], int, dict[str, str]]:
    """The section of the schematic footprints that KiCad does not find.

    Returns:
        (the lines, the symbols with no footprint, footprint ID: its symbols).
    """
    by_reason: dict[str, list[str]] = {}
    users: dict[str, list[str]] = {}
    for component in found.components:
        if reason := not_found_reason(component, found):
            by_reason.setdefault(reason, []).append(component.reference)
        else:
            users.setdefault(component.footprint, []).append(component.reference)
    lines = [
        "",
        "## Schematic footprints",
        "",
        f"_The footprints of the {len(found.components)} symbols of the netlist of "
        '"Update PCB from Schematic", as pcbnew finds them in the library tables._',
        "",
    ]
    if by_reason:
        if note := _annotation_note(found, "footprint"):
            lines += [f"⚠️ {note}", ""]
        lines += ["| Reason | Symbols |", "|--------|---------|"]
        lines += [
            f"| {cell(reason)} | {cell(', '.join(refs))} |" for reason, refs in by_reason.items()
        ]
    else:
        count = f"{len(users)} footprint{'' if len(users) == 1 else 's'}"
        found_text = f"KiCad finds the footprint of each symbol ({count})."
        if note := _annotation_note(found, "footprint"):
            lines.append(f"⚠️ {found_text} {note}")
        else:
            lines.append(f"✅ {found_text}")
    count = sum(len(refs) for refs in by_reason.values())
    return lines, count, {footprint: ", ".join(refs) for footprint, refs in users.items()}


def _missing(models: list[_Model]) -> int:
    """The shown models with no file. KiCad does not use a hidden model."""
    return sum(
        model.shown and model.found.state in ("not found", "no file name") for model in models
    )


@dataclass
class _Part:
    """The result of the check of the schematic or of the board."""

    header: list[str]
    body: list[str]
    counts: dict[str, int]
    problems: list[str]


async def _schematic_part(schematic: Path, kicad: str) -> _Part:
    part = _Part([], [], {}, [])
    if not schematic.is_file():
        part.header.append(f"**Schematic**: none ({schematic.name} does not exist)")
        return part
    try:
        project = await asyncio.to_thread(schematic_project_file, schematic)
        root, tops = await asyncio.to_thread(netlist_root, project, kicad)
    except (AmbiguousRootError, ValueError) as e:
        part.header.append(f"**Schematic**: {schematic}")
        part.body += ["", "## Schematic", "", f"❌ {e}"]
        part.counts["schematic not checked"] = 1
        return part
    part.header.append(
        f"**Schematic**: {root}" + (f", with {tops} top-level sheets" if tops > 1 else "")
    )
    # ERC needs only the root: it runs at the same time as the netlist and
    # the footprints. The part always waits for it.
    erc = asyncio.create_task(asyncio.to_thread(_run_check, ["sch", "erc"], root))
    try:
        try:
            found = await schematic_footprints(schematic, kicad, embedded=True)
        except (ValueError, LibraryTableError) as e:
            found = f"❌ {e}"
    finally:
        (report,) = await asyncio.gather(erc, return_exceptions=True)
    if isinstance(report, RuntimeError):
        part.body += ["", "## Schematic symbols (ERC of KiCad)", "", f"❌ ERC failed: {report}"]
        part.counts["schematic symbols not checked"] = 1
    elif isinstance(report, BaseException):
        raise report
    else:
        lines, part.counts["symbols with a library problem"] = _check_lines(
            "Schematic symbols (ERC of KiCad)", parse_erc_report(report), _ERC_TYPES, project,
            _ERC_KEYS, "erc", kicad,
        )  # fmt: skip
        part.body += lines
    if isinstance(found, str):
        part.body += ["", "## Schematic footprints", "", found]
        part.counts["schematic footprints not checked"] = 1
        return part
    part.header += annotation_lines(
        found,
        kicad,
        "With a duplicate reference, the netlist has one component for two symbols, thus "
        "the footprints and the 3D models of a symbol can be not checked.",
    )
    if found.annotation_errors:
        part.counts["annotation errors"] = len(found.annotation_errors)
    elif found.annotation:
        part.counts["annotation errors (the server does not know the number)"] = 1
    lines, part.counts["symbols whose footprint KiCad does not find"], users = _footprint_lines(
        found
    )
    part.body += lines
    try:
        models = await asyncio.to_thread(_schematic_models, found, kicad)
    except LibraryTableError as e:
        part.body += ["", "## 3D models of the schematic footprints", "", f"❌ {e}"]
        part.counts["schematic 3D models not checked"] = 1
    else:
        part.counts["3D models of the schematic footprints not found"] = _missing(models)
        part.body += _model_lines(
            "3D models of the schematic footprints",
            models,
            "Symbols",
            users,
            _annotation_note(found, "3D model"),
        )
    part.problems += found.problems
    return part


async def _board_part(board: Path, kicad: str) -> _Part:
    """The check of a board, with the project of its name (as KiCad opens a board)."""
    part = _Part([], [], {}, [])
    if not board.is_file():
        part.header.append(f"**Board**: none ({board.name} does not exist)")
        return part
    part.header.append(f"**Board**: {board}")
    project = board.with_suffix(".kicad_pro")
    drc = asyncio.create_task(asyncio.to_thread(_run_check, ["pcb", "drc"], board))
    try:
        try:
            models = await asyncio.to_thread(_board_models, board, project, kicad)
        except (KiCadEncodingError, SExprError, LibraryTableError) as e:
            models = f"❌ The server cannot read the board or its library tables: {e}"
    finally:
        (report,) = await asyncio.gather(drc, return_exceptions=True)
    if isinstance(report, RuntimeError):
        part.body += ["", "## Board footprints (DRC of KiCad)", "", f"❌ DRC failed: {report}"]
        part.counts["board footprints not checked"] = 1
    elif isinstance(report, BaseException):
        raise report
    else:
        lines, part.counts["board footprints with a library problem"] = _check_lines(
            "Board footprints (DRC of KiCad)", parse_drc_report(report), _DRC_TYPES, project,
            _DRC_KEYS, "drc", kicad,
        )  # fmt: skip
        part.body += lines
    if isinstance(models, str):
        part.body += ["", "## 3D models of the board footprints", "", models]
        part.counts["board 3D models not checked"] = 1
    else:
        models, problems = models
        part.counts["3D models of the board footprints not found"] = _missing(models)
        part.body += _model_lines("3D models of the board footprints", models, "Reference", {})
        part.problems += problems
    return part


async def _check_links(project_path: str) -> str:
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"
    project = await asyncio.to_thread(project_file, project_path)
    if project is None:
        return "❌ Give the project: a .kicad_pro, .kicad_sch or .kicad_pcb file, or its directory."
    given = Path(project_path)
    schematic = given if given.suffix == ".kicad_sch" else project.with_suffix(".kicad_sch")
    board = given if given.suffix == ".kicad_pcb" else project.with_suffix(".kicad_pcb")
    if not schematic.is_file() and not board.is_file():
        return f"❌ The project {project} has no schematic and no board."
    # The two parts run at the same time. Each part gives its errors in its
    # sections, thus the tool waits for the two.
    parts = await asyncio.gather(
        _schematic_part(schematic, kicad), _board_part(board, kicad), return_exceptions=True
    )
    for part in parts:
        if isinstance(part, BaseException):
            raise part
    counts = {name: n for part in parts for name, n in part.counts.items()}
    wrong = [_count_text(name, count) for name, count in counts.items() if count]
    result = (
        "**Result**: ❌ " + ", ".join(wrong)
        if wrong
        else "**Result**: ✅ KiCad finds each library item and 3D model."
    )
    lines = [f"# Library links of the project {project.stem}", "", f"**Project**: {project}"]
    for part in parts:
        lines += part.header
    lines += [f"**Rules**: KiCad {kicad}", result]
    for part in parts:
        lines += part.body
    problems = [problem for part in parts for problem in part.problems]
    return "\n".join(lines + problem_lines(list(dict.fromkeys(problems))))


@mcp.tool()
async def check_library_links(project_path: str) -> str:
    """Check the links of a project to its symbol and footprint libraries and 3D models.

    The tool uses the checks of KiCad: ERC for the symbols of the schematic
    (a library or a symbol that KiCad does not find, a symbol that is not
    equal to its library symbol) and DRC for the footprints of the board. It
    finds the footprints of the schematic as "Update PCB from Schematic"
    does, and the 3D models of the footprints as the STEP export does
    (KiCad does not check them).

    Args:
        project_path: The project: a .kicad_pro, .kicad_sch or .kicad_pcb
            file, or its directory.

    Returns:
        The result, and a section for the schematic symbols, the schematic
        footprints and their 3D models, the board footprints and their 3D
        models.
    """
    try:
        return await _check_links(project_path)
    except Exception as e:
        return error_answer(e, "Error")
