"""Tests for the conversion of the schematic files by the installed KiCad
(utils/schematic_upgrade.py).

The tests use copies of the fixtures in tmp_path. The tests of a conversion need
kicad-cli 10 or later (sch upgrade); spike/kicad-versions/upgrade_check.py
compares the conversion with KiCad 8.0.9, 9.0.9 and 10.
"""

import shutil
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import header_version, version_numbers
from kicad_workbench.utils import schematic_upgrade
from kicad_workbench.utils.kicad_cli import kicad_cli_version_sync
from kicad_workbench.utils.kicad_lock import lock_file_path

pytestmark = pytest.mark.schematic_conversion

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
INSTALLED = kicad_cli_version_sync()
needs_upgrade = pytest.mark.skipif(
    INSTALLED is None or (version_numbers(INSTALLED) or (0, 0))[0] < 10,
    reason="needs kicad-cli 10 or later (sch upgrade)",
)


def hier(tmp_path: Path) -> Path:
    """A copy of the hierarchy of KiCad 8 (file version 20231120), with its project."""
    folder = tmp_path / "hier"
    shutil.copytree(FIXTURES / "hier", folder)
    (folder / "root.kicad_pro").write_text("{}", encoding="utf-8")
    return folder


def version(path: Path) -> int | None:
    return header_version(path.read_text(encoding="utf-8"), "kicad_sch")


@needs_upgrade
def test_converts_each_file_of_the_hierarchy(tmp_path):
    folder = hier(tmp_path)
    result = schematic_upgrade.current_schematics(folder / "child.kicad_sch")
    written = version(folder / "root.kicad_sch")
    assert result.kicad == INSTALLED
    assert [(c.file.name, c.before, c.after) for c in result.conversions] == [
        ("root.kicad_sch", 20231120, written),
        ("child.kicad_sch", 20231120, written),
    ]
    assert version(folder / "child.kicad_sch") == written > 20231120
    # The files are current now: no second conversion.
    assert schematic_upgrade.current_schematics(folder / "root.kicad_sch").conversions == []
    (line, _empty) = schematic_upgrade.conversion_lines(result)
    assert line.startswith(f"⚠️ KiCad {INSTALLED} converted and saved the schematic files")
    assert "an older KiCad cannot open them now" in line


@needs_upgrade
async def test_tool_output_starts_with_the_warning(tmp_path):
    folder = hier(tmp_path)

    @schematic_upgrade.with_current_schematics("file_path")
    async def tool(file_path: str) -> str:
        # The tool reads the converted file.
        return f"# Tool\n\nversion {version(Path(file_path))}"

    out = await tool(str(folder / "root.kicad_sch"))
    assert out.startswith(f"⚠️ KiCad {INSTALLED} converted")
    assert out.endswith(f"version {version(folder / 'root.kicad_sch')}")
    assert "converted" not in await tool(str(folder / "root.kicad_sch"))


@needs_upgrade
def test_newer_file_is_an_error(tmp_path):
    folder = hier(tmp_path)
    child = folder / "child.kicad_sch"
    child.write_text(child.read_text().replace("20231120", "29991231"), encoding="utf-8")
    with pytest.raises(schematic_upgrade.SchematicVersionError, match="cannot open them"):
        schematic_upgrade.current_schematics(folder / "root.kicad_sch")
    assert version(folder / "root.kicad_sch") == 20231120  # no file changes


@needs_upgrade
def test_open_in_kicad_is_an_error(tmp_path):
    folder = hier(tmp_path)
    lock_file_path(folder / "root.kicad_sch").write_text("{}", encoding="utf-8")
    with pytest.raises(
        schematic_upgrade.SchematicVersionError, match="save the schematic in KiCad"
    ):
        schematic_upgrade.current_schematics(folder / "child.kicad_sch")
    assert version(folder / "child.kicad_sch") == 20231120


def test_kicad_9_asks_the_user_to_save(tmp_path, monkeypatch):
    """kicad-cli 8 and 9 cannot save a schematic: the user saves it in KiCad."""
    folder = hier(tmp_path)
    monkeypatch.setattr(schematic_upgrade, "_installed_kicad", lambda: "9.0.9")
    with pytest.raises(schematic_upgrade.SchematicVersionError) as error:
        schematic_upgrade.current_schematics(folder / "root.kicad_sch")
    message = str(error.value)
    assert "older file version than KiCad 9.0.9 (20250114)" in message
    assert "root.kicad_sch (version 20231120)" in message and "child.kicad_sch" in message
    assert "save each of these sheets in the schematic editor (File > Save)" in message
    # A file of the version of KiCad 9: no step.
    for name in ("root.kicad_sch", "child.kicad_sch"):
        path = folder / name
        path.write_text(path.read_text().replace("20231120", "20250114"), encoding="utf-8")
    assert schematic_upgrade.current_schematics(folder / "root.kicad_sch").conversions == []
    monkeypatch.setattr(schematic_upgrade, "_installed_kicad", lambda: "8.0.9")
    with pytest.raises(schematic_upgrade.SchematicVersionError, match="cannot open them"):
        schematic_upgrade.current_schematics(folder / "root.kicad_sch")


def test_no_kicad_is_an_error(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    monkeypatch.setattr(schematic_upgrade, "kicad_cli_version_sync", lambda: None)
    with pytest.raises(schematic_upgrade.SchematicVersionError, match="kicad-cli was not found"):
        schematic_upgrade.current_schematics(folder / "root.kicad_sch")


@needs_upgrade
def test_error_after_a_conversion_names_the_converted_files(tmp_path, monkeypatch):
    """A conversion that fails after an other conversion tells which files changed."""
    folder = hier(tmp_path)
    real = schematic_upgrade.run_kicad_cli_sync

    def fail_child(args, **kwargs):
        if args[:2] == ["sch", "upgrade"] and args[2].endswith("child.kicad_sch"):
            raise schematic_upgrade.KicadCliError("kicad-cli stopped")
        return real(args, **kwargs)

    monkeypatch.setattr(schematic_upgrade, "run_kicad_cli_sync", fail_child)
    with pytest.raises(schematic_upgrade.SchematicVersionError) as error:
        schematic_upgrade.current_schematics(folder / "root.kicad_sch")
    message = str(error.value)
    assert message.startswith("kicad-cli stopped. Before this error, KiCad")
    assert "root.kicad_sch (20231120 to" in message and "child.kicad_sch (" not in message


async def test_unexpected_error_gives_the_traceback(tmp_path, monkeypatch):
    def defect(path):
        raise ValueError("a defect")

    monkeypatch.setattr(schematic_upgrade, "current_schematics", defect)

    @schematic_upgrade.with_current_schematics("file_path")
    async def tool(file_path: str) -> str:
        return "# Tool"

    out = await tool(str(tmp_path / "x.kicad_sch"))
    assert out.startswith("❌ Error in the conversion of the schematic files: a defect")
    assert "Traceback" in out


@needs_upgrade
async def test_the_tool_uses_the_version_of_the_conversion(tmp_path):
    folder = hier(tmp_path)

    @schematic_upgrade.with_current_schematics("file_path")
    async def tool(file_path: str) -> str:
        return f"version {kicad_cli_version_sync()}"

    assert (await tool(str(folder / "root.kicad_sch"))).endswith(f"version {INSTALLED}")


def test_kicad_7_and_unknown_versions(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    for version, text in (
        ("7.0.11", "older than KiCad 8.0, the minimum"),
        ("9.99.0", "a nightly build with no known file versions"),
        ("9.1.0", "a version with no known file versions"),
    ):
        monkeypatch.setattr(schematic_upgrade, "_installed_kicad", lambda v=version: v)
        with pytest.raises(schematic_upgrade.SchematicVersionError, match=text):
            schematic_upgrade.current_schematics(folder / "root.kicad_sch")
    assert schematic_upgrade.current_schematics(folder / "missing.kicad_pro").conversions == []


async def test_path_that_is_not_a_project(tmp_path, monkeypatch):
    """A path that is not a schematic project gives no conversion: the tool gives
    its own error."""
    monkeypatch.setattr(schematic_upgrade, "_installed_kicad", lambda: pytest.fail("no call"))
    for path in (tmp_path / "none.kicad_sch", tmp_path / "x.txt", tmp_path / "b.kicad_pcb"):
        assert schematic_upgrade.current_schematics(path).conversions == []

    @schematic_upgrade.with_current_schematics("file_path")
    async def tool(file_path: str) -> str:
        return "❌ The schematic does not exist."

    assert await tool(str(tmp_path / "none.kicad_sch")) == "❌ The schematic does not exist."
