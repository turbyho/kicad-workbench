"""Index of the symbols and footprints of KiCad libraries, for the search tools.

A full read of the libraries of KiCad 10 takes some 17 s. The index keeps the
name, the description and the keywords of each item. It is in memory and in
the cache directory of the user, thus a restart of the server does not read
all libraries again. A library is read again only when the stamp of one of
its files changed: the modification time, the change time, the inode number
or the size. For a file that changed less than 2 s before the read, the index
also compares the content (lib_symbols.files_stamp, LibraryFiles).

- A symbol library is a .kicad_sym file, or a directory of .kicad_sym files
  (KiCad 10). In a directory, the index reads the files sorted by name, and a
  later file replaces a symbol with the same name. KiCad reads the files in
  the order of the file system. The description is the field "Description" (KiCad 8 and later) or
  "ki_description" (KiCad 7 and earlier). The keywords are "ki_keywords".
  A derived symbol (extends ...) gets the description, the keywords and the
  footprint of its parent when its own field is empty.
- The index keeps the text of the fields. KiCad also expands text variables
  (for example ${VALUE}) in the description and the keywords; the index does
  not do this.
- The chooser of KiCad also shows the own "Description" field of a symbol
  as a column, and searches it too. A derived symbol does not get this field
  from its parent.
- A footprint library is a .pretty directory. The name of a footprint is the
  name of its .kicad_mod file. Its description is (descr ...) and its
  keywords are (tags ...).
"""

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..parsers.kicad_versions import file_version, footprint_version
from ..parsers.lib_symbols import LibraryFiles, library_files, parent_name
from ..parsers.sexpr import Node, SExprError, parse, property_field
from ..parsers.sub_symbols import symbol_name
from .kicad_text import KiCadEncodingError, decode_as_kicad
from .library_tables import LibraryRow
from .user_files import cache_home, write_atomic

# Change the version when the content of an index entry changes.
# Version 4: the stamp has the change time and the inode number, and an index of
# a file that changed just before the read has the digest of the files.
# Version 5: each entry has its file and the file version (file_versions).
_INDEX_VERSION = 5
_EXTENSIONS = {"symbol": ".kicad_sym", "footprint": ".kicad_mod"}
_ENTRY_KEYS = ("name", "description", "keywords", "footprint", "description_field")
# The items of each library: key -> (stamp, digest, items, problems). The digest
# is None when the stamp was reliable (lib_symbols.files_stamp).
_memory: dict[tuple[str, Path, str], tuple[list, str | None, list["LibraryItem"], list[str]]] = {}
# One read of a library at a time: the search tools run in worker threads.
_locks: dict[tuple[str, Path, str], threading.Lock] = {}
_locks_lock = threading.Lock()


@dataclass(frozen=True)
class LibraryItem:
    """A symbol or a footprint of a library."""

    library: str
    name: str
    description: str
    keywords: str
    footprint: str = ""  # The default footprint of a symbol.
    # The own "Description" field of a symbol (with no value from a parent).
    description_field: str = ""
    # The library file of the item and its file version (None: no version).
    file: str = field(default="", compare=False)
    version: int | None = field(default=None, compare=False)
    _terms: list = field(default=None, init=False, repr=False, compare=False)

    @property
    def lib_id(self) -> str:
        return f"{self.library}:{self.name}"

    @property
    def terms(self) -> list[tuple[str, int, bool]]:
        """The search terms (made one time for each item)."""
        if self._terms is None:
            object.__setattr__(self, "_terms", _search_terms(self))
        return self._terms


def cache_dir() -> Path:
    """The cache directory of the index, in the cache directory of the user."""
    return cache_home() / "library-index"


def _files(kind: str, path: Path) -> list[Path]:
    """The files of a library, in a fixed order."""
    if kind == "symbol":
        return library_files(path)
    if path.is_dir():
        return sorted(path.glob(f"*{_EXTENSIONS[kind]}"))
    return [path]


def _read_symbol(symbol: Node) -> dict:
    """Read the name and the search fields of a (symbol ...) of a library."""
    entry = {
        "name": symbol_name(symbol.value(0, "")),
        "description": "",
        "keywords": "",
        "footprint": "",
        "description_field": "",
        "parent": None,
    }
    extends = symbol.first("extends")
    if extends is not None:
        entry["parent"] = parent_name(extends)
    for prop in symbol.children("property"):
        name_value = property_field(prop)
        if name_value is None:
            continue
        name, value = name_value
        # A later property replaces an earlier one, as in the parser of KiCad.
        # "ki_description" is the description of KiCad 7 and earlier.
        if name.lower() == "description" or name == "ki_description":
            entry["description"] = value
            entry["description_field"] = value
        elif name == "ki_keywords":
            entry["keywords"] = value
        elif name.lower() == "footprint":
            entry["footprint"] = value
    return entry


def _inherit(entries: dict[str, dict], library: Path, problems: list[str]) -> None:
    """Give a derived symbol the empty fields of its parent, as KiCad does."""
    for entry in entries.values():
        parent_name = entry["parent"]
        if parent_name is None:
            continue
        if parent_name not in entries:
            problems.append(
                f"No parent for extended symbol {entry['name']} found in library {library}."
            )
            continue
        for key in ("description", "keywords", "footprint"):
            value = entry[key]
            source = entry
            visited = {entry["name"]}
            while not value and source["parent"] in entries and source["parent"] not in visited:
                source = entries[source["parent"]]
                visited.add(source["name"])
                value = source[key]
            entry[key] = value


def _read(kind: str, path: Path, library: LibraryFiles) -> tuple[list[dict], list[str]]:
    """Read the items of the library files. Return (entries, problems)."""
    entries: dict[str, dict] = {}
    problems = []
    for file, data in zip(library.files, library.contents, strict=True):
        try:
            if isinstance(data, OSError):
                raise data
            root = parse(decode_as_kicad(data, file))
        except (OSError, KiCadEncodingError, SExprError) as e:
            problems.append(f"{file} cannot be read: {e}")
            continue
        if kind == "symbol":
            version = file_version(root)
            for symbol in root.children("symbol"):
                entry = _read_symbol(symbol)
                entry.update(file=str(file), version=version)
                entries[entry["name"]] = entry
        else:
            descr = root.first("descr")
            tags = root.first("tags")
            entries[file.stem] = {
                "name": file.stem,
                "description": descr.value(0, "") if descr else "",
                "keywords": tags.value(0, "") if tags else "",
                "footprint": "",
                "description_field": "",
                "file": str(file),
                "version": footprint_version(root),
            }
    if kind == "symbol":
        # The parent of a symbol can be in a different file of a directory library.
        _inherit(entries, path, problems)
        for entry in entries.values():
            del entry["parent"]
    return list(entries.values()), problems


def _cache_file(kind: str, path: Path) -> Path:
    digest = hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:32]
    return cache_dir() / f"{kind}-{digest}.json"


def _load_cache(
    file: Path, path: Path, library: LibraryFiles
) -> tuple[list[dict], list[str], str | None] | None:
    """Return (entries, problems, digest) of the index on the disk, or None.

    An index with a digest is used only when the files have this digest now.
    """
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("version") != _INDEX_VERSION
        or data.get("path") != str(path)
    ):
        return None
    entries = data.get("entries")
    problems = data.get("problems")
    digest = data.get("digest")
    # The file can be damaged: use it only when each value has its type.
    if not (
        isinstance(entries, list)
        and all(
            isinstance(entry, dict)
            and all(isinstance(entry.get(key), str) for key in _ENTRY_KEYS)
            and isinstance(entry.get("file"), str)
            and (entry.get("version") is None or isinstance(entry.get("version"), int))
            for entry in entries
        )
        and isinstance(problems, list)
        and all(isinstance(problem, str) for problem in problems)
        and (digest is None or isinstance(digest, str))
    ):
        return None
    if not library.matches(data.get("stamp"), digest):
        return None
    return entries, problems, digest


def _save_cache(
    file: Path, path: Path, stamp: list, digest: str | None, entries: list, problems: list
) -> None:
    """Write the index of a library. A failure only makes the next read slower."""
    data = {
        "version": _INDEX_VERSION,
        "path": str(path),
        "stamp": stamp,
        "digest": digest,
        "entries": entries,
        "problems": problems,
    }
    try:
        write_atomic(file, json.dumps(data))
    except OSError:
        pass


def path_problem(path: Path) -> str | None:
    """Return "missing" or "cannot be read: ..." for a library path, or None.

    Path.exists() gives False also when the user has no access (Python 3.14),
    thus stat() tells the two cases apart.
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError as e:
        return f"cannot be read: {e}"
    return None


def row_problem(row: LibraryRow) -> str | None:
    """Return why KiCad does not load the library of a row, or None.

    The text is "disabled", the problems of the URI, "missing" or
    "cannot be read: ...".
    """
    if row.disabled:
        return "disabled"
    if row.problems:
        return "; ".join(row.problems)
    return "missing" if row.path is None else path_problem(row.path)


def library_items(kind: str, row: LibraryRow) -> tuple[list[LibraryItem], list[str]]:
    """Return the items of a library, from the index when it is current.

    Args:
        kind: "symbol" or "footprint".
        row: The library, with an expanded path.

    Returns:
        (items, problems). A problem names a library file that cannot be read.
    """
    path = row.path
    if path is None:
        return [], [f"The library {row.name} is not at {row.uri}."]
    problem = path_problem(path)
    if problem == "missing":
        return [], [f"The library {row.name} is not at {path}."]
    if problem is not None:
        return [], [f"The library {row.name} {problem}"]
    try:
        library = LibraryFiles(_files(kind, path))
    except OSError as e:
        return [], [f"The library {row.name} cannot be read: {e}"]

    key = (kind, path, row.name)
    with _locks_lock:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        cached = _memory.get(key)
        if cached is None or not library.matches(cached[0], cached[1]):
            cache_file = _cache_file(kind, path)
            loaded = _load_cache(cache_file, path, library)
            if loaded is None:
                entries, problems = _read(kind, path, library)
                _save_cache(
                    cache_file, path, library.stamp, library.digest_to_keep(), entries, problems
                )
            else:
                entries, problems, digest = loaded
                if digest is not None and library.reliable:
                    # The stamp is reliable now: a restart needs no digest.
                    _save_cache(cache_file, path, library.stamp, None, entries, problems)
            items = [
                LibraryItem(
                    row.name,
                    entry["name"],
                    entry["description"],
                    entry["keywords"],
                    entry["footprint"],
                    entry["description_field"],
                    entry["file"],
                    entry["version"],
                )
                for entry in entries
            ]
        else:
            items, problems = cached[2], cached[3]
        # When the stamp is reliable now, the next read needs no digest.
        _memory[key] = (library.stamp, library.digest_to_keep(), items, problems)
    return list(items), list(problems)


# KiCad shortens a long search term (EDA_COMBINED_MATCHER::ScoreTerms).
_MAX_TERM = 1000


def _search_terms(item: LibraryItem) -> list[tuple[str, int, bool]]:
    """The search terms of an item as (text, weight, is_name), with the weights
    of the symbol and footprint choosers of KiCad (LIB_SYMBOL::cacheSearchTerms,
    FOOTPRINT_INFO::GetSearchTerms)."""
    terms = [
        (item.library, 4, False),
        (item.name, 8, True),
        (item.lib_id, 16, True),
        *((keyword, 4, False) for keyword in item.keywords.split()),
        (item.keywords, 1, False),
        (item.description, 1, False),
    ]
    if item.footprint:
        terms.append((item.footprint, 1, False))
    # The columns of the chooser are searched too. The default columns of
    # KiCad are the name and the "Description" field (LIB_TREE_MODEL_ADAPTER).
    if item.description_field:
        terms.append((item.description_field, 4, False))
    return [(text.lower().strip()[:_MAX_TERM], weight, is_name) for text, weight, is_name in terms]


def search(items: list[LibraryItem], query: str) -> list[LibraryItem]:
    """Return the items that hold each word of the query, the best first.

    The score is that of the symbol and footprint choosers of KiCad. Each word
    of the query is compared with each search term of an item, with no
    difference between upper and lower case: the library, the name, the
    library ID, each keyword, all keywords, the description, the default
    footprint of a symbol, and the "Description" field of a symbol (a
    default column of the chooser). A term that is equal to the word adds 8 times its
    weight, a term that starts with the word adds 2 times, and a term that
    holds the word adds 1 time. An item must match each word. An item whose
    name or library ID is equal to a word comes first, then the higher score,
    then the library ID.

    The words are plain text. The regular expressions, the wildcards and the
    relations of the KiCad search box are not used, and the other columns
    that a user can add to the chooser are not searched.
    """
    words = query.lower().split()
    found = []
    for item in items:
        terms = item.terms
        score = 1
        exact = False
        for word in words:
            word_score = 0
            for text, weight, is_name in terms:
                if text == word:
                    word_score += 8 * weight
                    exact = exact or is_name
                elif text.startswith(word):
                    word_score += 2 * weight
                elif word in text:
                    word_score += weight
            if word_score == 0:
                break
            score += word_score
        else:
            found.append((not exact, -score, item.lib_id, item))
    found.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in found]
