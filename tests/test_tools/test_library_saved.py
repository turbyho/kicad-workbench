"""Tests for utils/library_saved.py: flatten gets the symbols of a save of the library in
kicad-cli 10 (the tests need kicad-cli; spike/kicad-versions/library_saved_check.py
compares the tools with KiCad)."""

import asyncio
import functools
import subprocess
import threading
import time
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import KICAD10, kicad_release
from kicad_workbench.parsers.lib_symbols import flatten, read_library, symbol_chain
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import project_libraries, schematic_editor, symbol_update
from kicad_workbench.utils import library_saved
from kicad_workbench.utils.kicad_cli import decode_output, run_kicad_cli_sync
from kicad_workbench.utils.library_saved import saved_chain

from .installed_kicad import write_schematic
from .symbol_tables import row, use_global_table


@functools.cache
def _installed_kicad_10() -> bool:
    """Return True when the kicad-cli of the server is of KiCad 10.0: the save in
    kicad-cli runs only with KiCad 10.0, and the results are the ones of KiCad 10.0."""
    try:
        result = run_kicad_cli_sync(["version"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    version = decode_output(result.stdout).strip()
    return result.returncode == 0 and kicad_release(version) is KICAD10


# The tests use the save of library_saved (not the stub of conftest.py); the tests that
# run kicad-cli need it, the others give run_upgrade a copy (copy_save) and run in CI.
pytestmark = pytest.mark.kicad_save
needs_kicad_10 = pytest.mark.skipif(not _installed_kicad_10(), reason="no kicad-cli of KiCad 10.0")

EFFECTS = "(effects (font (size 1.27 1.27)))"


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    """Each test starts with no saves in the cache (the key is the content)."""
    monkeypatch.setattr(library_saved, "_cache", {})


def library(*symbols: str) -> str:
    return (
        '(kicad_symbol_lib (version 20251024) (generator "kicad_symbol_editor") '
        '(generator_version "10.0")\n' + "\n".join(symbols) + "\n)\n"
    )


def symbol(name: str = "R", footprint: str = " Pkg:SOT23 ") -> str:
    """A symbol that KiCad did not save: blanks in a mandatory field, a mandatory name in
    an other case, two user fields of one name, no Datasheet and no Description."""
    return (
        f'(symbol "{name}" (property "Reference" "R" (at 0 0 0) {EFFECTS}) '
        f'(property "value" "{name}" (at 0 0 0) {EFFECTS}) '
        f'(property "Footprint" "{footprint}" (at 0 0 0) {EFFECTS}) '
        f'(property "MPN" "a" (at 0 0 0) {EFFECTS}) (property "MPN" "b" (at 0 0 0) {EFFECTS}) '
        f'(symbol "{name}_1_1" (pin passive line (at 0 0 0) (length 1.27) (name "~" {EFFECTS}) '
        f'(number "1" {EFFECTS}))) (embedded_fonts no))'
    )


def fields(text: str) -> dict[str, str]:
    return node_fields(parse(text))


def node_fields(node) -> dict[str, str]:
    return {p.value(0): p.value(1) for p in node.children("property")}


@needs_kicad_10
def test_flatten_gets_the_text_of_a_save(tmp_path):
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    raw = fields(flatten(chain, "L:R", 10))
    assert raw["Footprint"] == " Pkg:SOT23 " and "value" in raw
    saved = fields(flatten(saved_chain(chain, path, "10.0.6"), "L:R", 10))
    # SCH_FIELD::SetText, the canonical names, the defaults of LIB_SYMBOL, "<name>_1".
    assert saved["Footprint"] == "Pkg:SOT23"
    assert saved["Value"] == "R"
    assert (saved["MPN"], saved["MPN_1"]) == ("a", "b")
    assert saved["Datasheet"] == "" and saved["Description"] == ""


def test_kicad_9_keeps_the_text_of_the_file(tmp_path):
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    assert saved_chain(chain, path, "9.0.9") is chain


@needs_kicad_10
def test_the_cache_and_a_changed_file(tmp_path, monkeypatch):
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    runs = []
    real = library_saved.run_upgrade

    def counted(*args, **kwargs):
        runs.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(library_saved, "run_upgrade", counted)
    chain = symbol_chain(read_library(path), "R")
    saved_chain(chain, path, "10.0.6")
    saved_chain(chain, path, "10.0.6")
    assert len(runs) == 1
    path.write_text(library(symbol(footprint=" Pkg:SOT89 ")), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    saved = saved_chain(chain, path, "10.0.6")
    assert len(runs) == 2
    assert fields(flatten(saved, "L:R", 10))["Footprint"] == "Pkg:SOT89"


@needs_kicad_10
def test_a_directory_library(tmp_path):
    folder = tmp_path / "L.kicad_symdir"
    folder.mkdir()
    (folder / "A.kicad_sym").write_text(library(symbol("SA")), encoding="utf-8")
    (folder / "B.kicad_sym").write_text(library(symbol("SB")), encoding="utf-8")
    chain = symbol_chain(read_library(folder), "SB")
    saved = saved_chain(chain, folder, "10.0.6")
    assert fields(flatten(saved, "L:SB", 10))["Footprint"] == "Pkg:SOT23"


@needs_kicad_10
def test_add_component_writes_the_text_of_a_save(tmp_path, monkeypatch):
    directory = tmp_path / "libs"
    directory.mkdir()
    (directory / "L.kicad_sym").write_text(library(symbol()), encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("L", directory / "L.kicad_sym"))
    board = tmp_path / "board"
    board.mkdir()
    sch = write_schematic(board, 20260306)
    result = asyncio.run(
        schematic_editor.add_component_from_library(str(sch), "L", "R", "R1", "1k")
    )
    assert "✅" in result, result
    root = parse(Path(sch).read_text(encoding="utf-8"))
    copy = {
        p.value(0): p.value(1)
        for p in root.first("lib_symbols").first("symbol").children("property")
    }
    placed = {p.value(0): p.value(1) for p in root.first("symbol").children("property")}
    assert copy["Footprint"] == "Pkg:SOT23" and placed["Footprint"] == "Pkg:SOT23"
    assert "MPN_1" in copy and "MPN_1" in placed


@needs_kicad_10
def test_the_save_uses_the_text_that_the_caller_read(tmp_path):
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    # A change of the file after the read (and the checks) of the caller.
    path.write_text(library(symbol(footprint="Pkg:OTHER")), encoding="utf-8")
    saved = saved_chain(chain, path, "10.0.6")
    assert fields(flatten(saved, "L:R", 10))["Footprint"] == "Pkg:SOT23"
    # The messages name the file of the library, not the temporary file.
    assert saved[0].file == path


def copy_save(runs):
    """A save in place of kicad-cli: the output is the first file of the temporary
    library (the logic of the cache needs no KiCad)."""

    def save(kind, source, output, force):
        runs.append(source)
        time.sleep(0.05)
        output.write_bytes(sorted(source.iterdir())[0].read_bytes())

    return save


def test_the_cache_keeps_the_last_saves(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(library_saved, "run_upgrade", copy_save(runs))
    monkeypatch.setattr(library_saved, "_CACHE_SIZE", 3)
    for n in range(5):
        path = tmp_path / f"L{n}.kicad_sym"
        path.write_text(library(symbol(footprint=f"P:{n}")), encoding="utf-8")
        saved_chain(symbol_chain(read_library(path), "R"), path, "10.0.6")
    assert len(library_saved._cache) == 3 and len(runs) == 5
    assert not library_saved._running


def test_two_calls_for_the_same_symbols_run_one_save(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(library_saved, "run_upgrade", copy_save(runs))
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    results = []

    def call():
        try:
            results.append(saved_chain(chain, path, "10.0.6"))
        except Exception as e:  # the assert below shows each error
            results.append(e)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(runs) == 1 and not library_saved._running
    # Each call gets the saved chain: the waiting calls too.
    assert len(results) == 4
    for result in results:
        assert isinstance(result, list), result
        assert [s.name for s in result] == ["R"] and result[0].file == path


def derived(name: str, parent: str) -> str:
    return (
        f'(symbol "{name}" (extends "{parent}") (property "Reference" "R" (at 0 0 0) {EFFECTS}) '
        f'(property "Value" "{name}" (at 0 0 0) {EFFECTS}) '
        f'(property "Footprint" "" (at 0 0 0) {EFFECTS}) (embedded_fonts no))'
    )


# The temporary library of the chain D (A.kicad_sym) and P (B.kicad_sym) of the directory
# library of saved_directory, and its save in kicad-cli 10.0.6. Thus the tests in CI (no
# kicad-cli) use the text of a real save: the files of the temporary library (_texts),
# the read of the save (_save) and the files of the chain (_map).
SAVED = Path(__file__).resolve().parents[1] / "fixtures" / "library_saved"


def saved_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "L.kicad_symdir"
    folder.mkdir()
    (folder / "A.kicad_sym").write_text(library(derived("D", "P")), encoding="utf-8")
    (folder / "B.kicad_sym").write_text(library(symbol("P")), encoding="utf-8")
    return folder


def test_the_save_of_kicad_10_in_ci(tmp_path, monkeypatch):
    sources = []

    def recorded(kind, source, output, force):
        sources.append({f.name: f.read_bytes() for f in source.iterdir()})
        output.write_bytes((SAVED / "saved.kicad_sym").read_bytes())

    monkeypatch.setattr(library_saved, "run_upgrade", recorded)
    folder = saved_directory(tmp_path)
    saved = saved_chain(symbol_chain(read_library(folder), "D"), folder, "10.0.6")
    # The temporary library is the input of the recorded save.
    assert sources == [{f.name: f.read_bytes() for f in (SAVED / "chain").iterdir()}]
    assert [(s.name, s.file.name) for s in saved] == [("D", "A.kicad_sym"), ("P", "B.kicad_sym")]
    assert {s.version for s in saved} == {KICAD10.symbol_library}
    flat = fields(flatten(saved, "L:D", 10))
    assert flat["Footprint"] == "Pkg:SOT23" and flat["Value"] == "D"
    assert (flat["MPN"], flat["MPN_1"]) == ("a", "b")


@needs_kicad_10
def test_the_recorded_save_is_the_save_of_kicad_10(tmp_path):
    output = tmp_path / "saved.kicad_sym"
    library_saved.run_upgrade("symbol", SAVED / "chain", output, force=True)
    assert output.read_bytes() == (SAVED / "saved.kicad_sym").read_bytes()


@needs_kicad_10
def test_a_derived_symbol(tmp_path):
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol("P"), derived("D", "P")), encoding="utf-8")
    chain = symbol_chain(read_library(path), "D")
    saved = saved_chain(chain, path, "10.0.6")
    assert [s.name for s in saved] == ["D", "P"] and saved[1].file == path
    # The fields of the parent as KiCad saves them (Flatten).
    flat = fields(flatten(saved, "L:D", 10))
    assert flat["Footprint"] == "Pkg:SOT23" and flat["Value"] == "D"


@needs_kicad_10
def test_a_parent_in_an_other_file_of_a_directory_library(tmp_path):
    folder = tmp_path / "L.kicad_symdir"
    folder.mkdir()
    (folder / "A.kicad_sym").write_text(library(derived("D", "P")), encoding="utf-8")
    (folder / "B.kicad_sym").write_text(library(symbol("P")), encoding="utf-8")
    chain = symbol_chain(read_library(folder), "D")
    saved = saved_chain(chain, folder, "10.0.6")
    assert [s.file.name for s in saved] == ["A.kicad_sym", "B.kicad_sym"]
    assert fields(flatten(saved, "L:D", 10))["Footprint"] == "Pkg:SOT23"


@needs_kicad_10
def test_kicad_cli_saves_only_the_symbols_of_the_chain(tmp_path):
    # A save of a symbol does not depend on the other symbols of its library.
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol("A"), symbol("R"), symbol("Z")), encoding="utf-8")
    saved = saved_chain(symbol_chain(read_library(path), "R"), path, "10.0.6")
    assert fields(flatten(saved, "L:R", 10))["Footprint"] == "Pkg:SOT23"
    assert [list(symbols) for symbols in library_saved._cache.values()][-1] == ["R"]


def test_an_equal_text_in_an_other_library_gets_its_file(tmp_path, monkeypatch):
    monkeypatch.setattr(library_saved, "run_upgrade", copy_save([]))
    paths = [tmp_path / "A.kicad_sym", tmp_path / "B.kicad_sym"]
    for path in paths:
        path.write_text(library(symbol()), encoding="utf-8")
    for path in paths:
        saved = saved_chain(symbol_chain(read_library(path), "R"), path, "10.0.6")
        assert saved[0].file == path
    assert len(library_saved._cache) == 1


def test_a_failed_save_gives_its_error_to_each_waiting_call(tmp_path, monkeypatch):
    runs = []

    def failing(kind, source, output, force):
        runs.append(source)
        time.sleep(0.05)
        raise library_saved.WorkbenchError("kicad-cli failed")

    monkeypatch.setattr(library_saved, "run_upgrade", failing)
    path = tmp_path / "L.kicad_sym"
    path.write_text(library(symbol()), encoding="utf-8")
    chain = symbol_chain(read_library(path), "R")
    errors = []

    def call():
        try:
            saved_chain(chain, path, "10.0.6")
        except library_saved.SavedLibraryError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(runs) == 1 and len(errors) == 3
    assert 'kicad-cli could not save the symbol "R"' in errors[0]


def test_the_tools_give_the_error_of_the_save(tmp_path, monkeypatch):
    def failing(kind, source, output, force):
        raise library_saved.WorkbenchError("kicad-cli failed")

    monkeypatch.setattr(library_saved, "run_upgrade", failing)
    directory = tmp_path / "libs"
    directory.mkdir()
    (directory / "L.kicad_sym").write_text(library(symbol()), encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("L", directory / "L.kicad_sym"))
    board = tmp_path / "board"
    board.mkdir()
    sch = write_schematic(board, 20260306)
    result = asyncio.run(
        schematic_editor.add_component_from_library(str(sch), "L", "R", "R1", "1k")
    )
    assert result.startswith("❌ Cannot add component L:R: kicad-cli could not save"), result


def test_a_waiting_call_of_an_other_library_names_its_file(tmp_path, monkeypatch):
    def failing(kind, source, output, force):
        time.sleep(0.1)
        raise library_saved.WorkbenchError("kicad-cli failed")

    monkeypatch.setattr(library_saved, "run_upgrade", failing)
    errors = {}
    threads = []
    for name in ("A", "B"):
        path = tmp_path / f"{name}.kicad_sym"
        path.write_text(library(symbol()), encoding="utf-8")
        chain = symbol_chain(read_library(path), "R")

        def call(chain=chain, path=path):
            try:
                saved_chain(chain, path, "10.0.6")
            except library_saved.SavedLibraryError as e:
                errors[path.name] = str(e)

        threads.append(threading.Thread(target=call))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(errors) == {"A.kicad_sym", "B.kicad_sym"}
    for name, message in errors.items():
        assert name in message and "kicad-cli failed" in message


def add_r(tmp_path, monkeypatch, text: str) -> Path:
    """The library L with the text in the global table, and a KiCad 10 schematic with
    L:R as R1 (add_component_from_library)."""
    directory = tmp_path / "libs"
    directory.mkdir(exist_ok=True)
    (directory / "L.kicad_sym").write_text(text, encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("L", directory / "L.kicad_sym"))
    board = tmp_path / "board"
    board.mkdir()
    sch = write_schematic(board, 20260306)
    result = asyncio.run(
        schematic_editor.add_component_from_library(str(sch), "L", "R", "R1", "1k")
    )
    assert "✅" in result, result
    return sch


@needs_kicad_10
def test_update_symbols_gets_the_text_of_a_save(tmp_path, monkeypatch):
    sch = add_r(tmp_path, monkeypatch, library(symbol()))

    async def version():
        return "10.0.6"

    monkeypatch.setattr(symbol_update, "get_kicad_version", version)
    supplier = f'(property "Supplier" " X " (at 0 0 0) {EFFECTS}) '
    text = library(
        symbol(footprint=" Pkg:SOT89 ").replace('(symbol "R_1_1"', supplier + '(symbol "R_1_1"')
    )
    (tmp_path / "libs" / "L.kicad_sym").write_text(text, encoding="utf-8")
    result = asyncio.run(symbol_update.update_symbols_from_library(str(sch)))
    assert "✅" in result and "Supplier added" in result, result
    root = parse(sch.read_text(encoding="utf-8"))
    placed = node_fields(root.first("symbol"))
    # SCH_FIELD::SetText removes the blanks of a mandatory field only.
    assert placed["Footprint"] == "Pkg:SOT89" and placed["Supplier"] == " X "
    copy = node_fields(root.first("lib_symbols").first("symbol"))
    assert copy["Footprint"] == "Pkg:SOT89" and "value" not in copy


@needs_kicad_10
@pytest.mark.parametrize("version", [20231120, 20241209])
def test_a_kicad_8_or_9_symbol_with_an_empty_text_marker(tmp_path, monkeypatch, version):
    # The pin name "~" of a KiCad 8 or 9 library is an empty text. The save in kicad-cli
    # 10 converts it, thus the symbol goes into a KiCad 10 schematic.
    text = library(symbol()).replace("(version 20251024)", f"(version {version})")
    sch = add_r(tmp_path, monkeypatch, text)
    root = parse(sch.read_text(encoding="utf-8"))
    copy = root.first("lib_symbols").first("symbol")
    names = [
        pin.first("name").value(0) for sub in copy.children("symbol") for pin in sub.children("pin")
    ]
    assert names == [""]


def copy_project(tmp_path, monkeypatch) -> Path:
    """A project with the library Mine in its table; the global library L has the
    symbol R of a file that KiCad did not save (symbol())."""
    directory = tmp_path / "libs"
    directory.mkdir()
    (directory / "L.kicad_sym").write_text(library(symbol()), encoding="utf-8")
    use_global_table(tmp_path, monkeypatch, row("L", directory / "L.kicad_sym"))

    async def version():
        return "10.0.6"

    monkeypatch.setattr(project_libraries, "get_kicad_version", version)
    folder = tmp_path / "board"
    folder.mkdir()
    (folder / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / "Mine.kicad_sym").write_text(library(), encoding="utf-8")
    (folder / "sym-lib-table").write_text(
        f"(sym_lib_table\n\t(version 7)\n\t{row('Mine', '${KIPRJMOD}/Mine.kicad_sym')}\n)\n",
        encoding="utf-8",
    )
    return folder / "board.kicad_pro"


@needs_kicad_10
@pytest.mark.parametrize("flatten_symbol", [False, True])
def test_copy_symbol_gets_the_text_of_a_save(tmp_path, monkeypatch, flatten_symbol):
    project = copy_project(tmp_path, monkeypatch)
    result = asyncio.run(
        project_libraries.copy_symbol_to_library(
            str(project), "L:R", "Mine", "R2", flatten=flatten_symbol
        )
    )
    assert result.startswith("✅"), result
    copy = node_fields(read_library(project.parent / "Mine.kicad_sym")["R2"].node)
    # valueFollowsName compares the loaded Value: "value" "R" is the Value "R".
    assert copy["Value"] == "R2" and "value" not in copy
    assert copy["Footprint"] == "Pkg:SOT23"
    assert (copy["MPN"], copy["MPN_1"]) == ("a", "b")
