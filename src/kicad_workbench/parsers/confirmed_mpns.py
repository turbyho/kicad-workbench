"""The model of the confirmed MPNs: the check of the fields Value, MPN and Value_Confirmed.

This module is a port of the model of datasheet-lens (same author, MIT,
src/datasheet_lens/project.py: split_value, _list_items, field_list,
format_mpns, symbol_state and the messages of check_parts). The model is in the
repository of datasheet-lens, plans/confirmed-mpns.md (decisions 13 to 24, user,
2026-11-05). datasheet-lens does the check in project_check when it is
available. This module is the fallback for the users with no datasheet-lens
(decision 8). The two must give the same results: a change of the model goes
into the two projects, with the same tests.

The convention:

- An MPN in Value is only the text in one pair of parentheses:
  "2N7002 (2N7002LT1G, BSS138LT1G)", the fitted part first. The separators are
  "," and ";". An MPN with ",", ";" or a parenthesis is in quotes, as in CSV.
  All other text of Value is the general value.
- The field MPN is the confirmed copy of these MPNs. The hidden field
  Value_Confirmed is a copy of the confirmed Value. A difference is a change
  that the user confirms.
- An MPN only in the field MPN gives the fields that complete the symbol by the
  convention.
"""

import re
from dataclasses import dataclass, field

from .unit_fields import CONFIRMED_FIELD, field_key, mpn_order

# The text of KiCad in a BOM line of more symbols or units with different values of
# a field (INDETERMINATE_STATE, include/widgets/ui_common.h; FIELDS_EDITOR_GRID_DATA_MODEL
# in eeschema/fields_data_model.cpp, KiCad 8, 9 and 10). A KiCad in an other language
# writes its translation.
MIXED_VALUES = "-- mixed values --"


def _filled(value: str) -> str:
    """The value with no outer spaces, or "" for an empty value of KiCad ("~")."""
    value = value.strip()
    return "" if value == "~" else value


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.lower())


def _is_field_mpn(item: str) -> bool:
    """True if an item of a list of MPNs is an MPN: the user writes the list, thus
    each text, also with no digit (PJ-SMD; user, 2026-11-05). Not an MPN:
    MIXED_VALUES."""
    return item.strip().lower() != MIXED_VALUES


def _unique(items: list[str]) -> list[str]:
    """The items in their order, each MPN one time (no regard to signs and case)."""
    seen: set[str] = set()
    return [i for i in items if not (_norm(i) in seen or seen.add(_norm(i)))]


# A list of MPNs, in the parentheses of Value and in the field MPN
# (plans/confirmed-mpns.md, decisions 13, 14 and 23). The separators are "," and ";":
# KiCad 9 and 10 join the different values of a field of the units of one symbol
# with "," (fields_data_model.cpp). Not a slash: it is part of many MPNs
# (LM358DR/NOPB). An item in quotes can have each character ("" is a quote, as in
# CSV); a quote has this function only at the start of an item.
MPN_SEPARATORS = ",;"
# A comma with no space before three digits can be a separator or a part of the MPN:
# the packing code of NXP and Nexperia (BC847B,215, 74HC595D,118). After the digits:
# a separator, the end of the list, or the closing parenthesis of Value.
PACKING_CODE = r"(?<=[A-Za-z0-9]),\d{{3}}\s*(?:[,;{closing}]|$)"
LIST_QUOTE_HINT = 'write an MPN with , ; or a parenthesis in quotes, for example "BC847B,215"'
NOT_CLEAR = "a comma before three digits can be a separator or part of the MPN: " + LIST_QUOTE_HINT


def _list_items(text: str, start: int = 0, closing: str = "") -> tuple[list[str], int, str]:
    """The items of a list of MPNs in text, from start. closing: ")" for the
    parentheses of Value (the list ends at it; a parenthesis in an MPN needs quotes),
    empty for the field MPN (the list ends at the end of the text). Give the items,
    the index after the list, and an error text (empty: no error).

    A comma of a packing code (PACKING_CODE) in a text with no quotes: when each
    separator of the list is such a comma, the list is one MPN (BC847B,215).
    Else the list is not clear (NOT_CLEAR): the item is the full text of the list
    (no MPN is lost, and the tool does not guess), and the user writes the MPN in
    quotes. The spaces around an item do not count, also in quotes."""
    stops = MPN_SEPARATORS + (closing + "(" if closing else "")
    packing = re.compile(PACKING_CODE.format(closing=re.escape(closing)))
    items: list[str] = []
    separators: list[bool] = []  # for each separator: a comma of a packing code
    quoted = False
    i, n = start, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == '"':
            quoted, out, i = True, [], i + 1
            while True:
                if i >= n:
                    return items, i, "a quote that is not closed"
                if text[i] == '"' and text[i + 1 : i + 2] == '"':
                    out.append('"')
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    out.append(text[i])
                    i += 1
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] not in MPN_SEPARATORS + closing:
                return items, i, "a text after a quote: the quotes must be around the full MPN"
            items.append("".join(out).strip())
        else:
            j = i
            while j < n and text[j] not in stops:
                j += 1
            if closing and j < n and text[j] == "(":
                return items, j, "a parenthesis in an MPN: " + LIST_QUOTE_HINT
            items.append(_filled(text[i:j]))
            i = j
        if i >= n:
            if closing:
                return items, i, "a parenthesis that is not closed"
            break
        if text[i] == closing:
            i += 1
            break
        separators.append(text[i] == "," and bool(packing.match(text, i)))
        i += 1
    if any(separators):
        body = [_filled(text[start : i - 1 if closing else i])]
        if all(separators) and not quoted:  # one MPN with a packing code: BC847B,215
            return body, i, ""
        return body, i, NOT_CLEAR
    return [item for item in items if item], i, ""


def field_list(text: str) -> tuple[list[str], str]:
    """The items of the field MPN, in their order, and an error text (see
    _list_items). "~" is an empty field (KiCad). A list that is not clear is one
    item, the full text: no MPN is lost, and the tool does not guess the items."""
    items, _, error = _list_items(_filled(text))
    return ([_filled(text)] if error else items), error


def field_items(text: str) -> list[str]:
    """The items of the field MPN (see field_list), also when the list is not clear."""
    return field_list(text)[0]


def format_mpns(items: list[str], value: bool = False) -> str:
    """The text of a list of MPNs, with quotes only where they are necessary (see
    _list_items). value: for the parentheses of Value, where a parenthesis in an
    MPN needs quotes too. The text must give the items again: else the tool writes
    quotes around each item (also "~", which is an empty field with no quotes)."""

    def reads_as(text: str) -> bool:
        if value:
            parts = split_value(f"({text})")
            return parts.mpns == items and not parts.error
        return field_list(text) == (items, "")

    def quoted(item: str) -> str:
        return '"' + item.replace('"', '""') + '"'

    special = MPN_SEPARATORS + ("()" if value else "")
    for text in (
        ", ".join(items),
        ", ".join(
            quoted(i) if any(c in i for c in special) or i.startswith('"') else i for i in items
        ),
    ):
        if reads_as(text):
            return text
    return ", ".join(quoted(i) for i in items)


# The texts of a list of MPNs that mean "no part": the symbol needs no datasheet (a
# generic resistor, a test point). Not a conflict. TBD (to be determined) is an MPN
# that is not selected yet: an empty field.
NO_PART = ("n/a", "na", "-", "--", "none")
NOT_SELECTED = ("tbd",)


def mpn_items(text: str) -> list[str]:
    """The items of a list of MPNs with no "no part" texts (NO_PART) and no "not
    selected" texts (NOT_SELECTED)."""
    return [i for i in field_items(text) if i.lower() not in NO_PART + NOT_SELECTED]


@dataclass
class ValueParts:
    """A Value by the convention of the parentheses (plans/confirmed-mpns.md,
    decision 13)."""

    general: str  # the text out of the parentheses, with one space between words
    mpns: list[str] | None  # the items in the parentheses (no TBD); None: no parentheses
    error: str = ""  # the text does not follow the convention: a conflict


def split_value(text: str) -> ValueParts:
    """The general value and the MPNs of a Value: "2N7002 (2N7002LT1G, BSS138LT1G)"
    or "100nF 10V (GRM155R71A104KA01D)". One text in parentheses holds the MPNs, the
    fitted part first; an MPN in quotes can have each character. More texts in
    parentheses, a parenthesis that is not closed, or a list that is not clear is
    an error. A list that is not clear is one item, the full text of the
    parentheses (no MPN is lost); after an other error, the full text is the
    general value."""
    text = _filled(text)
    start = text.find("(")
    if start < 0 and ")" not in text:
        return ValueParts(" ".join(text.split()), None)
    error, items, end = "", None, 0
    if ")" in text[: max(start, 0)] or start < 0:
        error = "a parenthesis that is not opened"
    else:
        items, end, error = _list_items(text, start + 1, ")")
        if not error and "(" in text[end:]:
            error = "more than one text in parentheses"
        elif not error and ")" in text[end:]:
            error = "a parenthesis that is not opened"
        if error and error != NOT_CLEAR:
            items = None  # the MPNs are not known
    message = (
        (
            f"Value {text!r}: {error}. Write the MPNs in one text in parentheses, for example "
            "'2N7002 (2N7002LT1G, BSS138LT1G)'"
        )
        if error
        else ""
    )
    if items is None:
        return ValueParts(" ".join(text.split()), None, message)
    items = [i for i in items if i.lower() not in NOT_SELECTED]  # TBD: an empty field
    # A list that is not clear (NOT_CLEAR) is one item, the full text: no MPN is lost.
    return ValueParts(" ".join((text[:start] + " " + text[end:]).split()), items, message)


def _value_key(text: str) -> tuple[str, tuple[str, ...] | None]:
    """A Value for a comparison (two Value columns, Value and Value_Confirmed): the
    words of the general value and the list of the MPNs (decision 15). The spaces
    between words, the separators of the list and the quotes that are not necessary
    do not count. The words and the case count: 10 k is not 10k, and 4m7 (milliohm)
    is not 4M7 (megaohm)."""
    parts = split_value(text)
    return parts.general, None if parts.mpns is None else tuple(parts.mpns)


def _mpns(items: list[str]) -> list[str]:
    """The MPNs of the items of a list: no "no part", "not selected" and KiCad texts."""
    return [i for i in items if i.lower() not in NO_PART + NOT_SELECTED and _is_field_mpn(i)]


def _no_part(items: list[str]) -> bool:
    """True if a list of MPNs tells that the symbol needs no part: a NO_PART text and
    no MPN."""
    return not _mpns(items) and any(i.lower() in NO_PART for i in items)


@dataclass
class CadSymbol:
    """The fields of one line of the BOM of kicad-cli (one reference)."""

    reference: str = ""
    value: str = ""  # a general value, and the MPNs in parentheses
    mpn: str = ""  # the field MPN: the confirmed copy of the MPNs in the parentheses of Value
    # The hidden field Value_Confirmed: the Value of the last confirmation. None: the
    # BOM has no column Value_Confirmed (no symbol of the project has the field).
    value_confirmed: str | None = None
    other_mpns: list[tuple[str, str]] = field(default_factory=list)  # (name, text)
    other_values: list[str] = field(default_factory=list)  # the texts of more Value columns
    conflict: str = ""  # a conflict that the BOM reader found (columns of one role)
    # The name of a variant of KiCad 10, or empty for the default variant. A variant
    # can set an other Value, MPN or DNP.
    variant: str = ""
    # The fields that a write uses (see write_fields): the MPN fields with the MPN of
    # the line (for example "Manufacturer Part Number"; more fields hold the same MPNs),
    # and the fields Value_Confirmed. known False: the line has no value and the project
    # has more names of the role; the fields are the choices, and the user selects the
    # field of the symbol.
    mpn_fields: list[str] = field(default_factory=lambda: ["MPN"])
    mpn_known: bool = True
    confirmed_fields: list[str] = field(default_factory=lambda: [CONFIRMED_FIELD])
    confirmed_known: bool = True
    # The conflicts of the units of one package with different fields (not in
    # datasheet-lens, which cannot see the units in a BOM line): each one is a conflict
    # of its own, thus a variant line does not repeat one of the default line.
    unit_conflicts: list[str] = field(default_factory=list)


@dataclass
class SymbolState:
    """The parts of a CAD symbol and what the user must do with them."""

    parts: list[str]  # the MPNs that the project must hold, the fitted one first
    value_mpns: list[str]  # the MPNs in the parentheses of Value
    mpns: list[str]  # the confirmed MPNs (the field MPN)
    change: str = ""  # a difference to the confirmed fields: the user confirms or corrects it
    new: list[str] = field(default_factory=list)  # the MPNs of Value not in the field MPN
    removed: list[str] = field(default_factory=list)  # the confirmed MPNs not in Value
    write_mpn: str = ""  # the field MPN that a confirmation or a completion writes
    select: bool = False  # the user selects a new part first, and writes it into Value
    unconfirmed: bool = False  # the field MPN or Value_Confirmed is not written yet
    # The Value that completes the fields by the convention (decision 16): the MPNs of
    # the field MPN in parentheses. Empty: no completion.
    complete: str = ""
    no_part: bool = False  # the symbol needs no part (a NO_PART text)
    extra: list[str] = field(default_factory=list)  # the parts of a conflict of more columns
    no_mpn: bool = False  # a general value with no selected part (and no NO_PART text)
    conflicts: list[str] = field(default_factory=list)


def symbol_state(symbol: CadSymbol) -> SymbolState:
    """The parts of one CAD symbol by the model of plans/confirmed-mpns.md
    (decisions 13 to 16).

    Value holds a general value, and the MPNs in one text in parentheses (the
    fitted part first). The field MPN is the confirmed copy of these MPNs, and
    Value_Confirmed the Value of the last confirmation:

    - Value is not equal to Value_Confirmed (_value_key): a change. The confirmed
      MPNs stay the parts until the user confirms the change.
    - Value with parentheses: the same items as the field MPN are confirmed (with
      an empty Value_Confirmed: not confirmed). An empty field MPN: not confirmed,
      the MPNs of Value are the parts. An other list: a change.
    - A Value with no parentheses and a field MPN with MPNs: the tool completes the
      fields by the convention (the parts do not change). Not an empty Value.
    - A NO_PART text (-) tells that the symbol needs no part. A general value with
      no part: no part is selected.
    - More MPN columns or Value columns with other texts, a Value that does not
      follow the convention, a NO_PART text and an MPN in one list, or the text
      MIXED_VALUES of KiCad: a conflict, which the user corrects. The MPNs of more
      MPN columns and of more Value columns are parts too. A symbol with a
      conflict gets no proposal: a write can hide the conflict.
    - TBD is an empty field, also in the parentheses.
    """
    value = _filled(symbol.value)
    split = split_value(value)
    f_items, f_error = field_list(symbol.mpn)
    f_items = [i for i in f_items if i.lower() not in NOT_SELECTED]  # TBD: an empty field
    mpns = _unique(_mpns(f_items))
    v_items = split.mpns
    v_mpns = _unique(_mpns(v_items or []))
    conflicts = [split.error] if split.error else []
    if f_error:
        conflicts.append(f"MPN {_filled(symbol.mpn)!r}: {f_error}")
    for name, items in (("MPN", f_items), ("Value", v_items or [])):
        words = [i for i in items if not _is_field_mpn(i)]
        if words:
            conflicts.append(f"{name} {', '.join(words)}: not an MPN")
        marks = [i for i in items if i.lower() in NO_PART]
        if marks and _mpns(items):  # "needs no part" and an MPN in one list
            conflicts.append(
                f"{name} {', '.join(items)}: {', '.join(marks)} tells that the symbol needs no part"
            )
    # More MPN columns must hold the same MPNs (no regard to signs): an other MPN,
    # also a longer code of one part, is a conflict and a part too (no MPN of an
    # other column is lost or replaces a confirmed MPN).
    other_mpns: list[str] = []
    for name, text in symbol.other_mpns:
        other_error = field_list(text)[1]
        if other_error:
            conflicts.append(f"{name} {_filled(text)!r}: {other_error}")
        other = mpn_items(text)
        if [_norm(i) for i in other] != [_norm(i) for i in mpn_items(symbol.mpn)]:
            conflicts.append(f"MPN {_filled(symbol.mpn) or '(empty)'}, {name} {_filled(text)}")
        other_mpns += [i for i in other if _is_field_mpn(i)]
    # More Value columns: the BOM reader gives the conflict of different values. The
    # MPNs of a different one are parts too; none replaces a confirmed MPN.
    other_values = [
        m
        for text in symbol.other_values
        if _value_key(text) != _value_key(value)
        for m in _mpns(split_value(text).mpns or [])
    ]
    state = SymbolState(
        parts=list(mpns),
        value_mpns=v_mpns,
        mpns=mpns,
        conflicts=conflicts
        + ([symbol.conflict] if symbol.conflict else [])
        + symbol.unit_conflicts,
    )
    state.no_part = _no_part(f_items) if f_items else _no_part(v_items or [])
    has_field = symbol.value_confirmed is not None
    confirmed = _filled(symbol.value_confirmed or "")
    if state.conflicts:
        # The user corrects the fields first: a write of a proposal can hide the
        # conflict. The parts: the confirmed MPNs, else the MPNs of Value (none is lost).
        state.parts = list(mpns) or list(v_mpns)
    elif has_field and confirmed and _value_key(value) != _value_key(confirmed):
        if v_items is not None:
            state.write_mpn = format_mpns(v_items)
            state.new = [m for m in v_mpns if m not in mpns]
            state.removed = [m for m in mpns if m not in v_mpns]
        elif state.no_part:
            state.write_mpn = _filled(symbol.mpn)  # the user keeps the NO_PART text
        else:  # a new general value, or no MPN in Value now: the user selects the part
            state.select = True
            state.removed = list(mpns)  # the new part replaces the confirmed MPN
        if mpns:
            now = f"the confirmed MPN {_filled(symbol.mpn)!r} can be wrong now"
        elif state.no_part:
            now = f"the symbol needs no part (MPN {_filled(symbol.mpn)!r}): this can be wrong now"
        else:
            now = "no MPN is confirmed"
        state.change = f"Value {value!r}, Value_Confirmed {confirmed!r}: {now}"
    elif v_items is not None:
        state.write_mpn = format_mpns(v_items)
        state.new = [m for m in v_mpns if m not in mpns]
        state.removed = [m for m in mpns if m not in v_mpns]
        if v_items == f_items:
            # (TBD): nothing to confirm
            state.unconfirmed = has_field and not confirmed and bool(v_items)
        elif not f_items and not (has_field and confirmed):  # the first confirmation
            state.parts, state.unconfirmed = list(v_mpns), True
        else:  # also a field MPN that is empty now
            state.change = (
                f"Value {value!r}, confirmed MPN {_filled(symbol.mpn) or '(empty)'!r}: the MPNs "
                "in the parentheses are not the confirmed MPNs"
            )
    elif value and mpns and _mpns(f_items) == f_items:
        # Only MPNs in the field: no conflict to copy into Value. An empty Value can be
        # a BOM with no Value column (Altium: Comment): the tool does not know it.
        state.write_mpn = format_mpns(mpns)  # each MPN one time
        state.complete = f"{value} ({format_mpns(mpns, value=True)})"
    state.extra = [
        m
        for m in _unique(other_mpns + other_values)
        if not any(_norm(m) == _norm(p) for p in state.parts)
    ]
    state.parts = _unique(state.parts + state.extra)
    state.no_mpn = (
        not state.parts
        and not state.no_part
        and bool(value)
        and not state.change
        and not state.conflicts
    )
    return state


def write_fields(filled: list[str], names: list[str], default: str) -> tuple[list[str], bool]:
    """The fields that a write uses for one role, and True if they are known (as
    _write_fields of datasheet-lens). filled: the fields of the role with a value in
    the line (they hold the same value, else the line has a conflict and no
    proposal); names: all fields of the role. With no value: the one name of the
    role, or the default name when there is no field of the role. More names (for
    example MPN and Manufacturer Part Number of two libraries): not known; the names
    are the choices, and the user selects the field that the symbol uses. The BOM of
    the tool has one reference in each line, thus not the rule of datasheet-lens for
    a line of more symbols."""
    if filled:
        return list(dict.fromkeys(filled)), True
    choices = list(dict.fromkeys(names))
    return (choices, False) if len(choices) > 1 else (choices or [default], True)


def bom_symbols(rows: list[list[str]], variant: str = "") -> list[CadSymbol]:
    """Read the lines of a BOM of kicad-cli (one line for each reference, no DNP
    symbols) as bom_parts of datasheet-lens reads them.

    The columns: Reference, each Value column, each MPN column (MPN_FIELDS, in
    their order: the first filled one is the field MPN, the others must hold the
    same MPNs) and
    each column of Value_Confirmed. The names of a role are compared with no
    case, spaces, "_", "." and "-" (field_key): the fields MPN and mpn of symbols
    from different libraries are two columns of one role. More columns of one role
    with different values are a conflict. A line with no Value, no MPN and no
    conflict gives no symbol.

    The fields of a write (CadSymbol.mpn_fields, confirmed_fields): see
    write_fields.
    """
    header, lines = rows[0], rows[1:]
    keys = [field_key(name) for name in header]
    # A name that more columns have gets the number of its column.
    labels = [
        f"{name} (column {i + 1})" if name and header.count(name) > 1 else name
        for i, name in enumerate(header)
    ]
    reference_columns = [i for i, name in enumerate(header) if name == "Reference"]
    value_columns = [i for i, key in enumerate(keys) if key == "value"]
    confirmed_columns = [i for i, key in enumerate(keys) if key == field_key(CONFIRMED_FIELD)]
    # The MPN columns in the order of MPN_FIELDS (MPN first), as datasheet-lens: the
    # first filled one is the field MPN of the line.
    field_columns = [i for i in mpn_order(header) if i not in value_columns]

    def role(row: list[str], columns: list[int]) -> tuple[str, str]:
        """The first filled value of the columns of a role, and the conflict text
        when two filled values are different."""
        filled = [(labels[i], _filled(row[i])) for i in columns if _filled(row[i])]
        conflict = len({_value_key(v) for _, v in filled}) > 1
        text = ", ".join(f"{label} {v}" for label, v in filled) if conflict else ""
        return (filled[0][1] if filled else ""), text

    symbols = []
    for row in lines:
        row = row + [""] * (len(header) - len(row))
        # Two Value columns with different values are a conflict, also two codes of
        # one part (TLV755P, TLV75533PDBVR): the user corrects the columns.
        values = [(labels[i], _filled(row[i])) for i in value_columns if _filled(row[i])]
        value_conflict = (
            ", ".join(f"{label} {text}" for label, text in values)
            if len({_value_key(text) for _, text in values}) > 1
            else ""
        )
        confirmed, confirmed_conflict = role(row, confirmed_columns)
        # The field MPN: the first filled MPN column; the other filled ones must hold
        # the same MPNs. A NO_PART text (-) is the field MPN only when no column has
        # an MPN.
        filled = [i for i in field_columns if mpn_items(row[i])]
        marked = [
            i
            for i in field_columns
            if not mpn_items(row[i]) and any(x.lower() in NO_PART for x in field_items(row[i]))
        ]
        filled_mpns = [(labels[i], _filled(row[i])) for i in filled]
        marks = [(labels[i], _filled(row[i])) for i in marked]
        field_mpn = filled_mpns[0][1] if filled_mpns else marks[0][1] if marks else ""
        # A column that tells "no part" and a column with an MPN: a conflict.
        mark_conflict = (
            ", ".join(f"{label} {text}" for label, text in filled_mpns[:1] + marks)
            if filled_mpns and marks
            else ""
        )
        symbol = CadSymbol(
            reference=" ".join(_filled(row[i]) for i in reference_columns if _filled(row[i])),
            value=values[0][1] if values else "",
            mpn=field_mpn,
            value_confirmed=confirmed if confirmed_columns else None,
            other_mpns=filled_mpns[1:],
            other_values=[text for _, text in values[1:]],
            variant=variant,
            conflict="; ".join(c for c in (value_conflict, mark_conflict, confirmed_conflict) if c),
        )
        symbol.mpn_fields, symbol.mpn_known = write_fields(
            [header[i] for i in filled or marked], [header[i] for i in field_columns], "MPN"
        )
        symbol.confirmed_fields, symbol.confirmed_known = write_fields(
            [header[i] for i in confirmed_columns if _filled(row[i])],
            [header[i] for i in confirmed_columns],
            CONFIRMED_FIELD,
        )
        if symbol.value or symbol.mpn or symbol.conflict:
            symbols.append(symbol)
    return symbols


@dataclass
class FieldWrite:
    """The fields that a confirmation or a completion writes into one reference."""

    reference: str
    variant: str
    fields: dict[str, str]  # by name, in the order Value, MPN, Value_Confirmed (CadSymbol)
    text: str = ""  # the message: the change, or the MPNs that are new or not in Value
    select: bool = False  # the user selects a new part first, and writes it into Value
    # The fields that are not known (CadSymbol.mpn_known, confirmed_known): (the role,
    # the text, the names to select from).
    choices: list[tuple[str, str, list[str]]] = field(default_factory=list)


@dataclass
class FieldCheck:
    """The result of check_symbols."""

    conflicts: list[str] = field(default_factory=list)
    changes: list[FieldWrite] = field(default_factory=list)
    unconfirmed: list[FieldWrite] = field(default_factory=list)
    complete: list[FieldWrite] = field(default_factory=list)
    no_mpn: list[str] = field(default_factory=list)  # the labels of the symbols
    no_value_confirmed: int = 0  # the symbols with no field Value_Confirmed
    lines: int = 0  # the symbols with a part (a reference of more variants counts one time)


# The texts of a write that the user selects first (a change with a new part).
NEW_MPN = "<the MPN of the new part>"
NEW_VALUE = "<the new Value>"


def _write(
    symbol: CadSymbol, state: SymbolState, text: str, mpn: str, confirmed: str, value: str = ""
) -> FieldWrite:
    """The fields that a confirmation or a completion writes into a symbol: Value
    (only a completion), the MPN field, and Value_Confirmed when the BOM has the
    field (the names of CadSymbol)."""
    fields = {"Value": value} if value else {}
    choices = []
    roles = [("the MPN", mpn, symbol.mpn_fields, symbol.mpn_known)]
    if symbol.value_confirmed is not None:
        roles.append(
            (
                "Value_Confirmed",
                confirmed,
                symbol.confirmed_fields,
                symbol.confirmed_known,
            )
        )
    for role, written, names, known in roles:
        if known:
            fields.update(dict.fromkeys(names, written))
        else:
            choices.append((role, written, names))
    return FieldWrite(symbol.reference.strip(), symbol.variant, fields, text, state.select, choices)


def check_symbols(symbols: list[CadSymbol]) -> FieldCheck:
    """Check the fields of the symbols by the model (symbol_state), as check_parts
    of datasheet-lens does with no project file.

    symbols: the lines of the BOM of the default variant and of each variant. The
    line of a variant gives only the messages that the line of the default
    variant of its reference does not give. A power symbol (#PWR01) has no part to
    confirm. The messages and the fields to write are in FieldCheck."""
    kinds = ("conflicts", "changes", "unconfirmed", "complete", "no_mpn")
    default: dict[str, set[tuple[str, str]]] = {kind: set() for kind in kinds}
    result = FieldCheck()

    def add(kind: str, symbol: CadSymbol, text: object, item: object) -> None:
        keys = {(symbol.reference.upper(), text)}
        if not symbol.variant:
            default[kind] |= keys
        elif keys <= default[kind]:
            return
        getattr(result, kind).append(item)

    counted: set[str] = set()
    unconfirmed_refs: set[str] = set()
    # The lines of the default variant first (sorted is stable): see add().
    for symbol in sorted(symbols, key=lambda s: bool(s.variant)):
        reference = symbol.reference.strip()
        key = reference.upper()
        if (
            symbol.value_confirmed is None
            and not reference.startswith("#")
            and (_filled(symbol.value) or _filled(symbol.mpn))
            and key not in unconfirmed_refs
        ):
            unconfirmed_refs.add(key)
            result.no_value_confirmed += 1
        label = (reference or "a symbol") + (
            f" (variant {symbol.variant})" if symbol.variant else ""
        )
        state = symbol_state(symbol)
        if state.parts and key not in counted:
            counted.add(key)
            result.lines += 1
        for c in state.conflicts:
            add("conflicts", symbol, c, f"{label}: {c}")
        if reference.startswith("#"):
            continue  # a power symbol (#PWR01) has no part to confirm
        # The fields that a confirmation writes: the MPNs of the parentheses of Value.
        value = _filled(symbol.value)
        mpn = NEW_MPN if state.select else state.write_mpn
        # A variant line that writes into other fields than the default line is no
        # duplicate.
        target = (
            tuple(symbol.mpn_fields),
            symbol.mpn_known,
            symbol.value_confirmed is not None,
            tuple(symbol.confirmed_fields),
            symbol.confirmed_known,
        )
        if state.change:
            new = f" New: {', '.join(state.new)}." if state.new else ""
            removed = f" Not in Value now: {', '.join(state.removed)}." if state.removed else ""
            confirmed = NEW_VALUE if state.select else value
            item = _write(symbol, state, f"{state.change}.{new}{removed}", mpn, confirmed)
            add("changes", symbol, (state.change, target), item)
        elif state.unconfirmed:
            new = state.new if state.mpns else []
            text = (f" Not in MPN: {', '.join(new)}." if new else "") + (
                f" Not in Value: {', '.join(state.removed)}." if state.removed else ""
            )
            item = _write(symbol, state, text, mpn, value)
            add("unconfirmed", symbol, (mpn, value, target), item)
        elif state.complete:
            item = _write(symbol, state, "", mpn, state.complete, state.complete)
            add("complete", symbol, (state.complete, target), item)
        if state.no_mpn:
            add("no_mpn", symbol, "", label)
    return result
