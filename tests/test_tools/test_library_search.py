"""Tests for the library index and the library tools (utils/library_index.py,
tools/library.py)."""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kicad_workbench.parsers import lib_symbols
from kicad_workbench.tools import library as library_tools
from kicad_workbench.utils import library_index, library_tables, user_files
from kicad_workbench.utils.kicad_cli import get_kicad_version
from kicad_workbench.utils.library_index import LibraryItem, library_items, search
from kicad_workbench.utils.library_tables import LibraryRow

from .file_times import move_clock_forward
from .test_sch_hierarchy import sch


def symbol(name: str, *fields: str, extends: str = "") -> str:
    parent = f'\n\t\t(extends "{extends}")' if extends else ""
    props = "".join(f"\n\t\t(property {field}\n\t\t\t(at 0 0 0)\n\t\t)" for field in fields)
    return f'\t(symbol "{name}"{parent}{props}\n\t)\n'


def symbol_lib(*symbols: str) -> str:
    return (
        '(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "kicad_symbol_editor")\n'
        + "".join(symbols)
        + ")\n"
    )


def footprint(name: str, descr: str = "", tags: str = "") -> str:
    lines = [f'(footprint "{name}"', "\t(version 20241229)"]
    if descr:
        lines.append(f"\t(descr {descr})")
    if tags:
        lines.append(f"\t(tags {tags})")
    return "\n".join(lines) + "\n)\n"


def row(name: str, path: Path, **values) -> LibraryRow:
    return LibraryRow(
        name=name,
        type=values.get("type", "KiCad"),
        uri=str(path),
        options="",
        description="",
        disabled=values.get("disabled", False),
        hidden=values.get("hidden", False),
        scope=values.get("scope", "global"),
        table=path.parent / "sym-lib-table",
        path=path,
    )


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """An empty index cache in tmp_path, on disk and in memory."""
    directory = tmp_path / "cache"
    monkeypatch.setattr(library_index, "cache_dir", lambda: directory)
    monkeypatch.setattr(library_index, "_memory", {})
    return directory


def by_name(items):
    return {item.name: item for item in items}


class TestSymbolLibrary:
    def test_fields(self, tmp_path):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(
            symbol_lib(
                symbol(
                    "R",
                    '"Reference" "R"',
                    '"Footprint" "Resistor_SMD:R_0603_1608Metric"',
                    '"Description" "Resistor"',
                    '"ki_keywords" "R res"',
                ),
                # KiCad 9 writes "private" before the name of a private field.
                symbol("C", 'private "Description" "Capacitor"', '"ki_keywords" "cap"'),
                # KiCad 7 and earlier: "ki_description".
                symbol("L", '"ki_description" "Inductor"'),
                # The name of a mandatory field has no case; a later field wins.
                symbol("D", '"description" "Old"', '"Description" "Diode"'),
                # A field with the name "private" is quoted.
                symbol("Q", '"private" "x"', '"Description" "Transistor"'),
            ),
            encoding="utf-8",
        )
        items, problems = library_items("symbol", row("Lib", path))
        assert problems == []
        found = by_name(items)
        assert found["R"] == LibraryItem(
            "Lib", "R", "Resistor", "R res", "Resistor_SMD:R_0603_1608Metric", "Resistor"
        )
        assert found["R"].lib_id == "Lib:R"
        assert (found["C"].description, found["C"].keywords) == ("Capacitor", "cap")
        assert found["L"].description == "Inductor"
        assert found["D"].description == "Diode"
        assert found["Q"].description == "Transistor"

    def test_name_escapes(self, tmp_path):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(
            symbol_lib(symbol("A{slash}B"), symbol("Other:C"), symbol("D", extends="A{slash}B")),
            encoding="utf-8",
        )
        items, problems = library_items("symbol", row("Lib", path))
        assert problems == []
        assert sorted(item.name for item in items) == ["A/B", "C", "D"]

    def test_derived_symbol_gets_empty_fields_of_parent(self, tmp_path):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(
            symbol_lib(
                symbol(
                    "Base",
                    '"Footprint" "Package_SO:SOIC-8"',
                    '"Description" "Op amp"',
                    '"ki_keywords" "opamp"',
                ),
                symbol("Child", '"Description" ""', '"ki_keywords" "own"', extends="Base"),
                symbol("Grandchild", extends="Child"),
            ),
            encoding="utf-8",
        )
        items, problems = library_items("symbol", row("Lib", path))
        assert problems == []
        found = by_name(items)
        assert (found["Child"].description, found["Child"].keywords) == ("Op amp", "own")
        assert found["Child"].footprint == "Package_SO:SOIC-8"
        assert (found["Grandchild"].description, found["Grandchild"].keywords) == (
            "Op amp",
            "own",
        )
        # The chooser column shows only the own field.
        assert (found["Base"].description_field, found["Grandchild"].description_field) == (
            "Op amp",
            "",
        )

    def test_missing_parent_and_cycle(self, tmp_path):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(
            symbol_lib(
                symbol("Orphan", '"Description" "Own"', extends="Gone"),
                symbol("A", extends="B"),
                symbol("B", extends="A"),
            ),
            encoding="utf-8",
        )
        items, problems = library_items("symbol", row("Lib", path))
        assert by_name(items)["Orphan"].description == "Own"
        assert by_name(items)["A"].description == ""
        assert problems == [f"No parent for extended symbol Orphan found in library {path}."]

    def test_directory_library(self, tmp_path):
        # KiCad 10: a symbol library can be a directory of .kicad_sym files.
        path = tmp_path / "Lib.kicad_symdir"
        path.mkdir()
        (path / "a.kicad_sym").write_text(
            symbol_lib(symbol("Base", '"Description" "First"')), encoding="utf-8"
        )
        (path / "b.kicad_sym").write_text(
            symbol_lib(symbol("Child", extends="Base")), encoding="utf-8"
        )
        (path / "c.kicad_sym").write_text(
            symbol_lib(symbol("Base", '"Description" "Second"')), encoding="utf-8"
        )
        (path / "notes.txt").write_text("not a library", encoding="utf-8")
        items, problems = library_items("symbol", row("Lib", path))
        assert problems == []
        found = by_name(items)
        assert sorted(found) == ["Base", "Child"]
        # A later file replaces a symbol, and the parent can be in an other file.
        assert found["Base"].description == "Second"
        assert found["Child"].description == "Second"

    def test_file_that_cannot_be_read(self, tmp_path):
        path = tmp_path / "Lib.kicad_symdir"
        path.mkdir()
        (path / "a.kicad_sym").write_text(symbol_lib(symbol("A")), encoding="utf-8")
        (path / "b.kicad_sym").write_text("(kicad_symbol_lib (symbol", encoding="utf-8")
        items, problems = library_items("symbol", row("Lib", path))
        assert [item.name for item in items] == ["A"]
        assert len(problems) == 1 and str(path / "b.kicad_sym") in problems[0]

    def test_missing_library(self, tmp_path):
        items, problems = library_items("symbol", row("Lib", tmp_path / "Gone.kicad_sym"))
        assert items == []
        assert problems == [f"The library Lib is not at {tmp_path / 'Gone.kicad_sym'}."]


class TestFootprintLibrary:
    def test_footprints(self, tmp_path):
        path = tmp_path / "Lib.pretty"
        path.mkdir()
        # The name of a footprint is the name of its file.
        (path / "R_0603.kicad_mod").write_text(
            footprint("Other", '"Resistor SMD 0603"', '"resistor"'), encoding="utf-8"
        )
        # A description can be a number token.
        (path / "C_0508.kicad_mod").write_text(footprint("C_0508", "0508", "cap"), encoding="utf-8")
        (path / "Bare.kicad_mod").write_text(footprint("Bare"), encoding="utf-8")
        items, problems = library_items("footprint", row("Lib", path))
        assert problems == []
        found = by_name(items)
        assert found["R_0603"] == LibraryItem("Lib", "R_0603", "Resistor SMD 0603", "resistor")
        assert (found["C_0508"].description, found["C_0508"].keywords) == ("0508", "cap")
        assert (found["Bare"].description, found["Bare"].keywords) == ("", "")


class TestIndexCache:
    def write(self, path: Path, description: str) -> None:
        path.write_text(symbol_lib(symbol("A", f'"Description" "{description}"')), encoding="utf-8")

    def test_index_on_disk_is_used_after_a_restart(self, tmp_path, cache, monkeypatch):
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        move_clock_forward(monkeypatch)
        library_items("symbol", row("Lib", path))
        assert len(list(cache.glob("symbol-*.json"))) == 1
        assert not list(cache.glob("*.tmp"))

        # A new server process: no index in memory.
        monkeypatch.setattr(library_index, "_memory", {})

        def no_read(*args):
            raise AssertionError("the library was read again")

        monkeypatch.setattr(library_index, "_read", no_read)
        items, _ = library_items("symbol", row("Lib", path))
        assert items[0].description == "one"
        # The index in memory is used too.
        cache_file = next(cache.glob("symbol-*.json"))
        cache_file.unlink()
        items, _ = library_items("symbol", row("Lib", path))
        assert items[0].description == "one"

    def test_changed_file_is_read_again(self, tmp_path, monkeypatch):
        # The files are old: only the stamp shows the changes.
        move_clock_forward(monkeypatch)
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        library_items("symbol", row("Lib", path))
        self.write(path, "three")
        assert library_items("symbol", row("Lib", path))[0][0].description == "three"
        # Same size, other time.
        stat = path.stat()
        self.write(path, "eight")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert library_items("symbol", row("Lib", path))[0][0].description == "eight"

    def same_stamp(self, path: Path, monkeypatch, reliable: bool) -> None:
        """Give each read of the library the current stamp of path."""
        stamp = lib_symbols.files_stamp([path])[0]
        monkeypatch.setattr(lib_symbols, "files_stamp", lambda files: (stamp, reliable))

    def test_change_with_the_same_stamp_is_read(self, tmp_path, cache, monkeypatch):
        # Two writes in one interval of the time resolution of the file system
        # can give the same stamp. For a new file, the index also compares the
        # content, in memory and on the disk.
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        self.same_stamp(path, monkeypatch, reliable=False)
        assert library_items("symbol", row("Lib", path))[0][0].description == "one"
        self.write(path, "two")
        assert library_items("symbol", row("Lib", path))[0][0].description == "two"
        # A new server process: the index on the disk has the digest of "two".
        monkeypatch.setattr(library_index, "_memory", {})
        assert library_items("symbol", row("Lib", path))[0][0].description == "two"
        self.write(path, "six")
        monkeypatch.setattr(library_index, "_memory", {})
        assert library_items("symbol", row("Lib", path))[0][0].description == "six"

    def test_digest_only_until_the_stamp_is_reliable(self, tmp_path, cache, monkeypatch):
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        self.same_stamp(path, monkeypatch, reliable=False)
        library_items("symbol", row("Lib", path))
        key = ("symbol", path, "Lib")
        assert library_index._memory[key][1] is not None
        cache_file = next(cache.glob("symbol-*.json"))
        assert json.loads(cache_file.read_text(encoding="utf-8"))["digest"] is not None
        self.same_stamp(path, monkeypatch, reliable=True)
        assert library_items("symbol", row("Lib", path))[0][0].description == "one"
        assert library_index._memory[key][1] is None
        # A new server process: the index on the disk is written again with no
        # digest.
        monkeypatch.setattr(library_index, "_memory", {})
        assert library_items("symbol", row("Lib", path))[0][0].description == "one"
        assert json.loads(cache_file.read_text(encoding="utf-8"))["digest"] is None

        def no_digest(self):
            raise AssertionError("the content was compared")

        monkeypatch.setattr(lib_symbols.LibraryFiles, "digest", property(no_digest))
        assert library_items("symbol", row("Lib", path))[0][0].description == "one"
        monkeypatch.setattr(library_index, "_memory", {})
        assert library_items("symbol", row("Lib", path))[0][0].description == "one"

    def test_index_of_an_old_file_has_no_digest(self, tmp_path, cache, monkeypatch):
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        move_clock_forward(monkeypatch)
        library_items("symbol", row("Lib", path))
        cache_file = next(cache.glob("symbol-*.json"))
        assert json.loads(cache_file.read_text(encoding="utf-8"))["digest"] is None

    def test_new_file_in_directory_library(self, tmp_path, monkeypatch):
        move_clock_forward(monkeypatch)
        path = tmp_path / "Lib.pretty"
        path.mkdir()
        (path / "A.kicad_mod").write_text(footprint("A"), encoding="utf-8")
        assert len(library_items("footprint", row("Lib", path))[0]) == 1
        (path / "B.kicad_mod").write_text(footprint("B"), encoding="utf-8")
        assert len(library_items("footprint", row("Lib", path))[0]) == 2

    def test_bad_index_file_is_replaced(self, tmp_path, cache, monkeypatch):
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        move_clock_forward(monkeypatch)
        library_items("symbol", row("Lib", path))
        cache_file = next(cache.glob("symbol-*.json"))
        good = json.loads(cache_file.read_text(encoding="utf-8"))
        texts = ["{not json", "[]", '{"version": 0}']
        for key, value in (("entries", None), ("entries", [{"name": 1}]), ("problems", [1])):
            texts.append(json.dumps({**good, key: value}))
        for text in texts:
            cache_file.write_text(text, encoding="utf-8")
            monkeypatch.setattr(library_index, "_memory", {})
            assert library_items("symbol", row("Lib", path))[0][0].description == "one"
        version = json.loads(cache_file.read_text(encoding="utf-8"))["version"]
        assert version == library_index._INDEX_VERSION

    def test_no_writable_cache(self, tmp_path, monkeypatch):
        blocker = tmp_path / "file"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(library_index, "cache_dir", lambda: blocker / "cache")
        path = tmp_path / "Lib.kicad_sym"
        self.write(path, "one")
        items, problems = library_items("symbol", row("Lib", path))
        assert (items[0].description, problems) == ("one", [])

    def test_text_with_all_character_sets(self, tmp_path, cache, monkeypatch):
        text = "Odpor 电阻 مقاومة Ω"
        path = tmp_path / "Knihovna_电阻.kicad_sym"
        self.write(path, text)
        move_clock_forward(monkeypatch)
        library_items("symbol", row("Lib", path))
        assert len(list(cache.glob("symbol-*.json"))) == 1
        monkeypatch.setattr(library_index, "_memory", {})
        assert library_items("symbol", row("Lib", path))[0][0].description == text


class TestConcurrentReads:
    def test_one_read_for_parallel_calls(self, tmp_path, cache, monkeypatch):
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(symbol_lib(symbol("A")), encoding="utf-8")
        move_clock_forward(monkeypatch)
        reads = []
        real_read = library_index._read

        def slow_read(*args):
            reads.append(args)
            time.sleep(0.2)
            return real_read(*args)

        monkeypatch.setattr(library_index, "_read", slow_read)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: library_items("symbol", row("Lib", path)), range(4)))
        assert len(reads) == 1
        assert all([item.name for item in items] == ["A"] for items, _ in results)
        assert [file.suffix for file in cache.iterdir()] == [".json"]

    def test_one_read_for_parallel_calls_of_a_new_file(self, tmp_path, cache, monkeypatch):
        # The stamp of a new file is not reliable. The calls that wait compare
        # the content, and do not read the library again.
        path = tmp_path / "Lib.kicad_sym"
        path.write_text(symbol_lib(symbol("A")), encoding="utf-8")
        stamp = lib_symbols.files_stamp([path])[0]
        monkeypatch.setattr(lib_symbols, "files_stamp", lambda files: (stamp, False))
        reads = []
        real_read = library_index._read

        def slow_read(*args):
            reads.append(args)
            time.sleep(0.2)
            return real_read(*args)

        monkeypatch.setattr(library_index, "_read", slow_read)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: library_items("symbol", row("Lib", path)), range(4)))
        assert len(reads) == 1
        assert all([item.name for item in items] == ["A"] for items, _ in results)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
async def test_library_that_cannot_be_read(tmp_path, tables):
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "Lib.kicad_sym"
    path.write_text(symbol_lib(symbol("A")), encoding="utf-8")
    locked.chmod(0)
    try:
        items, problems = library_items("symbol", row("Lib", path))
        assert items == [] and problems[0].startswith("The library Lib cannot be read:")
        state = library_tools._state(row("Lib", path), active=True)
        assert state.startswith("cannot be read:")
    finally:
        locked.chmod(0o755)


class TestCacheDir:
    def test_linux(self, monkeypatch, tmp_path):
        monkeypatch.undo()  # The real cache_dir().
        monkeypatch.setattr(user_files.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert library_index.cache_dir() == tmp_path / "kicad-workbench" / "library-index"
        monkeypatch.delenv("XDG_CACHE_HOME")
        assert library_index.cache_dir() == (
            Path.home() / ".cache" / "kicad-workbench" / "library-index"
        )

    def test_windows_and_macos(self, monkeypatch, tmp_path):
        monkeypatch.undo()
        monkeypatch.setattr(user_files.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert library_index.cache_dir() == tmp_path / "kicad-workbench" / "library-index"
        monkeypatch.setattr(user_files.sys, "platform", "darwin")
        assert library_index.cache_dir() == (
            Path.home() / "Library" / "Caches" / "kicad-workbench" / "library-index"
        )


class TestSearch:
    ITEMS = [
        LibraryItem("Device", "R", "Resistor", "R res resistor"),
        LibraryItem("Device", "R_Small", "Resistor, small symbol", "R resistor"),
        LibraryItem("Device", "RFShield", "RF shielding cabinet", "RF EMI"),
        LibraryItem("Amplifier", "OPA1", "Operational amplifier", "op amp", "Package_SO:SOIC-8"),
        LibraryItem("Amplifier", "INA1", "Instrumentation amplifier", "opamp"),
    ]

    def ids(self, query):
        return [item.lib_id for item in search(self.ITEMS, query)]

    def test_exact_name_first(self):
        # "r" is the name of Device:R: the tier of exact names.
        assert self.ids("R")[0] == "Device:R"
        assert self.ids("device:r")[0] == "Device:R"

    def test_score(self):
        # OPA1: keyword "op" is equal (8 * 4), "amp" too; INA1 only holds both.
        assert self.ids("op amp") == ["Amplifier:OPA1", "Amplifier:INA1"]
        assert self.ids("resistor") == ["Device:R", "Device:R_Small"]

    def test_each_word_must_match(self):
        assert self.ids("resistor small") == ["Device:R_Small"]
        assert self.ids("resistor capacitor") == []

    def test_footprint_of_symbol(self):
        assert self.ids("soic-8") == ["Amplifier:OPA1"]

    def test_score_values(self):
        # Library "device" (4), name "r_small" starts with "r" (2 * 8), library
        # ID holds "r" (16), keyword "r" is equal (8 * 4), keyword "resistor"
        # holds "r" (4), all keywords start with "r" (2 * 1), description
        # starts with "r" (2 * 1).
        terms = library_index._search_terms(self.ITEMS[1])
        assert [(text, weight) for text, weight, _name in terms] == [
            ("device", 4),
            ("r_small", 8),
            ("device:r_small", 16),
            ("r", 4),
            ("resistor", 4),
            ("r resistor", 1),
            ("resistor, small symbol", 1),
        ]
        # The own "Description" field: a default column of the chooser.
        item = LibraryItem("Device", "R", "Resistor", "", "", "Resistor")
        assert library_index._search_terms(item)[-1] == ("resistor", 4, False)

    def test_description_column_adds_to_the_score(self):
        own = LibraryItem("L", "B", "Diode", "", "", "Diode")
        inherited = LibraryItem("L", "A", "Diode", "", "", "")
        # Without the column, A comes first (same score, then the library ID).
        assert [item.name for item in search([inherited, own], "diode")] == ["B", "A"]


# The tools, with library tables in tmp_path.


@pytest.fixture
def tables(tmp_path, monkeypatch):
    """A KiCad 10.0 configuration with a global table, and a project."""
    return make_tables(tmp_path, monkeypatch)


def make_tables(tmp_path, monkeypatch) -> dict:
    """Write a KiCad 10.0 configuration with a global table, and a project."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)
    config = tmp_path / "config"
    (config / "10.0").mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: tmp_path / "stock")

    async def version():
        return "10.0.6"

    monkeypatch.setattr(library_tools, "get_kicad_version", version)

    libs = tmp_path / "libs"
    libs.mkdir()
    (libs / "Device.kicad_sym").write_text(
        symbol_lib(
            symbol("R", '"Description" "Resistor | global"', '"ki_keywords" "R res"'),
            symbol("C", '"Description" "Capacitor"'),
        ),
        encoding="utf-8",
    )
    (libs / "Hidden.kicad_sym").write_text(
        symbol_lib(symbol("RH", '"Description" "Hidden resistor"')), encoding="utf-8"
    )
    (libs / "Off.kicad_sym").write_text(
        symbol_lib(symbol("ROff", '"Description" "Disabled resistor"')), encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (project / "board.kicad_sch").write_text("", encoding="utf-8")
    (project / "Device.kicad_sym").write_text(
        symbol_lib(symbol("R", '"Description" "Resistor of the project"')), encoding="utf-8"
    )

    def lib(name, uri, flags="", kind_type="KiCad"):
        return (
            f'(lib (name "{name}")(type "{kind_type}")(uri "{uri}")(options "")(descr ""){flags})'
        )

    (config / "10.0" / "sym-lib-table").write_text(
        "(sym_lib_table\n\t(version 7)\n"
        + "\n".join(
            [
                lib("Device", libs / "Device.kicad_sym"),
                lib("Hidden", libs / "Hidden.kicad_sym", "(hidden)"),
                lib("Off", libs / "Off.kicad_sym", "(disabled)"),
                lib("Gone", libs / "Gone.kicad_sym"),
                lib("Var", "${NO_SUCH_VAR}/x.kicad_sym"),
                lib("Legacy", libs / "old.lib", kind_type="Legacy"),
            ]
        )
        + "\n)\n",
        encoding="utf-8",
    )
    (project / "sym-lib-table").write_text(
        "(sym_lib_table\n\t(version 7)\n" + lib("Device", "${KIPRJMOD}/Device.kicad_sym") + "\n)\n",
        encoding="utf-8",
    )
    return {"project": project, "libs": libs, "lib": lib, "config": config}


def table_rows(markdown: str) -> list[list[str]]:
    """The body rows of the Markdown table (not the header and the rule)."""
    lines = [line for line in markdown.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]] for line in lines[2:]]


class TestListLibraries:
    async def test_states(self, tables):
        out = await library_tools.list_libraries("symbol", str(tables["project"]))
        states = {(cells[0], cells[1]): cells[3] for cells in table_rows(out)}
        assert states == {
            ("Device", "project"): "ok",
            ("Device", "global"): "not used: a later library has this name",
            ("Hidden", "global"): "hidden",
            ("Off", "global"): "disabled",
            ("Gone", "global"): "missing",
            ("Var", "global"): "The URI has a variable with no value: ${NO_SUCH_VAR}/x.kicad_sym",
            ("Legacy", "global"): "missing",
        }
        assert "Total: 7 rows, 6 libraries" in out
        # The sequence of KiCad: the global table, then the project table.
        assert table_rows(out)[0][:2] == ["Device", "global"]
        assert table_rows(out)[-1][:2] == ["Device", "project"]

    async def test_project_paths(self, tables):
        project = tables["project"]
        # A sub-sheet has its own name: the one .kicad_pro of its directory.
        (project / "power.kicad_sch").write_text("", encoding="utf-8")
        for name in ("", "board.kicad_pro", "board.kicad_sch", "power.kicad_sch"):
            out = await library_tools.list_libraries("symbol", str(project / name))
            assert "| Device | project |" in out, name
        (project / "second.kicad_pro").write_text("{}", encoding="utf-8")
        out = await library_tools.list_libraries("symbol", str(project))
        assert out.startswith("❌") and "2 .kicad_pro files" in out
        out = await library_tools.list_libraries("symbol", str(project / "nothing.kicad_sch"))
        assert out.startswith("❌ No KiCad project")

    async def test_sub_sheet_in_a_directory_uses_the_root_project(self, tables):
        # The same project as add_component_from_library uses.
        project = tables["project"]
        (project / "board.kicad_sch").write_text(sch("sheets/power.kicad_sch"), encoding="utf-8")
        (project / "sheets").mkdir()
        (project / "sheets" / "power.kicad_sch").write_text(sch(), encoding="utf-8")
        out = await library_tools.list_libraries(
            "symbol", str(project / "sheets" / "power.kicad_sch")
        )
        assert "| Device | project |" in out, out

    async def test_bad_kind_and_no_kicad(self, tables, monkeypatch):
        out = await library_tools.list_libraries("design_block")
        assert out.startswith("❌") and "'design_block'" in out

        async def no_version():
            return None

        monkeypatch.setattr(library_tools, "get_kicad_version", no_version)
        out = await library_tools.list_libraries("symbol")
        assert out.startswith("❌ KiCad is not installed")
        assert "Tell the user: install KiCad 8 or later" in out


class TestSearchTools:
    @pytest.mark.file_versions
    async def test_the_versions_of_the_libraries_of_the_results(self, tables):
        """The libraries are of KiCad 9 (20241209): KiCad 10 reads them, with a warning.
        KiCad 10 cannot read a newer library: its symbols are not results."""
        device = tables["libs"] / "Device.kicad_sym"
        out = await library_tools.search_library_symbols("resistor")
        assert out.startswith(
            f"⚠️ The symbol library {device} has files of an older file version than KiCad "
            f"10.0.6 writes (20251024): {device} (version 20241209)."
        ), out
        assert table_rows(out) == [["Device:R", "Resistor \\| global", "R res", ""]]
        text = device.read_text(encoding="utf-8")
        device.write_text(text.replace("20241209", "20261015"), encoding="utf-8")
        out = await library_tools.search_library_symbols("resistor")
        assert "Found: 0 in 1 library" in out and "⚠️" not in out.split("\n")[0], out
        assert f"- The symbol library {device} has the files {device} (version 20261015)" in out
        assert "KiCad 10.0.6 cannot read them" in out

    async def test_project_library_replaces_global(self, tables):
        out = await library_tools.search_library_symbols("resistor", str(tables["project"]))
        assert table_rows(out) == [["Device:R", "Resistor of the project", "", ""]]
        assert "Found: 1 in 1 library" in out

    async def test_global_only(self, tables):
        out = await library_tools.search_library_symbols("resistor")
        # A "|" in a description is escaped in the table.
        assert table_rows(out) == [["Device:R", "Resistor \\| global", "R res", ""]]

    async def test_hidden_disabled_and_problems(self, tables):
        out = await library_tools.search_library_symbols("resistor", include_hidden=True)
        assert [cells[0] for cells in table_rows(out)] == ["Device:R", "Hidden:RH"]
        assert "Off:ROff" not in out
        assert f"The library Gone is not at {tables['libs'] / 'Gone.kicad_sym'}." in out
        assert "The library Var: The URI has a variable with no value" in out
        assert "The library Legacy has the type 'Legacy'" in out

    async def test_one_library_and_limit(self, tables):
        out = await library_tools.search_library_symbols("r", library="Device", limit=1)
        assert "Found: 2 in 1 library (the first 1 below)" in out
        assert [cells[0] for cells in table_rows(out)] == ["Device:R"]
        out = await library_tools.search_library_symbols("r", library="Off")
        assert out == "❌ The symbol library 'Off' is disabled."
        out = await library_tools.search_library_symbols("r", library="Nothing")
        assert out == "❌ No symbol library 'Nothing'."
        # A library that the user names is searched also when it is hidden.
        out = await library_tools.search_library_symbols("r", library="Hidden")
        assert [cells[0] for cells in table_rows(out)] == ["Hidden:RH"]

    @pytest.mark.parametrize("flag", ["(disabled)", "(hidden)"])
    async def test_project_row_replaces_global_row_with_its_flag(self, tables, flag):
        # KiCad uses the last row with a name, also when it is disabled or
        # hidden: the global library with the same name is not used.
        project = tables["project"]
        (project / "sym-lib-table").write_text(
            "(sym_lib_table\n\t(version 7)\n"
            + tables["lib"]("Device", "${KIPRJMOD}/Device.kicad_sym", flag)
            + "\n)\n",
            encoding="utf-8",
        )
        out = await library_tools.search_library_symbols("resistor", str(project))
        assert table_rows(out) == []
        out = await library_tools.list_libraries("symbol", str(project))
        states = {(cells[0], cells[1]): cells[3] for cells in table_rows(out)}
        assert states[("Device", "global")] == "not used: a later library has this name"
        assert states[("Device", "project")] == flag.strip("()")

    async def test_bad_input(self, tables):
        assert await library_tools.search_library_symbols("  ") == "❌ The query is empty."
        out = await library_tools.search_library_symbols("r", limit=0)
        assert out == "❌ The limit must be 1 or more."

    async def test_footprints(self, tables, tmp_path):
        pretty = tmp_path / "libs" / "Resistor_SMD.pretty"
        pretty.mkdir()
        (pretty / "R_0603_1608Metric.kicad_mod").write_text(
            footprint("R_0603_1608Metric", '"Resistor SMD 0603"', '"resistor"'), encoding="utf-8"
        )
        (tmp_path / "config" / "10.0" / "fp-lib-table").write_text(
            f'(fp_lib_table\n\t(version 7)\n\t(lib (name "Resistor_SMD")(type "KiCad")'
            f'(uri "{pretty}")(options "")(descr ""))\n)\n',
            encoding="utf-8",
        )
        out = await library_tools.search_library_footprints("0603 resistor")
        assert "| Library ID | Description | Tags |" in out
        assert table_rows(out) == [
            ["Resistor_SMD:R_0603_1608Metric", "Resistor SMD 0603", "resistor"]
        ]


# The installed KiCad and its libraries.


async def test_search_installed_libraries():
    version = await get_kicad_version()
    if version is None:
        pytest.skip("kicad-cli is not installed")
    rows, _problems = library_tables.library_rows("symbol", version)
    if not any(row.name == "Device" for row in rows):
        pytest.skip("KiCad has no global library table with the Device library")
    out = await library_tools.search_library_symbols("R", library="Device", limit=3)
    assert table_rows(out)[0][0] == "Device:R"
    out = await library_tools.search_library_footprints(
        "R_0603_1608Metric", library="Resistor_SMD", limit=3
    )
    assert table_rows(out)[0][0] == "Resistor_SMD:R_0603_1608Metric"
