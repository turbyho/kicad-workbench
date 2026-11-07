"""Tests of the numbers of the C library as KiCad reads them (parsers/c_numbers.py).

A C long has 64 bits on Linux and macOS and 32 bits on Windows: the fixtures long64
and long32 give each range on each system. The values of Linux are measured with
kicad-cli 8.0.9 to 10.0.7 (spike/kicad-versions/c_numbers_check.py and
sub_symbol_names_check.py); the values of Windows come from the source of KiCad and
the documentation of Microsoft (strtol, atoi).
"""

import pytest

from kicad_workbench.errors import LocaleNumberError
from kicad_workbench.parsers import c_numbers
from kicad_workbench.parsers.annotation import split_reference
from kicad_workbench.parsers.bus_names import parse_bus_vector
from kicad_workbench.parsers.c_numbers import atoi, strtol, to_int, to_long


@pytest.mark.parametrize(
    "text, value",
    [
        ("12", 12),
        (" \t+12", 12),
        ("-0012", -12),
        ("12 ", None),
        ("", None),
        ("1e3", None),
        ("9223372036854775807", 2**63 - 1),
        ("9223372036854775808", None),
        ("-9223372036854775808", -(2**63)),
    ],
)
def test_to_long_with_a_long_of_64_bits(long64, text, value):
    assert to_long(text) == value


@pytest.mark.parametrize(
    "text, value",
    [("2147483647", 2**31 - 1), ("2147483648", None), ("-2147483648", -(2**31))],
)
def test_to_long_with_a_long_of_32_bits(long32, text, value):
    assert to_long(text) == value


def test_to_int():
    assert (to_int(2**32 + 2), to_int(2**31), to_int(-1)) == (2, -(2**31), -1)


@pytest.mark.parametrize("bits", ["long64", "long32"])
@pytest.mark.parametrize(
    "text, value64, value32",
    [
        ("  -12x", -12, -12),
        ("x", 0, 0),
        ("-", 0, 0),
        ("4294967298", 4294967298, 2**31 - 1),
        ("-99999999999999999999", -(2**63), -(2**31)),
        ("9" * 5000, 2**63 - 1, 2**31 - 1),  # int() refuses more than 4300 digits
    ],
)
def test_strtol(request, bits, text, value64, value32):
    request.getfixturevalue(bits)
    assert strtol(text) == (value64 if bits == "long64" else value32)


def test_atoi_with_a_long_of_64_bits(long64):
    # glibc and macOS: (int)strtol. "4294967386" is 2**32 + 90.
    assert atoi("4294967386") == 90
    assert atoi("-4294967206") == 90


def test_atoi_with_a_long_of_32_bits(long32):
    # MSVC: INT_MAX on overflow, which is (int) of the LONG_MAX of 32 bits.
    assert atoi("4294967386") == 2**31 - 1
    assert atoi("-4294967206") == -(2**31)


def test_a_white_space_that_is_not_ascii():
    # Measured with kicad-cli 10.0.6 on Linux: U+2003 is white space in a UTF-8
    # locale, not in the locale C. The server does not know the locale of KiCad.
    with pytest.raises(LocaleNumberError, match="only in some locales"):
        to_long(" 12")
    with pytest.raises(LocaleNumberError):
        to_long(" 12")
    # Not at the start, or no number after it: no number in each locale.
    assert to_long("12 ") is None
    assert to_long(" abc") is None


def test_the_long_of_each_system():
    # Windows (also Cygwin and MSYS2, with the MSVC build of KiCad) has a long of 32 bits.
    for platform, bits in (("win32", 32), ("cygwin", 32), ("msys", 32), ("linux", 64)):
        assert c_numbers.long_bits(platform) == bits, platform


@pytest.mark.parametrize("bits", ["long64", "long32"])
def test_the_readers_with_each_long(request, bits):
    # SCH_REFERENCE::Split (atoi), NET_SETTINGS::ParseBusVector (wxString::ToLong:
    # the value of strtol also on overflow) and ParseAlphaNumericPin.
    request.getfixturevalue(bits)
    windows = bits == "long32"
    assert split_reference("R4294967298").number == (2**31 - 1 if windows else 2)
    # 64 bits: two numbers, a bus. 32 bits: LONG_MAX two times, no bus (begin == end).
    assert parse_bus_vector("D[2147483648..4294967295]") is not windows
