"""Match pins to the pads of a footprint as KiCad does.

map_pins matches the pins of a library symbol. match_pads matches the pin
numbers of a component of a netlist, which KiCad already expanded.

"Update PCB from Schematic" (BOARD_NETLIST_UPDATER, the same rules in KiCad
8.0.9, 9.0.9 and 10.0.6) compares the pin numbers of the netlist with the pad
numbers of the footprint:

- A pin with no number is an error ("Symbol %s has pins with no number").
- A pin number that no pad has is an error ("%s pad %s not found in %s.").
- A pad on a copper layer with a number that no pin has gets no net. KiCad
  gives a warning ("No net found for component %s pad %s (no pin %s in
  symbol).").

A pin number and a pad number are equal only when their texts are equal
(FOOTPRINT::FindPadByNumber, COMPONENT::GetNet). The nets of the netlist
have a pin number more than one time when more than one unit has the pin.
KiCad gives a message for each. This module gives each number one time.
When KiCad loads a footprint, it removes the number of an NPTH pad and of a
pad with no copper layer (PAD::CanHaveNumber).

KiCad 10 also reads a pin number in the stacked notation ("[1-4]", "[1,3,A5]")
as more than one number. The netlist gets each number
(SCH_PIN::GetStackedPinNumbers), and a pad gets the net of a pin whose number
expands to the pad number (COMPONENT::GetNet). KiCad 8 and 9 use the text as
one number.
"""

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..errors import InputError
from ..parsers.symbol_info import SymbolPin

# wxString::ToLong gives LONG_MAX for a number that is too large. A long has
# 32 bits on Windows and 64 bits on Linux and macOS.
_LONG_MAX = 2**31 - 1 if sys.platform == "win32" else 2**63 - 1
# wxString::Trim removes these characters.
_SPACES = " \t\n\r\v\f"
# The server does not expand one pin number to more numbers than this.
MAX_STACKED = 100_000


class StackedPinError(InputError):
    """A stacked pin number gives more numbers than the server expands."""


class Pad(Protocol):
    """A pad of a footprint, as pcbnew loaded it."""

    number: str
    on_copper: bool  # PAD::IsOnCopperLayer


def _alphanumeric(text: str) -> tuple[str, int]:
    """Return (prefix, value) as ParseAlphaNumericPin of KiCad 10.0 does.

    The value is the number of the digits at the end of the text, or -1 when
    the text does not end with a digit. Then the prefix is empty.
    """
    start = len(text)
    while start > 0 and text[start - 1] in "0123456789":
        start -= 1
    if start == len(text):
        return "", -1
    digits = text[start:].lstrip("0") or "0"
    # A number of more than 19 digits is larger than LONG_MAX. int() also
    # refuses a text of more than 4300 digits.
    return text[:start], _LONG_MAX if len(digits) > 19 else min(int(digits), _LONG_MAX)


def stacked_text(number: str) -> bool:
    """Return True for a pin number in brackets, which a stacked notation reads.

    KiCad 10.0 and branch master of 2026-11-04 read a number that does not
    start with "[" and end with "]" as one number (ExpandStackedPinNotation).
    """
    return number.startswith("[") and number.endswith("]")


def expand_stacked(number: str) -> tuple[list[str], bool]:
    """Expand a pin number as ExpandStackedPinNotation of KiCad 10.0 does.

    "[1-3,A5]" gives "1", "2", "3" and "A5". A range has the same prefix at
    its two ends, and its numbers have no leading zeros ("[A01-A03]" gives
    "A1" to "A3"). A number with no brackets is one number.

    Returns:
        (the numbers, False if the notation is not valid). For a notation
        that is not valid, the number is the full text.

    Raises:
        StackedPinError: The notation gives more than MAX_STACKED numbers.
    """
    if not (number.startswith("[") and number.endswith("]")):
        return [number], "[" not in number and "]" not in number
    # First read all parts: a part that is not valid makes the full notation
    # not valid, also after a range.
    # (a number, None) or (a prefix, (the first value, the last value))
    items: list[tuple[str, tuple[int, int] | None]] = []
    for part in number[1:-1].split(","):
        part = part.strip(_SPACES)
        if not part:
            continue
        dash = part.find("-")
        if dash < 0:
            items.append((part, None))
            continue
        prefix, first = _alphanumeric(part[:dash].strip(_SPACES))
        end_prefix, last = _alphanumeric(part[dash + 1 :].strip(_SPACES))
        if prefix != end_prefix or first == -1 or last == -1 or first > last:
            return [number], False
        items.append((prefix, (first, last)))
    if not items:
        return [number], False
    if sum(1 if span is None else span[1] - span[0] + 1 for _text, span in items) > MAX_STACKED:
        raise StackedPinError(
            f'The pin number "{number}" gives more than {MAX_STACKED} pin numbers.'
        )
    numbers: list[str] = []
    for text, span in items:
        if span is None:
            numbers.append(text)
        else:
            numbers.extend(f"{text}{value}" for value in range(span[0], span[1] + 1))
    return numbers, True


@dataclass(frozen=True)
class PinPadMapping:
    """The pins of one body style of a symbol, compared with the pads of a footprint."""

    no_number: tuple[SymbolPin, ...]  # Errors: pins with no number.
    # Errors: each pin number that no pad has, with its pins.
    no_pad: tuple[tuple[str, tuple[SymbolPin, ...]], ...]
    # Warnings: the index (in the pads) of each pad with a number that no pin has.
    no_pin: tuple[int, ...]
    # KiCad 10: the pins in the stacked notation, with their numbers.
    stacked: tuple[tuple[SymbolPin, tuple[str, ...]], ...]
    # KiCad 10: the pins with brackets that are not a valid stacked notation.
    # KiCad uses the text as one number.
    invalid: tuple[SymbolPin, ...]
    pin_numbers: int  # The pin numbers of the netlist.


def match_pads(
    numbers: Iterable[str], pads: Sequence[Pad], stacked: bool
) -> tuple[list[str], tuple[int, ...]]:
    """Match the pin numbers of a netlist to the pads as "Update PCB from Schematic" does.

    Args:
        numbers: The pin numbers of one component of the netlist, with no empty
            number. With no numbers, each pad with a number has no pin.
        pads: The pads of the footprint.
        stacked: True for KiCad 10.0 (COMPONENT::GetNet expands the stacked
            notation).

    Returns:
        (the pin numbers that no pad has, the index of each pad with a number
        that no pin has).
    """
    numbers = list(dict.fromkeys(numbers))
    pad_numbers = {pad.number for pad in pads}
    no_pad = [number for number in numbers if number not in pad_numbers]
    # A pad gets the net of a pin number of the netlist with the same text.
    # COMPONENT::GetNet of KiCad 10 also expands the pin numbers of the
    # netlist, thus a number such as "[[5]]" (netlist "[5]") matches pad 5.
    matched = set(numbers)
    if stacked:
        for number in numbers:
            if number.startswith("["):
                matched.update(expand_stacked(number)[0])
    no_pin = tuple(
        index
        for index, pad in enumerate(pads)
        if pad.number and pad.on_copper and pad.number not in matched
    )
    return no_pad, no_pin


def map_pins(pins: list[SymbolPin], pads: Sequence[Pad], stacked: bool) -> PinPadMapping:
    """Compare the pins of one body style with the pads as "Update PCB from Schematic" does.

    Args:
        pins: The pins of all units of one body style.
        pads: The pads of the footprint, as pcbnew loaded them.
        stacked: True for KiCad 10.0, which expands the stacked notation.

    Raises:
        StackedPinError: A pin number gives more than MAX_STACKED numbers.
    """
    names: dict[str, list[SymbolPin]] = {}
    no_number: list[SymbolPin] = []
    expanded: list[tuple[SymbolPin, tuple[str, ...]]] = []
    invalid: list[SymbolPin] = []
    for pin in pins:
        numbers = [pin.number]
        if stacked:
            numbers, valid = expand_stacked(pin.number)
            if not valid:
                invalid.append(pin)
            elif pin.number.startswith("["):
                expanded.append((pin, tuple(numbers)))
        for number in numbers:
            if number:
                names.setdefault(number, []).append(pin)
            else:
                no_number.append(pin)

    no_pad, no_pin = match_pads(names, pads, stacked)
    return PinPadMapping(
        no_number=tuple(no_number),
        no_pad=tuple((number, tuple(names[number])) for number in no_pad),
        no_pin=no_pin,
        stacked=tuple(expanded),
        invalid=tuple(invalid),
        pin_numbers=len(names),
    )
