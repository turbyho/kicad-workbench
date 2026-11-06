"""File versions of the KiCad releases, and the files that this server reads and changes.

A file is read and changed with the rules of the KiCad version that saved it
(its (version N)), not with the rules of the installed KiCad. This server reads
and changes the files of KiCad 8 and later, up to the newest nightly build with
known rules. KiCad does not open a file of a newer version.

The file versions are dates (SEXPR_SCHEMATIC_FILE_VERSION,
SEXPR_SYMBOL_LIB_FILE_VERSION and SEXPR_BOARD_FILE_VERSION in the KiCad
source). All file formats use the same time line.
"""

import re
from dataclasses import dataclass

from ..errors import WorkbenchError
from .sexpr import Atom, Node, file_header, property_field


class KicadFileError(WorkbenchError):
    """KiCad cannot read the file, or this server has no rules for its version."""


@dataclass(frozen=True)
class Release:
    """The file versions that one KiCad release writes."""

    name: str
    schematic: int
    symbol_library: int
    board: int


RELEASES = (
    Release("8.0", 20231120, 20231120, 20240108),
    Release("9.0", 20250114, 20241209, 20241229),
    Release("10.0", 20260306, 20251024, 20260206),
)
KICAD8, KICAD9, KICAD10 = RELEASES
# The newest schematic version with known rules: a nightly build of KiCad 11
# (branch master, 2026-11-02).
NEWEST_SCHEMATIC = 20261025
# In a file before this version, "~" is an empty field value, pin name or pin
# number (parseProperty and parseSymbolPin of KiCad 10).
EMPTY_TEXT_MARKER = 20250318
# From this version, a schematic has variants: (variant ...) in the instances of
# a symbol (SEXPR_SCHEMATIC_FILE_VERSION 20250922, sch_file_versions.h).
SCHEMATIC_VARIANTS = 20250922
# From this version, KiCad writes hide, show_name and do_not_autoplace of a field
# in the property; before it, the hidden state is in the effects
# (SEXPR_SCHEMATIC_FILE_VERSION 20251028, "Updated properties formatting").
SCHEMATIC_FIELD_FLAGS = 20251028
# From this version, a symbol declares its body styles with (body_styles ...).
# Before it, KiCad finds the body styles from the drawings (parseLibSymbol).
DECLARED_BODY_STYLES = 20250827


def version_numbers(version: str) -> tuple[int, int] | None:
    """Return (major, minor) of a KiCad version, for example "10.0.6" (kicad-cli
    version), or None when the text has no major and minor number."""
    match = re.match(r"\s*(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def version_triple(version: str) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) of a KiCad version, for example "10.0.6", or None."""
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def kicad_release(version: str) -> Release | None:
    """Return the release of an installed KiCad version, for example "10.0.6".

    Returns:
        The release, or None for a nightly build (for example "10.99.0") or a
        version that is not in RELEASES.
    """
    numbers = version_numbers(version)
    if numbers is None:
        return None
    name = f"{numbers[0]}.{numbers[1]}"
    return next((release for release in RELEASES if release.name == name), None)


def _version_text(text: str | None) -> int | None:
    return int(text) if text and text.isascii() and text.isdigit() else None


def file_version(root: Node) -> int | None:
    """Return the (version N) of a parsed KiCad file, or None.

    KiCad reads the version only as the first item after the head
    (parseHeader). Else it uses the version of its own build.
    """
    items = root.items[1:]
    first = items[0] if items else None
    if not isinstance(first, Node) or first.head != "version":
        return None
    return _version_text(first.value(0))


def header_version(text: str, head: str) -> int | None:
    """Return the (version N) of the text of a KiCad file with the head, or None.

    Only the header is read (sexpr.file_header), thus a large file needs no
    parse.
    """
    header = file_header(text)
    return _version_text(header[1]) if header and header[0] == head else None


def check_schematic_version(version: int | None) -> int:
    """Return the version of a schematic if this server can read and change it.

    Raises:
        KicadFileError: The file has no version, or a version older than
            KiCad 8.0 or newer than NEWEST_SCHEMATIC.
    """
    if version is None:
        raise KicadFileError("The schematic has no file version, (version N).")
    if version < KICAD8.schematic:
        raise KicadFileError(
            f"The schematic has the file version {version}, which is older than the "
            f"version of KiCad 8.0 ({KICAD8.schematic}). This server reads and changes "
            "the files of KiCad 8 and later. Open the file in KiCad 8 or later and save it."
        )
    if version > NEWEST_SCHEMATIC:
        raise KicadFileError(
            f"The schematic has the file version {version}, which is newer than the "
            f"newest version that this server knows ({NEWEST_SCHEMATIC}). Update the server."
        )
    return version


def check_board_version(version: int | None) -> int:
    """Return the version of a board if the text reader of this server reads it:
    the board version of the releases KiCad 8, 9 and 10 (the text reader has no
    rules of a nightly build).

    Raises:
        KicadFileError: The board has no version, or not the version of KiCad 8.0,
            9.0 or 10.0.
    """
    if version is None:
        raise KicadFileError("The board has no file version, (version N).")
    if version < KICAD8.board:
        raise KicadFileError(
            f"The board has the file version {version}, which is older than the version of "
            f"KiCad 8.0 ({KICAD8.board}). Open the board in KiCad 8 or later and save it."
        )
    if version > KICAD10.board:
        raise KicadFileError(
            f"The board has the file version {version}, which is newer than the version of "
            f"KiCad 10.0 ({KICAD10.board}). The text reader of this server has no rules for "
            "it: use a Python with pcbnew of that KiCad."
        )
    if version not in {release.board for release in RELEASES}:
        raise KicadFileError(
            f"The board has the file version {version} of a nightly build, not of a release "
            "of KiCad 8, 9 or 10. The text reader of this server has no rules for it: use a "
            "Python with pcbnew of that KiCad, or save the board in a release."
        )
    return version


def schematic_version(root: Node) -> int:
    """Return the version of a parsed schematic (see check_schematic_version)."""
    return check_schematic_version(file_version(root))


def schematic_text_version(text: str) -> int:
    """Return the version of the text of a schematic (see check_schematic_version)."""
    return check_schematic_version(header_version(text, "kicad_sch"))


def file_text(text: str, version: int | None) -> str:
    """Return a field value, a pin name or a pin number of a file as KiCad reads it.

    "~" of a file before EMPTY_TEXT_MARKER is an empty text: the parser of KiCad
    10 reads it as empty (parseSchField, parseProperty, parseSymbolPin). KiCad 8
    and 9 keep "~" of a pin, but GetShownName and GetShownNumber give an empty
    text (the netlist uses them). The caller decides where KiCad 8 and 9 keep "~"
    (for example the (pin "N") that KiCad writes with GetNumber).

    Args:
        version: The file version, or None when it is not known (the text stays).
    """
    if text == "~" and version is not None and version < EMPTY_TEXT_MARKER:
        return ""
    return text


def is_release_schematic(version: int) -> bool:
    """Return True if a release writes this schematic version, False for a nightly build."""
    return any(release.schematic == version for release in RELEASES)


def release_of_schematic(version: int) -> Release:
    """Return the newest release whose schematic version is not newer than a version.

    A file of a nightly build gets the release before the build. Each build of
    that nightly series reads the files of this release.
    """
    return [release for release in RELEASES if release.schematic <= version][-1]


def release_of_symbol_library(version: int) -> Release:
    """Return the oldest release that writes a symbol library version or a newer one."""
    for release in RELEASES:
        if version <= release.symbol_library:
            return release
    return KICAD10


def _has_empty_text_marker(symbol: Node) -> bool:
    """Return True if a field value, a pin name or a pin number is "~"."""
    for prop in symbol.walk("property"):
        field = property_field(prop)
        if field is not None and field[1] == "~":
            return True
    for pin in symbol.walk("pin"):
        for head in ("name", "number"):
            node = pin.first(head)
            if node is not None and node.value(0) == "~":
                return True
    return False


_SUB_SYMBOL_STYLE = re.compile(r"_\d+_(\d+)\Z")


def _has_alternate_body_style(symbol: Node) -> bool:
    """Return True if a sub-symbol "<name>_<unit>_<style>" has a style after 1."""
    for sub in symbol.children("symbol"):
        match = _SUB_SYMBOL_STYLE.search(sub.value(0, ""))
        if match and int(match.group(1)) > 1:
            return True
    return False


def _has_bold_stroke_text(symbol: Node) -> bool:
    """Return True if a text is bold, has a line width and uses the stroke font.

    EDA_TEXT::MigrateLegacyBoldStrokeWidth changes only such a text.
    """
    for font in symbol.walk("font"):
        bold = font.first("bold")
        thickness = font.first("thickness")
        if bold is None or bold.value(0, "yes") != "yes" or font.first("face") is not None:
            continue
        if thickness is None:
            continue
        try:
            width = float(thickness.value(0, "0"))
        except ValueError:
            # Not a number: the result of the conversion is not known.
            return True
        if width > 0.000001:
            return True
    return False


def _has_bare_bar(symbol: Node) -> bool:
    """Return True if a bare token holds "|"."""
    return any(
        not item.quoted and "|" in item.text
        for node in symbol.walk()
        for item in node.items
        if isinstance(item, Atom)
    )


# The parser of KiCad reads a symbol (parseLibSymbol, parseProperty,
# parseSymbolPin, parseEDA_TEXT) with conversions for files before these
# versions (branch master, 2026-11-02). A symbol of a library is in the file
# format of the library. In a schematic, KiCad reads it with the version of the
# schematic. Thus a conversion between the two versions must not apply to the
# symbol. Each item: the version, the test for the symbol, the conversion.
_SYMBOL_CONVERSIONS = (
    (
        EMPTY_TEXT_MARKER,
        _has_empty_text_marker,
        'the text "~" of a field or a pin is an empty text',
    ),
    (
        DECLARED_BODY_STYLES,
        _has_alternate_body_style,
        "the body styles of a symbol come from its drawings",
    ),
    (20260826, _has_bold_stroke_text, "the line width of bold text has the old meaning"),
)
# The lexer reads "|" as a token (SetKnowsBar) in a symbol library from this
# version, and in a schematic from the second version.
_BAR_LIBRARY = 20240529
_BAR_SCHEMATIC = 20240620


def symbol_library_problem(symbol: Node, library: int | None, schematic: int) -> str | None:
    """Return why a symbol of a library cannot go into a schematic, or None.

    Args:
        symbol: The (symbol ...) of the library.
        library: The file version of the symbol library, or None.
        schematic: The file version of the schematic.
    """
    if library is None:
        return "The symbol library has no file version, (version N)."
    if library < KICAD8.symbol_library:
        return (
            f"The symbol library has the file version {library}, which is older than "
            f"the version of KiCad 8.0 ({KICAD8.symbol_library})."
        )
    if library > schematic:
        return (
            f"The symbol library has the file version {library}, which is newer than "
            f"the schematic ({schematic}). The KiCad version of the schematic cannot "
            "read the symbol."
        )
    reasons = [
        f"for a file version before {version}, {meaning}"
        for version, applies, meaning in _SYMBOL_CONVERSIONS
        if library < version <= schematic and applies(symbol)
    ]
    if (library >= _BAR_LIBRARY) != (schematic >= _BAR_SCHEMATIC) and _has_bare_bar(symbol):
        reasons.append('the character "|" in a bare token ends the token only in one file')
    if not reasons:
        return None
    return (
        f"The symbol library has the file version {library} and the schematic has "
        f"the file version {schematic}. KiCad reads the symbol in the schematic in a "
        f"different way: {'; '.join(reasons)}."
    )
