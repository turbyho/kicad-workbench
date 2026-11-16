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
from ..parsers.kicad_versions import KICAD10, kicad_release, verified_triple
from ..server import mcp
from ..utils.file_save import SaveFilesError, file_locks, save_file, save_files
from ..utils.file_stamp import FileStamp, file_stamp, read_stamped
from ..utils.file_versions import FileVersionError, check_library_versions, library_file_version
from ..utils.kicad_cli import get_kicad_version, installed_kicad_problem
from ..utils.kicad_lock import check_project_not_open
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
    with file_locks(list(new_files)):
        _check_not_open(context.project)
        for file, stamp in stamps.items():
            if file_stamp(file) != stamp:
                item.result = f"❌ {file} changed after this tool read it: the tool wrote no file"
                return
        try:
            warnings = save_files(list(new_files.items()))
        except SaveFilesError as e:
            item.result = f"❌ {e}"
            return
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
