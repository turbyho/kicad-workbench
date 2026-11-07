"""The schematic files of a project in the version of the installed KiCad.

Each KiCad opens a file of an older version and saves it in its own version. A
file of a newer version than the installed KiCad is an error: KiCad cannot open
it (``SCH_IO_KICAD_SEXPR_PARSER::ParseSchematic``: ``m_requiredVersion >
SEXPR_SCHEMATIC_FILE_VERSION``). Thus, before each work with a schematic project,
the installed KiCad converts and saves each schematic file of the hierarchy with
an older version (user decision, 2026-11-07, plans/roadmap.md "Decisions", "File
versions"). Then the tools read and write the files in the format of the
installed KiCad, as the user sees them in KiCad.

Measured facts (spike/kicad-versions/upgrade_check.py, plans/roadmap.md):

- kicad-cli 10 ``sch upgrade <file>`` converts and saves only that file, not its
  sub-sheets. A current file stays as it is. The project file does not change.
  The netlist and the BOM of kicad-cli are equal before and after the conversion.
- kicad-cli 8 and 9 have no command that saves a schematic, and the IPC API of
  KiCad 9 has no schematic. Thus with KiCad 8 or 9 the user saves the files in
  KiCad.
"""

import asyncio
import functools
import inspect
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.kicad_versions import KICAD8, header_version, kicad_release, version_numbers
from .kicad_cli import (
    KicadCliError,
    decode_output,
    find_kicad_cli,
    kicad_cli_version_sync,
    reset_call_version,
    run_kicad_cli_sync,
    set_call_version,
)
from .kicad_lock import check_schematic_not_open
from .kicad_project import PRO_SUFFIX, SCH_SUFFIX, text_variables, top_level_sheets
from .kicad_text import read_as_kicad
from .sch_hierarchy import AmbiguousRootError, files_of_load, schematic_project_file
from .tool_errors import EXPECTED_ERRORS, error_answer

# The conversion of a file can show new files of the hierarchy (the installed
# KiCad reads the sheet fields of the new version). Each pass converts at least
# one file, thus the number of files is the limit; this is a limit for a defect.
_MAX_PASSES = 1000
# The schematic version that each kicad-cli writes, by (the path, the version).
_written_versions: dict[tuple[str, str], int] = {}
# One conversion at a time: two tool calls on one project must not convert one file
# at the same time. After the first call, the second finds the files current.
_conversion_lock = threading.Lock()

NO_KICAD = (
    "kicad-cli was not found, thus the installed KiCad is not known. The server reads "
    "and writes the schematic files in the version of the installed KiCad. Install KiCad "
    "8 or later, or give the path of kicad-cli in the environment variable KICAD_CLI."
)


class SchematicVersionError(WorkbenchError):
    """A schematic file is not in the version of the installed KiCad, and the server
    cannot convert it."""


@dataclass(frozen=True)
class Conversion:
    """One schematic file that the installed KiCad converted and saved."""

    file: Path
    before: int  # the file version before the conversion
    after: int  # the file version that KiCad wrote


@dataclass(frozen=True)
class CurrentSchematics:
    """The result of current_schematics."""

    kicad: str  # the version of the installed KiCad; empty: the path is no project
    conversions: list[Conversion]


def _file_version(path: Path) -> int | None:
    """Return the (version N) of the header of a schematic file, or None (a file that
    the system cannot read, a text that KiCad cannot read, or no header: the parser of
    the tool then gives the error)."""
    try:
        text = read_as_kicad(path)
    except (OSError, WorkbenchError):
        return None
    return header_version(text, "kicad_sch")


def _installed_kicad() -> str:
    """Return the version of the installed KiCad (kicad-cli version)."""
    version = kicad_cli_version_sync()
    if not version:
        raise SchematicVersionError(NO_KICAD)
    return version


def _written_version(kicad: str) -> int | None:
    """Return the schematic version that the installed KiCad writes, or None when its
    kicad-cli cannot save a schematic (KiCad 8 and 9: the version of the release).

    kicad-cli 10 and later: the version of a minimal schematic after `sch upgrade`
    (also for a nightly build, whose version no table has). The result is kept for
    each kicad-cli and version.
    """
    numbers = version_numbers(kicad)
    if numbers is None:
        raise SchematicVersionError(f"The version {kicad!r} of kicad-cli has no major number.")
    if numbers[0] < 8:
        raise SchematicVersionError(
            f"KiCad {kicad} is older than KiCad {KICAD8.name}, the minimum of this server. "
            "Install KiCad 8 or later."
        )
    if numbers[0] < 10:
        if kicad_release(kicad) is None:
            kind = "a nightly build" if numbers[1] == 99 else "a version"
            raise SchematicVersionError(
                f"KiCad {kicad} is {kind} with no known file versions: the server has no rules "
                "for it."
            )
        return None
    key = (find_kicad_cli() or "", kicad)
    if key not in _written_versions:
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / ("probe" + SCH_SUFFIX)
            probe.write_text('(kicad_sch (version 20231120) (generator "eeschema"))\n')
            result = run_kicad_cli_sync(["sch", "upgrade", str(probe)], timeout=60, english=True)
            version = _file_version(probe)
            if result.returncode != 0 or version is None:
                raise KicadCliError(
                    f"kicad-cli {kicad} did not convert a schematic (sch upgrade, exit code "
                    f"{result.returncode}): {decode_output(result.stdout + result.stderr).strip()}"
                )
            _written_versions[key] = version
    return _written_versions[key]


def _release_version(kicad: str) -> int:
    """Return the schematic version of the release of KiCad 8 or 9."""
    release = kicad_release(kicad)
    assert release is not None  # _written_version refuses a nightly build of 8 and 9
    return release.schematic


def _project_of(path: Path) -> Path | None:
    """Return the project file of a schematic, of a project file or of the project
    folder; None when the path is not a schematic project (the tool gives its own
    error)."""
    if path.is_dir():
        projects = sorted(path.glob("*" + PRO_SUFFIX))
        return projects[0] if len(projects) == 1 else None
    if path.suffix == PRO_SUFFIX:
        return path if path.is_file() else None
    if path.suffix == ".kicad_pcb":
        # The board of a project: the DRC loads the schematic of the board name for the
        # schematic parity, check_library_links the project of the board.
        schematic = path.with_suffix(SCH_SUFFIX)
        if schematic.is_file():
            return schematic_project_file(schematic)
        project = path.with_suffix(PRO_SUFFIX)
        return project if project.is_file() else None
    if path.suffix == SCH_SUFFIX and path.is_file():
        return schematic_project_file(path)
    return None


def _project_files(project: Path, kicad_major: int, path: Path) -> list[Path]:
    """Return the schematic files of the project as the installed KiCad loads it, and
    the schematic of the path (a schematic that KiCad opens alone is the root of its
    own project)."""
    variables = text_variables(project.with_suffix(SCH_SUFFIX))
    files = files_of_load(top_level_sheets(project), project.parent, variables, kicad_major)
    if path.suffix == SCH_SUFFIX and not any(same(file, path) for file in files):
        files.append(path)
    return files


def same(a: Path, b: Path) -> bool:
    """Return True if two paths are the same file."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _newer_error(kicad: str, newer: list[tuple[Path, int]], written: int) -> str:
    files = ", ".join(f"{file} (version {version})" for file, version in newer)
    return (
        f"The schematic files {files} have a newer file version than KiCad {kicad} writes "
        f"({written}): KiCad {kicad} cannot open them. Open them in the KiCad that saved "
        "them, or install that KiCad."
    )


def _save_in_kicad_error(kicad: str, old: list[tuple[Path, int]], written: int) -> str:
    files = ", ".join(f"{file} (version {version})" for file, version in old)
    return (
        f"The schematic files {files} have an older file version than KiCad {kicad} "
        f"({written}). The server works with the files in the version of the installed "
        f"KiCad, and kicad-cli {kicad} cannot convert a schematic. Tell the user: open the "
        f"project in KiCad {kicad}, save each of these sheets in the schematic editor "
        "(File > Save), then do the work again."
    )


def _open_in_kicad_error(message: str) -> str:
    return (
        "A schematic file of the project has an older file version than the installed KiCad, "
        "and KiCad has the schematic open, thus the server cannot convert it. Tell the user: "
        "save the schematic in KiCad (KiCad saves it in its version), then do the work again. "
        + message.removeprefix("❌ ")
    )


def current_schematics(path: Path) -> CurrentSchematics:
    """Make each schematic file of the project of a path the version of the installed
    KiCad, before a tool reads or writes it.

    Args:
        path: The schematic, the project file or the project folder that the tool gets.
            Another path (for example a file that is not there) gives no conversion:
            the tool gives its own error.

    Returns:
        The installed KiCad, and the files that it converted and saved.

    Raises:
        SchematicVersionError: No KiCad is installed; a file is newer than the
            installed KiCad; a file is older and the installed KiCad cannot convert
            it (KiCad 8 and 9: the user saves it in KiCad), or KiCad has it open.
        KicadCliError: kicad-cli did not convert a file.
        AmbiguousRootError: More than one project holds the schematic.

        SchematicVersionError: Also an error after a conversion: its message names the
            files that KiCad converted and saved before it.
    """
    path = Path(os.path.abspath(os.path.expanduser(path)))
    project = _project_of(path)
    if project is None:
        return CurrentSchematics("", [])
    kicad = _installed_kicad()
    with _conversion_lock:
        conversions: list[Conversion] = []
        try:
            return _convert(project, path, kicad, conversions)
        except EXPECTED_ERRORS as error:
            if not conversions:
                raise
            done = ", ".join(f"{c.file} ({c.before} to {c.after})" for c in conversions)
            raise SchematicVersionError(
                f"{str(error).rstrip('.')}. Before this error, KiCad {kicad} converted and saved the schematic "
                f"files {done}: tell the user that an older KiCad cannot open them now."
            ) from error


def _convert(
    project: Path, path: Path, kicad: str, conversions: list[Conversion]
) -> CurrentSchematics:
    """The passes of current_schematics; each conversion goes into conversions."""
    written = _written_version(kicad)
    target = written if written is not None else _release_version(kicad)
    major = version_numbers(kicad)[0]
    for _ in range(_MAX_PASSES):
        versions = [(file, _file_version(file)) for file in _project_files(project, major, path)]
        newer = [(file, v) for file, v in versions if v is not None and v > target]
        if newer:
            raise SchematicVersionError(_newer_error(kicad, newer, target))
        old = [(file, v) for file, v in versions if v is not None and v < target]
        if not old:
            return CurrentSchematics(kicad, conversions)
        if written is None:
            raise SchematicVersionError(_save_in_kicad_error(kicad, old, target))
        for file, _version in old:
            message = check_schematic_not_open(file)
            if message:
                raise SchematicVersionError(_open_in_kicad_error(message))
        for file, version in old:
            result = run_kicad_cli_sync(["sch", "upgrade", str(file)], timeout=120, english=True)
            after = _file_version(file)
            if result.returncode != 0 or after != target:
                raise KicadCliError(
                    f"kicad-cli {kicad} did not convert {file} from the file version {version} "
                    f"to {target} (sch upgrade, exit code {result.returncode}): "
                    f"{decode_output(result.stdout + result.stderr).strip()}"
                )
            conversions.append(Conversion(file, version, after))
    raise KicadCliError(f"The conversion of the schematic files of {project} did not stop.")


def conversion_lines(current: CurrentSchematics) -> list[str]:
    """Return the warning of the conversions for the output of a tool."""
    if not current.conversions:
        return []
    files = ", ".join(f"{c.file.name} ({c.before} to {c.after})" for c in current.conversions)
    return [
        f"⚠️ KiCad {current.kicad} converted and saved the schematic files {files} in its file "
        "version (kicad-cli sch upgrade), as a save in KiCad does, before the work. Tell "
        "the user: an older KiCad cannot open them now.",
        "",
    ]


def with_current_schematics(argument: str) -> Callable:
    """Make the schematic files of the project of a tool argument the version of the
    installed KiCad before an async tool runs (current_schematics).

    The output of the tool starts with the warning of the conversions. An error of
    the conversion is the answer of the tool (error_answer: "❌ <message>", and the
    traceback for an error that the server does not expect). The tool then uses the
    version of kicad-cli of the conversion (set_call_version). Put the decorator below
    @mcp.tool().
    """

    def decorator(function: Callable) -> Callable:
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            current = CurrentSchematics("", [])
            try:
                value = signature.bind_partial(*args, **kwargs).arguments.get(argument)
                if isinstance(value, str) and value.strip():
                    current = await asyncio.to_thread(current_schematics, Path(value))
            except AmbiguousRootError as error:
                return f"❌ {error}. Give the root schematic of the project."
            except Exception as error:
                return error_answer(error, "Error in the conversion of the schematic files")
            token = set_call_version(current.kicad) if current.kicad else None
            try:
                result = await function(*args, **kwargs)
            finally:
                if token is not None:
                    reset_call_version(token)
            lines = conversion_lines(current)
            if lines and isinstance(result, str):
                return "\n".join(lines) + "\n" + result
            return result

        return wrapper

    return decorator
