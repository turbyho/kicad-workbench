"""The rename and the delete of a symbol or a footprint of a library, as the library tree
of the symbol editor and of the footprint editor of KiCad 10.0.6 does it (tag 10.0.6,
checked in the source):

- RENAME_DIALOG (common/tool/library_editor_control.cpp) removes the white space at the
  two ends of the text (wxString::Trim); it has no other validator. Its text control has
  one line: it holds no CR and no LF.
- Symbols, SYMBOL_EDITOR_CONTROL::RenameSymbol: the name is EscapeString(text,
  CTX_LIBID) (escape_lib_id); a space stays a space. An other symbol whose name
  SymbolNameInUse finds (wxString::CmpNoCase of the unescaped names) is a conflict. The
  Value gets the new name when it was equal to the old name. The derived symbols keep
  their parent: the save writes (extends "<new name>") (LIB_BUFFER::SaveBuffer).
- SYMBOL_EDIT_FRAME::DeleteSymbolFromLibrary: LIB_BUFFER::DeleteBuffer removes the
  symbol and all symbols derived from it (GetDerivedSymbolNames: the descendants, also
  the derived symbols of a derived symbol).
- Footprints, FOOTPRINT_EDITOR_CONTROL::RenameFootprint: no escape. KiCad saves the
  footprint with the new name (FootprintSave: the file name is
  ReplaceIllegalFileNameChars of the name, FP_CACHE::Load reads the name from the file
  name), and deletes the old footprint (FootprintDelete: its file). KiCad 10.0.6 saves
  first, then deletes: on a file system with no difference of case, a change of only
  the case of the name then removes the footprint. KiCad 10.0.7 and later delete first
  (commit 46591b825e, "keep the footprint when a rename only changes its case"). The
  Value gets the new name when it was equal to the old name (footprint_copy of
  library_copy).

KiCad saves the symbol library (kicad_save_symbols of library_copy) and the footprint
(footprint_copy).
"""

from ..errors import InputError
from ..parsers.annotation import cmp_no_case
from ..parsers.bus_names import unescape_string
from ..parsers.kicad_fields import WX_SPACE
from ..parsers.lib_symbols import LibrarySymbol, parent_name
from ..parsers.sexpr import Node, edited, quote
from ..parsers.sub_symbols import symbol_name
from .library_copy import escape_lib_id, file_name_problem, renamed_symbol, value_text


def renamed_symbol_name(text: str) -> str:
    """The new name of RenameSymbol: the text with no white space at its ends, then
    EscapeString(CTX_LIBID)."""
    return escape_lib_id(text.strip(WX_SPACE))


def symbol_in_use(symbols: dict[str, LibrarySymbol], old: str, new: str) -> str | None:
    """The other symbol that SymbolNameInUse finds for the new name (CmpNoCase of the
    unescaped names), or None. The symbol itself is not a conflict: a change of only the
    case of its name."""
    wanted = unescape_string(new)
    for name in symbols:
        if name != old and cmp_no_case(unescape_string(name), wanted) == 0:
            return name
    return None


def footprint_rename_problem(name: str) -> str | None:
    """The error for the new name of a footprint (the text after the trim), or None."""
    if not name:
        return "Footprint must have a name."
    if "\r" in name or "\n" in name:
        return (
            f"The footprint name {name!r} has a line break: the rename dialog of KiCad has "
            "one line of text."
        )
    return file_name_problem(name)


def _symbol_nodes(root: Node) -> list[tuple[str, Node]]:
    """Each (symbol ...) of a library file with its name as KiCad reads it."""
    return [(symbol_name(node.value(0, "")), node) for node in root.children("symbol")]


def renamed_library(
    root: Node, symbols: dict[str, LibrarySymbol], old: str, new: str
) -> tuple[str, bool]:
    """The text of the library with the symbol old renamed to new.

    The symbol gets the new name (and its sub-symbols), and the new Value when its Value
    was equal to its name. Each symbol that extends old extends new. The other (symbol
    "old") of the file go: KiCad loads only the last of them.

    Args:
        root: The parsed text of the library file (the nodes of symbols are in it).

    Returns:
        (the text, True when the Value got the new name).

    Raises:
        InputError: The symbol extends itself.
    """
    symbol = symbols[old]
    own = symbol.node.first("extends")
    if own is not None and parent_name(own) == old:
        raise InputError(
            f"The symbol '{old}' extends itself: KiCad cannot load it. Repair the library first."
        )
    follows = value_text(symbol.node) == old
    edits = []
    for name, node in _symbol_nodes(root):
        if node.start == symbol.node.start:
            continue
        if name == old:
            # KiCad loads only the last (symbol "old"): the others go.
            edits.append((node.start, node.end, ""))
            continue
        extends = node.first("extends")
        if extends is not None and parent_name(extends) == old:
            atom = extends.atoms()[0]
            edits.append((atom.start, atom.end, quote(new)))
    edits.append(
        (symbol.node.start, symbol.node.end, renamed_symbol(symbol, new, new if follows else None))
    )
    return edited(root, edits), follows


def derived_symbols(symbols: dict[str, LibrarySymbol]) -> dict[str, list[str]]:
    """The names of the symbols that extend each parent (parent_name of (extends ...))."""
    children: dict[str, list[str]] = {}
    for symbol in symbols.values():
        extends = symbol.node.first("extends")
        if extends is not None:
            children.setdefault(parent_name(extends), []).append(symbol.name)
    return children


def descendants(children: dict[str, list[str]], name: str) -> list[str]:
    """The symbols derived from a symbol, also the derived symbols of a derived symbol
    (GetDerivedSymbolNames): each child, then its descendants.

    Args:
        children: The map of derived_symbols.
    """
    found: list[str] = []
    visited = {name}

    def walk(parent: str) -> None:
        for child in children.get(parent, []):
            if child in visited:
                continue
            visited.add(child)
            found.append(child)
            walk(child)

    walk(name)
    return found


def deleted_library(root: Node, names: set[str]) -> str:
    """The text of the library with no (symbol ...) of the names."""
    edits = [(node.start, node.end, "") for name, node in _symbol_nodes(root) if name in names]
    return edited(root, edits)
