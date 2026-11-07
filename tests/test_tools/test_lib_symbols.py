"""Tests for parsers/lib_symbols.py: library symbols as KiCad copies them into a schematic."""

import os
import sys
import time
from pathlib import Path

import pytest

from kicad_workbench.parsers import lib_symbols
from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.lib_symbols import (
    PlacedField,
    files_stamp,
    flatten,
    placed_fields,
    read_library,
    symbol_chain,
    symbol_pins,
    unit_count,
)
from kicad_workbench.parsers.sexpr import Node, parse, property_field

from .file_times import move_clock_forward

PINS = (
    '(symbol "R_0_1" (rectangle (start -1 -2) (end 1 2)))\n'
    '(symbol "R_1_1" (pin passive line (at 0 3 270) (length 1) (name "~") (number "1"))\n'
    '  (pin passive line (at 0 -3 90) (length 1) (name "~") (number "2")))'
)


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    """An empty library cache for each test."""
    monkeypatch.setattr(lib_symbols, "_cache", {})


def prop(name: str, value: str, extra: str = "") -> str:
    return f'(property "{name}" "{value}" (at 0 0 0){extra} (effects (font (size 1.27 1.27))))'


ROOT = "\n".join(
    [
        '(symbol "R"',
        "(exclude_from_sim yes)",
        "(in_bom no)",
        prop("Reference", "R"),
        prop("Value", "R"),
        prop("Footprint", "FP"),
        prop("Datasheet", "DS"),
        prop("Description", "Resistor"),
        prop("ki_keywords", "res"),
        prop("ki_fp_filters", "R_*"),
        prop("MPN", "M1"),
        prop("Extra", "X"),
        prop("ki_locked", ""),
        PINS,
        ")",
    ]
)


def symbol(name: str, parent: str, *items: str) -> str:
    return f'(symbol "{name}"\n(extends "{parent}")\n' + "\n".join(items) + "\n)"


def library(*symbols: str, version: int = 20251024) -> str:
    return (
        f'(kicad_symbol_lib (version {version}) (generator "test")\n' + "\n".join(symbols) + "\n)\n"
    )


@pytest.fixture
def lib(tmp_path):
    """Write a library and return its symbols."""

    def make(*symbols: str, version: int = 20251024) -> dict:
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library(*symbols, version=version), encoding="utf-8")
        return read_library(path)

    return make


def flat(symbols: dict, name: str, kicad: int = 10) -> Node:
    return parse(flatten(symbol_chain(symbols, name), f"Device:{name}", kicad))


def fields(node: Node) -> list[tuple[str, str]]:
    return [property_field(p) for p in node.children("property")]


def heads(node: Node) -> list[str]:
    return [child.head for child in node.children() if child.head != "property"]


class TestNotDerived:
    def test_text_is_kept(self, lib):
        text = flatten(symbol_chain(lib(ROOT), "R"), "Device:R", 10)
        assert text == ROOT.replace('(symbol "R"', '(symbol "Device:R"', 1)

    def test_library_name(self, lib):
        node = flat(lib(ROOT), "R")
        assert node.value(0) == "Device:R"
        assert [sub.value(0) for sub in node.children("symbol")] == ["R_0_1", "R_1_1"]


class TestDerived:
    def test_mandatory_fields(self, lib):
        derived = symbol(
            "R_US", "R", prop("Value", "R_US"), prop("Footprint", ""), prop("Description", "US")
        )
        node = flat(lib(ROOT, derived), "R_US")
        assert node.value(0) == "Device:R_US"
        # An empty field of the derived symbol keeps the field of the parent.
        assert fields(node)[:5] == [
            ("Reference", "R"),
            ("Value", "R_US"),
            ("Footprint", "FP"),
            ("Datasheet", "DS"),
            ("Description", "US"),
        ]

    @pytest.mark.parametrize("version, footprint", [(20241209, "FP"), (20251024, "~")])
    def test_tilde_is_an_empty_field_in_an_old_file(self, lib, version, footprint):
        # Before version 20250318, KiCad reads "~" as an empty text: the
        # derived symbol keeps the footprint of the parent.
        node = flat(lib(ROOT, symbol("R_US", "R", prop("Footprint", "~")), version=version), "R_US")
        assert fields(node)[2] == ("Footprint", footprint)

    def test_blanks_of_a_mandatory_field_are_removed(self, lib):
        # SCH_FIELD::SetText: a footprint " " is empty, thus the parent value stays.
        derived = symbol("R_US", "R", prop("Footprint", " \t"), prop("Value", " R_US "))
        assert fields(flat(lib(ROOT, derived), "R_US"))[1:3] == [
            ("Value", " R_US "),
            ("Footprint", "FP"),
        ]
        node = lib(ROOT, derived)
        assert lib_symbols.flattened_fields(symbol_chain(node, "R_US"), 10)[1:3] == [
            ("Value", "R_US"),
            ("Footprint", "FP"),
        ]

    def test_units_pins_and_drawings_of_the_root(self, lib):
        node = flat(lib(ROOT, symbol("R_US", "R", prop("Value", "R_US"))), "R_US")
        assert [sub.value(0) for sub in node.children("symbol")] == ["R_US_0_1", "R_US_1_1"]
        assert symbol_pins(node) == ["1", "2"]
        assert node.first("extends") is None

    def test_user_fields(self, lib):
        derived = symbol("R_US", "R", prop("MPN", "M2"), prop("Tolerance", "1%"))
        node = flat(lib(ROOT, derived), "R_US")
        # KiCad removes the field of the parent and adds the new field at the end.
        assert fields(node)[-4:] == [
            ("Extra", "X"),
            ("ki_locked", ""),
            ("MPN", "M2"),
            ("Tolerance", "1%"),
        ]
        assert ("MPN", "M1") not in fields(node)

    @pytest.mark.parametrize(
        "items, keywords, filters",
        [
            ((), "res", "R_*"),
            ((prop("ki_keywords", ""), prop("ki_fp_filters", "  ")), "res", "R_*"),
            ((prop("ki_keywords", "us"), prop("ki_fp_filters", "R_US_*")), "us", "R_US_*"),
        ],
    )
    def test_keywords_and_footprint_filters(self, lib, items, keywords, filters):
        node = flat(lib(ROOT, symbol("R_US", "R", *items)), "R_US")
        values = dict(fields(node))
        assert (values["ki_keywords"], values["ki_fp_filters"]) == (keywords, filters)
        assert [name for name, _ in fields(node)].count("ki_keywords") == 1

    def test_flags_of_the_derived_symbol_are_not_used(self, lib):
        derived = symbol("R_US", "R", "(in_bom yes)", "(exclude_from_sim no)")
        node = flat(lib(ROOT, derived), "R_US")
        assert node.first("in_bom").value(0) == "no"
        assert node.first("exclude_from_sim").value(0) == "yes"

    def test_unit_lock_of_the_derived_symbol_is_not_used(self, lib):
        root = ROOT.replace(prop("ki_locked", ""), "")
        node = flat(lib(root, symbol("R_US", "R", prop("ki_locked", ""))), "R_US")
        assert "ki_locked" not in dict(fields(node))
        node = flat(lib(ROOT, symbol("R_US", "R")), "R_US")
        assert "ki_locked" in dict(fields(node))

    def test_duplicate_user_field_gets_a_new_name(self, lib):
        derived = symbol("R_US", "R", prop("Tolerance", "1%"), prop("Tolerance", "5%"))
        node = flat(lib(ROOT, derived), "R_US")
        assert fields(node)[-2:] == [("Tolerance", "1%"), ("Tolerance_1", "5%")]

    def test_user_field_with_all_names_used_is_skipped(self, lib):
        names = ["Tol"] + [f"Tol_{i}" for i in range(1, 10)]
        derived = symbol("R_US", "R", *(prop(n, n) for n in names), prop("Tol", "skipped"))
        node = flat(lib(ROOT, derived), "R_US")
        assert ("Tol", "skipped") not in fields(node)
        assert len([n for n, _ in fields(node) if n.startswith("Tol")]) == 10

    @pytest.mark.parametrize(
        "kicad, expected",
        [
            # KiCad 8 and 9: an ID after the mandatory IDs makes a user field,
            # and its name is in use.
            (9, [("Footprint", "FP"), ("Footprint_1", "X")]),
            # KiCad 10 ignores the ID.
            (10, [("Footprint", "X")]),
        ],
    )
    def test_field_id(self, lib, kicad, expected):
        derived = symbol("R_US", "R", prop("Footprint", "X", " (id 7)"))
        node = flat(lib(ROOT, derived), "R_US", kicad)
        assert [f for f in fields(node) if f[0].startswith("Footprint")] == expected

    def test_field_id_that_is_not_a_number(self, lib):
        derived = symbol("R_US", "R", prop("Value", "X", " (id x)"))
        with pytest.raises(KicadFileError, match="not a number"):
            flat(lib(ROOT, derived), "R_US")

    def test_description_of_kicad_7(self, lib):
        node = flat(lib(ROOT, symbol("R_US", "R", prop("ki_description", "Old"))), "R_US")
        # The description field of the derived symbol replaces the field of the
        # parent: KiCad reads only its text from "ki_description".
        assert dict(fields(node))["ki_description"] == "Old"
        assert "Description" not in dict(fields(node))


class TestChain:
    MID = symbol(
        "R_MID",
        "R",
        "(in_bom yes)",
        "(on_board no)",
        prop("Footprint", "FP2"),
        prop("ki_keywords", "mid"),
    )

    def test_three_levels(self, lib):
        node = flat(lib(ROOT, self.MID, symbol("R_LOW", "R_MID", prop("Value", "LOW"))), "R_LOW")
        values = dict(fields(node))
        assert (values["Value"], values["Footprint"], values["ki_keywords"]) == (
            "LOW",
            "FP2",
            "mid",
        )
        # The flags come from the parent of the symbol, R_MID.
        assert node.first("in_bom").value(0) == "yes"
        assert node.first("on_board").value(0) == "no"
        assert node.first("exclude_from_sim") is None
        assert [sub.value(0) for sub in node.children("symbol")] == ["R_LOW_0_1", "R_LOW_1_1"]

    def test_empty_keywords_of_a_middle_level(self, lib):
        # GetKeyWords() of a derived symbol with no keywords gives the keywords of its parent.
        mid = symbol("R_MID", "R", prop("ki_keywords", ""), prop("ki_fp_filters", ""))
        node = flat(lib(ROOT, mid, symbol("R_LOW", "R_MID")), "R_LOW", 9)
        values = dict(fields(node))
        assert (values["ki_keywords"], values["ki_fp_filters"]) == ("res", "R_*")

    def test_missing_parent(self, lib):
        with pytest.raises(
            KicadFileError, match='no parent symbol "X" for the derived symbol "R_US"'
        ):
            symbol_chain(lib(ROOT, symbol("R_US", "X")), "R_US")

    def test_loop(self, lib):
        symbols = lib(symbol("A", "B"), symbol("B", "A"))
        with pytest.raises(KicadFileError, match="derived from itself"):
            symbol_chain(symbols, "A")

    def test_unknown_symbol(self, lib):
        with pytest.raises(KeyError):
            symbol_chain(lib(ROOT), "C")

    def test_slash_in_names(self, lib):
        root = ROOT.replace('"R"', '"A{slash}B"', 1).replace('"R_', '"A{slash}B_')
        symbols = lib(root, symbol("A{slash}C", "A{slash}B"))
        assert sorted(symbols) == ["A/B", "A/C"]
        node = parse(flatten(symbol_chain(symbols, "A/C"), "Device:A/C", 10))
        assert [sub.value(0) for sub in node.children("symbol")] == ["A/C_0_1", "A/C_1_1"]

    def test_unit_name_that_kicad_cannot_read(self, lib):
        root = ROOT.replace('"R_0_1"', '"X_0_1"')
        with pytest.raises(KicadFileError, match="Invalid symbol unit name prefix X_0_1"):
            flat(lib(root, symbol("R_US", "R")), "R_US")


def files(*names: str) -> str:
    return "(embedded_files " + " ".join(f'(file (name "{n}") (type font))' for n in names) + ")"


class TestEmbeddedFiles:
    def chain(self, lib, root_files: str):
        root = ROOT[:-1] + root_files + "\n)"
        mid = symbol("R_MID", "R", files("b"))
        return lib(root, mid, symbol("R_LOW", "R_MID", files("a", "c")))

    def names(self, node: Node) -> list[str]:
        return [f.first("name").value(0) for f in node.first("embedded_files").children("file")]

    def test_kicad_9_adds_the_files_of_each_level(self, lib):
        assert self.names(flat(self.chain(lib, files("a")), "R_LOW", 9)) == ["a", "b", "c"]

    def test_kicad_10_adds_only_the_files_of_the_symbol(self, lib):
        assert self.names(flat(self.chain(lib, files("a")), "R_LOW", 10)) == ["a", "c"]

    def test_root_with_no_files(self, lib):
        assert self.names(flat(self.chain(lib, ""), "R_LOW", 10)) == ["a", "c"]

    def test_fields_before_the_files_in_an_empty_root(self, lib):
        symbols = lib('(symbol "E")', symbol("E_1", "E", prop("Value", "V"), files("a")))
        text = flatten(symbol_chain(symbols, "E_1"), "Device:E_1", 10)
        assert text.index('(property "Value"') < text.index("(embedded_files")
        assert [n.head for n in parse(text).children()] == ["property", "embedded_files"]


class TestPins:
    def test_body_styles(self):
        node = parse(
            '(symbol "Device:G" (pin input line (number "0"))'
            ' (symbol "G_0_1" (pin input line (number "1")))'
            ' (symbol "G_1_0" (pin input line (number "5")))'
            ' (symbol "G_1_1" (pin input line (number "2")) (pin input line (number "2")))'
            ' (symbol "G_1_2" (pin input line (number "3")))'
            ' (symbol "G_2_1" (pin input line (number "4")) (pin input line)))'
        )
        # "<name>_<unit>_<body style>". A pin outside a sub-symbol has the body
        # style 1, and body style 0 is in all body styles. Each pin with a
        # duplicate number is a pin.
        assert symbol_pins(node) == ["0", "1", "5", "2", "2", "4", ""]
        assert symbol_pins(node, body_style=2) == ["5", "3"]

    def test_pin_number_has_no_spaces(self):
        # SCH_PIN::SetNumber replaces each space with "_".
        node = parse('(symbol "Device:G" (pin input line (number " A 1")))')
        assert symbol_pins(node) == ["_A_1"]


class TestPlacedFields:
    def test_mandatory_first_then_user_fields(self):
        node = parse(
            '(symbol "Device:R" (property "MPN" "a") (property "Value" "R")'
            ' (property "Reference" "R") (property "MPN" "b") (property "ki_keywords" "k")'
            ' (property "ki_locked" ""))'
        )
        fields = placed_fields(node, 10)
        # A duplicate user field gets a new name, as KiCad reads it.
        # The five mandatory fields, also with no property, then the user fields.
        assert [f.name for f in fields] == [
            "Reference",
            "Value",
            "Footprint",
            "Datasheet",
            "Description",
            "MPN",
            "MPN_1",
        ]
        assert [f.node is None for f in fields[:5]] == [False, False, True, True, True]
        assert property_field(fields[-1].node) == ("MPN", "b")

    def test_description_of_kicad_7(self):
        # "ki_description" changes only the text of the default description field.
        node = parse('(symbol "Device:R" (property "ki_description" "Old"))')
        assert placed_fields(node, 10)[4] == PlacedField("Description", None, "Old")
        node = parse(
            '(symbol "Device:R" (property "Description" "New" (at 1 2 0))'
            ' (property "ki_description" "Old"))'
        )
        field = placed_fields(node, 10)[4]
        assert (property_field(field.node), field.text) == (("Description", "New"), "Old")

    def test_unit_count(self):
        assert unit_count(parse('(symbol "Device:R")')) == 1
        node = parse('(symbol "Device:U" (symbol "U_0_1") (symbol "U_3_1") (symbol "U_2_2"))')
        assert unit_count(node) == 3


class TestReadLibrary:
    def test_directory_library(self, tmp_path):
        directory = tmp_path / "Device.kicad_symdir"
        directory.mkdir()
        (directory / "a.kicad_sym").write_text(library(ROOT, version=20251024), encoding="utf-8")
        (directory / "b.kicad_sym").write_text(
            library(symbol("R_US", "R"), version=20260101), encoding="utf-8"
        )
        symbols = read_library(directory)
        # The parent is in an other file. Each symbol has the version of its file.
        assert [s.name for s in symbol_chain(symbols, "R_US")] == ["R_US", "R"]
        assert (symbols["R_US"].version, symbols["R"].version) == (20260101, 20251024)
        assert symbols["R"].file == directory / "a.kicad_sym"

    def test_symbol_in_more_than_one_file_of_a_directory(self, tmp_path):
        # KiCad reads the files in the order of the file system.
        directory = tmp_path / "Device.kicad_symdir"
        directory.mkdir()
        (directory / "a.kicad_sym").write_text(library(ROOT), encoding="utf-8")
        (directory / "b.kicad_sym").write_text(
            library(symbol("R_US", "R"), ROOT.replace('"Resistor"', '"Other"')), encoding="utf-8"
        )
        symbols = read_library(directory)
        with pytest.raises(KicadFileError, match='"R" is in more than one file'):
            symbol_chain(symbols, "R_US")

    def test_later_symbol_of_a_file_replaces_the_earlier_one(self, lib):
        symbols = lib(ROOT, ROOT.replace('"Resistor"', '"Later"'))
        assert dict(fields(symbols["R"].node))["Description"] == "Later"
        assert symbols["R"].other_files == ()

    def test_cache_keeps_the_library_that_was_used_last(self, tmp_path, monkeypatch):
        move_clock_forward(monkeypatch)
        paths = []
        for index in range(lib_symbols._CACHE_SIZE + 1):
            path = tmp_path / f"L{index}.kicad_sym"
            path.write_text(library(ROOT), encoding="utf-8")
            paths.append(path)
        first = read_library(paths[0])
        for path in paths[1:]:
            assert read_library(paths[0]) is first
            read_library(path)
        assert read_library(paths[0]) is first
        assert paths[1] not in lib_symbols._cache

    def test_cache_until_a_file_changes(self, tmp_path, monkeypatch):
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library(ROOT), encoding="utf-8")
        move_clock_forward(monkeypatch)
        first = read_library(path)
        assert read_library(path) is first
        path.write_text(library(ROOT, symbol("R_US", "R")), encoding="utf-8")
        assert sorted(read_library(path)) == ["R", "R_US"]

    def test_change_with_the_same_stamp_is_read(self, tmp_path, monkeypatch):
        # Two writes in one interval of the time resolution of the file system
        # can give the same stamp. For a new file, the cache also compares the
        # content, thus it gives the symbols of the second write.
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library(ROOT), encoding="utf-8")
        stamp = files_stamp([path])[0]
        monkeypatch.setattr(lib_symbols, "files_stamp", lambda files: (stamp, False))
        first = read_library(path)
        assert read_library(path) is first
        path.write_text(library(ROOT.replace('"Resistor"', '"Resistox"')), encoding="utf-8")
        assert dict(fields(read_library(path)["R"].node))["Description"] == "Resistox"

    def test_digest_only_until_the_stamp_is_reliable(self, tmp_path, monkeypatch):
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library(ROOT), encoding="utf-8")
        stamp = files_stamp([path])[0]
        monkeypatch.setattr(lib_symbols, "files_stamp", lambda files: (stamp, False))
        first = read_library(path)
        assert lib_symbols._cache[path][1] is not None
        monkeypatch.setattr(lib_symbols, "files_stamp", lambda files: (stamp, True))
        assert read_library(path) is first
        assert lib_symbols._cache[path][1] is None

        def no_digest(self):
            raise AssertionError("the content was compared")

        monkeypatch.setattr(lib_symbols.LibraryFiles, "digest", property(no_digest))
        assert read_library(path) is first

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no change time")
    def test_replaced_file_with_the_old_time_and_size(self, tmp_path, monkeypatch):
        # A program (for example cp -p) can write a file and give it the old
        # modification time. The change time shows the change.
        path = tmp_path / "Device.kicad_sym"
        path.write_text(library(ROOT), encoding="utf-8")
        move_clock_forward(monkeypatch)
        first = read_library(path)
        assert read_library(path) is first
        stat = path.stat()
        # The second write must be in a later tick of the kernel clock (10 ms
        # or less), else it has the same change time.
        time.sleep(0.05)
        path.write_text(library(ROOT.replace('"Resistor"', '"Resistox"')), encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert (path.stat().st_mtime_ns, path.stat().st_size) == (stat.st_mtime_ns, stat.st_size)
        assert dict(fields(read_library(path)["R"].node))["Description"] == "Resistox"

    def test_library_with_no_version(self, tmp_path):
        path = tmp_path / "Device.kicad_sym"
        path.write_text(f"(kicad_symbol_lib\n{ROOT}\n)\n", encoding="utf-8")
        assert read_library(path)["R"].version is None

    def test_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            read_library(Path(tmp_path / "none.kicad_sym"))


class TestFilesStamp:
    def test_stamp(self, tmp_path):
        path = tmp_path / "Device.kicad_sym"
        path.write_text("(kicad_symbol_lib)", encoding="utf-8")
        stat = path.stat()
        change_ns = 0 if sys.platform == "win32" else stat.st_ctime_ns
        stamp, _ = files_stamp([path])
        assert stamp == [["Device.kicad_sym", stat.st_mtime_ns, change_ns, stat.st_ino, 18]]

    def test_reliable_only_for_a_file_older_than_2_s(self, tmp_path, monkeypatch):
        path = tmp_path / "Device.kicad_sym"
        path.write_text("(kicad_symbol_lib)", encoding="utf-8")
        stamp, _ = files_stamp([path])
        newest_ns = max(stamp[0][1], stamp[0][2])
        for now_ns, reliable in (
            (newest_ns + 3_000_000_000, True),
            (newest_ns + 1_000_000_000, False),
            # A time in the future: a clock that is not correct.
            (newest_ns - 3_600_000_000_000, False),
        ):
            monkeypatch.setattr(lib_symbols, "_now_ns", lambda now_ns=now_ns: now_ns)
            assert files_stamp([path])[1] is reliable


def test_body_points_of_the_graphic_items():
    from kicad_workbench.parsers.lib_symbols import body_points

    symbol = parse(
        '(symbol "L:X" (symbol "X_0_1" (polyline (pts (xy 0 0) (xy 0 -1)))'
        " (circle (center 1 1) (radius 0.5)) (rectangle (start -2 -2) (end 2 2))"
        " (arc (start 0 1) (mid 1 0) (end 0 -1)) (bezier (pts (xy 0 0) (xy 9 9) (xy 8 8) (xy 3 3))))"
        ' (symbol "X_2_1" (polyline (pts (xy 7 7) (xy 7 8)))))'
    )
    assert body_points(symbol, 1, {1}) == [
        (0.0, 0.0),
        (0.0, -1.0),
        (0.5, 1.0),
        (1.5, 1.0),
        (1.0, 0.5),
        (1.0, 1.5),
        (-2.0, -2.0),
        (2.0, 2.0),
        (0.0, 1.0),
        (1.0, 0.0),
        (0.0, -1.0),
        (0.0, 0.0),
        (3.0, 3.0),
    ]
    assert (7.0, 7.0) in body_points(symbol, 1, {2})
