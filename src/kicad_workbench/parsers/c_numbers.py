"""The numbers of the C library as KiCad reads them.

KiCad reads many numbers of its files with wxString::ToLong (wcstol and a check
that the full text is the number) and keeps them in an int (static_cast<int>).
"""

import re

from ..errors import LocaleNumberError

# The range of a C long: 64 bits on Linux and macOS.
LONG_MIN, LONG_MAX = -(2**63), 2**63 - 1

# wcstol skips the white space at the start (isspace), and reads a sign and digits.
_LONG_TEXT = re.compile(r"[ \t\n\v\f\r]*([+-]?[0-9]+)")
_NUMBER = re.compile(r"[+-]?[0-9]+")


def to_long(text: str) -> int | None:
    """Return the integer of a text as ``wxString::ToLong`` reads it (base 10), or
    None when the text is not a number or the number is out of the range of a long.

    ``wcstol`` skips the white space at the start, reads a sign and digits, and
    the full text must be the number.

    Raises:
        LocaleNumberError: The text is a number after white space at the start
            with a character that is not ASCII. ``iswspace`` of the C library
            decides on it with the locale and the system of KiCad: measured with
            kicad-cli 10.0.6 on Linux, U+2003 is white space in a UTF-8 locale but
            not in the locale C, and U+00A0 in neither. Other C libraries (Windows,
            macOS) have other sets. The server does not know them.
    """
    start = len(text) - len(text.lstrip())
    if any(not character.isascii() for character in text[:start]) and _NUMBER.fullmatch(
        text[start:]
    ):
        raise LocaleNumberError(
            f"KiCad reads {text!r} as a number only in some locales (the white space "
            "at the start is not ASCII), thus the server does not know its value."
        )
    match = _LONG_TEXT.fullmatch(text)
    if match is None:
        return None
    number = match.group(1)
    sign = "-" if number.startswith("-") else ""
    digits = number.lstrip("+-").lstrip("0") or "0"
    # More than 19 digits is out of the range. int() refuses a text of more than
    # 4300 digits, also with zeros at the start.
    if len(digits) > 19:
        return None
    value = int(sign + digits)
    return value if LONG_MIN <= value <= LONG_MAX else None


def to_int(value: int) -> int:
    """Return a long as static_cast<int> gives it: the low 32 bits, with a sign."""
    return (value + 2**31) % 2**32 - 2**31
