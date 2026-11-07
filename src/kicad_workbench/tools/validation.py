"""Design Rule Checking (DRC) and Electrical Rules Check (ERC) tools for KiCad Workbench."""

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..models.types import DRCError, ERCError, ERCItem
from ..parsers.annotation import (
    ANNOTATION_TYPES,
    NOT_FULLY_ANNOTATED,
    UNANNOTATED_NOTE,
    UNANNOTATED_ROWS,
    AnnotationItem,
    ErcAnnotation,
    erc_violations,
)
from ..parsers.check_report_parser import (
    ErcReport,
    ErcUnits,
    parse_drc_report,
    read_erc_report,
    read_report_file,
)
from ..parsers.sch_geometry import IU_PER_MM
from ..server import mcp
from ..utils.file_save import save_files
from ..utils.kicad_cli import (
    KicadCliError,
    cli_root,
    decode_output,
    get_kicad_version,
    run_kicad_cli,
)
from ..utils.kicad_project import (
    PRO_SUFFIX,
)
from ..utils.sch_hierarchy import (
    AmbiguousRootError,
    project_files,
    schematic_project_file,
)
from ..utils.schematic_upgrade import with_current_schematics
from ..utils.tool_errors import EXPECTED_ERRORS, error_answer


def _temp_dir() -> Path:
    return Path(tempfile.gettempdir())


def _report_path(source: Path, kind: str) -> Path:
    """Deterministic report path in the temp directory for one source file.

    A short digest of the resolved source path is in the name, so two projects
    with the same file stem do not overwrite each other's reports. The
    reports are in a subdirectory, so reports from older server versions
    (different format, localized text) are not used again.
    """
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
    report_dir = _temp_dir() / "kicad-workbench"
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir / f"{source.stem}_{digest}_{kind}.json"


def _erc_sources(root: Path) -> list[Path]:
    """Return the files that change the ERC result of a root schematic: each
    schematic of its project (all top-level sheets) and the project files."""
    return project_files(root.with_suffix(".kicad_pro")) + sorted(root.parent.glob("*.kicad_pro"))


def _drc_sources(pcb: Path) -> list[Path]:
    """Return the files that change the DRC result."""
    return (
        [pcb]
        + sorted(pcb.parent.glob("*.kicad_pro"))
        + sorted(pcb.parent.glob("*.kicad_dru"))
        # The schematic parity: each schematic of the project (all top-level sheets).
        + project_files(pcb.with_suffix(".kicad_pro"))
    )


def _report_is_stale(report: Path, sources: list[Path]) -> bool:
    """Return True if the report is missing or older than one of the sources."""
    if not report.exists():
        return True
    report_mtime = report.stat().st_mtime
    return any(src.exists() and src.stat().st_mtime > report_mtime for src in sources)


def _md(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _erc_item_text(item: ERCItem) -> str:
    """Give a short text for one ERC item: pin, label or symbol."""
    if item.pin_number:
        ref = f"{item.reference}." if item.reference else ""
        name = f" {item.pin_name}" if item.pin_name else ""
        pin_type = f" ({item.pin_type})" if item.pin_type else ""
        return f"{ref}{item.pin_number}{name}{pin_type}"
    if item.label:
        return item.description
    if item.reference:
        return f"{item.reference} [{item.value}]" if item.value else item.reference
    return item.description


def _position_text(x: float | None, y: float | None) -> str:
    return f"({x:.2f}, {y:.2f})" if x is not None and y is not None else ""


def _join_cells(values: list[str]) -> str:
    return _md("; ".join(v for v in values if v)) or "-"


def _severity_cell(severity: str, excluded: bool) -> str:
    icon = "❌" if severity == "error" else "⚠️"
    return f"{icon} {severity} (excluded)" if excluded else f"{icon} {severity}"


_ERC_TABLE_HEADER = (
    "| Severity | Type | Description | Components | Items | Position (mm) |\n"
    "|----------|------|-------------|------------|-------|---------------|\n"
)


def _erc_row(error: ERCError, description_limit: int | None = None) -> str:
    """Format one ERC violation as a Markdown table row."""
    description = error.description[:description_limit] if description_limit else error.description
    components = ", ".join(dict.fromkeys(error.components)) or "N/A"
    items = _join_cells([_erc_item_text(i) for i in error.items])
    positions = _join_cells([_position_text(i.x_mm, i.y_mm) for i in error.items])
    return (
        f"| {_severity_cell(error.severity, error.excluded)} | {error.type} "
        f"| {_md(description)} | {components} | {items} | {positions} |\n"
    )


_DRC_TABLE_HEADER = (
    "| Severity | Type | Location | Description | Items |\n"
    "|----------|------|----------|-------------|-------|\n"
)


def _drc_row(error: DRCError, description_limit: int | None = None) -> str:
    """Format one DRC violation as a Markdown table row."""
    description = error.description[:description_limit] if description_limit else error.description
    x, y = error.location
    items = _join_cells([i.description for i in error.items])
    return (
        f"| {_severity_cell(error.severity, error.excluded)} | {error.type} "
        f"| ({x:.2f}, {y:.2f}) | {_md(description)} | {items} |\n"
    )


def _count_text(errors: list[ERCError] | list[DRCError]) -> str:
    """Give the error, warning and exclusion counts as Markdown list lines."""
    active = [e for e in errors if not e.excluded]
    error_count = sum(1 for e in active if e.severity == "error")
    warning_count = sum(1 for e in active if e.severity == "warning")
    text = f"- Errors: {error_count}\n- Warnings: {warning_count}"
    excluded_count = len(errors) - len(active)
    if excluded_count:
        text += f"\n- Excluded: {excluded_count}"
    return text


def _parse_erc_report(report_path: Path, schematic_files: list[Path] | None = None) -> ErcReport:
    """Read the ERC JSON report of kicad-cli.

    Args:
        report_path: Path to ERC report file
        schematic_files: Schematic files of the project. They give the lengths
            of the wires and buses for the item texts (read_erc_report).

    Returns:
        The ERC errors/warnings, and the notes about the limits of the values.

    Raises:
        OSError: The file cannot be read.
        InputError: The file is not a JSON report of KiCad.
    """
    data = read_report_file(report_path, "ERC")
    return read_erc_report(data, schematic_files or [], f"the ERC report {report_path}")


@dataclass
class _ErcResult:
    """The violations of an ERC run and the annotation check of the ERC dialog."""

    errors: list[ERCError]  # the annotation errors of the ERC dialog, then the report
    notes: list[str]  # Markdown lines
    annotation_checked: bool  # False: the result has no annotation check
    not_fully_annotated: bool = False  # the info bar of the ERC dialog
    unannotated: list[AnnotationItem] = field(default_factory=list)
    units: ErcUnits = field(default_factory=ErcUnits)  # the units of the JSON report

    @property
    def passed(self) -> bool:
        """True when the ERC dialog shows no violation and no annotation info bar."""
        return (
            self.annotation_checked
            and not self.not_fully_annotated
            and not any(not error.excluded for error in self.errors)
        )


async def _annotation_errors(root: Path) -> tuple[ErcAnnotation | None, list[str]]:
    """The annotation check of the ERC dialog, which kicad-cli does not run, and the
    notes about it (Markdown lines). None when the check did not run.

    ``kicad-cli sch erc`` does not run CheckAnnotate. The ERC dialog of the
    schematic editor runs it before the other checks (parsers/annotation.py).
    """
    version = await get_kicad_version()
    if version is None:
        return None, [
            "⚠️ The version of KiCad is not known: the result has no annotation check of "
            "the ERC dialog."
        ]
    try:
        project = await asyncio.to_thread(schematic_project_file, root)
        check = await asyncio.to_thread(erc_violations, project, version)
    except (WorkbenchError, OSError) as e:
        return None, [f"⚠️ The result has no annotation check of the ERC dialog: {e}"]
    lines = [
        f"_The annotation check is the check of the ERC dialog of KiCad {version} (kicad-cli "
        "does not run it): the server runs it with the rules of the KiCad source._"
    ]
    if check.not_fully_annotated:
        lines.insert(0, f"⚠️ **KiCad**: {NOT_FULLY_ANNOTATED}")
    return check, lines + [f"⚠️ {note}" for note in check.notes]


def _unannotated_text(items: list[AnnotationItem]) -> str:
    """The section of the symbols that are not annotated, or "" for none."""
    if not items:
        return ""
    rows = [
        f"| {_md(item.reference)} "
        f"| {f'{item.unit} of {item.unit_count}' if item.unit_count > 1 else '-'} "
        f"| {_md(item.value)} | {_md(item.sheet)} "
        f"| {_position_text(item.position[0] / IU_PER_MM, item.position[1] / IU_PER_MM)} |"
        for item in items[:UNANNOTATED_ROWS]
    ]
    more = (
        f"\n\n*... and {len(items) - UNANNOTATED_ROWS} more symbols*"
        if len(items) > UNANNOTATED_ROWS
        else ""
    )
    return (
        f"\n\n## Symbols that are not annotated ({len(items)})\n\n"
        f"_{UNANNOTATED_NOTE} Annotate the schematic in KiCad._\n\n"
        "| Reference | Unit | Value | Sheet | Position (mm) |\n"
        "|-----------|------|-------|-------|---------------|\n" + "\n".join(rows) + more
    )


async def _erc_result(report_path: Path, root: Path) -> _ErcResult:
    """The violations of the kicad-cli report and of the annotation check of the ERC
    dialog, and the notes about the annotation check."""
    # The files of all top-level sheets give the lines of the item texts.
    report = await asyncio.to_thread(
        lambda: _parse_erc_report(report_path, project_files(root.with_suffix(PRO_SUFFIX)))
    )
    errors = report.errors
    check, notes = await _annotation_errors(root)
    notes = notes + [f"⚠️ {note}" for note in report.notes]
    if check is None:
        return _ErcResult(errors, notes, False, units=report.units)
    return _ErcResult(
        check.violations + errors,
        notes,
        True,
        check.not_fully_annotated,
        check.unannotated,
        report.units,
    )


def _save_report(report: Path, output: Path) -> list[str]:
    """Save the report of kicad-cli into the output file: all of it or nothing.

    Returns:
        The warnings of the save (AfterSaveError).

    Raises:
        SaveFilesError: The output file cannot be written. It did not change.
    """
    return save_files([(output, report.read_bytes())])


def _notes_text(notes: list[str]) -> str:
    return "".join(f"\n\n{note}" for note in notes)


def _parse_drc_report(report_path: Path) -> list[DRCError]:
    """Read the DRC JSON report of kicad-cli.

    Args:
        report_path: Path to DRC report file

    Returns:
        List of DRC errors/warnings

    Raises:
        OSError: The file cannot be read.
        InputError: The file is not a JSON report of KiCad.
    """
    data = read_report_file(report_path, "DRC")
    return parse_drc_report(data, f"the DRC report {report_path}")


def _filter_erc_errors(errors: list[ERCError], severity: str = "") -> list[ERCError]:
    """Filter ERC errors by severity.

    Args:
        errors: List of ERC errors
        severity: Severity filter ('error', 'warning', '') for all

    Returns:
        Filtered list of ERC errors
    """
    if not severity:
        return errors

    return [e for e in errors if e.severity == severity]


def _filter_drc_errors(errors: list[DRCError], violation_type: str = "") -> list[DRCError]:
    """Filter DRC errors by type.

    Args:
        errors: List of DRC errors
        violation_type: Type filter (e.g., 'clearance', 'spacing', '') for all

    Returns:
        Filtered list of DRC errors
    """
    if not violation_type:
        return errors

    return [e for e in errors if e.type == violation_type]


@mcp.tool()
@with_current_schematics("schematic_path")
async def run_erc(
    schematic_path: str,
) -> str:
    """Run Electrical Rules Check (ERC) on schematic.

    Checks for:
    - Unconnected pins
    - Power conflicts
    - Multiple outputs on same net
    - Pin type mismatches
    - Annotation errors (symbols that are not annotated, duplicate references,
      units with different values), as the ERC dialog of KiCad finds them.
      kicad-cli does not run this check: the server runs it.

    Args:
        schematic_path: Path to .kicad_sch file

    Returns:
        ERC report with violations and recommendations
    """
    return await _run_erc(schematic_path, [])


async def _erc_root(sch_path: Path) -> tuple[Path, str] | str:
    """Return the schematic that kicad-cli checks (cli_root: the schematic of the
    project name, with the full project) and the note for the answer, or the error.
    kicad-cli drops the nets with no label when it checks a sub-sheet alone."""
    try:
        root = await asyncio.to_thread(cli_root, sch_path, "ERC")
    except AmbiguousRootError as e:
        return f"❌ **{e}.** Run the check on the root schematic that you want."
    except InputError as e:
        return f"❌ {e}"
    if root is None:
        return sch_path, ""
    return root, (
        f"\n\n⚠️ **Note:** `{sch_path.name}` is not the root schematic of its project. The "
        f"ERC is the ERC of `{root.name}`, the schematic that kicad-cli loads with the full "
        "project."
    )


async def _run_erc(
    schematic_path: str, results: list[_ErcResult], target: tuple[Path, str] | None = None
) -> str:
    """run_erc. It appends the result of a parsed report to results. target: the
    result of _erc_root, when the caller has it."""
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        found = target or await _erc_root(sch_path)
        if isinstance(found, str):
            return found
        sch_path, subsheet_note = found

        # The report has the name of the schematic that kicad-cli checks.
        erc_report_path = _report_path(sch_path, "erc")
        # Remove the old report: after the run, a report file must be the
        # result of this run.
        erc_report_path.unlink(missing_ok=True)

        # Run ERC using kicad-cli (KiCad 7+). --severity-all also gives the
        # excluded violations, with "excluded": true. The English kicad-cli
        # makes the item text parsable (pin number, pin name and pin type).
        try:
            result = await run_kicad_cli(
                [
                    "sch",
                    "erc",
                    "--format",
                    "json",
                    "--severity-all",
                    "--output",
                    str(erc_report_path),
                    str(sch_path),
                ],
                timeout=60,
                english=True,
            )
        except KicadCliError as e:  # the settings for an English kicad-cli
            return f"❌ {e}"

        stderr_text = decode_output(result.stderr)
        output_text = (decode_output(result.stdout) + "\n" + stderr_text).strip()

        if result.returncode != 0:
            return f"""❌ **ERC Check Failed**

**Schematic:** {schematic_path}

**Error:**
```
{stderr_text}
```

**Possible causes:**
- Invalid schematic file
- Missing project file (.kicad_pro)

**Manual steps:**
1. Open schematic in KiCad Eeschema
2. Run: Inspect → Electrical Rules Checker
3. Export report manually{subsheet_note}"""

        # Parse ERC report
        if not erc_report_path.exists():
            # kicad-cli returned success but produced no report — do NOT claim
            # "all pins connected" (that was never verified).
            return f"""❌ **ERC Report Not Generated**

**Schematic:** {schematic_path}

kicad-cli stopped with the exit code 0, but it wrote no report file
({erc_report_path}). Thus the tool has no result of the check.

**Output of kicad-cli:**
```
{output_text or "(none)"}
```{subsheet_note}"""

        # Parse and format results
        try:
            result = await _erc_result(erc_report_path, sch_path)
            results.append(result)
            errors = result.errors
            notes_text = _notes_text(result.notes)
            unannotated_text = _unannotated_text(result.unannotated)

            if not any(not e.excluded for e in errors):
                excluded_note = (
                    f"\n\n{len(errors)} excluded violation(s) are not counted." if errors else ""
                )
                if result.passed:
                    title = "✅ **ERC Check Passed**"
                elif result.annotation_checked:
                    title = "⚠️ **ERC: no violation, but the schematic is not fully annotated**"
                else:
                    title = "⚠️ **ERC: no violation in the report of kicad-cli**"
                return f"""{title}

**Schematic:** {schematic_path}

No electrical violations detected!{excluded_note}{notes_text}{subsheet_note}{unannotated_text}"""

            # Format violations
            violations_table = _ERC_TABLE_HEADER
            for error in errors[:20]:  # Limit to first 20
                violations_table += _erc_row(error, description_limit=50)

            if len(errors) > 20:
                violations_table += f"\n*... and {len(errors) - 20} more violations*\n"

            return f"""❌ **ERC Violations Detected**

**Schematic:** {schematic_path}
**Total Violations:** {len(errors)}
{_count_text(errors)}{notes_text}{subsheet_note}

## Violations

{violations_table}{unannotated_text}

## Recommendations

1. **Fix all errors** before proceeding to PCB layout
2. Review warnings - some may be acceptable
3. Re-run ERC after fixes
4. Use `get_erc_violations()` for detailed filtering

## Next Steps

- Run `get_erc_violations()` with filters to analyze specific issues
- Fix violations in KiCad Eeschema
- Re-run `run_erc()` to verify fixes"""

        except EXPECTED_ERRORS as e:  # an other exception is a defect: the traceback
            return f"""❌ **ERC Report Parse Error**

**Schematic:** {schematic_path}

ERC check completed but failed to parse report.

**Error:** {str(e)}

**Report location:** {erc_report_path}

You can manually inspect the report file or re-run ERC in KiCad GUI.{subsheet_note}"""

    except FileNotFoundError as e:
        # Distinguish kicad-cli missing (raised by subprocess) from a missing
        # schematic file (already checked at the top of the function).
        if "kicad-cli" in str(e):
            return f"""❌ **kicad-cli Not Found**

**Schematic:** {schematic_path}

KiCad's command-line tool (kicad-cli) could not be located on PATH or in the
detected KiCad install directory. Install KiCad 8 or later (https://www.kicad.org/),
or point the KICAD_CLI environment variable at the kicad-cli executable."""
        return f"""❌ **File Not Found**

**Schematic:** {schematic_path}

{e}
Please check the file path and try again."""
    except Exception as e:
        return error_answer(e, f"ERC of {schematic_path} failed")


@mcp.tool()
@with_current_schematics("pcb_path")
async def run_drc(
    pcb_path: str,
) -> str:
    """Run Design Rules Check (DRC) on PCB.

    Checks for:
    - Clearance violations
    - Track spacing violations
    - Missing connections
    - Pad/footprint overlaps
    - Board edge constraints

    Args:
        pcb_path: Path to .kicad_pcb file

    Returns:
        DRC report with violations and locations
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ **PCB file not found:** {pcb_path}"

        # DRC report output path
        drc_report_path = _report_path(pcb, "drc")
        # Remove the old report: after the run, a report file must be the
        # result of this run.
        drc_report_path.unlink(missing_ok=True)

        # Run DRC using kicad-cli (KiCad 7+). --severity-all also gives the
        # excluded violations. The schematic parity check is possible only
        # when the schematic of the board is available.
        args = ["pcb", "drc", "--format", "json", "--severity-all"]
        if pcb.with_suffix(".kicad_sch").exists():
            args.append("--schematic-parity")
        try:
            result = await run_kicad_cli(
                args + ["--output", str(drc_report_path), str(pcb)],
                timeout=60,
                english=True,
            )
        except KicadCliError as e:  # the settings for an English kicad-cli
            return f"❌ {e}"

        stderr_text = decode_output(result.stderr)
        output_text = (decode_output(result.stdout) + "\n" + stderr_text).strip()

        if result.returncode != 0:
            return f"""❌ **DRC Check Failed**

**PCB:** {pcb_path}

**Error:**
```
{stderr_text}
```

**Possible causes:**
- Invalid PCB file
- Missing design rules

**Manual steps:**
1. Open PCB in KiCad Pcbnew
2. Run: Inspect → Design Rules Checker
3. Export report manually"""

        # Parse DRC report
        if not drc_report_path.exists():
            # kicad-cli returned success but wrote no report. Do NOT claim
            # "passed": no check was verified.
            return f"""❌ **DRC Report Not Generated**

**PCB:** {pcb_path}

kicad-cli stopped with the exit code 0, but it wrote no report file
({drc_report_path}). Thus the tool has no result of the check.

**Output of kicad-cli:**
```
{output_text or "(none)"}
```"""

        # Parse and format results
        try:
            errors = _parse_drc_report(drc_report_path)

            if not any(not e.excluded for e in errors):
                excluded_note = (
                    f"\n\n{len(errors)} excluded violation(s) are not counted." if errors else ""
                )
                return f"""✅ **DRC Check Passed**

**PCB:** {pcb_path}

No design rule violations detected!{excluded_note}"""

            # Group by type (excluded violations are not counted)
            type_counts: dict[str, int] = {}
            for error in errors:
                if not error.excluded:
                    type_counts[error.type] = type_counts.get(error.type, 0) + 1

            # Format violations: the most severe first. Unconnected items and
            # schematic parity errors are at the end of the report, thus
            # they could be lost after the 20-row limit.
            ordered = sorted(
                errors,
                key=lambda e: (e.excluded, e.severity != "error", e.category == "violation"),
            )
            violations_table = _DRC_TABLE_HEADER
            for error in ordered[:20]:  # Limit to first 20
                violations_table += _drc_row(error, description_limit=40)

            if len(errors) > 20:
                violations_table += f"\n*... and {len(errors) - 20} more violations*\n"

            # Format type summary
            type_summary = "\n## Violation Summary\n\n"
            for vtype, count in sorted(type_counts.items()):
                type_summary += f"- **{vtype}**: {count} violations\n"

            return f"""❌ **DRC Violations Detected**

**PCB:** {pcb_path}
**Total Violations:** {len(errors)}
{_count_text(errors)}

{type_summary}
## Violations

{violations_table}

## Recommendations

1. **Fix all errors** before manufacturing
2. Review warnings - some may be acceptable
3. Pay attention to clearance violations
4. Re-run DRC after fixes
5. Use `get_drc_violations()` for detailed filtering

## Next Steps

- Run `get_drc_violations()` with filters to analyze specific issues
- Fix violations in KiCad Pcbnew
- Re-run `run_drc()` to verify fixes"""

        except EXPECTED_ERRORS as e:  # an other exception is a defect: the traceback
            return f"""❌ **DRC Report Parse Error**

**PCB:** {pcb_path}

DRC check completed but failed to parse report.

**Error:** {str(e)}

**Report location:** {drc_report_path}

You can manually inspect the report file or re-run DRC in KiCad GUI."""

    except FileNotFoundError:
        return f"""❌ **File Not Found**

**PCB:** {pcb_path}

Please check the file path and try again."""
    except Exception as e:
        return error_answer(e, f"DRC of {pcb_path} failed")


@mcp.tool()
@with_current_schematics("schematic_path")
async def get_erc_violations(
    schematic_path: str,
    severity: str = "",
) -> str:
    """Get filtered ERC violations from schematic.

    Args:
        schematic_path: Path to .kicad_sch file
        severity: Filter by severity ('error', 'warning', '') for all

    Returns:
        Filtered list of ERC violations
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # The report has the name of the schematic that kicad-cli checks.
        found = await _erc_root(sch_path)
        if isinstance(found, str):
            return found
        root = found[0]
        erc_report_path = _report_path(root, "erc")

        results: list[_ErcResult] = []
        if _report_is_stale(erc_report_path, _erc_sources(root)):
            # Run ERC first. run_erc gives "❌" also when it found violations,
            # thus only a missing report is a failure.
            initial_result = await _run_erc(schematic_path, results, found)
            if not erc_report_path.exists():
                return initial_result

        # Parse ERC report
        erc = results[0] if results else await _erc_result(erc_report_path, root)
        errors = erc.errors
        notes_text = _notes_text(erc.notes)
        unannotated_text = _unannotated_text(erc.unannotated)

        if not errors:
            if erc.passed:
                title = "✅ **No ERC Violations**"
            elif erc.annotation_checked:
                title = "⚠️ **No ERC violation, but the schematic is not fully annotated**"
            else:
                title = "⚠️ **No violation in the report of kicad-cli**"
            return f"""{title}

**Schematic:** {schematic_path}

No electrical violations detected.{notes_text}{unannotated_text}"""

        # Apply filter
        filtered_errors = _filter_erc_errors(errors, severity)

        if not filtered_errors:
            title = (
                "✅ **No Matching Violations**" if erc.passed else "⚠️ **No Matching Violations**"
            )
            return f"""{title}

**Schematic:** {schematic_path}
**Filter:** severity = '{severity or 'all'}'

No violations match the specified filter.{notes_text}{unannotated_text}"""

        # Format results
        result = f"""# ERC Violations

**Schematic:** {schematic_path}
**Filter:** severity = '{severity or 'all'}'
**Count:** {len(filtered_errors)}{notes_text}

## Violations

{_ERC_TABLE_HEADER}"""

        for error in filtered_errors:
            result += _erc_row(error)

        return result + unannotated_text

    except Exception as e:
        return error_answer(e, f"ERC of {schematic_path} failed")


@mcp.tool()
@with_current_schematics("pcb_path")
async def get_drc_violations(
    pcb_path: str,
    violation_type: str = "",
) -> str:
    """Get filtered DRC violations from PCB.

    Args:
        pcb_path: Path to .kicad_pcb file
        violation_type: Filter by type (e.g., 'clearance', 'spacing', '') for all

    Returns:
        Filtered list of DRC violations
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ **PCB file not found:** {pcb_path}"

        # DRC report output path
        drc_report_path = _report_path(pcb, "drc")

        if _report_is_stale(drc_report_path, _drc_sources(pcb)):
            # Run DRC first. run_drc gives "❌" also when it found violations,
            # thus only a missing report is a failure.
            initial_result = await run_drc(pcb_path)
            if not drc_report_path.exists():
                return initial_result

        # Parse DRC report
        errors = _parse_drc_report(drc_report_path)

        if not errors:
            return f"""✅ **No DRC Violations**

**PCB:** {pcb_path}

No design rule violations detected."""

        # Apply filter
        filtered_errors = _filter_drc_errors(errors, violation_type)

        if not filtered_errors:
            return f"""✅ **No Matching Violations**

**PCB:** {pcb_path}
**Filter:** type = '{violation_type or 'all'}'

No violations match the specified filter."""

        # Format results
        result = f"""# DRC Violations

**PCB:** {pcb_path}
**Filter:** type = '{violation_type or 'all'}'
**Count:** {len(filtered_errors)}

## Violations

{_DRC_TABLE_HEADER}"""

        for error in filtered_errors:
            result += _drc_row(error)

        return result

    except Exception as e:
        return error_answer(e, f"DRC of {pcb_path} failed")


@mcp.tool()
@with_current_schematics("schematic_path")
async def export_erc_report(
    schematic_path: str,
    output_path: str = "",
) -> str:
    """Export ERC report to file.

    The report is the kicad-cli ERC JSON report.

    Args:
        schematic_path: Path to .kicad_sch file
        output_path: Optional output file path (default: schematic_name_erc_report.json)

    Returns:
        Confirmation with output file location
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # The report has the name of the schematic that kicad-cli checks.
        found = await _erc_root(sch_path)
        if isinstance(found, str):
            return found
        root = found[0]
        erc_report_path = _report_path(root, "erc")

        # Run ERC first to generate a current report. run_erc gives "❌" also
        # when it found violations, thus only a missing report is a failure.
        results: list[_ErcResult] = []
        initial_result = await _run_erc(schematic_path, results, found)

        if not erc_report_path.exists():
            return f"❌ **ERC Check Failed**\n\n{initial_result}"

        # Determine output path
        if not output_path:
            output_path = str(sch_path.parent / (sch_path.stem + "_erc_report.json"))

        output = Path(output_path)
        if output.is_dir():
            return f"❌ The output path {output} is a directory. Give a .json file."
        # The save of KiCad 10.0.7 (file_save.save_files): after an error, the old
        # report stays, not a part of the new one.
        saved = await asyncio.to_thread(_save_report, erc_report_path, output)
        result = results[0] if results else await _erc_result(erc_report_path, root)
        notes = list(result.notes)
        if result.units.pcb_scale:
            notes.insert(
                0,
                "⚠️ kicad-cli of this KiCad version writes the positions and the lengths of "
                "the JSON report 100 times too small (ERC_REPORT::WriteJsonReport uses the "
                "board units; KiCad 10.0.7 corrects it). run_erc and get_erc_violations give "
                "the correct values.",
            )
        active = [
            error
            for error in result.errors
            if error.type in ANNOTATION_TYPES and not error.excluded
        ]
        if active:
            notes.insert(
                0,
                f"⚠️ The report is the report of kicad-cli: it does not have the "
                f"{len(active)} annotation error(s) of the ERC dialog. run_erc and "
                "get_erc_violations show them.",
            )
        if result.unannotated:
            notes.append(
                f"⚠️ {len(result.unannotated)} symbol(s) are not annotated: run_erc and "
                "get_erc_violations list them."
            )
        notes += [f"⚠️ {warning}" for warning in saved]

        return f"""✅ **ERC Report Exported**

**Schematic:** {schematic_path}
**Output:** {output}

The ERC report has been successfully exported to the specified location.{_notes_text(notes)}"""

    except Exception as e:
        return error_answer(e, f"Export of the ERC report of {schematic_path} failed")


@mcp.tool()
@with_current_schematics("pcb_path")
async def export_drc_report(
    pcb_path: str,
    output_path: str = "",
) -> str:
    """Export DRC report to file.

    The report is the kicad-cli DRC JSON report.

    Args:
        pcb_path: Path to .kicad_pcb file
        output_path: Optional output file path (default: pcb_name_drc_report.json)

    Returns:
        Confirmation with output file location
    """
    try:
        pcb = Path(pcb_path)
        if not pcb.exists():
            return f"❌ **PCB file not found:** {pcb_path}"

        drc_report_path = _report_path(pcb, "drc")

        # Run DRC first to generate a current report. run_drc gives "❌" also
        # when it found violations, thus only a missing report is a failure.
        initial_result = await run_drc(pcb_path)

        if not drc_report_path.exists():
            return f"❌ **DRC Check Failed**\n\n{initial_result}"

        # Determine output path
        if not output_path:
            output_path = str(pcb.parent / (pcb.stem + "_drc_report.json"))

        output = Path(output_path)
        if output.is_dir():
            return f"❌ The output path {output} is a directory. Give a .json file."
        saved = await asyncio.to_thread(_save_report, drc_report_path, output)
        notes = [f"⚠️ {warning}" for warning in saved]

        return f"""✅ **DRC Report Exported**

**PCB:** {pcb_path}
**Output:** {output}

The DRC report has been successfully exported to the specified location.{_notes_text(notes)}"""

    except Exception as e:
        return error_answer(e, f"Export of the DRC report of {pcb_path} failed")
