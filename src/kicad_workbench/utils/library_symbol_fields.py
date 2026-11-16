"""The properties of a library symbol, as the "Symbol Properties" dialog of the symbol
editor of KiCad 10.0.6 changes them (DIALOG_LIB_SYMBOL_PROPERTIES, FIELDS_GRID_TABLE).

The rules (tag 10.0.6):

- A field name: the grid trims it (FIELDS_GRID_TABLE::SetValue, a column that is not
  the value); FIELD_VALIDATOR refuses a carriage return, a line feed and a tab; an
  empty name of a field with text gives "Fields must have a name."; a name that an
  other field has gives "The name '%s' is already in use." (FieldNamesAreDuplicates:
  the same text, or a case variant of a mandatory name).
- A field text: FIELD_VALIDATOR refuses a carriage return, a line feed and a tab (the
  Reference also a space, an empty text, "${" and a text with no letter prefix of a
  root symbol: "References must start with a letter."). Then the grid gives
  ``SetText(UnescapeString(ConvertPathToFileUri(text)))`` (the grid of a library
  symbol has the parent type SCH_SYMBOL_T); SCH_FIELD::SetText strips the white space
  at the two ends of a mandatory field, and ignores the text of a generated field
  ("${NAME}").
- A new field (OnAddField): hidden, at (0, 0), the text size of the symbol editor
  settings (defaults.text_size, default 50 mils). The mandatory fields cannot be
  removed ("The first %d fields are mandatory.").
- A derived symbol: an edited field of a parent that the symbol does not have becomes
  a field of the symbol, with the attributes of the parent field.
- The keywords: SetKeyWords. The footprint filters: OnAddFootprintFilter drops an empty
  filter and changes spaces to "_"; the save writes them in "ki_fp_filters".
- A power symbol: at OK, the Value gets the name of the symbol, and the Footprint is
  read only.

The tool changes the text of the symbol, then KiCad saves the library (kicad-cli).
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InputError
from ..parsers.bus_names import unescape_string
from ..parsers.lib_symbols import LibrarySymbol
from ..parsers.sexpr import Node, edited, property_atoms, quote
from .kicad_text import read_kicad_json
from .library_tables import KicadEnvironment, expand

MANDATORY = ("Reference", "Value", "Footprint", "Datasheet", "Description")
# The property names that the parser reads as properties of the symbol, not as fields
# (SCH_IO_KICAD_SEXPR_PARSER::parseProperty): the tool refuses them as field names.
RESERVED = ("ki_keywords", "ki_description", "ki_fp_filters", "ki_locked")
# FIELD_VALIDATOR: the characters of each field (the Reference also a space).
_BAD_CHARS = {"\r": "carriage return", "\n": "line feed", "\t": "tab"}
_GENERATED = re.compile(r"\$\{\w*\}")
# wxString::Trim and Strip: ASCII white space (wxSafeIsspace).
_SPACE = " \t\n\r\v\f"
# DEFAULT_TEXT_SIZE (mils) and the size of the ki_ properties (SCH_FIELD default).
DEFAULT_TEXT_SIZE_MILS = 50


@dataclass
class FieldChanges:
    """The result: the new text of the symbol and a line for each change."""

    text: str
    lines: list[str] = field(default_factory=list)


def symbol_editor_text_size(config_dir: Path) -> tuple[float, str | None]:
    """The text size of a new field in mm: defaults.text_size of symbol_editor.json
    (SYMBOL_EDITOR_SETTINGS, PARAM<int>), in mils.

    Returns:
        (the size, a warning or None). A file that cannot be read, or a value that is
        not a number, gives the default of KiCad (50 mils) and a warning.
    """
    path = config_dir / "symbol_editor.json"
    if not path.is_file():
        return DEFAULT_TEXT_SIZE_MILS * 0.0254, None
    try:
        data = read_kicad_json(path)
    except (OSError, InputError) as e:
        return DEFAULT_TEXT_SIZE_MILS * 0.0254, (
            f"{path} cannot be read ({e}): a new field gets the default text size of KiCad "
            f"({DEFAULT_TEXT_SIZE_MILS} mils)."
        )
    defaults = data.get("defaults") if isinstance(data, dict) else None
    value = defaults.get("text_size") if isinstance(defaults, dict) else None
    if value is None:
        return DEFAULT_TEXT_SIZE_MILS * 0.0254, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TEXT_SIZE_MILS * 0.0254, (
            f"defaults.text_size of {path} is not a number: a new field gets the default "
            f"text size of KiCad ({DEFAULT_TEXT_SIZE_MILS} mils)."
        )
    return int(value) * 0.0254, None


def _bad_chars(text: str, reference: bool) -> list[str]:
    names = [name for char, name in _BAD_CHARS.items() if char in text]
    if reference and " " in text:
        names.append("space")
    return names


def name_problem(name: str) -> str | None:
    """The error of the dialog for a field name (after the trim), or None."""
    if not name:
        return "Fields must have a name."
    bad = _bad_chars(name, False)
    if bad:
        return f"The field cannot contain {', '.join(bad)} character(s)."
    if name in RESERVED:
        return (
            f"The field name '{name}' is a property of the symbol in the file format: the "
            "tool refuses it. Use the arguments keywords and footprint_filters."
        )
    if _GENERATED.fullmatch(name):
        return f"The field '{name}' is a generated field: KiCad does not let you set its text."
    return None


def text_problem(name: str, text: str, root: bool) -> str | None:
    """The error of FIELD_VALIDATOR for a field text, or None."""
    reference = name == "Reference"
    bad = _bad_chars(text, reference)
    if bad:
        what = "The reference designator" if reference else "The field"
        return f"{what} cannot contain {', '.join(bad)} character(s)."
    if reference:
        if not text:
            return "The value of the field cannot be empty."
        if "${" in text:
            return "The reference designator cannot contain text variable references"
        if root and not text.rstrip("?0123456789"):
            return "References must start with a letter."
    return None


def convert_path_to_file_uri(
    text: str, env: KicadEnvironment, project: dict[str, str], project_dir: Path
) -> str:
    """ConvertPathToFileUri: a text that looks like a path ("/", "${", "./", "../") to a
    file or a folder that exists gets "file://" before it."""
    if not text or text == "~":
        return text
    prefixes = ["/", "${", "./", "../"]
    if os.name == "nt":
        prefixes += ["\\\\", ".\\", "..\\"]
    drive = os.name == "nt" and len(text) >= 2 and text[0].isalpha() and text[1] == ":"
    if not (drive or any(text.startswith(prefix) for prefix in prefixes)):
        return text
    resolved = expand(text, env, project)
    if not os.path.isabs(resolved):
        resolved = os.path.join(project_dir, resolved)
    unc = resolved.startswith("\\\\")
    if not unc and not os.path.exists(resolved):
        return text
    if os.name == "nt" and text.startswith("\\\\"):
        return "file://" + text[2:].replace("\\", "/")
    if drive:
        return "file:///" + text.replace("\\", "/")
    return "file://" + text.replace("\\", "/") if os.name == "nt" else "file://" + text


def field_text(
    name: str, text: str, env: KicadEnvironment, project: dict[str, str], project_dir: Path
) -> str:
    """The text that the grid gives a field (SetValue, SCH_FIELD::SetText)."""
    value = unescape_string(convert_path_to_file_uri(text, env, project, project_dir))
    return value.strip(_SPACE) if name in MANDATORY else value


def same_name(a: str, b: str) -> bool:
    """FieldNamesAreDuplicates."""
    if a == b:
        return True
    if a.lower() != b.lower():
        return False
    return any(a.lower() == mandatory.lower() for mandatory in MANDATORY)


def _fields(symbol: Node) -> list[tuple[str, Node]]:
    found = []
    for prop in symbol.children("property"):
        atoms = property_atoms(prop)
        if atoms:
            found.append((atoms[0].value, prop))
    return found


def _find(fields: list[tuple[str, Node]], name: str) -> Node | None:
    return next((prop for prop_name, prop in fields if same_name(prop_name, name)), None)


def _canonical(name: str) -> str:
    return next((m for m in MANDATORY if m.lower() == name.lower()), name)


def _property(name: str, text: str, size: float, hidden: bool = True) -> str:
    size_text = f"{size:.6f}".rstrip("0").rstrip(".")
    hide = " (hide yes)" if hidden else ""
    return (
        f"(property {quote(name)} {quote(text)} (at 0 0 0){hide} "
        f"(effects (font (size {size_text} {size_text}))))"
    )


def change_fields(
    chain: list[LibrarySymbol],
    changes: dict[str, str | None],
    keywords: str | None,
    footprint_filters: list[str] | None,
    env: KicadEnvironment,
    project: dict[str, str],
    project_dir: Path,
    text_size: float,
) -> FieldChanges:
    """The new text of the symbol chain[0] with the changes of the dialog.

    Raises:
        InputError: A change that the dialog refuses.
    """
    symbol = chain[0].node
    root = len(chain) == 1
    # LIB_SYMBOL::IsPower: the flag of the root symbol.
    power = chain[-1].node.first("power") is not None
    own = _fields(symbol)
    edits: list[tuple[int, int, str]] = []
    added: list[str] = []
    lines: list[str] = []
    seen: list[str] = []
    for raw_name, raw_text in changes.items():
        # FIELD_VALIDATOR checks the text of the cell; the grid trims it after.
        if bad := _bad_chars(raw_name, False):
            raise InputError(f"The field cannot contain {', '.join(bad)} character(s).")
        name = _canonical(raw_name.strip(_SPACE))
        if problem := name_problem(name):
            raise InputError(problem)
        if any(same_name(name, other) for other in seen):
            raise InputError(f"The name '{name}' is already in use.")
        seen.append(name)
        prop = _find(own, name)
        if raw_text is None:
            if name in MANDATORY:
                raise InputError(
                    f"The first {len(MANDATORY)} fields are mandatory: '{name}' cannot be removed."
                )
            if prop is None:
                inherited = any(_find(_fields(parent.node), name) for parent in chain[1:])
                if inherited:
                    raise InputError(
                        f"The field '{name}' comes from the parent of the symbol: the delete of "
                        "the dialog gives the field of the parent again (no change)."
                    )
                raise InputError(f"The symbol has no field '{name}' to remove.")
            edits.append((prop.start, prop.end, ""))
            lines.append(f"- Removed: {name}")
            continue
        if power and name in ("Value", "Footprint"):
            raise InputError(
                f"The {name} of a power symbol cannot be set: the dialog gives the Value the "
                "name of the symbol, and the Footprint is read only."
            )
        if problem := text_problem(name, raw_text, root):
            raise InputError(problem)
        text = field_text(name, raw_text, env, project, project_dir)
        if prop is not None:
            atoms = property_atoms(prop)
            old = atoms[1].value if len(atoms) > 1 else ""
            if old == text:
                continue
            if len(atoms) > 1:
                edits.append((atoms[1].start, atoms[1].end, quote(text)))
            else:
                edits.append((atoms[0].end, atoms[0].end, " " + quote(text)))
            lines.append(f"- {name}: {old!r} -> {text!r}")
            continue
        if name in MANDATORY:
            # KiCad saves the five mandatory fields of each symbol; a file with no such
            # property was not saved by KiCad.
            raise InputError(
                f"The symbol has no '{name}' property in its text. Save the library in KiCad "
                "first (KiCad writes each mandatory field)."
            )
        parent_prop = next(
            (p for parent in chain[1:] if (p := _find(_fields(parent.node), name)) is not None),
            None,
        )
        if parent_prop is not None and name not in MANDATORY:
            # An edited field of a parent becomes a field of the derived symbol, with
            # the attributes of the parent field.
            atoms = property_atoms(parent_prop)
            parent_edits = [(atoms[1].start, atoms[1].end, quote(text))] if len(atoms) > 1 else []
            added.append(edited(parent_prop, parent_edits))
            lines.append(f"- {name}: a field of the symbol now (from its parent) -> {text!r}")
            continue
        added.append(_property(name, text, text_size))
        lines.append(f"- New field {name} (hidden): {text!r}")
    for key, value, label in (
        ("ki_keywords", keywords, "Keywords"),
        (
            "ki_fp_filters",
            None if footprint_filters is None else _filters(footprint_filters),
            "Footprint filters",
        ),
    ):
        if value is None:
            continue
        if any(char in value for char in "\r\n"):
            raise InputError(f"The {label.lower()} cannot contain a line feed.")
        if key == "ki_fp_filters" and any(char in value for char in "\t\v\f"):
            # The parser splits ki_fp_filters at " \t\r\n": a tab makes two filters.
            raise InputError("A footprint filter cannot contain a tab.")
        prop = _find(own, key)
        old = (
            property_atoms(prop)[1].value
            if prop is not None and len(property_atoms(prop)) > 1
            else ""
        )
        if old == value:
            continue
        if prop is not None:
            edits.append((prop.start, prop.end, ""))
        if value:
            added.append(_property(key, value, 1.27))
        lines.append(f"- {label}: {old!r} -> {value!r}")
    if power:
        # TransferDataFromWindow: the Value of a power symbol gets the name of the symbol.
        value_prop = _find(own, "Value")
        name_value = chain[0].name
        if value_prop is not None:
            atoms = property_atoms(value_prop)
            if len(atoms) > 1 and atoms[1].value != name_value:
                edits.append((atoms[1].start, atoms[1].end, quote(name_value)))
                lines.append(f"- Value: the name of the power symbol, {name_value!r}")
    if added:
        props = list(symbol.children("property"))
        subs = list(symbol.children("symbol"))
        if props:
            at = max(prop.end for prop in props)
        elif subs:
            at = subs[0].start
        else:
            at = symbol.end - 1
        edits.append((at, at, "\n" + "\n".join(added) + "\n"))
    edits.sort(key=lambda edit: (edit[0], edit[1]))
    return FieldChanges(edited(symbol, edits), lines)


def _filters(filters: list[str]) -> str:
    """OnAddFootprintFilter: no empty filter (IsEmpty, with no trim), spaces as "_"; the
    save joins them with a space (ki_fp_filters)."""
    return " ".join(text.replace(" ", "_") for text in filters if text)
