"""The symbols of a library as KiCad 10 saves them, for lib_symbols.flatten.

KiCad loads a library symbol and writes its copy into lib_symbols of a schematic with
the writer of the library (SCH_IO_KICAD_SEXPR_LIB_CACHE::SaveSymbol). The load
changes the text of a file that KiCad did not save. Measured with kicad-cli 10.0.6
and 10.0.7 (``sym upgrade --force``, the same bytes): the blanks at the two ends of
the text of a mandatory field go (SCH_FIELD::SetText), a mandatory field gets its
canonical name, a missing mandatory field gets the defaults of LIB_SYMBOL, and a
second user field with the same name gets the name "<name>_1". A library that KiCad
saved stays the same, except the order of the symbols in the file.

flatten copies the text of the file. Thus the tools give it the symbols of a save in
kicad-cli (rule 2 of CLAUDE.md): a temporary directory library with the symbols of the
chain (the text that the caller read and checked; one file for each file of the
chain, with its header and its version), saved by ``kicad-cli sym upgrade --force -o``
(EESCHEMA_JOBS_HANDLER::JobSymUpgrade: one output file with all symbols). The save of
a symbol does not depend on the other symbols of its library, and the chain has all
its parents, also a parent in an other file of a directory library (measured: the
two orders of the files give the same save). The cache keeps the last results by the
SHA-256 of the texts. KiCad 10.0 only (new work): the existing rules of KiCad 8 and 9
copy the text of the file, and a nightly build has no verified rule (kicad_build_notes
gives its warning).
"""

import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.kicad_versions import (
    KICAD10,
    kicad_release,
    library_version_problem,
    symbol_library_problem,
)
from ..parsers.lib_symbols import LibrarySymbol, read_library_file, symbol_chain
from ..parsers.sexpr import Node
from .file_stamp import bytes_stamp
from .library_upgrade import run_upgrade

# The number of saves that the cache keeps (the last ones that the tools used). A save
# holds only the symbols of one chain.
_CACHE_SIZE = 256
# The installed KiCad, and (file name, SHA-256) of each file of the temporary library.
_Key = tuple[str, tuple[tuple[str, str], ...]]
_cache: dict[_Key, dict[str, LibrarySymbol]] = {}
_lock = threading.Lock()
# The saves that run now: a second call for the same key waits for the first one.
_running: dict[_Key, "_Run"] = {}


class _Run:
    """A save that runs: the waiting calls get its result or the reason of its error."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.reason: str | None = None


class SavedLibraryError(WorkbenchError):
    """kicad-cli could not save the library, or the saved library has no such symbol."""


def _texts(symbols: list[LibrarySymbol]) -> list[tuple[str, bytes]]:
    """The files of a temporary library: for each file of the symbols, the header of the
    file and its symbols, in the order of the file."""
    files: dict[Path, dict[int, Node]] = {}
    for symbol in symbols:
        files.setdefault(symbol.file, {})[symbol.node.start] = symbol.node
    texts = []
    for n, nodes in enumerate(files.values()):
        ordered = [nodes[start] for start in sorted(nodes)]
        root = ordered[0].root
        header = root.source[root.start : root.children("symbol")[0].start]
        body = "\n".join(root.source[node.start : node.end] for node in ordered)
        texts.append((f"{n}.kicad_sym", f"{header}{body}\n)\n".encode()))
    return texts


def _key(texts: list[tuple[str, bytes]], kicad: str) -> _Key:
    return (kicad, tuple((name, bytes_stamp(data)) for name, data in texts))


def _keep(key: _Key, symbols: dict[str, LibrarySymbol]) -> None:
    with _lock:
        _cache.pop(key, None)  # the last one that the tools used
        _cache[key] = symbols
        while len(_cache) > _CACHE_SIZE:
            del _cache[next(iter(_cache))]


def _saved_symbols(chain: list[LibrarySymbol], kicad: str) -> dict[str, LibrarySymbol]:
    """The symbols of a save of the chain in kicad-cli (the files of the temporary
    library: _map gives them the files of a chain). The first save of a chain takes
    about 0.2 s (the start of kicad-cli); the cache keeps the last saves.

    Raises:
        SavedLibraryError: kicad-cli could not save the symbols.
    """
    texts = _texts(chain)
    key = _key(texts, kicad)
    while True:
        with _lock:
            if key in _cache:
                _cache[key] = _cache.pop(key)  # the last one that the tools used
                return _cache[key]
            run = _running.get(key)
            if run is None:
                run = _running[key] = _Run()
                break
        # An other call saves the same text (maybe of an other library): wait for its
        # result, or give its reason with the symbol of this call.
        run.done.wait()
        if run.reason is not None:
            raise SavedLibraryError(_failure(chain[0], run.reason))
    try:
        symbols = _save(texts)
        _keep(key, symbols)
    except _SaveError as e:
        run.reason = e.reason
        raise SavedLibraryError(_failure(chain[0], e.reason)) from e.__cause__
    finally:
        with _lock:
            del _running[key]
        run.done.set()
    return symbols


class _SaveError(WorkbenchError):
    """kicad-cli could not save the temporary library (the reason)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _failure(symbol: LibrarySymbol, reason: str) -> str:
    return (
        f'kicad-cli could not save the symbol "{symbol.name}" of {symbol.file} (the tool '
        "saves a temporary copy of the symbol and its parents: a path in the message is a "
        f"path of that copy, which the tool removed): {reason}"
    )


def _save(texts: list[tuple[str, bytes]]) -> dict[str, LibrarySymbol]:
    """Save a temporary library in kicad-cli and read its symbols.

    Raises:
        _SaveError: kicad-cli could not save the symbols, or a file of the temporary
            folder could not be written or removed.
    """
    try:
        # The thread that runs kicad-cli owns its temporary folder (CLAUDE.md).
        with tempfile.TemporaryDirectory(prefix="kicad-workbench-") as work:
            source = Path(work) / "chain.kicad_symdir"
            source.mkdir()
            for name, data in texts:
                (source / name).write_bytes(data)
            output = Path(work) / "saved.kicad_sym"
            run_upgrade("symbol", source, output, force=True)
            return read_library_file(output)
    # WorkbenchError: also the errors of the reader (KiCadEncodingError, SExprError,
    # KicadParseError) and of kicad-cli (UpgradeError).
    except (OSError, WorkbenchError) as e:
        raise _SaveError(str(e)) from e


def _map(chain: list[LibrarySymbol], symbols: dict[str, LibrarySymbol]) -> dict[str, LibrarySymbol]:
    """The saved symbols of the chain with the files of the chain (the cache keeps the
    symbols of an equal text of an other library too)."""
    return {
        s.name: replace(symbols[s.name], file=s.file, other_files=s.other_files)
        for s in chain
        if s.name in symbols
    }


def saved_chain(chain: list[LibrarySymbol], library: Path, kicad: str) -> list[LibrarySymbol]:
    """Return the symbol and its parents from a save in the installed KiCad 10.0, for
    flatten. The checks of the file versions use the chain of the file.

    Args:
        chain: The chain of the library file (symbol_chain).
        library: The path of the library row: a .kicad_sym file or a directory.
        kicad: The version of the installed KiCad.

    Returns:
        The saved chain, for flatten only: each symbol has the file of the chain (for
        the messages), the node of the save, and the version of the save (the format
        of its text); for an other KiCad than 10.0, the chain as it is.

    Raises:
        SavedLibraryError: kicad-cli could not save the library, or the saved library
            does not have the symbols of the chain.
    """
    if kicad_release(kicad) is not KICAD10:
        return chain
    symbols = _map(chain, _saved_symbols(chain, kicad))
    try:
        saved = symbol_chain(symbols, chain[0].name)
    except (KeyError, WorkbenchError) as e:
        raise SavedLibraryError(
            f'The save of the library {library} in kicad-cli has no symbol "{chain[0].name}" '
            f"with its parents: {e}"
        ) from e
    if [symbol.name for symbol in saved] != [symbol.name for symbol in chain]:
        raise SavedLibraryError(
            f'The save of the library {library} in kicad-cli gives the symbol "{chain[0].name}" '
            "other parents than the file."
        )
    return saved


class SymbolLibraryError(WorkbenchError):
    """A symbol of the chain cannot go into the schematic (the symbol and the problem of
    library_version_problem or symbol_library_problem)."""

    def __init__(self, symbol: LibrarySymbol, problem: str):
        super().__init__(problem)
        self.symbol = symbol
        self.problem = problem


def schematic_chain(
    chain: list[LibrarySymbol], library: Path, kicad: str, schematic: int
) -> list[LibrarySymbol]:
    """Return the chain for flatten into a schematic: the checks of the file version of
    each symbol of the file (library_version_problem), the save (saved_chain), and the
    checks of the text that goes into the schematic in the format of the save
    (symbol_library_problem: KiCad must read it there in the same way).

    Args:
        chain: The chain of the library file (symbol_chain).
        library: The path of the library row.
        kicad: The version of the installed KiCad.
        schematic: The file version of the schematic.

    Raises:
        SymbolLibraryError: A symbol cannot go into the schematic.
        SavedLibraryError: As saved_chain.
        KicadParseError, LocaleNumberError: As symbol_library_problem.
    """
    for symbol in chain:
        problem = library_version_problem(symbol.version, schematic)
        if problem is not None:
            raise SymbolLibraryError(symbol, problem)
    saved = saved_chain(chain, library, kicad)
    for symbol in saved:
        problem = symbol_library_problem(symbol.node, symbol.version, schematic)
        if problem is not None:
            raise SymbolLibraryError(symbol, problem)
    return saved
