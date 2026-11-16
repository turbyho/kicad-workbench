"""Tools that make project libraries and change the project library tables.

The tools copy the paths of KiCad 10.0.6 (utils/project_library_tables.py): "New
Library" of the symbol editor and of the footprint editor, and the add, the remove and
the flags of a row in the table dialog. They change only the tables of the project; the
global tables stay as they are. KiCad 8 and 9, and the nightly builds, have no rules for
these tools.

KiCad reads the project tables only when it opens the project
(SETTINGS_MANAGER::LoadProject), and the table dialog and "New Library" write the copy
in memory. Thus the tools refuse a project that KiCad has open: KiCad would not see the
change, and its next write of the table would remove it.
"""

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError
from ..parsers.kicad_versions import KICAD10, kicad_release, verified_triple
from ..server import mcp
from ..utils.file_save import SaveFilesError, file_locks, save_file, save_files
from ..utils.file_stamp import file_stamp
from ..utils.file_versions import FileVersionError, check_library_versions, library_file_version
from ..utils.kicad_cli import get_kicad_version, installed_kicad_problem
from ..utils.kicad_lock import check_project_not_open
from ..utils.library_tables import LibraryRow, LibraryTableError, kicad_environment, library_rows
from ..utils.project_library_tables import (
    FOOTPRINT_EXTENSION,
    KICAD_TYPE,
    KINDS,
    NEW_SYMBOL_LIBRARY,
    SYMBOL_EXTENSION,
    ProjectTable,
    TableRow,
    changed,
    default_nickname,
    escape_nickname,
    format_table,
    library_files,
    library_is_kicad,
    nickname_problem,
    normalize_path,
    read_project_table,
    rows_with_nickname,
    table_problems,
    value_problem,
    with_row,
)
from ..utils.tool_errors import error_answer
from .library import project_file

_RELOAD_NOTE = (
    "KiCad reads the project tables when it opens the project: open the project in "
    "KiCad again to use the change."
)


@dataclass
class _Context:
    """The project and the installed KiCad of a tool call."""

    project: Path
    kicad: str


async def _context(project_path: str, kind: str) -> _Context:
    """Check the arguments, the installed KiCad and the lock of the project.

    Raises:
        InputError: The check failed. The message is the answer of the tool.
    """
    if kind not in KINDS:
        raise InputError(f"The kind {kind!r} is not 'symbol' or 'footprint'.")
    found = await asyncio.to_thread(project_file, project_path)
    if found is None:
        raise InputError("Give project_path: the .kicad_pro file or the project folder.")
    project = Path(os.path.abspath(found))
    kicad = await get_kicad_version()
    if problem := installed_kicad_problem(kicad):
        raise InputError(problem)
    if kicad_release(kicad) is not KICAD10:
        raise InputError(
            f"The installed KiCad is {kicad}. The tools for the project library tables have "
            "the rules of KiCad 10.0 only."
        )
    await asyncio.to_thread(_check_not_open, project)
    return _Context(project, kicad)


def _check_not_open(project: Path) -> None:
    """Raises InputError when KiCad has the project open (its lock file)."""
    if error := check_project_not_open(project):
        raise InputError(error.removeprefix("❌ "))


def _path(context: _Context, text: str) -> Path:
    """An absolute path: a relative path is relative to the project folder."""
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = context.project.parent / path
    return Path(os.path.abspath(path))


def _read_table(context: _Context, kind: str) -> ProjectTable:
    """Read the project table for a write.

    Raises:
        InputError: KiCad 10.0 cannot read the table.
    """
    try:
        return read_project_table(context.project, kind)
    except LibraryTableError as e:
        raise InputError(
            f"{e} KiCad skips such a table, and its next write of the table keeps only the "
            "new row. Thus the tool writes no table: repair the table first."
        ) from e
    except OSError as e:
        raise InputError(f"The project library table cannot be read: {e}") from e


def _global_rows(context: _Context, kind: str, nickname: str) -> list[LibraryRow]:
    """The rows of the global table (with its nested tables) with the nickname."""
    rows, _problems = library_rows(kind, context.kicad)
    return [row for row in rows if row.name == nickname]


def _write(
    context: _Context,
    table: ProjectTable,
    rows: list[TableRow],
    library: tuple[Path, bytes] | None = None,
) -> list[str]:
    """Write the table (and a new symbol library file) as one change.

    KiCad 10.0.7 and later first copy the old table to "<table>.bak"
    (LIBRARY_TABLE::Save, an atomic write); an error of the copy does not stop the
    save. KiCad 10.0.6 makes no copy.

    Returns:
        The warnings of save_files and of the copy.

    Raises:
        InputError: The table changed after the tool read it, the library file exists
            now, or the save failed.
    """
    data = format_table(table.kind, rows)
    files = [(table.path, data)] + ([library] if library else [])
    with file_locks([path for path, _data in files]):
        # Again, just before the write: KiCad can have opened the project in the
        # meantime, and it then has the old table in memory.
        _check_not_open(context.project)
        if file_stamp(table.path) != table.stamp:
            raise InputError(
                f"The file {table.path} changed after this tool read it. Thus the tool "
                "wrote no file, to keep that change. Do this operation again."
            )
        if library and library[0].exists():
            raise InputError(f"The library {library[0]} exists now. The tool wrote no file.")
        warnings = []
        backup = table.path.with_name(table.path.name + ".bak")
        if table.stamp is not None and verified_triple(context.kicad) >= (10, 0, 7):
            try:
                save_file(backup, table.data)
            except OSError as e:
                warnings.append(f"The copy of the old table to {backup} failed: {e}")
        try:
            return warnings + save_files(files)
        except SaveFilesError as e:
            raise InputError(str(e)) from e


def _row_text(row: TableRow) -> str:
    flags = " (disabled)" * row.disabled + " (hidden)" * row.hidden
    return (
        f'`(lib (name "{row.text("name")}") (type "{row.text("type")}") (uri '
        f'"{row.text("uri")}") (options "{row.text("options")}") (descr '
        f'"{row.text("descr")}"){flags})`'
    )


def _answer(
    title: str, table: ProjectTable, lines: list[str], warnings: list[str], reload: bool = True
) -> str:
    parts = [title, "", *lines, f"- Table: {table.path}"] + (["", _RELOAD_NOTE] if reload else [])
    for warning in warnings:
        parts += ["", warning if warning.startswith("⚠️") else f"⚠️ {warning}"]
    return "\n".join(parts)


def _existing_problems(rows: list[TableRow]) -> list[str]:
    problems = table_problems(rows)
    return (
        [
            "The project table had these problems before the change (the table dialog of "
            "KiCad gives them at OK): " + " ".join(problems)
        ]
        if problems
        else []
    )


def _uri(context: _Context, path: Path) -> str:
    env = kicad_environment(context.kicad)
    uri = normalize_path(path, env.variables, context.project.parent)
    if problem := value_problem(uri, "URI"):
        raise InputError(problem)
    return uri


def _check_new_nickname(table: ProjectTable, nickname: str) -> None:
    if rows_with_nickname(table.rows, nickname):
        raise InputError(
            f"Multiple libraries cannot share the same nickname ('{nickname}'). The project "
            f"table {table.path} has a row with this nickname."
        )


def _hidden_global(context: _Context, kind: str, nickname: str) -> list[str]:
    if _global_rows(context, kind, nickname):
        return [
            f"The global table has a library with the nickname '{nickname}'. KiCad uses the "
            "row of the project table for it (LIBRARY_MANAGER::Rows): the global library is "
            "not available in this project."
        ]
    return []


def _create(context: _Context, kind: str, name: str, folder: str | None) -> str:
    for problem in (nickname_problem(name), value_problem(name, "name")):
        if problem:
            raise InputError(problem)
    if "/" in name or name in (".", ".."):
        raise InputError(f"The name {name!r} is not a file name.")
    base = _path(context, folder) if folder else context.project.parent
    if not base.is_dir():
        raise InputError(f"The folder {base} does not exist.")
    extension = SYMBOL_EXTENSION if kind == "symbol" else FOOTPRINT_EXTENSION
    path = base / (name + extension)
    if path.exists():
        raise InputError(
            f"The library {path} exists. The tool does not replace a library: add it with "
            "add_library_to_table."
        )
    table = _read_table(context, kind)
    _check_new_nickname(table, name)
    warnings = _existing_problems(table.rows)
    if kind == "symbol":
        # SYMBOL_EDIT_FRAME::AddLibraryFile refuses a library that the adapter loaded
        # (HasLibrary: an enabled row that KiCad loaded). The tool takes the enabled rows
        # whose library exists: it cannot know the load state of a running KiCad.
        loaded = [row for row in _global_rows(context, kind, name) if not row.disabled]
        if any(row.path is not None and row.path.exists() for row in loaded):
            raise InputError(f"Library '{name}' already exists. (The global table has it.)")
    else:
        warnings += _hidden_global(context, kind, name)
    # "New Library" gives the file name as the nickname with no EscapeString
    # (SYMBOL_LIBRARY_MANAGER::addLibrary, PCB_BASE_EDIT_FRAME::AddLibrary); the table
    # dialog escapes it (add_library_to_table).
    row = TableRow(name.encode(), KICAD_TYPE.encode(), _uri(context, path).encode(), b"", b"")
    rows = with_row(table.rows, row)
    if kind == "symbol":
        warnings += _write(context, table, rows, (path, NEW_SYMBOL_LIBRARY.encode()))
    else:
        # FP_CACHE::Save: an empty folder (Mkdir, the parent folder must exist).
        path.mkdir()
        try:
            warnings += _write(context, table, rows)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
    what = "symbol library" if kind == "symbol" else "footprint library (an empty folder)"
    return _answer(
        f"✅ New {what} '{name}' in the project table.",
        table,
        [f"- Library: {path}", f"- Row: {_row_text(row)}"],
        warnings,
    )


def _version_warnings(context: _Context, kind: str, path: Path) -> list[str]:
    """The warnings for a library of an older file version than the installed KiCad
    (file_versions.check_library_versions).

    Raises:
        InputError: A file is newer than the installed KiCad: KiCad cannot read it.
    """
    files = library_files(path, kind)
    versions = [(file, library_file_version(file, kind)) for file in files]
    known = [(file, version) for file, version in versions if version is not None]
    try:
        check = check_library_versions(kind, {path: known}, context.kicad)
    except FileVersionError as e:
        raise InputError(f"{e} The tool added no row.") from e
    if check.problems:
        raise InputError(" ".join(check.problems) + " The tool added no row.")
    return check.warnings


def _add(
    context: _Context, kind: str, library_path: str, nickname: str | None, description: str
) -> str:
    path = _path(context, library_path)
    if not path.exists():
        raise InputError(f"The library {path} does not exist.")
    if not library_is_kicad(path, kind):
        expected = (
            "a .kicad_sym file or a folder with .kicad_sym files"
            if kind == "symbol"
            else "a folder with .kicad_mod files (a .pretty folder)"
        )
        raise InputError(
            f"{path} is not a KiCad {kind} library ({expected}). The tool adds only libraries "
            "of the KiCad format."
        )
    shown = nickname if nickname is not None else default_nickname(path, kind)
    for problem in (nickname_problem(shown), value_problem(description, "description")):
        if problem:
            raise InputError(problem)
    versions = _version_warnings(context, kind, path)
    table = _read_table(context, kind)
    _check_new_nickname(table, shown)
    warnings = _existing_problems(table.rows)
    warnings += _hidden_global(context, kind, escape_nickname(shown))
    warnings += versions
    row = TableRow(
        escape_nickname(shown).encode(),
        KICAD_TYPE.encode(),
        _uri(context, path).encode(),
        b"",
        description.encode(),
    )
    warnings += _write(context, table, with_row(table.rows, row))
    return _answer(
        f"✅ The library '{shown}' is in the project table.",
        table,
        [f"- Library: {path}", f"- Row: {_row_text(row)}"],
        warnings,
    )


def _one_row(table: ProjectTable, nickname: str) -> int:
    found = rows_with_nickname(table.rows, nickname)
    if len(found) == 1:
        return found[0]
    if not found:
        names = ", ".join(f"'{row.text('name')}'" for row in table.rows) or "no rows"
        raise InputError(
            f"The project table {table.path} has no library '{nickname}'. Its rows: {names}."
        )
    raise InputError(
        f"The project table {table.path} has {len(found)} rows with the nickname "
        f"'{nickname}'. Repair the table in KiCad first (the table dialog refuses it)."
    )


def _remove(context: _Context, kind: str, nickname: str) -> str:
    table = _read_table(context, kind)
    index = _one_row(table, nickname)
    row = table.rows[index]
    rows = table.rows[:index] + table.rows[index + 1 :]
    warnings = _write(context, table, rows)
    return _answer(
        f"✅ The library '{nickname}' is not in the project table now.",
        table,
        [
            f"- Removed row: {_row_text(row)}",
            "- The library files stay: KiCad does not delete them, and the tool does not either.",
        ],
        warnings,
    )


def _set_row(
    context: _Context, kind: str, nickname: str, enabled: bool | None, visible: bool | None
) -> str:
    if enabled is None and visible is None:
        raise InputError("Give enabled, visible or the two.")
    if visible is not None and kind == "footprint":
        raise InputError(
            "A footprint table has no visible flag in KiCad 10: the table dialog has no such "
            "column, and it removes (hidden) from each footprint row at OK "
            "(LIB_TABLE_GRID_TRICKS::VerifyTable)."
        )
    table = _read_table(context, kind)
    index = _one_row(table, nickname)
    row = table.rows[index]
    new = changed(
        row,
        disabled=None if enabled is None else not enabled,
        hidden=None if visible is None else not visible,
    )
    if new == row:
        return _answer(
            f"✅ The row of '{nickname}' has these flags already: the tool wrote no file.",
            table,
            [f"- Row: {_row_text(row)}"],
            [],
            reload=False,
        )
    rows = [*table.rows[:index], new, *table.rows[index + 1 :]]
    warnings = _write(context, table, rows)
    return _answer(
        f"✅ The row of '{nickname}' has new flags.",
        table,
        [f"- Before: {_row_text(row)}", f"- After: {_row_text(new)}"],
        warnings,
    )


async def _run(project_path: str, kind: str, work, *args) -> str:
    try:
        context = await _context(project_path, kind)
        return await asyncio.to_thread(work, context, kind, *args)
    except InputError as e:
        return f"❌ {e}"


@mcp.tool()
async def create_project_library(
    project_path: str, kind: str, name: str, folder: str | None = None
) -> str:
    """Make a new, empty library and add it to the project library table, as "New Library"
    of the symbol editor and of the footprint editor of KiCad 10 does.

    A symbol library is a .kicad_sym file with no symbols, in the format of KiCad 10.0.
    A footprint library is an empty .pretty folder. The new row has the nickname name,
    the type KiCad, and a URI with a path variable of KiCad or ${KIPRJMOD} when one
    holds the library. The tool does not replace a library that exists. KiCad must not
    have the project open: KiCad reads the project tables only when it opens the
    project. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        kind: "symbol" or "footprint".
        name: The name of the library: the file name with no extension, and the
            nickname in the table.
        folder: The folder of the new library: an absolute path, or a path relative to
            the project folder. Default: the project folder.

    Returns:
        The library, the new row and the table, or an error.
    """
    try:
        return await _run(project_path, kind, _create, name, folder)
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
async def add_library_to_table(
    project_path: str,
    kind: str,
    library_path: str,
    nickname: str | None = None,
    description: str = "",
) -> str:
    """Add a KiCad library that exists to the project library table, as the table dialog
    of KiCad 10 does.

    The library must be of the KiCad format: a .kicad_sym file or a folder with
    .kicad_sym files (symbols), a folder with .kicad_mod files (footprints). The tool
    refuses other formats. A nickname that the project table has is an error. A
    nickname of the global table gives a warning: the project row hides the global
    library. A library of an older file version gives a warning with the step for its
    upgrade; a library of a newer file version is an error. KiCad must not have the
    project open. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        kind: "symbol" or "footprint".
        library_path: The library file or folder: an absolute path, or a path relative
            to the project folder.
        nickname: The nickname in the table. Default: the name of the library with no
            extension, as KiCad gives it.
        description: The description of the row.

    Returns:
        The new row and the table, or an error.
    """
    try:
        return await _run(project_path, kind, _add, library_path, nickname, description)
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
async def remove_library_from_table(project_path: str, kind: str, nickname: str) -> str:
    """Remove a library from the project library table, as the table dialog of KiCad 10
    does. The library files stay: KiCad does not delete them.

    KiCad must not have the project open. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        kind: "symbol" or "footprint".
        nickname: The nickname of the row.

    Returns:
        The removed row and the table, or an error.
    """
    try:
        return await _run(project_path, kind, _remove, nickname)
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
async def set_library_table_row(
    project_path: str,
    kind: str,
    nickname: str,
    enabled: bool | None = None,
    visible: bool | None = None,
) -> str:
    """Enable, disable, show or hide a library of the project library table, as the table
    dialog of KiCad 10 does.

    KiCad does not load a disabled library. A hidden symbol library is loaded, but the
    symbol chooser and the library tree do not show it. Only a symbol table has the
    visible flag. KiCad must not have the project open. The installed KiCad must be
    KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        kind: "symbol" or "footprint".
        nickname: The nickname of the row.
        enabled: True enables the library, False disables it. None: no change.
        visible: True shows the library, False hides it (symbol tables only). None: no
            change.

    Returns:
        The row before and after, and the table, or an error.
    """
    try:
        return await _run(project_path, kind, _set_row, nickname, enabled, visible)
    except Exception as e:
        return error_answer(e, "Error")
