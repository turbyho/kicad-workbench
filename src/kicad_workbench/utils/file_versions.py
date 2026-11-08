"""The file versions of the schematic files and the board of a project, and the
installed KiCad.

Each KiCad opens a file of an older version, and its next save writes the file in its
own version (``SCH_EDIT_FRAME::SaveProject`` writes each sheet,
``PCB_EDIT_FRAME::SavePcbFile`` the board). A file of a newer version than the
installed KiCad is an error: KiCad cannot open it
(``SCH_IO_KICAD_SEXPR_PARSER::ParseSchematic`` and the board parser:
``m_requiredVersion`` newer than the file version of the build).

The server does not convert the files: the user converts them in KiCad (user
decision, 2026-11-08, plans/roadmap.md "Decisions", "File versions"; the same for the
boards, user: "princip je uplne stejny jako pro schemata"). Before each tool,
``with_schematic_versions`` compares the version of each schematic file of the
project, and ``with_board_versions`` the version of the board, with the version that
the installed KiCad writes:

- A newer file is an error, with the step for the user: a newer KiCad.
- An older file: a tool that reads gives a warning with the step for the user (the
  conversion in KiCad), and does its work. A tool that writes gives an error with
  this step: KiCad saves an edit of an older file in its own version, thus a write
  of the tool in the older format is not what the installed KiCad does.
"""

import asyncio
import functools
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.kicad_versions import (
    KICAD8,
    RELEASES,
    Release,
    header_version,
    kicad_release,
    version_numbers,
)
from .kicad_cli import (
    kicad_cli_version_sync,
    no_kicad_message,
    reset_call_version,
    set_call_version,
)
from .kicad_project import (
    PRO_SUFFIX,
    SCH_SUFFIX,
    TopLevelSheet,
    text_variables,
    top_level_sheets,
)
from .sch_hierarchy import AmbiguousRootError, file_versions_of_load, schematic_project_file
from .tool_errors import error_answer

# The bytes at the start of a file that hold its header: "(kicad_pcb (version N)".
_HEADER_BYTES = 4096


class FileVersionError(WorkbenchError):
    """A schematic file or a board has a version that the tool cannot use with the
    installed KiCad: a newer file, or an older file for a tool that writes."""


@dataclass(frozen=True)
class Written:
    """The schematic version that the installed KiCad writes."""

    version: int
    # True: the version of a release. False: a nightly build after the newest release
    # (release, version): it writes a newer version, but not a newer one than
    # NEWEST_SCHEMATIC (the rules of branch master); its exact version is not known.
    exact: bool
    release: Release


@dataclass(frozen=True)
class FileVersions:
    """The result of check_schematic_versions and check_board_file_version."""

    kicad: str  # the version of the installed KiCad; empty: the path is no project
    warning: str  # the warnings for the output of a tool (older files), or empty


@dataclass(frozen=True)
class _ProjectFiles:
    """The schematic files of a project as the GUI of the installed KiCad loads them."""

    root: Path  # the schematic that KiCad opens for the project
    files: list[tuple[Path, int | None]]  # each file of the hierarchy, with its version
    alone: Path | None  # the schematic of the path when it is not one of the files


def _file_version(path: Path, head: str = "kicad_sch") -> int | None:
    """Return the (version N) of the header of a KiCad file, or None (a file that the
    system cannot read, or no header: the reader of the tool then gives the error).

    The header is at the start of the file (sexpr.file_header): the function reads
    only the first _HEADER_BYTES, not a board of many MB.
    """
    try:
        with open(path, "rb") as file:
            start = file.read(_HEADER_BYTES)
    except OSError:
        return None
    return header_version(start.decode("utf-8", errors="replace"), head)


def _installed_kicad() -> str:
    """Return the version of the installed KiCad (kicad-cli version)."""
    version = kicad_cli_version_sync()
    if not version:
        raise FileVersionError(no_kicad_message())
    return version


def _written_version(kicad: str, kind: str = "schematic") -> Written:
    """Return the version of a file kind ("schematic" or "board") that the installed
    KiCad writes.

    Each release of a major version writes one schematic version
    (SEXPR_SCHEMATIC_FILE_VERSION of eeschema/sch_file_versions.h: 20231120 in 8.0.0 to
    8.0.9, 20250114 in 9.0.0 to 9.0.9, 20260306 in 10.0.0 to 10.0.7) and one board
    version (SEXPR_BOARD_FILE_VERSION of pcb_io_kicad_sexpr.h: 20240108 in 8.0.0 to
    8.0.9, 20241229 in 9.0.0 to 9.0.9, 20260206 in 10.0.0 to 10.0.7). The server has
    rules of a nightly build only for the nightly builds after the newest release
    (10.99, branch master): such a build writes a newer version than that release.
    """
    numbers = version_numbers(kicad)
    if numbers is None:
        raise FileVersionError(f"The version {kicad!r} of kicad-cli has no major number.")
    if numbers[0] < 8:
        raise FileVersionError(
            f"KiCad {kicad} is older than KiCad {KICAD8.name}, the minimum of this server. "
            "Install KiCad 8 or later."
        )
    release = kicad_release(kicad)
    if release is not None:
        return Written(getattr(release, kind), True, release)
    newest = RELEASES[-1]
    if numbers[1] == 99 and newest.name == f"{numbers[0]}.0":
        return Written(getattr(newest, kind), False, newest)
    build = "a nightly build" if numbers[1] == 99 else "a version"
    raise FileVersionError(
        f"KiCad {kicad} is {build} with no known file versions: the server has no rules for it."
    )


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


def _project_files(project: Path, kicad_major: int, path: Path) -> _ProjectFiles:
    """Return the schematic files of the project as the GUI of the installed KiCad loads
    it, and the schematic of the path when it is not one of them (KiCad opens such a
    schematic alone: it is the root of its own hierarchy).

    The user works with the project in the GUI. The GUI of KiCad 10 loads each top-level
    sheet whose file exists (SCH_EDIT_FRAME::OpenProjectFiles, annotation._tops). KiCad 8
    and 9 have no top-level sheets in the project file: they load the schematic of the
    project name.
    """
    root = Path(os.path.abspath(project.with_suffix(SCH_SUFFIX)))
    declared = top_level_sheets(project) if kicad_major >= 10 else []
    existing = [top for top in declared if top.file.is_file()]
    tops = existing or [TopLevelSheet(root, None, root.stem)]
    variables = text_variables(project.with_suffix(SCH_SUFFIX))
    files = file_versions_of_load(tops, project.parent, variables, kicad_major)
    alone = None
    if path.suffix == SCH_SUFFIX and not any(same(file, path) for file, _version in files):
        alone = path
    return _ProjectFiles(tops[0].file, files, alone)


def same(a: Path, b: Path) -> bool:
    """Return True if two paths are the same file."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _files_text(files: list[tuple[Path, int]]) -> str:
    return ", ".join(f"{file} (version {version})" for file, version in files)


def _written_text(kicad: str, written: Written) -> str:
    if written.exact:
        return f"KiCad {kicad} writes ({written.version})"
    return (
        f"KiCad {kicad} writes (a nightly build: a newer version than {written.version} "
        f"of KiCad {written.release.name})"
    )


def _newer_error(
    kicad: str,
    newer: list[tuple[Path, int]],
    written: Written,
    project: Path,
    loaded: _ProjectFiles,
) -> str:
    newest = max(version for _file, version in newer)
    release = next((r for r in RELEASES if r.schematic >= newest), None)
    places = []
    if any(file != loaded.alone for file, _version in newer):
        places.append(f"the project {project}" if project.is_file() else str(loaded.root))
    if any(file == loaded.alone for file, _version in newer):
        places.append(str(loaded.alone))
    step = f"open {' and '.join(places)} in the KiCad that saved the files"
    if release is not None:
        step += f", or install KiCad {release.name} or later"
    return (
        f"The schematic files {_files_text(newer)} have a newer file version than "
        f"{_written_text(kicad, written)}: KiCad {kicad} cannot open them. Tell the user: "
        f"{step}. Then do the work again."
    )


def _open_alone(kicad: str, schematic: Path, reason: str) -> str:
    return (
        f"start the schematic editor of KiCad {kicad} alone (eeschema) and open {schematic} "
        f"(File > Open): {reason}"
    )


def _conversion_step(
    kicad: str, old: list[tuple[Path, int]], project: Path, loaded: _ProjectFiles
) -> str:
    """The step for the user that converts the older files in KiCad.

    SCH_EDIT_FRAME::SaveProject of KiCad 8.0.9 saves no file when the schematic has no
    change. KiCad 9.0.9 also saves a schematic with no change when its root sheet has an
    older version. KiCad 10.0.6 puts the virtual root first (version 0), thus it always
    saves. When it saves, it saves each sheet of the hierarchy. A schematic with no
    project file is the root of a project with no settings
    (sch_hierarchy.schematic_project_file): the project manager cannot open it, and the
    schematic editor has File > Open only when it runs alone (menubar.cpp). The
    schematic of the path that the project does not load is open only alone.
    """
    steps = []
    if any(file != loaded.alone for file, _version in old):
        if project.is_file():
            steps.append(
                f"open the project {project} in KiCad {kicad} and open its schematic editor"
            )
        else:
            steps.append(_open_alone(kicad, loaded.root, "it has no project file"))
    if any(file == loaded.alone for file, _version in old):
        reason = f"it is not a sheet of the project that KiCad {kicad} loads"
        steps.append(_open_alone(kicad, loaded.alone, reason))
    where = "Then, in the schematic editor"
    if len(steps) > 1:
        where = "In each of the two schematic editors"
    text = (
        ", and ".join(steps)
        + f". {where}, make one change (for example, move an item and move it back) and "
        "save the schematic (File > Save). The save writes each sheet of the hierarchy in "
        f"the version of KiCad {kicad}, and an older KiCad cannot open the files then."
    )
    if version_numbers(kicad)[0] < 10:
        text += (
            " Without a change, KiCad 8 saves no file, and KiCad 9 saves the files only "
            "when the root sheet has an older version."
        )
    return text


def _split(
    versions: list[tuple[Path, int]], written: Written
) -> tuple[list[tuple[Path, int]], list[tuple[Path, int]], list[tuple[Path, int]]]:
    """Return the newer, the older and the files of an unknown order of a list of
    (file, version). A nightly build writes a newer version than its release
    (written.version): a file of that version or older is older; a newer file can be
    older or newer (the reader of the tool refuses a file that the server does not
    know)."""
    if written.exact:
        newer = [(file, v) for file, v in versions if v > written.version]
        old = [(file, v) for file, v in versions if v < written.version]
        return newer, old, []
    old = [(file, v) for file, v in versions if v <= written.version]
    unknown = [(file, v) for file, v in versions if v > written.version]
    return [], old, unknown


def _unknown_warning(kicad: str, what: str, files: list[tuple[Path, int]], written: Written) -> str:
    return (
        f"⚠️ KiCad {kicad} is a nightly build: the server does not know the file version "
        f"that it writes. Thus it cannot tell if the file version of {what} "
        f"{_files_text(files)} is older or newer. Each of these versions is newer than "
        f"KiCad {written.release.name} writes ({written.version})."
    )


def check_schematic_versions(path: Path, write: bool) -> FileVersions:
    """Compare the version of each schematic file of the project of a path with the
    version that the installed KiCad writes.

    Args:
        path: The schematic, the project file or the project folder that the tool gets.
            Another path (for example a file that is not there) gives no check: the
            tool gives its own error.
        write: True for a tool that writes a schematic file of the project.

    Returns:
        The installed KiCad, and the warning about older files (a tool that reads).

    Raises:
        FileVersionError: No KiCad is installed or its version has no rules; a file
            is newer than the installed KiCad; a file is older and the tool writes.
        AmbiguousRootError: More than one project holds the schematic.
    """
    path = Path(os.path.abspath(os.path.expanduser(path)))
    project = _project_of(path)
    if project is None:
        return FileVersions("", "")
    kicad = _installed_kicad()
    written = _written_version(kicad)
    loaded = _project_files(project, version_numbers(kicad)[0], path)
    alone = [(loaded.alone, _file_version(loaded.alone))] if loaded.alone else []
    versions = [(file, v) for file, v in [*loaded.files, *alone] if v is not None]
    newer, old, unknown = _split(versions, written)
    if newer:
        raise FileVersionError(_newer_error(kicad, newer, written, project, loaded))
    lines = []
    if unknown:
        lines.append(_unknown_warning(kicad, "the schematic files", unknown, written))
    if old:
        found = (
            f"The schematic files {_files_text(old)} have an older file version than "
            f"{_written_text(kicad, written)}."
        )
        step = _conversion_step(kicad, old, project, loaded)
        if write:
            raise FileVersionError(
                f"{found} KiCad {kicad} saves an edit of these files in its own version, "
                "thus the tool does not write them in the older version: the user converts "
                f"them in KiCad first. Tell the user: {step} Then do the work again."
            )
        lines.append(
            f"⚠️ {found} The tool reads the files as they are, and changes no file. A tool "
            "that writes refuses them until the user converts them in KiCad. To convert "
            f"them, tell the user: {step}"
        )
    return FileVersions(kicad, "\n\n".join(lines))


def with_schematic_versions(argument: str, write: bool = False) -> Callable:
    """Compare the schematic files of the project of a tool argument with the installed
    KiCad before an async tool runs (check_schematic_versions).

    The output of the tool starts with the warnings of the check. An error of the
    check is the answer of the tool (error_answer: "❌ <message>", and the traceback for
    an error that the server does not expect). The tool then uses the version of
    kicad-cli of the check (set_call_version). Put the decorator below @mcp.tool().

    Args:
        argument: The name of the path argument of the tool.
        write: True for a tool that writes a schematic file of the project.
    """
    return _with_check("check_schematic_versions", argument, write, "the schematic file versions")


def _with_check(check: str, argument: str, write: bool, what: str) -> Callable:
    """The decorator of a check of this module. The wrapper gets the check from the
    module at each call, by its name: a test can replace it. A name that the module
    does not have is an error at the import of the tool module."""
    if not callable(globals().get(check)):
        raise NameError(f"file_versions has no check {check!r}")

    def decorator(function: Callable) -> Callable:
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            checked = FileVersions("", "")
            try:
                value = signature.bind_partial(*args, **kwargs).arguments.get(argument)
                if isinstance(value, str) and value.strip():
                    checked = await asyncio.to_thread(globals()[check], Path(value), write)
            except AmbiguousRootError as error:
                return f"❌ {error}. Give the root schematic of the project."
            except Exception as error:
                return error_answer(error, f"Error in the check of {what}")
            token = set_call_version(checked.kicad) if checked.kicad else None
            try:
                result = await function(*args, **kwargs)
            finally:
                if token is not None:
                    reset_call_version(token)
            if checked.warning and isinstance(result, str):
                return f"{checked.warning}\n\n{result}"
            return result

        return wrapper

    return decorator


def _board_of(path: Path) -> Path | None:
    """Return the board of a tool argument: the board itself, or the board of the
    project of a project file, a project folder or a schematic (_project_of: also of a
    sub-sheet); None when it is not there (the tool gives its own error, or it needs no
    board)."""
    if path.suffix == ".kicad_pcb":
        return path if path.is_file() else None
    project = _project_of(path)
    board = project.with_suffix(".kicad_pcb") if project is not None else None
    return board if board is not None and board.is_file() else None


def check_board_file_version(path: Path, write: bool) -> FileVersions:
    """Compare the version of the board of a path with the version that the installed
    KiCad writes.

    PCB_EDIT_FRAME::SaveBoard (KiCad 10) and Files_io_from_id with ID_SAVE_BOARD (KiCad 8
    and 9) save the board also when it has no change: the action Save is always on
    (SELECTION_CONDITIONS::ShowAlways), thus the step for the user is File > Save.

    Args:
        path: The board, or the project file, the project folder or a schematic of the
            project. Another path, or a project with no board, gives no check.
        write: True for a tool that writes the board.

    Returns:
        The installed KiCad, and the warning about an older board (a tool that reads).

    Raises:
        FileVersionError: No KiCad is installed or its version has no rules; the board
            is newer than the installed KiCad; the board is older and the tool writes.
        AmbiguousRootError: More than one project holds the schematic.
    """
    board = _board_of(Path(os.path.abspath(os.path.expanduser(path))))
    if board is None:
        return FileVersions("", "")
    kicad = _installed_kicad()
    written = _written_version(kicad, "board")
    version = _file_version(board, "kicad_pcb")
    if version is None:
        return FileVersions(kicad, "")  # the reader of the tool gives the error
    newer, old, unknown = _split([(board, version)], written)
    if newer:
        release = next((r for r in RELEASES if r.board >= version), None)
        step = "open the board in the KiCad that saved it"
        if release is not None:
            step += f", or install KiCad {release.name} or later"
        raise FileVersionError(
            f"The board {_files_text(newer)} has a newer file version than "
            f"{_written_text(kicad, written)}: KiCad {kicad} cannot open it. Tell the user: "
            f"{step}. Then do the work again."
        )
    if unknown:
        return FileVersions(kicad, _unknown_warning(kicad, "the board", unknown, written))
    if not old:
        return FileVersions(kicad, "")
    text = (
        f"The board {_files_text(old)} has an older file version than "
        f"{_written_text(kicad, written)}."
    )
    step = (
        f"open the board {board} in the PCB editor of KiCad {kicad} and save it (File > "
        f"Save). The save writes the board in the version of KiCad {kicad}, and an older "
        "KiCad cannot open the board then."
    )
    if write:
        raise FileVersionError(
            f"{text} KiCad {kicad} saves an edit of the board in its own version, thus the "
            "tool does not write it in the older version: the user converts it in KiCad "
            f"first. Tell the user: {step} Then do the work again."
        )
    return FileVersions(
        kicad,
        f"⚠️ {text} The tool reads the board as it is, and changes no file. A tool that "
        f"writes refuses it until the user converts it in KiCad. To convert it, tell the "
        f"user: {step}",
    )


def with_board_versions(argument: str, write: bool = False) -> Callable:
    """Compare the board of a tool argument with the installed KiCad before an async
    tool runs (check_board_file_version), as with_schematic_versions does for the schematic
    files. Put the decorator below @mcp.tool().

    Args:
        argument: The name of the path argument of the tool.
        write: True for a tool that writes the board.
    """
    return _with_check("check_board_file_version", argument, write, "the board file version")
