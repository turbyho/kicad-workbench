"""The model of the confirmed MPNs: the check of the fields Value, MPN, Value_Confirmed and
MPN_Confirmed.

This module is a port of the model of datasheet-lens (same author, MIT,
src/datasheet_lens/project.py: split_value, _list_items, field_list,
format_mpns, symbol_state and the messages of check_parts). The model is in the
repository of datasheet-lens, plans/confirmed-mpns.md (decisions 26 to 30, user,
2026-11-07; they replace decisions 13 to 16). datasheet-lens does the check in
project_check when it is available. This module is the fallback for the users with
no datasheet-lens (decision 8). The two must give the same results: a change of the
model goes into the two projects, with the same tests.

The convention (four fields, each with one function):

- Value: the general value ("2N7002", "100nF 10V"). The check reads no MPN from it.
- MPN: the MPNs of the designer, the fitted part first, then the alternatives. The
  separators are "," and ";". An MPN with "," or ";" is in quotes, as in CSV.
- Value_Confirmed and MPN_Confirmed (hidden): the copies of the last confirmation.
  A difference is a change that the user confirms.
- The old convention (the MPNs in the parentheses of Value, "2N7002 (2N7002LT1G)"):
  the check proposes the move into the four fields.
"""

import re
from dataclasses import dataclass, field

from .unit_fields import CONFIRMED_FIELD, MPN_CONFIRMED_FIELD, field_key, mpn_order

# The text of KiCad in a BOM line of more symbols or units with different values of
# a field (INDETERMINATE_STATE, include/widgets/ui_common.h; FIELDS_EDITOR_GRID_DATA_MODEL
# in eeschema/fields_data_model.cpp, KiCad 8, 9 and 10). The tool runs kicad-cli in
# English (utils/kicad_cli.py); a BOM of KiCad in an other language has its translation.
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


def format_mpns(items: list[str]) -> str:
    """The text of a list of MPNs for the field MPN, with quotes only where they are
    necessary (see _list_items). The text must give the items again: else the tool
    writes quotes around each item (also "~", which is an empty field with no
    quotes)."""

    def reads_as(text: str) -> bool:
        return field_list(text) == (items, "")

    def quoted(item: str) -> str:
        return '"' + item.replace('"', '""') + '"'

    special = MPN_SEPARATORS
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
    """A Value by the old convention of the parentheses (plans/confirmed-mpns.md,
    decision 13): the tool reads it only for the move into the four fields (decision
    30)."""

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


def _words(text: str) -> str:
    """A general value for a comparison: its words, with one space between them. The
    case counts: 4m7 (milliohm) is not 4M7 (megaohm), and 10 k is not 10k."""
    return " ".join(_filled(text).split())


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
    value: str = ""  # the general value: 2N7002, 100nF 10V (decision 26)
    mpn: str = ""  # the field MPN: the MPNs of the designer, the fitted part first
    # The hidden field Value_Confirmed: the Value of the last confirmation. None: the
    # BOM has no column Value_Confirmed (no symbol of the project has the field).
    value_confirmed: str | None = None
    # The hidden field MPN_Confirmed: the field MPN of the last confirmation. None: the
    # BOM has no column MPN_Confirmed.
    mpn_confirmed: str | None = None
    other_mpns: list[tuple[str, str]] = field(default_factory=list)  # (name, text)
    other_values: list[str] = field(default_factory=list)  # the texts of more Value columns
    conflict: str = ""  # a conflict that the BOM reader found (columns of one role)
    # The name of a variant of KiCad 10, or empty for the default variant. A variant
    # can set an other Value, MPN or DNP.
    variant: str = ""
    # The fields that a write uses (see write_fields): the MPN fields with the MPN of
    # the line (for example "Manufacturer Part Number"; more fields hold the same MPNs),
    # and the fields Value_Confirmed and MPN_Confirmed. known False: the line has no
    # value and the project has more names of the role; the fields are the choices, and
    # the user selects the field of the symbol.
    mpn_fields: list[str] = field(default_factory=lambda: ["MPN"])
    mpn_known: bool = True
    confirmed_fields: list[str] = field(default_factory=lambda: [CONFIRMED_FIELD])
    confirmed_known: bool = True
    mpn_confirmed_fields: list[str] = field(default_factory=lambda: [MPN_CONFIRMED_FIELD])
    mpn_confirmed_known: bool = True
    # The conflicts of the units of one package with different fields (not in
    # datasheet-lens, which cannot see the units in a BOM line): each one is a conflict
    # of its own, thus a variant line does not repeat one of the default line.
    unit_conflicts: list[str] = field(default_factory=list)


@dataclass
class SymbolState:
    """The parts of a CAD symbol and what the user must do with them."""

    parts: list[str]  # the MPNs that the project must hold, the fitted one first
    mpns: list[str]  # the MPNs of the field MPN
    confirmed: list[str]  # the confirmed MPNs (the field MPN_Confirmed)
    change: str = ""  # a difference to the confirmed fields: the user confirms or corrects it
    new: list[str] = field(default_factory=list)  # the MPNs of MPN not in MPN_Confirmed
    removed: list[str] = field(default_factory=list)  # the confirmed MPNs not in MPN
    write_mpn: str = ""  # the field MPN_Confirmed that a confirmation writes (the text of MPN)
    # A new general value with the confirmed MPN (decision 29): the user selects a new
    # part, or confirms that the part stays.
    select: bool = False
    unconfirmed: bool = False  # MPN_Confirmed or Value_Confirmed is not written yet
    # The old convention (decisions 30 and 32): the fields of the move into the four
    # fields, {"value", "mpn", "mpn_confirmed", "value_confirmed", "old"}; empty: no
    # move. "old": the field with the parentheses (Value or Value_Confirmed).
    move: dict = field(default_factory=dict)
    no_part: bool = False  # the symbol needs no part (a NO_PART text)
    extra: list[str] = field(default_factory=list)  # the parts of a conflict of more columns
    no_mpn: bool = False  # a general value with no selected part (and no NO_PART text)
    conflicts: list[str] = field(default_factory=list)


def _list_conflicts(name: str, text: str, items: list[str], error: str) -> list[str]:
    """The conflicts of a list of MPNs: an error of the list, a text that is not an MPN,
    and a no-part text with an MPN."""
    conflicts = [f"{name} {_filled(text)!r}: {error}"] if error else []
    words = [i for i in items if not _is_field_mpn(i)]
    if words:
        conflicts.append(f"{name} {', '.join(words)}: not an MPN")
    marks = [i for i in items if i.lower() in NO_PART]
    if marks and _mpns(items):  # "needs no part" and an MPN in one list
        conflicts.append(
            f"{name} {', '.join(items)}: {', '.join(marks)} tells that the symbol needs no part"
        )
    return conflicts


def _old_list(
    text: str, symbol: CadSymbol, m_items: list[str], confirmed_items: list[str]
) -> ValueParts | None:
    """A Value text of the old convention (see _old_convention), or None."""
    if confirmed_items:
        return None  # the new convention: the parentheses are text of the general value
    split = split_value(text)
    if split.error or split.mpns is None or _list_conflicts("Value", text, split.mpns, ""):
        return None
    old = split_value(symbol.value_confirmed or "")
    if m_items and m_items != split.mpns and (old.error or old.mpns != m_items):
        return None
    return split


def _old_convention(
    symbol: CadSymbol, m_items: list[str], confirmed_items: list[str]
) -> tuple[dict, list[str]]:
    """The fields of the move of a symbol of the old convention into the four fields
    (decision 30), and the MPNs of its parentheses; or ({}, []).

    The old convention: a list of MPNs in the parentheses of Value (also "(TBD)", an
    empty list), no MPN_Confirmed, and a field MPN that is empty, holds the same list,
    or holds the list of the parentheses of Value_Confirmed (the old field MPN was the
    confirmed copy of the parentheses: a change that is not confirmed yet). The tool
    does not guess: a field MPN with other MPNs tells the new convention ("10k (1%)"
    and MPN RC0603FR-0710KL), and its parentheses are text of the general value. The
    move keeps the confirmed copies: MPN_Confirmed is the old field MPN, Value_Confirmed
    the general value of the old Value_Confirmed. A confirmed symbol stays confirmed.

    The old convention only in Value_Confirmed (decision 32): the user removed the
    parentheses from Value, and the field MPN holds the list of the parentheses of
    Value_Confirmed. The move writes only the confirmed copies (value and mpn are
    None: Value and MPN keep their texts); "old" tells which field has the
    parentheses."""
    split = _old_list(symbol.value, symbol, m_items, confirmed_items)
    if split is None:
        return _old_confirmed_value(symbol, m_items, confirmed_items), []
    old_confirmed = (
        None if symbol.value_confirmed is None else split_value(symbol.value_confirmed).general
    )
    move = {
        "value": split.general,
        "mpn": format_mpns(split.mpns) if split.mpns else "",
        "mpn_confirmed": _filled(symbol.mpn),
        "value_confirmed": old_confirmed,
        "old": "Value",
    }
    return move, _unique(_mpns(split.mpns or []))


def _old_confirmed_value(symbol: CadSymbol, m_items: list[str], confirmed_items: list[str]) -> dict:
    """The move of a symbol with the old convention only in Value_Confirmed (decision
    32), or {}: no MPN_Confirmed, a Value with no parentheses (not empty: the removal of
    Value is a change), a Value_Confirmed with a
    valid list of MPNs in parentheses, and a field MPN that holds the same list (the
    old field MPN was the confirmed copy of the parentheses)."""
    if confirmed_items or symbol.value_confirmed is None:
        return {}
    split = split_value(symbol.value)
    old = split_value(symbol.value_confirmed)
    if (
        not split.general
        or split.error
        or split.mpns is not None
        or old.error
        or old.mpns is None
        or m_items != old.mpns
        or _list_conflicts("Value_Confirmed", symbol.value_confirmed, old.mpns, "")
    ):
        return {}
    return {
        "value": None,
        "mpn": None,
        "mpn_confirmed": _filled(symbol.mpn),
        "value_confirmed": old.general,
        "old": "Value_Confirmed",
    }


def symbol_state(symbol: CadSymbol) -> SymbolState:
    """The parts of one CAD symbol by the model of plans/confirmed-mpns.md
    (decisions 26 to 30, 32 and 33).

    Value is the general value, the field MPN holds the MPNs of the designer (the
    fitted part first), Value_Confirmed and MPN_Confirmed are the Value and the MPN
    of the last confirmation:

    - Value is not Value_Confirmed, or MPN is not MPN_Confirmed: a change. The
      confirmed MPNs stay the parts until the user confirms the change. A new
      general value with the confirmed MPN: the user selects a new part, or confirms
      that the part stays (select). A new general value with an MPN that the user
      never confirmed is a change, not select (decision 33).
    - An empty Value_Confirmed or MPN_Confirmed (and no change): not confirmed, the
      MPNs of the field MPN are the parts.
    - The old convention (MPNs in the parentheses of Value, no MPN_Confirmed): the
      tool proposes the move into the four fields (move). Also when only
      Value_Confirmed has the parentheses (decision 32).
    - A NO_PART text (-) tells that the symbol needs no part. A general value with
      no part: no part is selected.
    - More MPN columns with other texts, a list error, a NO_PART text and an MPN in
      one list, or the text MIXED_VALUES of KiCad: a conflict, which the user
      corrects. The MPNs of more MPN columns are parts too. A symbol with a conflict
      gets no proposal: a write can hide the conflict.
    - TBD is an empty field.
    """
    value = _filled(symbol.value)
    m_items, m_error = field_list(symbol.mpn)
    m_items = [i for i in m_items if i.lower() not in NOT_SELECTED]  # TBD: an empty field
    mpns = _unique(_mpns(m_items))
    c_text = symbol.mpn_confirmed or ""
    c_items, c_error = field_list(c_text)
    c_items = [i for i in c_items if i.lower() not in NOT_SELECTED]
    confirmed_mpns = _unique(_mpns(c_items))
    conflicts = _list_conflicts("MPN", symbol.mpn, m_items, m_error) + _list_conflicts(
        "MPN_Confirmed", c_text, c_items, c_error
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
    # A symbol of the old convention (decision 30): the MPNs in the parentheses of Value,
    # also of more Value columns, are parts until the move (no MPN is lost).
    move, old_mpns = _old_convention(symbol, m_items, c_items)
    # A symbol of the old convention has it in each Value column; else the column itself
    # must show it.
    for text in symbol.other_values:
        if _words(text) == _words(value):
            continue
        # Decision 32: Value has no parentheses, thus an other Value column must show the
        # old convention itself.
        old_value = move.get("old") == "Value"
        split = split_value(text) if old_value else _old_list(text, symbol, m_items, c_items)
        if split is not None and not split.error:
            other_mpns += _mpns(split.mpns or [])
    state = SymbolState(
        parts=list(mpns),
        mpns=mpns,
        confirmed=confirmed_mpns,
        conflicts=conflicts
        + ([symbol.conflict] if symbol.conflict else [])
        + symbol.unit_conflicts,
    )
    state.no_part = _no_part(m_items)
    has_vc = symbol.value_confirmed is not None
    has_mc = symbol.mpn_confirmed is not None
    vc = _filled(symbol.value_confirmed or "")
    if state.conflicts:
        # The user corrects the fields first: a write of a proposal can hide the
        # conflict. The parts: the confirmed MPNs, else the MPNs of MPN, else the MPNs of
        # the old convention (none is lost).
        state.parts = list(confirmed_mpns) or list(mpns) or old_mpns
    elif move:
        state.move = move
        state.parts = list(mpns) or old_mpns  # the old field MPN was the confirmed copy
    else:
        value_changed = has_vc and bool(vc) and _words(value) != _words(vc)
        mpn_changed = has_mc and bool(c_items) and m_items != c_items
        state.write_mpn = format_mpns(m_items) if m_items else _filled(symbol.mpn)
        if value_changed or mpn_changed:
            state.new = [m for m in mpns if m not in confirmed_mpns]
            state.removed = [m for m in confirmed_mpns if m not in mpns]
            if c_items:
                state.parts = list(confirmed_mpns)  # the confirmed MPNs until the confirmation
            shown = _filled(symbol.mpn) or "(empty)"
            if mpn_changed and value_changed:
                state.change = (
                    f"Value {value!r}, Value_Confirmed {vc!r}; MPN {shown!r}, MPN_Confirmed "
                    f"{_filled(c_text)!r}: the value and the part are not the confirmed ones"
                )
            elif mpn_changed:
                state.change = (
                    f"MPN {shown!r}, MPN_Confirmed {_filled(c_text)!r}: the MPN is not the "
                    "confirmed MPN"
                )
            else:
                # Not for a no-part text (it keeps the text), and not for an MPN that the
                # user never confirmed (decision 33): that is a change of its own. With no
                # field MPN_Confirmed, the tool cannot know it.
                unconfirmed_mpn = has_mc and not confirmed_mpns
                state.select = bool(mpns) and not unconfirmed_mpn
                if mpns and unconfirmed_mpn:
                    now = f"the MPN {_filled(symbol.mpn)!r} is not confirmed"
                elif mpns:
                    now = f"the MPN {_filled(symbol.mpn)!r} can be wrong now"
                elif state.no_part:
                    now = (
                        f"the symbol needs no part (MPN {_filled(symbol.mpn)!r}): this can be "
                        "wrong now"
                    )
                else:
                    now = "no MPN is confirmed"
                state.change = f"Value {value!r}, Value_Confirmed {vc!r}: {now}"
        elif m_items and ((has_vc and not vc) or (has_mc and not c_items)):
            state.unconfirmed = True
            state.new = [m for m in mpns if m not in confirmed_mpns]
    state.extra = [
        m for m in _unique(other_mpns) if not any(_norm(m) == _norm(p) for p in state.parts)
    ]
    state.parts = _unique(state.parts + state.extra)
    state.no_mpn = (
        not state.parts
        and not state.no_part
        and bool(value)
        and not state.change
        and not state.conflicts
        and not state.move
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
    """Read the lines of a BOM of kicad-cli (one line for each reference, also the DNP
    symbols) as bom_parts of datasheet-lens reads them. A DNP line gets the same check
    as a fitted line (CadSymbol.fitted of datasheet-lens has no effect on the check of
    the fields).

    The columns: Reference, each Value column, each MPN column (MPN_FIELDS, in
    their order: the first filled one is the field MPN, the others must hold the
    same MPNs), each column of Value_Confirmed and each column of MPN_Confirmed.
    The names of a role are compared with no case, spaces, "_", "." and "-"
    (field_key): the fields MPN and mpn of symbols from different libraries are
    two columns of one role. More columns of one role with different values are a
    conflict. A line with no Value, no MPN, no confirmed field and no conflict
    gives no symbol.

    The fields of a write (CadSymbol.mpn_fields, confirmed_fields,
    mpn_confirmed_fields): see write_fields.
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
    mpn_confirmed_columns = [
        i for i, key in enumerate(keys) if key == field_key(MPN_CONFIRMED_FIELD)
    ]
    # The MPN columns in the order of MPN_FIELDS (MPN first), as datasheet-lens: the
    # first filled one is the field MPN of the line.
    field_columns = [i for i in mpn_order(header) if i not in value_columns]

    def role(row: list[str], columns: list[int], key=_words) -> tuple[str, str]:
        """The first filled value of the columns of a role, and the conflict text
        when two filled values are different (by the key of the role)."""
        filled = [(labels[i], _filled(row[i])) for i in columns if _filled(row[i])]
        conflict = len({key(v) for _, v in filled}) > 1
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
            if len({_words(text) for _, text in values}) > 1
            else ""
        )
        confirmed, confirmed_conflict = role(row, confirmed_columns)
        mpn_confirmed, mpn_confirmed_conflict = role(
            row, mpn_confirmed_columns, lambda v: tuple(field_items(v))
        )
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
            mpn_confirmed=mpn_confirmed if mpn_confirmed_columns else None,
            other_mpns=filled_mpns[1:],
            other_values=[text for _, text in values[1:]],
            variant=variant,
            conflict="; ".join(
                c
                for c in (value_conflict, mark_conflict, confirmed_conflict, mpn_confirmed_conflict)
                if c
            ),
        )
        symbol.mpn_fields, symbol.mpn_known = write_fields(
            [header[i] for i in filled or marked], [header[i] for i in field_columns], "MPN"
        )
        symbol.confirmed_fields, symbol.confirmed_known = write_fields(
            [header[i] for i in confirmed_columns if _filled(row[i])],
            [header[i] for i in confirmed_columns],
            CONFIRMED_FIELD,
        )
        symbol.mpn_confirmed_fields, symbol.mpn_confirmed_known = write_fields(
            [header[i] for i in mpn_confirmed_columns if _filled(row[i])],
            [header[i] for i in mpn_confirmed_columns],
            MPN_CONFIRMED_FIELD,
        )
        # A line with only a confirmed field: the user removed Value and MPN of a
        # confirmed symbol, which is a change.
        if (
            symbol.value
            or symbol.mpn
            or symbol.value_confirmed
            or symbol.mpn_confirmed
            or symbol.conflict
        ):
            symbols.append(symbol)
    return symbols


@dataclass
class FieldWrite:
    """The fields that a confirmation or a move writes into one reference."""

    reference: str
    variant: str
    # By name, in the order Value, MPN (a move only), MPN_Confirmed, Value_Confirmed.
    fields: dict[str, str]
    text: str = ""  # the message: the change, or the MPNs that are new
    # The MPNs of MPN that MPN_Confirmed does not have: the agent finds their datasheets
    # at once (decision 28).
    new: list[str] = field(default_factory=list)
    # A new general value with the confirmed MPN: the user selects a new part, or
    # confirms that the part stays (the fields are the confirmation of the same part).
    select: bool = False
    # The fields that are not known (CadSymbol.mpn_known, confirmed_known,
    # mpn_confirmed_known): (the role, the text, the names to select from).
    choices: list[tuple[str, str, list[str]]] = field(default_factory=list)
    old: str = ""  # a move: the field with the parentheses (Value or Value_Confirmed)


@dataclass
class FieldCheck:
    """The result of check_symbols."""

    conflicts: list[str] = field(default_factory=list)
    changes: list[FieldWrite] = field(default_factory=list)
    unconfirmed: list[FieldWrite] = field(default_factory=list)
    move: list[FieldWrite] = field(default_factory=list)  # the old convention (decision 30)
    no_mpn: list[str] = field(default_factory=list)  # the labels of the symbols
    no_value_confirmed: int = 0  # the symbols with no field Value_Confirmed
    no_mpn_confirmed: int = 0  # the symbols with an MPN and no field MPN_Confirmed
    lines: int = 0  # the symbols with a part (a reference of more variants counts one time)


def _write(
    symbol: CadSymbol,
    state: SymbolState,
    text: str,
    mpn_confirmed: str | None,
    confirmed: str | None,
    value: str | None = None,
    mpn: str | None = None,
    old: str = "",
) -> FieldWrite:
    """The fields that a confirmation or a move writes into a symbol: Value and the MPN
    fields (only a move), MPN_Confirmed and Value_Confirmed (None: the BOM has no such
    field and a confirmation does not write it; a move writes the new fields). old: for
    a move, the field with the parentheses (FieldWrite.old)."""
    fields = {"Value": value} if value is not None else {}
    choices = []
    roles = []
    if mpn is not None:
        roles.append(("the MPN", mpn, symbol.mpn_fields, symbol.mpn_known))
    if mpn_confirmed is not None:
        roles.append(
            (
                "MPN_Confirmed",
                mpn_confirmed,
                symbol.mpn_confirmed_fields,
                symbol.mpn_confirmed_known,
            )
        )
    if confirmed is not None:
        roles.append(
            ("Value_Confirmed", confirmed, symbol.confirmed_fields, symbol.confirmed_known)
        )
    for role, written, names, known in roles:
        if known:
            fields.update(dict.fromkeys(names, written))
        else:
            choices.append((role, written, names))
    return FieldWrite(
        symbol.reference.strip(),
        symbol.variant,
        fields,
        text,
        list(state.new),
        state.select,
        choices,
        old,
    )


def check_symbols(symbols: list[CadSymbol]) -> FieldCheck:
    """Check the fields of the symbols by the model (symbol_state), as check_parts
    of datasheet-lens does with no project file.

    symbols: the lines of the BOM of the default variant and of each variant. The
    line of a variant gives only the messages that the line of the default
    variant of its reference does not give. A power symbol (#PWR01) has no part to
    confirm. The messages and the fields to write are in FieldCheck."""
    kinds = ("conflicts", "changes", "unconfirmed", "move", "no_mpn")
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
    no_mpn_confirmed_refs: set[str] = set()
    # The lines of the default variant first (sorted is stable): see add().
    for symbol in sorted(symbols, key=lambda s: bool(s.variant)):
        reference = symbol.reference.strip()
        key = reference.upper()
        power = reference.startswith("#")  # a power symbol (#PWR01) has no part
        if (
            symbol.value_confirmed is None
            and not power
            and (_filled(symbol.value) or _filled(symbol.mpn))
            and key not in unconfirmed_refs
        ):
            unconfirmed_refs.add(key)
            result.no_value_confirmed += 1
        if (
            symbol.mpn_confirmed is None
            and not power
            and _filled(symbol.mpn)
            and key not in no_mpn_confirmed_refs
        ):
            no_mpn_confirmed_refs.add(key)
            result.no_mpn_confirmed += 1
        label = (reference or "a symbol") + (
            f" (variant {symbol.variant})" if symbol.variant else ""
        )
        state = symbol_state(symbol)
        if state.parts and key not in counted:
            counted.add(key)
            result.lines += 1
        for c in state.conflicts:
            add("conflicts", symbol, c, f"{label}: {c}")
        if power:
            continue  # no part to confirm
        # The fields that a confirmation writes: the field MPN into MPN_Confirmed, and
        # Value into Value_Confirmed (only the fields that the BOM has).
        value = _filled(symbol.value)
        mpn_confirmed = state.write_mpn if symbol.mpn_confirmed is not None else None
        confirmed = value if symbol.value_confirmed is not None else None
        # A variant line that writes into other fields than the default line is no
        # duplicate.
        target = (
            tuple(symbol.mpn_fields),
            symbol.mpn_known,
            symbol.value_confirmed is not None,
            tuple(symbol.confirmed_fields),
            symbol.confirmed_known,
            symbol.mpn_confirmed is not None,
            tuple(symbol.mpn_confirmed_fields),
            symbol.mpn_confirmed_known,
        )
        if state.change:
            # A new general value with the confirmed MPN has no new MPN to tell (as
            # check_parts of datasheet-lens).
            new = f" New: {', '.join(state.new)}." if state.new and not state.select else ""
            removed = f" Not in MPN now: {', '.join(state.removed)}." if state.removed else ""
            item = _write(symbol, state, f"{state.change}.{new}{removed}", mpn_confirmed, confirmed)
            add("changes", symbol, (state.change, target), item)
        elif state.unconfirmed:
            text = f" Not confirmed: {', '.join(state.new)}." if state.new else ""
            item = _write(symbol, state, text, mpn_confirmed, confirmed)
            add("unconfirmed", symbol, (state.write_mpn, value, target), item)
        elif state.move:
            move = state.move
            # A move of decision 32 has no Value and no MPN to write (None).
            item = _write(
                symbol,
                state,
                "",
                move["mpn_confirmed"],
                move["value_confirmed"],
                move["value"],
                move["mpn"],
                move["old"],
            )
            # Each field that the move writes (None: not written): a variant line with
            # other confirmed copies is no duplicate, and a variant line that writes the
            # same fields is one.
            key = (move["value"], move["mpn"], move["mpn_confirmed"], move["value_confirmed"])
            add("move", symbol, (key, target), item)
        if state.no_mpn:
            add("no_mpn", symbol, "", label)
    return result
