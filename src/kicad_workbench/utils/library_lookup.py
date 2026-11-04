"""Find a symbol or a footprint library of KiCad through the library tables."""

from pathlib import Path

from ..parsers.kicad_versions import KicadFileError
from ..parsers.lib_symbols import LibrarySymbol, read_library, symbol_chain
from ..parsers.sexpr import SExprError
from .kicad_text import KiCadEncodingError
from .library_index import row_problem
from .library_tables import TABLE_FILES, LibraryRow, LibraryTableError, find_library


class LibraryLookupError(Exception):
    """A library, a symbol or a footprint cannot be found or read."""


def find_kicad_library(
    kind: str,
    library_name: str,
    kicad: str,
    project: Path | None,
    rows: tuple[list[LibraryRow], list[str]] | None = None,
) -> tuple[LibraryRow, list[str]]:
    """Find the library row that KiCad uses for a library name.

    The library comes from the library tables of the installed KiCad and of
    the project, as in the chooser of KiCad.

    Args:
        kind: "symbol" or "footprint".
        library_name: The name of the library in the library tables.
        kicad: The installed KiCad version, for example "10.0.6".
        project: The .kicad_pro file of the project, or None.
        rows: The result of library_rows for the same kind, KiCad and
            project, or None to read the tables.

    Returns:
        (the library row, the problems of the library tables).

    Raises:
        LibraryLookupError: The library is not there, KiCad does not load it,
            or its type is not "KiCad".
    """
    try:
        row, problems = find_library(kind, library_name, kicad, project, rows)
    except LibraryTableError as e:
        raise LibraryLookupError(str(e)) from e
    if row is None:
        tables = f"the global library table of KiCad {kicad}"
        if project is not None:
            tables += f" or in the project table {project.parent / TABLE_FILES[kind]}"
        raise LibraryLookupError(
            f'no {kind} library "{library_name}" is in {tables}. Use list_libraries to see '
            "the libraries." + "".join(f" {problem}" for problem in problems)
        )
    # The same states as list_libraries gives.
    if problem := row_problem(row):
        raise LibraryLookupError(f'the library "{library_name}" of {row.table}: {problem}.')
    if row.type != "KiCad":
        raise LibraryLookupError(
            f'the library "{library_name}" has the type "{row.type}". '
            'This tool reads only libraries of the type "KiCad".'
        )
    return row, problems


def find_library_symbol(
    library_name: str, symbol_name: str, kicad: str, project: Path | None
) -> tuple[list[LibrarySymbol], LibraryRow, list[str]]:
    """Find a library symbol through the library tables, as the symbol chooser of KiCad.

    Args:
        library_name: The name of the library in the library tables.
        symbol_name: The name of the symbol in the library.
        kicad: The installed KiCad version, for example "10.0.6".
        project: The .kicad_pro file of the project, or None.

    Returns:
        (the symbol and its parents (symbol_chain), the library row, the
        problems of the library tables).

    Raises:
        LibraryLookupError: The library or the symbol is not there, or KiCad
            cannot read it.
    """
    row, problems = find_kicad_library("symbol", library_name, kicad, project)
    try:
        symbols = read_library(row.path)
    except (OSError, KiCadEncodingError, SExprError) as e:
        raise LibraryLookupError(f"the symbol library {row.path} cannot be read: {e}") from e
    try:
        chain = symbol_chain(symbols, symbol_name)
    except KeyError:
        raise LibraryLookupError(
            f'the library "{library_name}" ({row.path}) has no symbol "{symbol_name}".'
        ) from None
    except KicadFileError as e:
        raise LibraryLookupError(str(e)) from e
    return chain, row, problems
