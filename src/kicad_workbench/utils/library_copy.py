"""The copy of a symbol or a footprint into an other library, as "Save As" of the
symbol editor and of the footprint editor of KiCad 10.0.6 does it.

The rules (tag 10.0.6):

- Symbols: ``SYMBOL_SAVE_AS_HANDLER::DoSave`` (``eeschema/symbol_editor/symbol_editor.cpp``).
  To an other library, KiCad copies the full parent chain, from the root down; only the
  leaf gets the new name. The Value of each saved symbol gets its new name when the
  Value of the original symbol was equal to its name. With "Flatten", KiCad saves only
  ``LIB_SYMBOL::Flatten`` of the symbol. In the same library, the leaf keeps its parent.
  The dialog trims the name, changes spaces to "_" and escapes it (dialog_symbol_name).
- Footprints: ``FOOTPRINT_EDIT_FRAME::SaveFootprintAs``: the new name, and the Value
  gets it when the Value was equal to the old name. ``FP_CACHE::Load`` takes the name
  from the file name.

KiCad does each format: the functions put the text into a temporary file and let
``kicad-cli sym upgrade --force`` or ``kicad-cli fp upgrade --force`` load and save it,
with the same Save that the editors use (SCH_IO_KICAD_SEXPR_LIB_CACHE::Save,
FP_CACHE::Save). Then a second save must give the same bytes, and the names must be
the expected ones.
"""

from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.kicad_fields import WX_SPACE
from ..parsers.kicad_versions import KicadFileError
from ..parsers.lib_symbols import LibrarySymbol, flatten, read_library, symbol_chain
from ..parsers.sexpr import Node, edited, parse, property_atoms, quote
from ..parsers.sub_symbols import sub_symbols
from .kicad_text import decode_as_kicad
from .library_upgrade import FOOTPRINT_FILE, footprint_files, run_upgrade, symbol_names

# The characters that the validator of the "Save Footprint As" dialog refuses
# (FOOTPRINT::StringLibNameInvalidChars), and the characters that
# ReplaceIllegalFileNameChars changes in the file name (GetIllegalFileNameWxChars).
FOOTPRINT_NAME_CHARS = '%$<>\t\n\r"\\/:'
ILLEGAL_FILE_NAME_CHARS = '\\/:"<>|*?'
# EscapeString with CTX_LIBID.
_LIBID_ESCAPES = {"\\": "{backslash}", "<": "{lt}", ">": "{gt}", ":": "{colon}", '"': "{dblquote}"}


class CopyError(WorkbenchError):
    """The copy is not possible, or the check of the result failed."""


def escape_lib_id(text: str) -> str:
    """EscapeString(text, CTX_LIBID): CR and LF dropped, the characters of _LIBID_ESCAPES
    as escapes."""
    return "".join(_LIBID_ESCAPES.get(char, char) for char in text if char not in "\n\r")


def dialog_symbol_name(text: str) -> str:
    """The name that the "Save Symbol As" dialog gives (SAVE_SYMBOL_AS_DIALOG): the text
    with no white space at its ends, each space as "_", then EscapeString(CTX_LIBID)."""
    return escape_lib_id(text.strip(WX_SPACE).replace(" ", "_"))


def footprint_name_problem(name: str) -> str | None:
    """The error for a new footprint name, or None."""
    if not name:
        return "The footprint name is empty."
    bad = sorted({char for char in name if char in FOOTPRINT_NAME_CHARS})
    if bad:
        return (
            f"The footprint name {name!r} has the characters {bad}: the Save As dialog of "
            "KiCad refuses them."
        )
    return file_name_problem(name)


def file_name_problem(name: str) -> str | None:
    """The error for a footprint name that ReplaceIllegalFileNameChars changes in the
    file name (FootprintSave), or None."""
    bad = sorted({char for char in name if char in ILLEGAL_FILE_NAME_CHARS})
    if bad:
        return (
            f"The footprint name {name!r} has the characters {bad}: KiCad changes them to "
            "'_' in the file name, and reads the name from the file name. The tool refuses "
            "such a name."
        )
    return None


def _value_atom(symbol: Node):
    """The value token of the Value property of a symbol, or None."""
    for prop in symbol.children("property"):
        atoms = property_atoms(prop)
        if len(atoms) > 1 and atoms[0].value == "Value":
            return atoms[1]
    return None


def value_text(symbol: Node) -> str | None:
    atom = _value_atom(symbol)
    return atom.value if atom is not None else None


def renamed_symbol(symbol: LibrarySymbol, name: str, value: str | None) -> str:
    """The text of a symbol with a new name (and its sub-symbols), and a new Value."""
    node = symbol.node
    edits = []
    if name != symbol.name:
        atom = node.atoms()[0]
        edits.append((atom.start, atom.end, quote(name)))
        # SaveSymbol writes the name of a sub-symbol as "<name>_<unit>_<body style>".
        for unit, style, sub in sub_symbols(node):
            sub_atom = sub.atoms()[0]
            edits.append((sub_atom.start, sub_atom.end, quote(f"{name}_{unit}_{style}")))
    if value is not None:
        atom = _value_atom(node)
        if atom is None:
            raise CopyError(f'The symbol "{symbol.name}" has no Value field in its text.')
        edits.append((atom.start, atom.end, quote(value)))
    return edited(node, edits)


@dataclass
class SymbolCopy:
    """The symbols that the copy writes: (the name in the target, its text)."""

    symbols: list[tuple[str, str]]


def symbol_copy(
    chain: list[LibrarySymbol], new_name: str, same_library: bool, flatten_symbol: bool
) -> SymbolCopy:
    """The symbols that "Save As" writes into the target (DoSave).

    Args:
        chain: The symbol and its parents (symbol_chain), in the version of the target.
        new_name: The name of the leaf (dialog_symbol_name).
        same_library: The target is the library of the symbol.
        flatten_symbol: The "Flatten" check box.
    """
    leaf = chain[0]
    follows = value_text(leaf.node) == leaf.name
    if flatten_symbol:
        text = flatten(chain, f"x:{new_name}", 10, library_item=True)
        node = parse(text)
        flat = LibrarySymbol(new_name, node, leaf.file, leaf.version)
        if follows:
            text = renamed_symbol(flat, new_name, new_name)
        return SymbolCopy([(new_name, text)])
    if same_library:
        return SymbolCopy(
            [(new_name, renamed_symbol(leaf, new_name, new_name if follows else None))]
        )
    symbols = []
    for index in range(len(chain) - 1, -1, -1):
        symbol = chain[index]
        name = new_name if index == 0 else symbol.name
        symbols.append((name, renamed_symbol(symbol, name, name if follows else None)))
    return SymbolCopy(symbols)


def symbol_conflicts(
    copy: SymbolCopy, target: dict[str, LibrarySymbol], chain: list[LibrarySymbol], same: bool
) -> list[str]:
    """The names of the copy that the target has. In the same library, the errors of
    the dialog for a circular inheritance (CheckForParentalChainConflicts).

    Raises:
        CopyError: The same library, and the new name is the symbol itself, an ancestor
            of it or a descendant of it.
    """
    names = [name for name, _text in copy.symbols]
    leaf = chain[0]
    new = names[-1]
    if not same and len(names) > 1 and new in names[:-1]:
        # The leaf would get the name of a parent that the copy writes too: a symbol
        # that extends itself.
        raise CopyError(
            f'The new name "{new}" is the name of a parent of the symbol, and the copy writes '
            "the parents too. Give an other new_name, or flatten=True."
        )
    if same:
        # CheckSavingIntoOwnInheritance: the symbol itself is not a conflict of the
        # inheritance (KiCad asks to overwrite it).
        if new in [symbol.name for symbol in chain[1:]]:
            raise CopyError(
                f"Symbol '{leaf.name}' cannot replace another symbol '{new}' that it descends from"
            )
        if new in target and new != leaf.name:
            try:
                ancestors = [symbol.name for symbol in symbol_chain(target, new)[1:]]
            except (KeyError, KicadFileError) as e:
                raise CopyError(
                    f'The parent chain of the symbol "{new}" of the target cannot be read: {e}'
                ) from e
            if leaf.name in ancestors:
                raise CopyError(
                    f"Symbol '{leaf.name}' cannot replace another symbol '{new}' that is a "
                    "descendent of it"
                )
    return [name for name in names if name in target]


def library_text(target_text: str, copy: SymbolCopy, replaced: list[str]) -> str:
    """The text of the target library with the new symbols at the end and the replaced
    symbols removed. KiCad sorts the symbols at the save."""
    root = parse(target_text)
    edits = []
    for node in root.children("symbol"):
        if node.value(0, "") in replaced:
            edits.append((node.start, node.end, ""))
    at = root.end - 1
    edits.append((at, at, "\n" + "\n".join(text for _name, text in copy.symbols) + "\n"))
    return edited(root, edits)


def kicad_save_symbols(text: str, target: Path, work: Path, names: set[str]) -> bytes:
    """Let KiCad save the library text (kicad-cli sym upgrade --force), and check it: a
    second save gives the same bytes, and the library has the names.

    Raises:
        CopyError: kicad-cli failed, or the check found a difference.
    """
    source = work / "copy" / target.name
    source.parent.mkdir()
    source.write_bytes(text.encode("utf-8"))
    (work / "save").mkdir()
    (work / "check").mkdir()
    out, again = work / "save" / target.name, work / "check" / target.name
    run_upgrade("symbol", source, out, force=True)
    run_upgrade("symbol", out, again, force=True)
    data = out.read_bytes()
    if again.read_bytes() != data:
        raise CopyError("KiCad saved the new library again with other bytes: no file written.")
    if set(symbol_names(data, out)) != names:
        raise CopyError(
            "The library that KiCad saved does not have the expected symbols: no file written."
        )
    return data


def symbols_in_version(library: Path, work: Path, written: int) -> dict[str, LibrarySymbol]:
    """The symbols of a library file in the version that KiCad writes: an older file is
    converted with kicad-cli into the folder work first."""
    symbols = read_library(library)
    versions = {symbol.version for symbol in symbols.values()}
    if versions <= {written}:
        return symbols
    out = work / "source" / library.name
    out.parent.mkdir()
    run_upgrade("symbol", library, out, force=True)
    return read_library(out)


def footprint_copy(source: Path, new_name: str, work: Path) -> tuple[str, bytes, bool]:
    """The file name and the bytes of a footprint with a new name, as KiCad saves it, and
    True when the Value got the new name.

    1. KiCad saves the source footprint in its version (fp upgrade --force).
    2. The Value gets the new name when it was equal to the old name.
    3. The file "<new name>.kicad_mod" (FP_CACHE::Load takes the name from it), saved by
       KiCad, then the check: a second save gives the same bytes.

    Raises:
        CopyError: The footprint is not on F.Cu, kicad-cli failed, or the check found a
            difference.
    """
    old_name = source.name[: -len(FOOTPRINT_FILE)]
    first = work / "source.pretty"
    first.mkdir()
    (first / source.name).write_bytes(source.read_bytes())
    normal = work / "normal.pretty"
    run_upgrade("footprint", first, normal, force=True)
    data = (normal / source.name).read_bytes()
    root = parse(decode_as_kicad(data, source))
    layer = root.first("layer")
    if layer is None or layer.value(0, "") != "F.Cu":
        raise CopyError(
            f"The footprint {old_name} is not on F.Cu. FootprintSave of KiCad flips such a "
            "footprint; the tool has no verified rules for it."
        )
    edits = []
    for prop in root.children("property"):
        atoms = property_atoms(prop)
        if len(atoms) > 1 and atoms[0].value == "Value" and atoms[1].value == old_name:
            edits.append((atoms[1].start, atoms[1].end, quote(new_name)))
    text = edited(root, edits) if edits else data.decode("utf-8", errors="surrogateescape")
    file_name = new_name + FOOTPRINT_FILE
    named = work / "named.pretty"
    named.mkdir()
    (named / file_name).write_bytes(text.encode("utf-8", errors="surrogateescape"))
    out, again = work / "save.pretty", work / "check.pretty"
    run_upgrade("footprint", named, out, force=True)
    run_upgrade("footprint", out, again, force=True)
    files = [file.name for file in footprint_files(out)]
    if files != [file_name]:
        raise CopyError(f"KiCad saved the files {files}, not {file_name}: no file written.")
    result = (out / file_name).read_bytes()
    if (again / file_name).read_bytes() != result:
        raise CopyError("KiCad saved the footprint again with other bytes: no file written.")
    return file_name, result, bool(edits)
