"""The rules of "Update Symbols from Library" of KiCad 10.0.6 (DIALOG_CHANGE_SYMBOLS in the
mode UPDATE, checked in the source; plans/roadmap.md, "Step 10: plan"), and the fields of
a placed symbol as SCH_IO_KICAD_SEXPR reads and writes them:

- WildCompareString: "*" is any text, "?" one character; with no case, the two texts in
  upper case (wxString::MakeUpper).
- UTIL::GetRefDesPrefix removes the digits and the "?" at the end of a reference.
  UTIL::GetRefDesNumber reads the text from the first digit as a number (wxString::ToLong:
  the full text), else -1. UTIL::GetRefDesUnannotated is the prefix and "?".
- The text attributes of a field (parseEDA_TEXT: font face, size, thickness, bold,
  italic, color, line spacing, justify; mirror is not read for schematic text) and their
  text (EDA_TEXT::Format). SCH_IO_KICAD_SEXPR::saveField writes the effects when the
  formatting is not the default (EDA_TEXT::IsDefaultFormatting) or the text height is not
  50 mils. A field allows more lines (TEXT_ATTRIBUTES::m_Multiline is true, and SCH_FIELD
  does not change it), thus its formatting is never the default: KiCad writes the
  effects of each field.
- A field of a placed symbol (parseSchField, saveField): the private flag, the name, the
  text, (at x y angle), (hide yes) when hidden, (show_name ...), (do_not_autoplace ...),
  and the effects with the hyperlink last.
"""

from dataclasses import dataclass, replace

from ..errors import InputError
from ..parsers.annotation import ref_des_prefix, ref_des_unannotated
from ..parsers.c_numbers import atoi, to_int, to_long
from ..parsers.sch_geometry import IU_PER_MIL, iu, mm_text
from ..parsers.sexpr import (
    Atom,
    Node,
    indent,
    parse_bool,
    parse_maybe_absent_bool,
    property_atoms,
    quote,
)

# DEFAULT_SIZE_TEXT of eda_text.h: 50 mils.
DEFAULT_TEXT_SIZE = 50 * IU_PER_MIL


def _upper(text: str) -> str:
    """wxString::MakeUpper: each character in upper case (one character for each)."""
    return "".join(u if len(u := char.upper()) == 1 else char for char in text)


def wild_compare(pattern: str, text: str) -> bool:
    """WildCompareString with case_sensitive false (string_utils.cpp)."""
    wild, string = _upper(pattern), _upper(text)
    w = s = 0
    mp = cp = 0
    while s < len(string) and (w >= len(wild) or wild[w] != "*"):
        if w >= len(wild) or (wild[w] != string[s] and wild[w] != "?"):
            return False
        w += 1
        s += 1
    while s < len(string):
        if w < len(wild) and wild[w] == "*":
            w += 1
            if w >= len(wild):
                return True
            mp, cp = w, s + 1
        elif w < len(wild) and (wild[w] == string[s] or wild[w] == "?"):
            w += 1
            s += 1
        else:
            w, s = mp, cp
            cp += 1
    while w < len(wild) and wild[w] == "*":
        w += 1
    return w >= len(wild)


def refdes_number(reference: str) -> int:
    """UTIL::GetRefDesNumber: the number from the first digit, or -1 when the text from
    the first digit is not a number (wxString::ToLong reads the full text)."""
    first = next((i for i, char in enumerate(reference) if char in "0123456789"), None)
    if first is None:
        return -1
    # wxString::ToLong of the text from the first digit (false: -1), static_cast<int>.
    value = to_long(reference[first:])
    return -1 if value is None else to_int(value)


def reset_reference(library_text: str, reference: str) -> str:
    """The reference of an instance after "Update/reset field text" of the Reference."""
    prefix = ref_des_prefix(library_text)
    number = refdes_number(reference)
    return f"{prefix}{number}" if number >= 0 else ref_des_unannotated(prefix)


@dataclass(frozen=True)
class TextAttributes:
    """The TEXT_ATTRIBUTES of a field that EDA_TEXT::SetAttributes copies (not the
    visibility, which SCH_FIELD keeps on its own)."""

    face: str = ""
    height: int = DEFAULT_TEXT_SIZE
    width: int = DEFAULT_TEXT_SIZE
    thickness: int = 0  # 0: the automatic thickness (GetAutoThickness)
    bold: bool = False
    italic: bool = False
    # r, g, b (parseInt), the alpha text (FormatDouble2Str); None: COLOR4D::UNSPECIFIED
    color: tuple[int, int, int, str] | None = None
    line_spacing: str | None = None  # the text (FormatDouble2Str) of a spacing that is not 1
    horizontal: str = ""  # "", "left" or "right"
    vertical: str = ""  # "", "top" or "bottom"


def format_double(value: float) -> str:
    """FormatDouble2Str: 16 decimals with no zeros at the end for a value near 0, else
    10 significant digits ({:.10g})."""
    if value != 0.0 and abs(value) <= 0.0001:
        text = f"{value:.16f}".rstrip("0")
        return text[:-1] if text.endswith(".") else text
    return f"{value:.10g}"


def format_angle(degrees: float) -> str:
    """EDA_UNIT_UTILS::FormatAngle: {:.10g} of the degrees."""
    return f"{degrees:.10g}"


def _number(node: Node, index: int, where: str) -> float:
    """A number that the parser of KiCad needs (NeedNUMBER, parseDouble).

    Raises:
        InputError: The number is missing, or it is not a number that KiCad reads.
    """
    if node.value(index) is None:
        raise InputError(f"The ({node.head} ...) of {where} has no number: KiCad cannot read it.")
    value = node.number(index)
    assert value is not None
    return value


def read_effects(effects: Node | None, where: str) -> tuple[TextAttributes, bool | None, str]:
    """The text attributes of an (effects ...) as parseEDA_TEXT reads them.

    Returns:
        (the attributes, the visibility of a hide in the effects or None, the hyperlink).

    Raises:
        InputError: A value is not a number, or not yes or no.
    """
    attributes = TextAttributes()
    if effects is None:
        return attributes, None, ""
    hidden: bool | None = None
    href = ""
    values: dict = {}
    for item in effects.items[1:]:
        if isinstance(item, Atom):
            if item.text == "hide":
                hidden = True
            continue
        if item.head == "font":
            for part in item.items[1:]:
                if isinstance(part, Atom):
                    # A bare bold or italic of an older file (parseMaybeAbsentBool).
                    if part.text in ("bold", "italic"):
                        values[part.text] = True
                    continue
                if part.head == "face":
                    values["face"] = part.value(0, "")
                elif part.head == "size":
                    values["height"] = iu(_number(part, 0, where))
                    values["width"] = iu(_number(part, 1, where))
                elif part.head == "thickness":
                    values["thickness"] = iu(_number(part, 0, where))
                elif part.head in ("bold", "italic"):
                    values[part.head] = parse_maybe_absent_bool(part, where)
                elif part.head == "color":
                    # parseInt (strtol) of each color, and the alpha in 0 to 1.
                    for i in range(3):
                        _number(part, i, where)
                    r, g, b = (atoi(part.value(i, "")) for i in range(3))
                    alpha = min(max(_number(part, 3, where), 0.0), 1.0)
                    unspecified = (r, g, b, alpha) == (0, 0, 0, 0.0)
                    values["color"] = None if unspecified else (r, g, b, format_double(alpha))
                elif part.head == "line_spacing":
                    spacing = _number(part, 0, where)
                    values["line_spacing"] = None if spacing == 1.0 else format_double(spacing)
        elif item.head == "justify":
            for atom in item.atoms():
                if atom.text in ("left", "right"):
                    values["horizontal"] = atom.text
                elif atom.text in ("top", "bottom"):
                    values["vertical"] = atom.text
        elif item.head == "href":
            href = item.value(0, "")
        elif item.head == "hide":
            hidden = parse_maybe_absent_bool(item, where)
    return replace(attributes, **values), hidden, href


def effects_text(attributes: TextAttributes, href: str) -> str:
    """The (effects ...) of EDA_TEXT::Format, one list on each line."""
    font = []
    if attributes.face:
        font.append(f"(face {quote(attributes.face)})")
    font.append(f"(size {mm_text(attributes.height)} {mm_text(attributes.width)})")
    if attributes.line_spacing is not None:
        font.append(f"(line_spacing {attributes.line_spacing})")
    if attributes.thickness:
        font.append(f"(thickness {mm_text(attributes.thickness)})")
    if attributes.bold:
        font.append("(bold yes)")
    if attributes.italic:
        font.append("(italic yes)")
    if attributes.color is not None:
        r, g, b, alpha = attributes.color
        font.append(f"(color {r} {g} {b} {alpha})")
    parts = ["(font\n" + "\n".join(font) + "\n)"]
    justify = " ".join(j for j in (attributes.horizontal, attributes.vertical) if j)
    if justify:
        parts.append(f"(justify {justify})")
    if href:
        parts.append(f"(href {quote(href)})")
    return "(effects\n" + "\n".join(parts) + "\n)"


@dataclass(frozen=True)
class FieldState:
    """A field of a placed symbol as KiCad has it after the load."""

    name: str  # the name of the file
    text: str  # the text of the file (the atom value)
    x: int  # the position in IU, the Y axis of the sheet (down)
    y: int
    angle: str  # the text of the angle (FormatAngle)
    visible: bool
    show_name: bool
    can_autoplace: bool
    attributes: TextAttributes
    href: str
    private: bool = False


def read_field(prop: Node, where: str, library: bool = False) -> FieldState:
    """A (property ...) as parseSchField (a placed symbol) or parseProperty (a library
    symbol, library=True: its Y axis is up) reads it. The last token of a kind stays.

    Raises:
        InputError: A value is not a number, or not yes or no.
    """
    atoms = prop.atoms()
    private = bool(atoms) and not atoms[0].quoted and atoms[0].text == "private"
    named = property_atoms(prop)
    name = named[0].value if named else ""
    text = named[1].value if len(named) > 1 else ""
    x = y = 0
    angle = "0"
    visible = True
    show_name = False
    can_autoplace = True
    attributes, href = TextAttributes(), ""
    for item in prop.items[1:]:
        if not isinstance(item, Node):
            continue
        if item.head == "at":
            x, y = iu(_number(item, 0, where)), iu(_number(item, 1, where))
            if library:
                y = -y
            angle = format_angle(_number(item, 2, where))
        elif item.head == "hide":
            visible = not parse_bool(item, where)
        elif item.head == "show_name":
            show_name = parse_maybe_absent_bool(item, where)
        elif item.head == "do_not_autoplace":
            can_autoplace = not parse_maybe_absent_bool(item, where)
        elif item.head == "effects":
            attributes, hidden, href = read_effects(item, where)
            if hidden is not None:
                visible = not hidden
    return FieldState(
        name, text, x, y, angle, visible, show_name, can_autoplace, attributes, href, private
    )


def field_text(field: FieldState, depth: int) -> str:
    """The (property ...) of a placed symbol as SCH_IO_KICAD_SEXPR::saveField writes it.

    Args:
        depth: The number of tabs before the (property ...): the lines after the first
            get one more tab for each list that holds them.
    """
    private = "private " if field.private else ""
    lines = [
        f"(property {private}{quote(field.name)} {quote(field.text)}",
        f"(at {mm_text(field.x)} {mm_text(field.y)} {field.angle})",
    ]
    if not field.visible:
        lines.append("(hide yes)")
    lines.append(f"(show_name {'yes' if field.show_name else 'no'})")
    lines.append(f"(do_not_autoplace {'no' if field.can_autoplace else 'yes'})")
    # saveField: the formatting of a field is never the default (m_Multiline).
    lines.append(effects_text(field.attributes, field.href))
    lines.append(")")
    return indent("\n".join(lines), depth).lstrip("\t")
