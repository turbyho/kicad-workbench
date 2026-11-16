"""Tests of the upgrade of the project libraries (utils/library_upgrade.py and the tool
upgrade_project_libraries). kicad-cli is replaced: the check script
spike/kicad-versions/library_upgrade_check.py compares the tool with real KiCad."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.tools import project_libraries
from kicad_workbench.tools.project_libraries import upgrade_project_libraries
from kicad_workbench.utils import library_tables, library_upgrade
from kicad_workbench.utils.library_upgrade import (
    UpgradeError,
    convert,
    footprint_files,
    git_note,
    symbol_names,
)

OLD_SYMBOL = 20241209
NEW_SYMBOL = 20251024
OLD_FOOTPRINT = 20241229
NEW_FOOTPRINT = 20260206


def symbol_library(version, *names):
    symbols = "".join(f'\t(symbol "{name}")\n' for name in names)
    return f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "x")\n{symbols})\n'


def footprint(name, version):
    return f'(footprint "{name}"\n\t(version {version})\n\t(generator "pcbnew")\n)\n'


class FakeCli:
    """kicad-cli sym and fp upgrade: the output gets the new version. ``unstable`` gives
    other bytes at the save with --force; ``drop`` removes a symbol or a footprint."""

    def __init__(self, unstable=False, drop=None, fail=False):
        self.unstable, self.drop, self.fail, self.calls = unstable, drop, fail, []

    def __call__(self, args, timeout=60.0, english=False):
        self.calls.append(args)
        kind, force = args[0], "--force" in args
        output, source = Path(args[-2]), Path(args[-1])
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "Unable to load library")
        if kind == "sym":
            text = source.read_text(encoding="utf-8")
            text = re.sub(r"\(version \d+\)", f"(version {NEW_SYMBOL})", text)
            if self.drop:
                text = text.replace(f'\t(symbol "{self.drop}")\n', "")
            if force and self.unstable:
                text += "\n"
            output.write_text(text, encoding="utf-8")
        else:
            output.mkdir()
            for file in footprint_files(source):
                if file.stem == self.drop:
                    continue
                text = re.sub(
                    r"\(version \d+\)", f"(version {NEW_FOOTPRINT})", file.read_text("utf-8")
                )
                if force and self.unstable:
                    text += "\n"
                (output / file.name).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "Saving", "")


class TestConvert:
    def test_symbol_library(self, tmp_path, monkeypatch):
        cli = FakeCli()
        monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", cli)
        library = tmp_path / "a.kicad_sym"
        library.write_text(symbol_library(OLD_SYMBOL, "R", "C"), encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        files = convert("symbol", library, work, {"R", "C"})
        assert list(files) == [library]
        assert f"(version {NEW_SYMBOL})".encode() in files[library]
        # The upgrade, then the save of the result with --force.
        assert [call[:3] for call in cli.calls] == [
            ["sym", "upgrade", "-o"],
            ["sym", "upgrade", "--force"],
        ]

    def test_footprint_library(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", FakeCli())
        library = tmp_path / "a.pretty"
        library.mkdir()
        for name in ("R", ".hidden"):
            (library / f"{name}.kicad_mod").write_text(footprint(name, OLD_FOOTPRINT), "utf-8")
        (library / "readme.txt").write_text("x", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        files = convert("footprint", library, work, {"R.kicad_mod", ".hidden.kicad_mod"})
        assert sorted(path.name for path in files) == [".hidden.kicad_mod", "R.kicad_mod"]

    @pytest.mark.parametrize(
        ("cli", "message"),
        [
            (FakeCli(unstable=True), "not stable"),
            (FakeCli(drop="C"), "other symbols"),
            (FakeCli(fail=True), "Unable to load library"),
        ],
    )
    def test_symbol_check(self, tmp_path, monkeypatch, cli, message):
        monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", cli)
        library = tmp_path / "a.kicad_sym"
        library.write_text(symbol_library(OLD_SYMBOL, "R", "C"), encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        with pytest.raises(UpgradeError, match=message):
            convert("symbol", library, work, {"R", "C"})

    @pytest.mark.parametrize(
        ("cli", "message"), [(FakeCli(unstable=True), "not stable"), (FakeCli(drop="C"), "missing")]
    )
    def test_footprint_check(self, tmp_path, monkeypatch, cli, message):
        monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", cli)
        library = tmp_path / "a.pretty"
        library.mkdir()
        for name in ("R", "C"):
            (library / f"{name}.kicad_mod").write_text(footprint(name, OLD_FOOTPRINT), "utf-8")
        work = tmp_path / "work"
        work.mkdir()
        with pytest.raises(UpgradeError, match=message):
            convert("footprint", library, work, {"R.kicad_mod", "C.kicad_mod"})

    def test_symbol_names(self, tmp_path):
        data = symbol_library(OLD_SYMBOL, "R", "A").encode()
        assert symbol_names(data, tmp_path / "x.kicad_sym") == ["A", "R"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestGitNote:
    def git(self, folder, *args):
        subprocess.run(
            ["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )

    def test_states(self, tmp_path):
        library = tmp_path / "a.kicad_sym"
        library.write_text("x", encoding="utf-8")
        assert "not in a git repository" in git_note(library)
        self.git(tmp_path, "init", "-q")
        assert "not all its files are in git" in git_note(library)
        self.git(tmp_path, "add", "a.kicad_sym")
        self.git(tmp_path, "commit", "-q", "--no-gpg-sign", "-m", "a")
        assert git_note(library) is None
        library.write_text("y", encoding="utf-8")
        assert "not committed" in git_note(library)

    def test_folder_ignored_files_and_a_link(self, tmp_path):
        repo = tmp_path / "repo"
        folder = repo / "a.pretty"
        folder.mkdir(parents=True)
        (folder / "R.kicad_mod").write_text("x", encoding="utf-8")
        self.git(repo, "init", "-q")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "--no-gpg-sign", "-m", "a")
        assert git_note(folder) is None
        # The path through a symbolic link to the repository.
        link = tmp_path / "link"
        link.symlink_to(repo)
        assert git_note(link / "a.pretty") is None
        (repo / ".gitignore").write_text("*.tmp.kicad_mod\n", encoding="utf-8")
        (folder / "C.tmp.kicad_mod").write_text("x", encoding="utf-8")
        assert "not all its files are in git" in git_note(folder)


# The tool.


@pytest.fixture(autouse=True)
def kicad_10(monkeypatch, tmp_path):
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)

    async def version():
        return "10.0.6"

    monkeypatch.setattr(project_libraries, "get_kicad_version", version)
    (tmp_path / "config" / "10.0").mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
    monkeypatch.setattr(project_libraries, "git_note", lambda library: None)


def row(name, uri):
    return f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr ""))\n'


def table(head, *rows):
    return f"({head}\n\t(version 7)\n" + "".join(rows) + ")\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with an older symbol library and footprint library, and a footprint
    library outside the project folder."""
    monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", FakeCli())
    folder = tmp_path / "board"
    folder.mkdir()
    (folder / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / "Sym.kicad_sym").write_text(symbol_library(OLD_SYMBOL, "R"), encoding="utf-8")
    (folder / "Fp.pretty").mkdir()
    (folder / "Fp.pretty" / "R.kicad_mod").write_text(footprint("R", OLD_FOOTPRINT), "utf-8")
    shared = tmp_path / "shared.pretty"
    shared.mkdir()
    (shared / "C.kicad_mod").write_text(footprint("C", OLD_FOOTPRINT), "utf-8")
    (folder / "sym-lib-table").write_text(
        table("sym_lib_table", row("Sym", "${KIPRJMOD}/Sym.kicad_sym")), encoding="utf-8"
    )
    (folder / "fp-lib-table").write_text(
        table(
            "fp_lib_table",
            row("Fp", "${KIPRJMOD}/Fp.pretty"),
            row("Shared", str(shared).replace("\\", "/")),
        ),
        encoding="utf-8",
    )
    return folder / "board.kicad_pro"


class TestUpgradeTool:
    async def test_project_libraries(self, project):
        out = await upgrade_project_libraries(str(project))
        assert "| Sym | symbol | 20241209 | 1 | ✅ converted to 20251024" in out, out
        assert "| Fp | footprint | 20241229 | 1 | ✅ converted to 20260206" in out
        assert "| Shared | footprint |  |  | skipped: outside the project folder" in out
        assert f"(version {NEW_SYMBOL})" in (project.parent / "Sym.kicad_sym").read_text("utf-8")
        shared = project.parent.parent / "shared.pretty" / "C.kicad_mod"
        assert f"(version {OLD_FOOTPRINT})" in shared.read_text("utf-8")
        again = await upgrade_project_libraries(str(project))
        assert "| Sym | symbol | 20251024 | 1 | ✅ in the version of the installed KiCad" in again

    async def test_named_library_outside(self, project):
        out = await upgrade_project_libraries(str(project), ["Shared", "Missing"])
        assert "| Shared | footprint | 20241229 | 1 | ✅ converted" in out, out
        assert "| Sym |" not in out
        assert "❌ The project tables have no library Missing." in out
        shared = project.parent.parent / "shared.pretty" / "C.kicad_mod"
        assert f"(version {NEW_FOOTPRINT})" in shared.read_text("utf-8")

    async def test_newer_library_and_a_symbol_folder(self, project):
        (project.parent / "Sym.kicad_sym").write_text(
            symbol_library(20991231, "R"), encoding="utf-8"
        )
        (project.parent / "Dir").mkdir()
        (project.parent / "sym-lib-table").write_text(
            table(
                "sym_lib_table",
                row("Sym", "${KIPRJMOD}/Sym.kicad_sym"),
                row("Dir", "${KIPRJMOD}/Dir"),
            ),
            encoding="utf-8",
        )
        out = await upgrade_project_libraries(str(project), ["Sym", "Dir"])
        assert "| Sym | symbol | 20991231 |  | ❌ files of a newer version" in out, out
        assert "| Dir | symbol |  |  | ❌ a folder of symbol files" in out

    async def test_parent_folder_in_the_uri(self, project):
        # "${KIPRJMOD}/../shared.pretty" is outside the project folder (MakeAbsolute).
        (project.parent / "fp-lib-table").write_text(
            table("fp_lib_table", row("Shared", "${KIPRJMOD}/../shared.pretty")),
            encoding="utf-8",
        )
        out = await upgrade_project_libraries(str(project), None)
        assert "| Shared | footprint |  |  | skipped: outside the project folder" in out, out

    async def test_footprint_with_no_version(self, project):
        # A footprint of KiCad 5 has no (version N): KiCad reads and converts it.
        (project.parent / "Fp.pretty" / "Old.kicad_mod").write_text(
            "(module Old (layer F.Cu) (tedit 5B307E4C))\n", encoding="utf-8"
        )
        out = await upgrade_project_libraries(str(project), ["Fp"])
        assert "| Fp | footprint | 20241229, none | 2 | ✅ converted" in out, out

    async def test_error_of_one_library(self, project):
        # A footprint row whose URI is a file: an error for this row, the others go on.
        (project.parent / "fp-lib-table").write_text(
            table("fp_lib_table", row("File", "${KIPRJMOD}/Sym.kicad_sym")), encoding="utf-8"
        )
        out = await upgrade_project_libraries(str(project))
        assert "| File | footprint |  |  | ❌ a footprint library must be a folder |" in out, out
        assert "| Sym | symbol | 20241209 | 1 | ✅ converted" in out

    async def test_failed_check_writes_nothing(self, project, monkeypatch):
        monkeypatch.setattr(library_upgrade, "run_kicad_cli_sync", FakeCli(unstable=True))
        before = (project.parent / "Sym.kicad_sym").read_bytes()
        monkeypatch.setattr(project_libraries, "git_note", lambda library: "not in git")
        out = await upgrade_project_libraries(str(project), ["Sym"])
        assert "not stable" in out, out
        assert (project.parent / "Sym.kicad_sym").read_bytes() == before
        # No note about git for a library that the tool did not write.
        assert "No way back" not in out

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    async def test_git_state_before_the_write(self, project, monkeypatch):
        monkeypatch.setattr(project_libraries, "git_note", git_note)
        folder = project.parent
        TestGitNote().git(folder, "init", "-q")
        TestGitNote().git(folder, "add", "Sym.kicad_sym")
        TestGitNote().git(folder, "commit", "-q", "--no-gpg-sign", "-m", "a")
        out = await upgrade_project_libraries(str(project), ["Sym", "Fp"])
        # Sym is committed: no note, also after the write of the tool. Fp is not in git.
        assert "- Sym (symbol)" not in out, out
        assert "- Fp (footprint): not all its files are in git" in out

    async def test_git_note_and_open_project(self, project, monkeypatch):
        monkeypatch.setattr(project_libraries, "git_note", lambda library: "not in git")
        out = await upgrade_project_libraries(str(project), ["Sym"])
        assert "⚠️ No way back for these libraries:\n- Sym (symbol): not in git." in out
        (project.parent / "~board.kicad_pro.lck").write_text("{}", encoding="utf-8")
        assert (await upgrade_project_libraries(str(project))).startswith("❌")
