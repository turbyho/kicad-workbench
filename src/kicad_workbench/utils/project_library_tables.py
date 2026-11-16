"""The project library tables and new project libraries, as KiCad 10.0.6 makes them.

The rules come from the KiCad source, tag 10.0.6:

- The table file: ``LIBRARY_TABLE::Format`` and ``Prettify`` in the mode
  ``LIBRARY_TABLE`` (``common/libraries/library_table.cpp``,
  ``common/io/kicad/kicad_io_utils.cpp``). See format_table.
- The nickname: ``LIB_TABLE_GRID_TRICKS::VerifyTable`` (the check of the table dialog
  at OK) and ``LIB_ID::FindIllegalLibraryNameChar``. See nickname_problem.
- The URI: ``NormalizePath`` (``common/env_paths.cpp``). See normalize_path.
- The library type: ``GuessPluginTypeFromLibPath`` and ``CanReadLibrary`` of the KiCad
  formats. See library_is_kicad.
- A new symbol library: ``SCH_IO_KICAD_SEXPR_LIB_CACHE::Save`` (NEW_SYMBOL_LIBRARY). A
  new footprint library is an empty folder (``FP_CACHE::Save``).

KiCad 10.0 writes a value with OUTPUTFORMATTER::Quotes (it escapes a backslash, a
quote, a line feed and a carriage return), but it reads the quoted text with no
escapes (library_tables._parse_table): a value with a quote makes a table that KiCad
cannot read, and each save doubles a backslash. Thus the tools write each existing
value with its bytes (KiCad 10.0 reads the same value again), and they refuse these
characters in a new value (value_problem).
"""

import os
from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import InputError
from ..parsers.bus_names import unescape_string
from ..parsers.kicad_versions import KICAD10
from .file_stamp import FileStamp, read_stamped
from .library_tables import TABLE_FILES, LibraryTableError, parse_raw_table

KINDS = ("symbol", "footprint")
_TABLE_HEADS = {"symbol": b"sym_lib_table", "footprint": b"fp_lib_table"}
# The type of a KiCad library (SCH_IO_MGR::ShowType, PCB_IO_MGR::ShowType).
KICAD_TYPE = "KiCad"
# The characters that OUTPUTFORMATTER::Quotes escapes. KiCad 10.0 does not read the
# escapes back (see the module text).
_ESCAPED = '\\"\n\r'
# The variable of the project folder (PROJECT_VAR_NAME).
PROJECT_VARIABLE = "KIPRJMOD"
# SCH_IO_KICAD_SEXPR_LIB_CACHE::Save of an empty library: formatLibraryHeader
# (SEXPR_SYMBOL_LIB_FILE_VERSION, the generator, GetMajorMinorVersion) and ")", through
# Prettify. Measured with `kicad-cli sym upgrade --force` of KiCad 10.0.6 (the same
# Save).
NEW_SYMBOL_LIBRARY = (
    "(kicad_symbol_lib\n"
    f"\t(version {KICAD10.symbol_library})\n"
    '\t(generator "kicad_symbol_editor")\n'
    f'\t(generator_version "{KICAD10.name}")\n'
    ")\n"
)
# The extension of a library (FILEEXT::KiCadSymbolLibFileExtension,
# KiCadFootprintLibPathExtension, KiCadFootprintFileExtension).
SYMBOL_EXTENSION = ".kicad_sym"
FOOTPRINT_EXTENSION = ".pretty"
_FOOTPRINT_FILE = "kicad_mod"


@dataclass(frozen=True)
class TableRow:
    """One row of a table. Each value is the bytes of the file (parse_raw_table)."""

    name: bytes
    type: bytes
    uri: bytes
    options: bytes
    descr: bytes
    disabled: bool = False
    hidden: bool = False

    def text(self, field: str) -> str:
        """The value as KiCad 10.0 reads it (wxString::FromUTF8: "" for bytes that are
        not valid UTF-8)."""
        try:
            return getattr(self, field).decode("utf-8")
        except UnicodeDecodeError:
            return ""


@dataclass
class ProjectTable:
    """The project table of one kind: its file, its rows, the bytes that the tool read
    and their stamp (None: no file)."""

    path: Path
    kind: str
    rows: list[TableRow]
    stamp: FileStamp | None
    data: bytes = b""


def table_path(project_file: Path, kind: str) -> Path:
    """The project table of a kind: next to the .kicad_pro file."""
    return project_file.parent / TABLE_FILES[kind]


def read_project_table(project_file: Path, kind: str) -> ProjectTable:
    """Read the project table of a kind as KiCad 10.0 reads it.

    A missing table, or a file of 0 or 1 byte, is a table with no rows: KiCad makes an
    empty table in memory (LIBRARY_MANAGER::createEmptyTable,
    LIBRARY_TABLE::LIBRARY_TABLE) and writes the file at its first save.

    Raises:
        LibraryTableError: KiCad 10.0 cannot read the table, or it is a table of an
            other kind. KiCad then skips the table and its next save replaces it with
            only the new row: the tools refuse to write it.
        OSError: The file cannot be read.
    """
    path = table_path(project_file, kind)
    if not path.exists():
        return ProjectTable(path, kind, [], None)
    data, stamp = read_stamped(path)
    if len(data) <= 1:
        return ProjectTable(path, kind, [], stamp, data)
    head, rows = parse_raw_table(data, path)
    if head.encode() != _TABLE_HEADS[kind]:
        raise LibraryTableError(
            f"The library table {path} is a {head!r} table, not {_TABLE_HEADS[kind].decode()!r}."
        )
    return ProjectTable(
        path,
        kind,
        [
            TableRow(
                row["name"], row["type"], row["uri"], row["options"], row["descr"],
                row["disabled"], row["hidden"],
            )
            for row in rows
        ],
        stamp,
        data,
    )  # fmt: skip


def format_table(kind: str, rows: list[TableRow]) -> bytes:
    """The bytes of a table as LIBRARY_TABLE::Format and Prettify of KiCad 10.0.6
    write them (verified against a table that KiCad 10.0.6 wrote).

    - ``(sym_lib_table`` or ``(fp_lib_table``, then a TAB and ``(version 7)``.
    - One line for each row: a TAB, ``(lib (name "...") (type "...") (uri "...")
      (options "...") (descr "...")``, then `` (disabled)`` and `` (hidden)`` when the
      row has them, and ``)``. The five properties come always, also when empty.
    - ``)`` and a line feed at the end.

    Each value keeps its bytes (see the module text). A new value has no character that
    Quotes escapes (value_problem), thus its bytes are the bytes that KiCad writes.

    Raises:
        InputError: A value has a quote: KiCad 10.0 cannot read the table then (a value
            that the table had as a token, for example ``(descr a"b)``).
    """
    lines = [b"(" + _TABLE_HEADS[kind], b"\t(version 7)"]
    for row in rows:
        values = (row.name, row.type, row.uri, row.options, row.descr)
        for field, value in zip(("name", "type", "uri", "options", "descr"), values, strict=True):
            if b'"' in value:
                raise InputError(
                    f"The {field} {value!r} of a row has a quote. KiCad 10.0 cannot read a "
                    "table with a quote in a value. Change the row in KiCad first."
                )
        line = b"\t(lib " + b" ".join(
            b"(" + key + b' "' + value + b'")'
            for key, value in zip(
                (b"name", b"type", b"uri", b"options", b"descr"), values, strict=True
            )
        )
        if row.disabled:
            line += b" (disabled)"
        if row.hidden:
            line += b" (hidden)"
        lines.append(line + b")")
    lines.append(b")")
    return b"\n".join(lines) + b"\n"


def value_problem(value: str, what: str) -> str | None:
    """Return why the tools cannot write a new value, or None.

    KiCad 10.0 escapes a backslash, a quote, a line feed and a carriage return in a
    value, but it reads the text with no escapes (see the module text).
    """
    found = sorted({char for char in value if char in _ESCAPED})
    if found:
        names = ", ".join(repr(char) for char in found)
        return (
            f"The {what} {value!r} has the characters {names}. KiCad 10.0 writes them with "
            "an escape that it cannot read back (a quote makes a table that KiCad cannot "
            "read, a backslash doubles at each save). Use a text with no such character."
        )
    return None


def nickname_problem(nickname: str) -> str | None:
    """Return the error of LIB_TABLE_GRID_TRICKS::VerifyTable for a nickname, or None.

    VerifyTable trims the white space at the two ends of a nickname (the tools refuse
    such a nickname: KiCad would change it at the next OK of the table dialog), refuses
    an empty nickname, and refuses a character below U+0020, a backslash and a colon
    (LIB_ID::isLegalLibraryNameChar).
    """
    if not nickname.strip():
        return "Library must have a nickname."
    if nickname != nickname.strip():
        return (
            f"The nickname {nickname!r} has white space at its start or end. The table "
            "dialog of KiCad removes it."
        )
    for char in nickname:
        if ord(char) < 0x20 or char in "\\:":
            return f"Illegal character {char!r} in nickname {nickname!r}."
    return None


def default_nickname(path: Path, kind: str) -> str:
    """The nickname that the table dialog gives a library (browseLibrariesHandler):
    FixIllegalChars of the name with no extension. A KiCad footprint folder with an
    other extension than ".pretty" keeps its extension."""
    if kind == "footprint" and path.suffix != FOOTPRINT_EXTENSION:
        return fix_illegal_chars(path.name)
    return fix_illegal_chars(path.stem)


def fix_illegal_chars(name: str) -> str:
    """LIB_ID::FixIllegalChars for a library name: each illegal character becomes
    "_"."""
    return "".join("_" if ord(char) < 0x20 or char in "\\:" else char for char in name)


def escape_nickname(nickname: str) -> str:
    """EscapeString with CTX_LIBID: the nickname that the table dialog writes
    (LIB_TABLE_GRID_DATA_MODEL::SetValue)."""
    escapes = {"\\": "{backslash}", "<": "{lt}", ">": "{gt}", ":": "{colon}", '"': "{dblquote}"}
    return "".join(escapes.get(char, char) for char in nickname if char not in "\n\r")


def rows_with_nickname(rows: list[TableRow], nickname: str) -> list[int]:
    """The indexes of the rows with a nickname: the text of the file, the text that the
    table dialog shows (UnescapeString), or that text with no white space at its ends
    (VerifyTable compares the trimmed texts)."""
    found = []
    for index, row in enumerate(rows):
        shown = unescape_string(row.text("name"))
        if nickname in (row.text("name"), shown, shown.strip()):
            found.append(index)
    return found


def table_problems(rows: list[TableRow]) -> list[str]:
    """The errors that VerifyTable gives for the rows at the next OK of the table
    dialog: a nickname with an error, two rows with one nickname (case-sensitive, after
    the trim). VerifyTable also removes a row with no URI."""
    problems = []
    seen: dict[str, int] = {}
    for row in rows:
        shown = unescape_string(row.text("name")).strip()
        if not row.text("uri").strip():
            problems.append(f"The row {shown!r} has no URI: the table dialog removes it.")
            continue
        if shown and (problem := nickname_problem(shown)):
            problems.append(problem)
        elif not shown:
            problems.append("Library must have a nickname.")
        seen[shown] = seen.get(shown, 0) + 1
    problems += [
        f"Multiple libraries cannot share the same nickname ('{name}')."
        for name, count in seen.items()
        if count > 1
    ]
    return problems


def with_row(rows: list[TableRow], row: TableRow) -> list[TableRow]:
    """The rows with a new row at the end (LIBRARY_TABLE::InsertRow)."""
    return [*rows, row]


def changed(row: TableRow, disabled: bool | None, hidden: bool | None) -> TableRow:
    """The row with new flags (None: no change)."""
    return replace(
        row,
        disabled=row.disabled if disabled is None else disabled,
        hidden=row.hidden if hidden is None else hidden,
    )


def _split(path: str) -> tuple[str, list[str]]:
    """The volume and the folder names of a folder path, as wxFileName::SetPath gives
    them (GetVolume, GetDirs): no empty names."""
    volume, rest = os.path.splitdrive(path)
    separators = "\\/" if os.name == "nt" else "/"
    for separator in separators[1:]:
        rest = rest.replace(separator, separators[0])
    return volume, [name for name in rest.split(separators[0]) if name]


def _remaining(folder: str, file_folder: str) -> str | None:
    """normalizeAbsolutePaths: the folders of the file below the folder, each with "/"
    after it, or None when the file is not below the folder."""
    volume_a, dirs_a = _split(folder)
    volume_b, dirs_b = _split(file_folder)
    if (volume_a, dirs_a) == (volume_b, dirs_b):
        return ""
    if len(dirs_a) > len(dirs_b) or volume_a.lower() != volume_b.lower():
        return None
    if dirs_a != dirs_b[: len(dirs_a)]:
        return None
    return "".join(name + "/" for name in dirs_b[len(dirs_a) :])


def normalize_path(file_path: Path, variables: dict[str, str], project_dir: Path) -> str:
    """NormalizePath: the URI of a library in a table.

    1. The path variables of KiCad (Pgm().GetLocalEnvVariables(), in the sequence of
       their names), each with a folder that exists and can be read: the variable
       whose folder holds the library and has the most folder levels wins (the first
       one at an equal count).
    2. Else ``${KIPRJMOD}`` when the project folder holds the library.
    3. Else the absolute path.

    The result is ``${NAME}/``, the folders below the folder of the variable, and the
    file name. LIBRARY_TABLE::Format writes each backslash as "/".
    """
    folder, name = str(file_path.parent), file_path.name
    best: tuple[int, str, str] | None = None
    for variable in sorted(variables):
        value = variables[variable]
        if not value or not os.path.isabs(value):
            continue
        # wxFileName::DirExists and IsDirReadable (wxIsReadable: access with R_OK).
        if not os.path.isdir(value) or not os.access(value, os.R_OK):
            continue
        remaining = _remaining(value, folder)
        depth = len(_split(value)[1])
        if remaining is not None and (best is None or depth > best[0]):
            best = (depth, variable, remaining)
    if best is None:
        remaining = _remaining(str(project_dir), folder)
        if remaining is not None:
            best = (0, PROJECT_VARIABLE, remaining)
    text = str(file_path) if best is None else f"${{{best[1]}}}/{best[2]}{name}"
    return text.replace("\\", "/")


def library_is_kicad(path: Path, kind: str) -> bool:
    """True when GuessPluginTypeFromLibPath of KiCad 10.0.6 gives the KiCad format.

    - Symbols (SCH_IO_KICAD_SEXPR::CanReadLibrary): a folder with a file
      ``*.kicad_sym`` (wxDir::GetFirst with no hidden files; the case of the
      extension matters, except on Windows), or a file with the extension
      ``.kicad_sym`` (any case).
    - Footprints (IO_BASE::CanReadLibrary, the KiCad plugin is the first one): a
      folder with a file whose extension is ``kicad_mod`` (any case, hidden files
      too). The tools also take an empty folder (a new library): the table dialog
      then keeps the type of its KiCad button.
    """
    if kind == "symbol":
        if path.is_dir():
            return bool(library_files(path, kind))
        return path.suffix.lower() == SYMBOL_EXTENSION and path.is_file()
    if not path.is_dir():
        return False
    return bool(library_files(path, kind)) or not any(e.is_file() for e in path.iterdir())


def library_files(path: Path, kind: str) -> list[Path]:
    """The library files that KiCad finds in a library (see library_is_kicad): a
    .kicad_sym file, the visible ``*.kicad_sym`` files of a folder, or the files of a
    footprint folder whose extension is ``kicad_mod``."""
    if not path.is_dir():
        return [path]
    if kind == "symbol":
        return sorted(
            entry
            for entry in path.iterdir()
            if entry.is_file()
            and not entry.name.startswith(".")
            and _matches(entry.name, "*" + SYMBOL_EXTENSION)
        )
    return sorted(
        entry
        for entry in path.iterdir()
        if entry.is_file()
        and "." in entry.name
        and entry.name.rpartition(".")[2].lower() == _FOOTPRINT_FILE
    )


def _matches(name: str, pattern: str) -> bool:
    """wxMatchWild of wxDir: with no regard to case only on Windows."""
    suffix = pattern[1:]
    if os.name == "nt":
        return name.lower().endswith(suffix.lower())
    return name.endswith(suffix)
