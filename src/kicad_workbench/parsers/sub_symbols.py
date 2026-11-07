"""The names of the sub-symbols of a library symbol, as the parser of KiCad reads them.

A library symbol (of a library file, or of the lib_symbols section of a schematic)
keeps the items of each unit and body style in a sub-symbol with the name
"<name>_<unit>_<body style>" (SCH_IO_KICAD_SEXPR_PARSER::parseLibSymbol).
"""

import functools

from ..errors import KicadParseError
from .c_numbers import to_int, to_long
from .sexpr import Node


def symbol_name(text: str) -> str:
    """Return the name of a library symbol as KiCad reads it.

    "{slash}" is "/", and a library nickname before ":" is not part of the
    name (LIB_ID::Parse).
    """
    return text.replace("{slash}", "/").split(":", 1)[-1]


# A pure function of two texts: the tools read the same names many times (the units
# of each placed symbol). An error is not kept (lru_cache keeps only a result).
@functools.lru_cache(maxsize=4096)
def sub_symbol_unit(symbol: str, name: str) -> tuple[int, int]:
    """Return (unit, body style) of a sub-symbol name, as parseLibSymbol of KiCad 8, 9,
    10 and branch master reads it.

    The name must start with the item name of the symbol (LIB_ID::GetLibItemName).
    The parser skips one more character (usually "_"), splits the rest at "_"
    (wxStringTokenizer: the empty tokens at the start and in the middle count, the
    "_" at the end give no token) into exactly two tokens, and reads each one with
    wxString::ToLong into an int.

    Args:
        symbol: The item name of the symbol (symbol_name).
        name: The name of the sub-symbol, "<name>_<unit>_<body style>".

    Raises:
        KicadParseError: KiCad cannot read the name. The message has the text of
            KiCad: the name for the prefix, else the rest after the prefix.
        LocaleNumberError: The number has a white space that is not ASCII:
            KiCad reads it as a number only in some locales.
    """
    text = name.replace("{slash}", "/")
    where = f'(the sub-symbol "{name}" of the symbol "{symbol}")'
    if not text.startswith(symbol):
        raise KicadParseError(f"Invalid symbol unit name prefix {text} {where}.")
    # wxString::Right(len - symbol - 1): the full text when the count is negative.
    count = len(text) - len(symbol) - 1
    rest = text if count < 0 else text[len(text) - count :]
    # wxTOKEN_RET_EMPTY: no token after the last character that is not "_"
    # (measured: "2_1__" gives two tokens, "2__1" and "_2_1" three).
    tokens = rest.split("_")
    while tokens and tokens[-1] == "":
        tokens.pop()
    if len(tokens) != 2:
        raise KicadParseError(
            f"Invalid symbol unit name suffix {rest} {where}: the name must be "
            f'"{symbol}_<unit>_<body style>".'
        )
    unit, style = (to_long(token) for token in tokens)
    if unit is None:
        raise KicadParseError(f"Invalid symbol unit number {rest} {where}.")
    if style is None:
        raise KicadParseError(f"Invalid symbol body style number {rest} {where}.")
    return to_int(unit), to_int(style)


def sub_symbols(symbol: Node) -> list[tuple[int, int, Node]]:
    """Return (unit, body style, sub-symbol) for each sub-symbol of a library symbol
    (a symbol of a library file, or of the lib_symbols section of a schematic).

    Raises:
        KicadParseError: KiCad cannot read the name of a sub-symbol.
        LocaleNumberError: A number of a name depends on the locale of KiCad.
    """
    name = symbol_name(symbol.value(0, ""))
    return [(*sub_symbol_unit(name, sub.value(0, "")), sub) for sub in symbol.children("symbol")]
