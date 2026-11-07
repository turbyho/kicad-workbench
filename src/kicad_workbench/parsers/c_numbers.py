"""The numbers of the C library as KiCad reads them.

KiCad reads many numbers of its files with wxString::ToLong (wcstol and a check
that the full text is the number), strtol or atoi, and keeps them in an int
(static_cast<int>). A C long has 64 bits on Linux and macOS (LP64) and 32 bits on
Windows (LLP64, the MSVC build of KiCad): the same text can give an other number.
"""

import re
import sys

from ..errors import LocaleNumberError


def long_bits(platform: str) -> int:
    """Return the bits of a C long of KiCad on the system of the server.

    The server uses the KiCad of its own system (kicad-cli, the library tables).
    Each KiCad for Windows (the MSVC build, also a MinGW build) has a long of 32
    bits (LLP64), also when the server runs in a Python of Cygwin or MSYS2
    ("cygwin", "msys"), which has a long of 64 bits. Linux and macOS: 64 bits.
    """
    return 32 if platform in ("win32", "cygwin", "msys") else 64


# The range of a C long of KiCad on the system of the server.
_BITS = long_bits(sys.platform)
LONG_MIN, LONG_MAX = -(2 ** (_BITS - 1)), 2 ** (_BITS - 1) - 1

_NUMBER = re.compile(r"[+-]?[0-9]+")
# strtol and wcstol: the ASCII white space (isspace), a sign and digits at the start.
_LEADING = re.compile(r"[ \t\n\v\f\r]*([+-]?)([0-9]*)")


def _value(sign: str, digits: str) -> int | None:
    """Return the number of a sign and digits, or None out of the range of a long."""
    digits = digits.lstrip("0") or "0"
    # More than 19 digits is out of the range of each long. int() refuses a text of
    # more than 4300 digits, also with zeros at the start.
    if len(digits) > 19:
        return None
    value = int(sign + digits)
    return value if LONG_MIN <= value <= LONG_MAX else None


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
    match = _LEADING.fullmatch(text)
    if match is None or not match.group(2):
        return None
    return _value(match.group(1), match.group(2))


def strtol(text: str) -> int:
    """Return ``strtol(text, nullptr, 10)``: the number at the start of a text.

    strtol skips the white space at the start, reads a sign and the digits up to
    the first other character, and gives 0 for no digit, and LONG_MIN or LONG_MAX
    for a number out of the range of a long.

    wxString::ToLong (wcstol) stores the same value, also when it gives false, for
    a text with no white space at the start that is not ASCII (wcstol skips such a
    character in some locales: to_long). The callers give it such a text, for
    example digits only.
    """
    match = _LEADING.match(text)
    value = _value(match.group(1), match.group(2))
    if value is None:
        return LONG_MIN if match.group(1) == "-" else LONG_MAX
    return value


def atoi(text: str) -> int:
    """Return ``atoi(text)``: ``(int)strtol(text)``.

    glibc and macOS give the cast of the long to an int. MSVC gives INT_MAX or
    INT_MIN on overflow (Microsoft documentation of atoi): its long is an int, thus
    the cast of the long of strtol gives the same number.
    """
    return to_int(strtol(text))


def to_int(value: int) -> int:
    """Return a long as static_cast<int> gives it: the low 32 bits, with a sign."""
    return (value + 2**31) % 2**32 - 2**31
