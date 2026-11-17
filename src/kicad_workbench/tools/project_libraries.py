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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.kicad_fields import WX_SPACE
from ..parsers.kicad_versions import KICAD10, kicad_release, verified_triple
from ..parsers.lib_symbols import read_library, symbol_chain
from ..server import mcp
from ..utils.file_save import SaveFilesError, file_locks, save_file, save_files
from ..utils.file_stamp import FileStamp, file_stamp, read_stamped
from ..utils.file_versions import FileVersionError, check_library_versions, library_file_version
from ..utils.kicad_cli import get_kicad_version, installed_kicad_problem
from ..utils.kicad_lock import check_project_not_open
from ..utils.kicad_text import decode_as_kicad
from ..utils.library_copy import (
    dialog_symbol_name,
    footprint_copy,
    footprint_name_problem,
    kicad_save_symbols,
    library_text,
    symbol_conflicts,
    symbol_copy,
    symbols_in_version,
)
from ..utils.library_lookup import find_kicad_library, find_library_symbol
from ..utils.library_symbol_fields import change_fields, symbol_editor_text_size
from ..utils.library_tables import (
    LibraryRow,
    LibraryTableError,
    expand,
    kicad_environment,
    library_rows,
    project_variables,
)
from ..utils.library_upgrade import (
    convert,
    footprint_files,
    git_note,
    symbol_names,
)
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
from .library import cell, project_file

_RELOAD_NOTE = (
    "KiCad reads the project tables when it opens the project: open the project in "
    "KiCad again to use the change."
)


@dataclass
class _Context:
    """The project and the installed KiCad of a tool call."""

    project: Path
    kicad: str


async def _context(project_path: str, kind: str | None) -> _Context:
    """Check the arguments, the installed KiCad and the lock of the project.

    Raises:
        InputError: The check failed. The message is the answer of the tool.
    """
    if kind is not None and kind not in KINDS:
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


def _save_guarded(
    context: _Context,
    files: list[tuple[Path, bytes]],
    stamps: dict[Path, FileStamp | None],
    prepare=None,
) -> list[str]:
    """Write files as one change, with the lock of each file. Just before the write, the
    lock of the project and the stamps are checked again: KiCad can have opened the
    project, or an other program can have changed a file (a stamp None: no file).

    Args:
        prepare: An optional function that runs under the locks before the save, and
            gives warnings.

    Raises:
        InputError: The project is open, a file changed, or the save failed.
    """
    with file_locks([path for path, _data in files]):
        _check_not_open(context.project)
        for path, stamp in stamps.items():
            if file_stamp(path) != stamp:
                raise InputError(
                    f"The file {path} changed after this tool read it. Thus the tool wrote no "
                    "file, to keep that change. Do this operation again."
                )
        warnings = prepare() if prepare is not None else []
        try:
            return warnings + save_files(files)
        except SaveFilesError as e:
            raise InputError(str(e)) from e


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
    # A new library file must not exist (its stamp None).
    stamps = {table.path: table.stamp} | ({library[0]: None} if library else {})

    def backup() -> list[str]:
        path = table.path.with_name(table.path.name + ".bak")
        if table.stamp is None or verified_triple(context.kicad) < (10, 0, 7):
            return []
        try:
            save_file(path, table.data)
        except OSError as e:
            return [f"The copy of the old table to {path} failed: {e}"]
        return []

    return _save_guarded(context, files, stamps, backup)


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


# The upgrade of the project libraries (phase 4, step 3).


@dataclass
class _Upgrade:
    """The result of one library of upgrade_project_libraries."""

    nickname: str
    kind: str
    path: str
    result: str
    versions: str = ""
    items: str = ""
    notes: list[str] = field(default_factory=list)


def _versions_text(versions: list[int | None]) -> str:
    texts = {"none" if version is None else str(version) for version in versions}
    return ", ".join(sorted(texts))


def _upgrade_one(
    context: _Context, kind: str, nickname: str, path: Path, named: bool, include_outside: bool
) -> _Upgrade:
    """Check, convert and write one library. An error of this library goes into its
    result: the other libraries of the call continue."""
    item = _Upgrade(nickname, kind, str(path), "")
    try:
        _upgrade_library(context, item, path, named, include_outside)
    except (InputError, OSError, WorkbenchError) as e:
        item.result = f"❌ {e}"
    return item


def _upgrade_library(
    context: _Context, item: _Upgrade, path: Path, named: bool, include_outside: bool
) -> None:
    kind = item.kind
    written = KICAD10.symbol_library if kind == "symbol" else KICAD10.board
    if not path.exists():
        item.result = "❌ the library does not exist"
        return
    if kind == "symbol" and path.is_dir():
        item.result = (
            "❌ a folder of symbol files: the tool has no verified rules for its upgrade "
            "(SCH_IO_KICAD_SEXPR_LIB_CACHE saves a folder in an other way). Convert it in "
            "KiCad"
        )
        return
    if kind == "footprint" and not path.is_dir():
        item.result = "❌ a footprint library must be a folder"
        return
    if not path.is_relative_to(context.project.parent) and not (include_outside or named):
        item.result = (
            "skipped: outside the project folder (other projects can use it). Give its "
            "nickname in libraries, or include_outside=True"
        )
        return
    files = footprint_files(path) if kind == "footprint" else [path]
    if not files:
        item.result = "skipped: the library has no files"
        return
    stamps: dict[Path, FileStamp] = {}
    versions: list[int | None] = []
    data = b""
    for file in files:
        data, stamps[file] = read_stamped(file)
        # A file with no version (for example a footprint of KiCad 5) is older: KiCad
        # reads it with GetFileFormatVersionAtLoad 0 (kicad-cli gives the error when it
        # cannot read the file).
        versions.append(library_file_version(file, kind))
    item.versions = _versions_text(versions)
    known = [version for version in versions if version is not None]
    if known and max(known) > written:
        item.result = (
            f"❌ files of a newer version than KiCad {context.kicad} writes ({written}): "
            "KiCad cannot read them"
        )
        return
    names = {file.name for file in files} if kind == "footprint" else set(symbol_names(data, path))
    item.items = str(len(names))
    if None not in versions and min(known) == written:
        item.result = "✅ in the version of the installed KiCad: no change"
        return
    # The state of git before the write: after it, the files have the changes of the tool.
    note = git_note(path)
    with tempfile.TemporaryDirectory(prefix="kicad-workbench-upgrade-") as work:
        new_files = convert(kind, path, Path(work), names)
    # The files of the first read: a file that came in the meantime has no stamp (None),
    # thus it gives the error of a changed file.
    all_stamps = {file: stamps.get(file) for file in new_files} | stamps
    warnings = _save_guarded(context, list(new_files.items()), all_stamps)
    item.result = f"✅ converted to {written}, and KiCad saved the result again with no change"
    if note:
        item.notes.append(f"{item.nickname} ({kind}): {note}.")
    item.notes += warnings


def _upgrade(context: _Context, _kind, libraries: list[str] | None, include_outside: bool) -> str:
    env = kicad_environment(context.kicad)
    variables = project_variables(context.project)
    wanted = set(libraries or [])
    found: set[str] = set()
    results: list[_Upgrade] = []
    for kind in KINDS:
        table = _read_table(context, kind)
        for row in table.rows:
            nickname = row.text("name")
            names = {nickname} | {n for n in wanted if rows_with_nickname([row], n)}
            if wanted and not (names & wanted):
                continue
            found |= names & wanted
            if row.text("type") != KICAD_TYPE:
                results.append(
                    _Upgrade(nickname, kind, row.text("uri"), "skipped: not the KiCad format")
                )
                continue
            expanded = expand(row.text("uri"), env, variables)
            if not os.path.isabs(expanded):
                results.append(
                    _Upgrade(nickname, kind, expanded, "❌ the URI is not an absolute path")
                )
                continue
            # LIBRARY_MANAGER::ExpandURI makes the path absolute (wxFileName::MakeAbsolute
            # removes "." and "..").
            path = Path(os.path.abspath(expanded))
            results.append(
                _upgrade_one(context, kind, nickname, path, bool(names & wanted), include_outside)
            )
    lines = [
        "# The upgrade of the project libraries",
        "",
        f"KiCad {context.kicad} (kicad-cli) converts each library of an older file version "
        "of the project tables. The check: KiCad saves the result again with no change, "
        "and the names of the symbols or footprints stay.",
        "",
        "| Library | Kind | Versions before | Items | Result |",
        "|---|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            f"| {cell(item.nickname)} | {item.kind} | {item.versions} | {item.items} | "
            f"{cell(item.result)} |"
        )
    if not results:
        lines.append("| (no library) | | | | |")
    missing = sorted(wanted - found)
    if missing:
        lines += ["", f"❌ The project tables have no library {', '.join(missing)}."]
    notes = [note for item in results for note in item.notes]
    if notes:
        lines += ["", "⚠️ No way back for these libraries:", *[f"- {note}" for note in notes]]
    if any(item.result.startswith("✅ converted") for item in results):
        lines += [
            "",
            "An older KiCad cannot read a converted library. Open the project in KiCad "
            "again to use the converted libraries.",
        ]
    return "\n".join(lines)


@mcp.tool()
async def upgrade_project_libraries(
    project_path: str, libraries: list[str] | None = None, include_outside: bool = False
) -> str:
    """Convert the libraries of the project tables to the file version of the installed
    KiCad 10, with kicad-cli (`kicad-cli sym upgrade`, `kicad-cli fp upgrade`).

    KiCad converts each library of an older version into a temporary folder. Then
    KiCad saves the result again: the bytes must not change, and the names of the
    symbols or the footprints must stay. Only then the tool writes the files of that
    library, all or none. The tool makes no backup: git gives the way back, and the
    output names a library that git cannot give back. A converted library cannot be
    read by an older KiCad. A library outside the project folder (other projects can
    use it) is converted only when libraries names it or include_outside is True. A
    library of a newer version, and a folder of symbol files, give an error. KiCad must
    not have the project open. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        libraries: The nicknames of the libraries to convert. Default: all libraries
            of the project tables.
        include_outside: True also converts the libraries outside the project folder.

    Returns:
        A table with the result of each library, or an error.
    """
    try:
        return await _run(project_path, None, _upgrade, libraries, include_outside)
    except Exception as e:
        return error_answer(e, "Error")


# The copy of a symbol or a footprint into a project library (phase 4, step 4).


def _project_library(context: _Context, kind: str, nickname: str) -> tuple[Path, list[str]]:
    """The library of a row of the project table, in the version of the installed KiCad.

    Returns:
        (the library, warnings).

    Raises:
        InputError: No such row, not the KiCad format, the wrong form (a folder of
            symbol files, a footprint file), or an other file version.
    """
    table = _read_table(context, kind)
    row = table.rows[_one_row(table, nickname)]
    warnings = []
    if row.disabled:
        # The Save As dialogs list the disabled rows too (LIBRARY_MANAGER::Rows).
        warnings.append(
            f"The row of '{nickname}' is disabled: KiCad does not load the library, thus "
            "KiCad does not show the copy until the row is enabled."
        )
    if row.text("type") != KICAD_TYPE:
        raise InputError(f"The library '{nickname}' is not of the KiCad format.")
    env = kicad_environment(context.kicad)
    expanded = expand(row.text("uri"), env, project_variables(context.project))
    if not os.path.isabs(expanded):
        raise InputError(f"The URI of the library '{nickname}' is not an absolute path.")
    path = Path(os.path.abspath(expanded))
    if kind == "symbol" and not path.is_file():
        raise InputError(
            f"The library '{nickname}' ({path}) is not a .kicad_sym file. The tool writes "
            "only into a symbol library file."
        )
    if kind == "footprint" and not path.is_dir():
        raise InputError(f"The library '{nickname}' ({path}) is not a folder.")
    files = [path] if kind == "symbol" else footprint_files(path)
    versions = [(file, library_file_version(file, kind)) for file in files]
    unknown = [file.name for file, version in versions if version is None]
    if unknown:
        raise InputError(
            f"The library '{nickname}' has files with no file version that the server can "
            f"read: {', '.join(unknown[:5])}."
        )
    check = check_library_versions(kind, {path: versions}, context.kicad)
    if check.problems:
        raise InputError(" ".join(check.problems))
    if check.warnings:
        raise InputError(
            f"The library '{nickname}' has files of an older version than the installed "
            "KiCad writes. A tool that writes into a library refuses it: convert it first "
            "with upgrade_project_libraries."
        )
    return path, warnings


def _read_symbol_library(path: Path) -> tuple[bytes, FileStamp, dict]:
    """The bytes, the stamp and the symbols of a library file, from one state of the
    file (the stamp is checked after the parse).

    Raises:
        InputError: The file changed during the read.
    """
    data, stamp = read_stamped(path)
    symbols = read_library(path)
    if file_stamp(path) != stamp:
        raise InputError(f"The file {path} changed while the tool read it. Do it again.")
    return data, stamp, symbols


def _split_id(lib_id: str, kind: str) -> tuple[str, str]:
    library, _sep, name = lib_id.partition(":")
    if not library or not name:
        example = "Device:R" if kind == "symbol" else "Resistor_SMD:R_0603_1608Metric"
        raise InputError(
            f'The library ID {lib_id!r} is not "<library>:<name>", for example "{example}".'
        )
    return library, name


def _copy_symbol(
    context: _Context,
    _kind,
    lib_id: str,
    target_library: str,
    new_name: str | None,
    flatten_symbol: bool,
    overwrite: bool,
) -> str:
    library, name = _split_id(lib_id, "symbol")
    chain, row, _problems = find_library_symbol(library, name, context.kicad, context.project)
    written = KICAD10.symbol_library
    if any(symbol.version is not None and symbol.version > written for symbol in chain):
        raise InputError(
            f"The library '{library}' has a newer file version than KiCad {context.kicad} "
            "writes: KiCad cannot read it."
        )
    target, warnings = _project_library(context, "symbol", target_library)
    source_file = chain[0].file
    leaf_name = dialog_symbol_name(new_name) if new_name is not None else chain[0].name
    if not leaf_name:
        raise InputError("Symbol must have a name.")
    same = library == target_library
    target_data, stamp, target_symbols = _read_symbol_library(target)
    with tempfile.TemporaryDirectory(prefix="kicad-workbench-copy-") as work:
        if any(symbol.version != written for symbol in chain):
            if row.path is None or not row.path.is_file():
                raise InputError(
                    f"The library '{library}' is an older folder of symbol files: the tool "
                    "has no verified rules for its conversion."
                )
            chain = symbol_chain(symbols_in_version(row.path, Path(work), written), name)
        copy = symbol_copy(chain, leaf_name, same, flatten_symbol)
        conflicts = symbol_conflicts(copy, target_symbols, chain, same)
        if conflicts and not overwrite:
            if len(copy.symbols) == 1:
                text = f"Symbol '{leaf_name}' already exists in library '{target_library}'."
            else:
                text = (
                    f"The following symbols in the inheritance chain of '{leaf_name}' already "
                    f"exist in library '{target_library}': {', '.join(conflicts)}."
                )
            raise InputError(
                f"{text} Give overwrite=True to overwrite (KiCad: Overwrite / Overwrite All), "
                "or an other new_name."
            )
        text = library_text(decode_as_kicad(target_data, target), copy, conflicts)
        names = set(target_symbols) | {symbol for symbol, _text in copy.symbols}
        data = kicad_save_symbols(text, target, Path(work), names)
    warnings += _save_guarded(context, [(target, data)], {target: stamp})
    copied = [symbol for symbol, _text in copy.symbols]
    lines = [
        f"- From: {lib_id} ({source_file})",
        f"- Into: {target_library} ({target})",
        f"- Symbols written: {', '.join(copied)}"
        + (" (the parents of the derived symbol too, as KiCad does)" if len(copied) > 1 else ""),
    ]
    if conflicts:
        lines.append(f"- Replaced: {', '.join(conflicts)}")
    if flatten_symbol:
        lines.append("- Flattened: the symbol has no parent in the target.")
    lines.append("- KiCad saved the library (kicad-cli), and a second save gave the same bytes.")
    return "\n".join(
        [f"✅ The symbol '{leaf_name}' is in the library '{target_library}'.", "", *lines]
        + [f"\n⚠️ {warning}" for warning in warnings]
    )


def _copy_footprint(
    context: _Context,
    _kind,
    lib_id: str,
    target_library: str,
    new_name: str | None,
    overwrite: bool,
) -> str:
    library, name = _split_id(lib_id, "footprint")
    row, _problems = find_kicad_library("footprint", library, context.kicad, context.project)
    source = row.path / f"{name}.kicad_mod" if row.path is not None else None
    if source is None or source not in footprint_files(row.path):
        raise InputError(f'The library "{library}" has no footprint "{name}".')
    version = library_file_version(source, "footprint")
    if version is not None and version > KICAD10.board:
        raise InputError(
            f"The footprint {lib_id} has a newer file version than KiCad {context.kicad} "
            "writes: KiCad cannot read it."
        )
    target_dir, warnings = _project_library(context, "footprint", target_library)
    # SAVE_AS_DIALOG: wxString::Trim (ASCII white space).
    new = new_name.strip(WX_SPACE) if new_name is not None else name
    if problem := footprint_name_problem(new):
        raise InputError(problem)
    target = target_dir / f"{new}.kicad_mod"
    # KiCad asks to overwrite, also for the footprint itself.
    if target.exists() and not overwrite:
        raise InputError(
            f"Footprint {new} already exists in {target_library}. Give overwrite=True to "
            "overwrite it (KiCad: Overwrite), or an other new_name."
        )
    stamp = file_stamp(target)
    with tempfile.TemporaryDirectory(prefix="kicad-workbench-copy-") as work:
        _file_name, data = footprint_copy(source, new, Path(work))
    replaced = stamp is not None
    warnings += _save_guarded(context, [(target, data)], {target: stamp})
    lines = [
        f"- From: {lib_id} ({source})",
        f"- Into: {target_library} ({target})" + (" (replaced)" if replaced else ""),
        "- KiCad saved the footprint (kicad-cli), and a second save gave the same bytes.",
    ]
    return "\n".join(
        [f"✅ The footprint '{new}' is in the library '{target_library}'.", "", *lines]
        + [f"\n⚠️ {warning}" for warning in warnings]
    )


@mcp.tool()
async def copy_symbol_to_library(
    project_path: str,
    lib_id: str,
    target_library: str,
    new_name: str | None = None,
    flatten: bool = False,
    overwrite: bool = False,
) -> str:
    """Copy a symbol into a library of the project table, as "Save As" of the symbol
    editor of KiCad 10 does.

    To an other library, a derived symbol goes with its parents (KiCad copies the full
    parent chain; only the symbol gets the new name). flatten=True saves only the
    flattened symbol, as the "Flatten" check box. The Value gets the new name when it
    was equal to the old name. KiCad saves the library (kicad-cli), and a second save
    must give the same bytes. The target must be a symbol library file of the project
    table in the version of the installed KiCad (else: upgrade_project_libraries
    first). KiCad must not have the project open. The installed KiCad must be KiCad
    10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        lib_id: The symbol, "<library>:<symbol>", for example "Device:R".
        target_library: The nickname of the target in the project symbol table.
        new_name: The new name. KiCad changes spaces to "_". Default: the old name.
        flatten: True saves only the flattened symbol, with no parent.
        overwrite: True replaces the symbols with the same names in the target
            (KiCad: Overwrite / Overwrite All). False refuses them.

    Returns:
        The written symbols, or an error.
    """
    try:
        return await _run(
            project_path, None, _copy_symbol, lib_id, target_library, new_name, flatten, overwrite
        )
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
async def copy_footprint_to_library(
    project_path: str,
    lib_id: str,
    target_library: str,
    new_name: str | None = None,
    overwrite: bool = False,
) -> str:
    """Copy a footprint into a library of the project table, as "Save As" of the
    footprint editor of KiCad 10 does.

    The Value gets the new name when it was equal to the old name. KiCad saves the
    footprint (kicad-cli), and a second save must give the same bytes. The target must
    be a footprint library of the project table in the version of the installed KiCad
    (else: upgrade_project_libraries first). A footprint that is not on F.Cu is refused.
    KiCad must not have the project open. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        lib_id: The footprint, "<library>:<footprint>".
        target_library: The nickname of the target in the project footprint table.
        new_name: The new name. Default: the old name.
        overwrite: True replaces a footprint with the same name in the target (KiCad:
            Overwrite). False refuses it.

    Returns:
        The written footprint, or an error.
    """
    try:
        return await _run(
            project_path, None, _copy_footprint, lib_id, target_library, new_name, overwrite
        )
    except Exception as e:
        return error_answer(e, "Error")


# The properties of a symbol of a project library (phase 4, step 5).


def _symbol_properties(
    context: _Context,
    _kind,
    lib_id: str,
    fields: dict[str, str | None] | None,
    keywords: str | None,
    footprint_filters: list[str] | None,
) -> str:
    if not fields and keywords is None and footprint_filters is None:
        raise InputError("Give fields, keywords or footprint_filters.")
    library, name = _split_id(lib_id, "symbol")
    path, warnings = _project_library(context, "symbol", library)
    data, stamp, symbols = _read_symbol_library(path)
    if name not in symbols:
        raise InputError(f"The library '{library}' ({path}) has no symbol '{name}'.")
    chain = symbol_chain(symbols, name)
    env = kicad_environment(context.kicad)
    size, size_warning = symbol_editor_text_size(env.config_dir)
    result = change_fields(
        chain,
        fields or {},
        keywords,
        footprint_filters,
        env,
        project_variables(context.project),
        context.project.parent,
        size,
    )
    node = chain[0].node
    if not result.lines:
        return (
            f"✅ The symbol '{name}' of the library '{library}' has these properties already: "
            "the tool wrote no file."
        )
    if size_warning and any(line.startswith("- New field") for line in result.lines):
        warnings.append(size_warning)
    text = decode_as_kicad(data, path)
    # The positions of the node are positions in the text that read_library parsed.
    if node.source != text:
        raise InputError(f"The text of {path} is not the text that the tool read. Do it again.")
    text = text[: node.start] + result.text + text[node.end :]
    with tempfile.TemporaryDirectory(prefix="kicad-workbench-fields-") as work:
        new_data = kicad_save_symbols(text, path, Path(work), set(symbols))
    warnings += _save_guarded(context, [(path, new_data)], {path: stamp})
    lines = [
        f"✅ The symbol '{name}' of the library '{library}' has the changes.",
        "",
        *result.lines,
        f"- Library: {path}",
        "- KiCad saved the library (kicad-cli), and a second save gave the same bytes.",
    ]
    if len(chain) > 1:
        lines.append(
            "- A derived symbol: KiCad 10.0 saves the keywords and the footprint filters of "
            "its parent when it has none of its own (LIB_SYMBOL::GetKeyWords)."
        )
    return "\n".join(lines + [f"\n⚠️ {warning}" for warning in warnings])


@mcp.tool()
async def set_library_symbol_properties(
    project_path: str,
    lib_id: str,
    fields: dict[str, str | None] | None = None,
    keywords: str | None = None,
    footprint_filters: list[str] | None = None,
) -> str:
    """Change the properties of a symbol of a project library, as the Symbol Properties
    dialog of the symbol editor of KiCad 10 does.

    A field name that the symbol has gets the new text (a mandatory name in any case).
    A new name gives a new hidden field at (0, 0), with the text size of the symbol
    editor settings. A text of None removes a user field (the five mandatory fields
    cannot be removed). As in the dialog, a text that is a path to a file that exists
    ("/", "${", "./", "../") gets "file://" before it, the escapes ("{slash}") become
    characters, and the text of a mandatory field has no white space at its ends. The
    footprint filters replace the list (spaces become "_"). KiCad saves the library
    (kicad-cli), and a second save must give the same bytes. The library must be in the
    project table, in the version of the installed KiCad. KiCad must not have the
    project open. The installed KiCad must be KiCad 10.0.

    Args:
        project_path: The .kicad_pro file or the project folder.
        lib_id: The symbol, "<library>:<symbol>", of a library of the project table.
        fields: The field texts by name. None as a text removes the user field.
        keywords: The new keywords. None: no change. "" removes them.
        footprint_filters: The new list of footprint filters. None: no change.

    Returns:
        The changes, or an error.
    """
    try:
        return await _run(
            project_path,
            None,
            _symbol_properties,
            lib_id,
            fields,
            keywords,
            footprint_filters,
        )
    except Exception as e:
        return error_answer(e, "Error")
