"""Tests for the check of the schematic file versions against the installed KiCad
(utils/schematic_versions.py).

The tests use copies of the fixtures in tmp_path, and set the installed KiCad;
spike/kicad-versions/schematic_versions_check.py compares the check with KiCad 8.0.9,
9.0.9 and 10.
"""

import json
import shutil
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import header_version
from kicad_workbench.utils import schematic_versions
from kicad_workbench.utils.kicad_cli import kicad_cli_version_sync

pytestmark = pytest.mark.schematic_versions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
Error = schematic_versions.SchematicVersionError


def hier(tmp_path: Path) -> Path:
    """A copy of the hierarchy of KiCad 8 (file version 20231120), with its project."""
    folder = tmp_path / "hier"
    shutil.copytree(FIXTURES / "hier", folder)
    (folder / "root.kicad_pro").write_text("{}", encoding="utf-8")
    return folder


def version(path: Path) -> int | None:
    return header_version(path.read_text(encoding="utf-8"), "kicad_sch")


def set_version(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def installed(monkeypatch, kicad: str) -> None:
    monkeypatch.setattr(schematic_versions, "_installed_kicad", lambda: kicad)


def check(path: Path, write: bool = False) -> schematic_versions.SchematicVersions:
    return schematic_versions.check_schematic_versions(path, write)


def test_older_files_give_a_warning_for_a_read(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    installed(monkeypatch, "10.0.6")
    root, child = folder / "root.kicad_sch", folder / "child.kicad_sch"
    result = check(child)
    assert result.kicad == "10.0.6"
    assert result.warning.startswith(
        f"⚠️ The schematic files {root} (version 20231120), {child} (version 20231120) have "
        "an older file version than KiCad 10.0.6 writes (20260306). The tool reads the "
        "files as they are, and changes no file. A tool that writes refuses them"
    )
    assert (
        f"To convert them, tell the user: open the project {folder / 'root.kicad_pro'} in "
        "KiCad 10.0.6. In each schematic, make a change" in result.warning
    )
    assert result.warning.endswith("an older KiCad cannot open the files then.")
    # The server changes no file.
    assert version(root) == version(child) == 20231120


def test_older_files_are_an_error_for_a_write(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    installed(monkeypatch, "10.0.6")
    with pytest.raises(Error) as error:
        check(folder / "root.kicad_sch", write=True)
    message = str(error.value)
    assert "have an older file version than KiCad 10.0.6 writes (20260306)." in message
    assert "the tool does not write them in the older version" in message
    assert message.endswith("an older KiCad cannot open the files then. Then do the work again.")
    assert version(folder / "root.kicad_sch") == 20231120


def test_current_files_give_nothing(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    installed(monkeypatch, "8.0.9")
    assert check(folder / "root.kicad_sch", write=True) == schematic_versions.SchematicVersions(
        "8.0.9", ""
    )


async def test_tool_output_starts_with_the_warning(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    installed(monkeypatch, "10.0.6")

    @schematic_versions.with_schematic_versions("file_path")
    async def read(file_path: str) -> str:
        return f"# Tool\n\nversion {kicad_cli_version_sync()}"

    @schematic_versions.with_schematic_versions("file_path", write=True)
    async def write(file_path: str) -> str:
        return pytest.fail("the tool must not run")

    out = await read(str(folder / "root.kicad_sch"))
    assert out.startswith("⚠️ The schematic files")
    # The tool uses the version of kicad-cli of the check.
    assert out.endswith("\n\n# Tool\n\nversion 10.0.6")
    out = await write(str(folder / "root.kicad_sch"))
    assert out.startswith("❌ The schematic files") and "Traceback" not in out


@pytest.mark.parametrize(
    ("kicad", "newer", "step"),
    [
        ("9.0.9", "20260306", "install KiCad 10.0 or later"),
        ("10.0.6", "20261025", "install the KiCad that saved them"),
    ],
)
def test_newer_file_is_an_error(tmp_path, monkeypatch, kicad, newer, step):
    folder = hier(tmp_path)
    child = folder / "child.kicad_sch"
    set_version(child, "20231120", newer)
    installed(monkeypatch, kicad)
    with pytest.raises(Error) as error:
        check(folder / "root.kicad_sch")
    message = str(error.value)
    assert message.startswith(f"The schematic files {child} (version {newer}) have a newer")
    assert f"KiCad {kicad} cannot open them" in message and step in message


@pytest.mark.parametrize("kicad", ["8.0.9", "9.0.9"])
def test_the_step_of_kicad_8_and_9(tmp_path, monkeypatch, kicad):
    """KiCad 8 saves no schematic with no change, KiCad 9 only one with an older root."""
    folder = hier(tmp_path)
    for name in ("root.kicad_sch", "child.kicad_sch"):
        set_version(folder / name, "20231120", "20230121")
    installed(monkeypatch, kicad)
    warning = check(folder / "root.kicad_sch").warning
    assert "(version 20230121)" in warning and "eeschema" not in warning
    assert warning.endswith(
        "Without a change, KiCad 8 saves no file, and KiCad 9 saves the files only when "
        "the root sheet has an older version."
    )


@pytest.mark.parametrize("kicad", ["8.0.9", "9.0.9"])
@pytest.mark.parametrize("entry", ["alone.kicad_sch", "alone.kicad_pcb"])
def test_a_schematic_with_no_project_file(tmp_path, monkeypatch, kicad, entry):
    """A schematic that is not a sheet of a project is the root of a project with no
    settings: the user opens it in the schematic editor alone. A board gets the
    schematic of its name."""
    folder = hier(tmp_path)
    alone = folder / "alone.kicad_sch"
    text = (folder / "child.kicad_sch").read_text()
    alone.write_text(text.replace("20231120", "20230121"), encoding="utf-8")
    (folder / "alone.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    installed(monkeypatch, kicad)
    warning = check(folder / entry).warning
    assert warning.startswith(f"⚠️ The schematic files {alone} (version 20230121) have")
    assert (
        f"tell the user: start the schematic editor of KiCad {kicad} alone (eeschema) and "
        f"open {alone} (File > Open): it has no project file. In each schematic" in warning
    )


def test_kicad_9_loads_no_top_level_sheets_of_kicad_10(tmp_path, monkeypatch):
    """KiCad 9 loads the schematic of the project name, not the top-level sheets of a
    project file of KiCad 10: an other top-level sheet is open only alone."""
    folder = hier(tmp_path)
    other = folder / "other.kicad_sch"
    shutil.copy(folder / "child.kicad_sch", other)
    sheets = [
        {"filename": "root.kicad_sch", "name": "root", "uuid": "a"},
        {"filename": "other.kicad_sch", "name": "other", "uuid": "b"},
    ]
    project = folder / "root.kicad_pro"
    project.write_text(
        json.dumps({"meta": {"version": 3}, "schematic": {"top_level_sheets": sheets}}),
        encoding="utf-8",
    )
    installed(monkeypatch, "9.0.9")
    warning = check(other).warning
    root, child = folder / "root.kicad_sch", folder / "child.kicad_sch"
    assert warning.startswith(
        f"⚠️ The schematic files {root} (version 20231120), {child} (version 20231120), "
        f"{other} (version 20231120) have"
    )
    assert (
        f"tell the user: open the project {project} in KiCad 9.0.9, and start the schematic "
        f"editor of KiCad 9.0.9 alone (eeschema) and open {other} (File > Open): it is not "
        "a sheet of the project that KiCad 9.0.9 loads. In each schematic" in warning
    )
    # KiCad 10 loads the two top-level sheets.
    loaded = schematic_versions._project_files(project, 10, other)
    files = [(file, 20231120) for file in (root, child, other)]
    assert (loaded.root, loaded.files, loaded.alone) == (root, files, None)


def test_nightly_build(tmp_path, monkeypatch):
    """A nightly build after KiCad 10.0 writes a newer version than 20260306, and not a
    newer one than NEWEST_SCHEMATIC: a file of 20260306 or older is older, a file
    between the two can be older or newer."""
    folder = hier(tmp_path)
    root, child = folder / "root.kicad_sch", folder / "child.kicad_sch"
    installed(monkeypatch, "10.99.0")
    warning = check(root).warning
    assert (
        "have an older file version than KiCad 10.99.0 writes (a nightly build: a newer "
        "version than 20260306 of KiCad 10.0)." in warning
    )
    set_version(root, "20231120", "20260306")
    with pytest.raises(Error, match=f"{child} \\(version 20231120\\), the tool|{root}"):
        check(root, write=True)
    set_version(child, "20231120", "20261025")
    with pytest.raises(Error) as error:
        check(root, write=True)
    assert str(error.value).startswith(f"The schematic files {root} (version 20260306) have")
    set_version(root, "20260306", "20261025")
    warning = check(root, write=True).warning
    assert warning == (
        "⚠️ KiCad 10.99.0 is a nightly build: the server does not know the file version "
        f"that it writes. Thus it cannot tell if the schematic files {root} (version "
        f"20261025), {child} (version 20261025) have an older or a newer version. Their "
        "version is newer than KiCad 10.0 writes (20260306)."
    )


def test_no_kicad_is_an_error(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    monkeypatch.setattr(schematic_versions, "kicad_cli_version_sync", lambda: None)
    with pytest.raises(Error, match="kicad-cli was not found"):
        check(folder / "root.kicad_sch")


def test_kicad_7_and_unknown_versions(tmp_path, monkeypatch):
    folder = hier(tmp_path)
    for kicad, text in (
        ("7.0.11", "older than KiCad 8.0, the minimum"),
        ("11.99.0", "a nightly build with no known file versions"),
        ("9.99.0", "a nightly build with no known file versions"),
        ("8.99.0", "a nightly build with no known file versions"),
        ("9.1.0", "a version with no known file versions"),
    ):
        installed(monkeypatch, kicad)
        with pytest.raises(Error, match=text):
            check(folder / "root.kicad_sch")


async def test_unexpected_error_gives_the_traceback(tmp_path, monkeypatch):
    def defect(path, write):
        raise ValueError("a defect")

    monkeypatch.setattr(schematic_versions, "check_schematic_versions", defect)

    @schematic_versions.with_schematic_versions("file_path")
    async def tool(file_path: str) -> str:
        return "# Tool"

    out = await tool(str(tmp_path / "x.kicad_sch"))
    assert out.startswith("❌ Error in the check of the schematic file versions: a defect")
    assert "Traceback" in out


async def test_path_that_is_not_a_project(tmp_path, monkeypatch):
    """A path that is not a schematic project gives no check: the tool gives its own
    error."""
    monkeypatch.setattr(schematic_versions, "_installed_kicad", lambda: pytest.fail("no call"))
    missing = (
        tmp_path / "none.kicad_sch",
        tmp_path / "x.txt",
        tmp_path / "b.kicad_pcb",
        tmp_path / "missing.kicad_pro",
    )
    for path in missing:
        assert check(path) == schematic_versions.SchematicVersions("", "")

    @schematic_versions.with_schematic_versions("file_path", write=True)
    async def tool(file_path: str) -> str:
        return "❌ The schematic does not exist."

    assert await tool(str(tmp_path / "none.kicad_sch")) == "❌ The schematic does not exist."
