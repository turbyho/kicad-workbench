"""A derived symbol, as "Derive from existing symbol" of the symbol editor of KiCad 10.0.6
does it (tag 10.0.6, checked in the source):

- The name: the text control has FIELD_VALIDATOR(FIELD_T::VALUE), which refuses CR, LF
  and tab (text_problem of library_symbol_fields); then DIALOG_LIB_NEW_SYMBOL::GetName
  gives EscapeString(text, CTX_LIBID) (escape_lib_id), and each space as "_". The dialog
  does not trim the name.
- The symbol (LIB_SYMBOL_LIBRARY_MANAGER::CreateSymbol with a parent): (extends
  parent); each mandatory field is a copy of the field of the parent: the Value gets
  the new name when the parent is a power symbol (the power flag of the root,
  LIB_SYMBOL::IsPower), the Footprint and the Datasheet are empty unless the dialog
  keeps them. With "Transfer user-defined fields", a copy of each user field of the
  parent, with an empty text unless the dialog keeps the content. No keywords and no
  footprint filters of its own (ki_keywords, ki_fp_filters are properties, not fields).

KiCad saves the library (kicad_save_symbols of library_copy): the order of the fields,
the format and the keywords of the parent (KiCad 10.0 saves them into a derived symbol
with none of its own) come from the save of KiCad.
"""

from ..parsers.lib_symbols import LibrarySymbol
from ..parsers.sexpr import Node, edited, property_atoms, quote
from .library_copy import escape_lib_id
from .library_symbol_fields import MANDATORY, RESERVED


def new_symbol_name(text: str) -> str:
    """The name of the new symbol, as DIALOG_LIB_NEW_SYMBOL::GetName gives it (after the
    check of FIELD_VALIDATOR, text_problem)."""
    return escape_lib_id(text).replace(" ", "_")


def _with_text(prop: Node, text: str) -> str:
    """The text of a (property ...) with an other field text."""
    atoms = property_atoms(prop)
    if atoms[1].value == text:
        return edited(prop, [])
    return edited(prop, [(atoms[1].start, atoms[1].end, quote(text))])


def derived_symbol(
    chain: list[LibrarySymbol],
    name: str,
    keep_footprint: bool,
    keep_datasheet: bool,
    transfer_user_fields: bool,
    keep_user_field_content: bool,
) -> str:
    """The text of the new symbol that extends chain[0] (the parent, with its parents).

    Args:
        chain: The parent and its parents (symbol_chain), the parent first.
        name: The name of the new symbol (new_symbol_name).
    """
    parent = chain[0]
    power = chain[-1].node.first("power") is not None
    mandatory: dict[str, Node] = {}
    user: list[Node] = []
    for prop in parent.node.children("property"):
        atoms = property_atoms(prop)
        if len(atoms) < 2 or atoms[0].value in RESERVED:
            continue
        canonical = next((m for m in MANDATORY if m.lower() == atoms[0].value.lower()), None)
        if canonical is None:
            user.append(prop)
        else:
            # The parser sets the field of each property: the last one stays.
            mandatory[canonical] = prop
    texts = []
    for field in MANDATORY:
        prop = mandatory.get(field)
        if prop is None:
            # KiCad gives the symbol a default field, as the parent has.
            continue
        text = property_atoms(prop)[1].value
        if field == "Value" and power:
            text = name
        elif (field == "Footprint" and not keep_footprint) or (
            field == "Datasheet" and not keep_datasheet
        ):
            text = ""
        texts.append(_with_text(prop, text))
    if transfer_user_fields:
        for prop in user:
            text = property_atoms(prop)[1].value if keep_user_field_content else ""
            texts.append(_with_text(prop, text))
    body = "\n".join(f"\t\t{text}" for text in texts)
    return f"\t(symbol {quote(name)} (extends {quote(parent.name)})\n{body}\n\t)"
