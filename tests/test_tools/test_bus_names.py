"""Tests of bus_names: SCH_CONNECTION::IsBusLabel of KiCad 10.0.6."""

import pytest

from kicad_workbench.parsers.bus_names import (
    is_bus_label,
    parse_bus_group,
    parse_bus_vector,
    unescape_string,
)

# (label text, after UnescapeString, vector, group) from the code of KiCad 10.0.6.
CASES = [
    ("D[0..7]", "D[0..7]", True, False),
    ("D[7..0]+", "D[7..0]+", True, False),
    ("D[3..3]", "D[3..3]", False, False),
    ("D[0..7]x", "D[0..7]x", False, False),
    ("D [0..7]", "D [0..7]", False, False),
    ('"D X"[0..1]', '"D X"[0..1]', True, False),
    ("D_{[1..2]}", "D_{[1..2]}", True, False),
    ("~{BE[0..3]}", "~{BE[0..3]}", True, False),
    ("D[1..7", "D[1..7", True, False),
    ("D[0..7", "D[0..7", False, False),
    ("D[0...2]", "D[0...2]", False, False),
    ("[0..3]", "[0..3]", True, False),
    ("{A B}", "{A B}", False, True),
    ("BUS{A,B}", "BUS{A,B}", False, True),
    ("{}", "{}", False, True),
    ("A{}", "A{}", False, True),
    ("BUS{A}xyz", "BUS{A}xyz", False, True),
    ("BUS{A {B}}", "BUS{A {B}}", False, False),
    ("BUS{A", "BUS{A", False, False),
    ("BUS{slash}", "BUS/", False, False),
    ("~{RESET}", "~{RESET}", False, False),
    ("${VAR}", "${VAR}", False, True),
    ("NET1", "NET1", False, False),
]


@pytest.mark.parametrize("text, unescaped, vector, group", CASES)
def test_bus_label(text, unescaped, vector, group):
    assert unescape_string(text) == unescaped
    assert parse_bus_vector(unescaped) is vector
    assert parse_bus_group(unescaped) is group
    assert is_bus_label(text) is (vector or group)


@pytest.mark.parametrize(
    "text, result",
    [
        ("a{dblquote}b", 'a"b'),
        ("a{unknown}b", "a{unknown}b"),
        ("a{b", "a{b"),
        ("x~{slash}", "x~{slash}"),
        ("{a{b}c}", "{a{b}c}"),
        ("{slash}{comma}", "/,"),
    ],
)
def test_unescape_string(text, result):
    assert unescape_string(text) == result
