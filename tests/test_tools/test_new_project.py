"""Tests of the rules of new KiCad projects (utils/new_project.py).

The expected texts and rules come from the KiCad source of each release:
KICAD_MANAGER_FRAME::CreateNewProject, PROJECT_TEMPLATE::GetFileList and
CreateProject, EnsureDefaultProjectTemplate.
"""

import os
import re
import sys
from pathlib import Path

import pytest

from kicad_workbench.utils import library_tables
from kicad_workbench.utils import new_project as np
from kicad_workbench.utils.library_tables import kicad_documents_dir, kicad_environment


@pytest.fixture
def kicad_home(tmp_path, monkeypatch):
    """An isolated KiCad: settings, documents and the stock data folder in tmp_path."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KICAD", name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "docs"))
    stock = tmp_path / "share" / "kicad"
    (stock / "template").mkdir(parents=True)
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: stock)
    return tmp_path


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestRules:
    @pytest.mark.parametrize("version", [None, "", "7.0.11", "10.99.0", "11.0.0", "10.0.1-rc1"])
    def test_versions_with_no_rules(self, version):
        with pytest.raises(np.NewProjectError):
            np.rules(version)

    @pytest.mark.parametrize("version", ["10.0.8", "10.0.7-15-gabc1234", "9.0.10"])
    def test_a_newer_patch_release_gets_the_newest_verified_rules(self, version):
        verified = {10: (10, 0, 7), 9: (9, 0, 9)}[int(version.split(".")[0])]
        assert np.rules(version).version == verified

    @pytest.mark.parametrize(
        "version, lock_by_extension, skip_autosave, gitignore, new_stub, writable",
        [
            ("8.0.0", True, True, False, False, False),
            ("8.0.7", True, True, False, False, False),
            ("8.0.8", False, True, False, False, False),
            ("9.0.1", False, True, False, False, False),
            ("9.0.2", False, True, False, True, False),
            ("10.0.0", False, False, True, True, False),
            ("10.0.6", False, False, True, True, False),
            ("10.0.7", False, False, True, True, True),
        ],
    )
    def test_rules_of_each_release(
        self, version, lock_by_extension, skip_autosave, gitignore, new_stub, writable
    ):
        rule = np.rules(version)
        assert rule.lock_by_extension == lock_by_extension
        assert rule.skip_autosave == skip_autosave
        assert rule.gitignore == gitignore == rule.rename_folders
        assert rule.new_stub == new_stub
        assert rule.make_writable == writable


class TestStubs:
    def test_old_schematic_stub(self):
        """KiCad 8.0.0 to 9.0.1: no UUID, symbol_instances."""
        assert np.schematic_stub(np.rules("8.0.9")) == (
            '(kicad_sch (version 20231120) (generator "eeschema") (generator_version "8.0")\n'
            '  (paper "A4")\n  (lib_symbols)\n  (symbol_instances)\n)\n'
        )
        assert np.schematic_stub(np.rules("9.0.1")).startswith(
            '(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
        )

    @pytest.mark.parametrize(
        "version, file_version, name", [("9.0.2", 20250114, "9.0"), ("10.0.6", 20260306, "10.0")]
    )
    def test_new_schematic_stub(self, version, file_version, name):
        text = np.schematic_stub(np.rules(version))
        expected = (
            "(kicad_sch\n"
            f"\t(version {file_version})\n"
            '\t(generator "eeschema")\n'
            f'\t(generator_version "{name}")\n'
            "\t(uuid UUID)\n"
            '\t(paper "A4")\n'
            "\t(lib_symbols)\n"
            '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
            "\t(embedded_fonts no)\n"
            ")"
        )
        assert re.sub(r"\(uuid [0-9a-f-]{36}\)", "(uuid UUID)", text) == expected

    @pytest.mark.parametrize(
        "version, board, name",
        [("8.0.9", 20240108, "8.0"), ("9.0.9", 20241229, "9.0"), ("10.0.7", 20260206, "10.0")],
    )
    def test_board_stub(self, version, board, name):
        assert np.board_stub(np.rules(version)) == (
            f'(kicad_pcb (version {board}) (generator "pcbnew") (generator_version "{name}")\n)'
        )

    def test_default_project_file(self):
        assert np.default_project_file(np.rules("10.0.6")) == "{}"
        assert np.default_project_file(np.rules("10.0.7")) == (
            '{\n  "meta": {\n    "version": 1\n  }\n}\n'
        )


class TestWildMatch:
    @pytest.mark.parametrize(
        "pattern, name, result",
        [
            ("*.bak", "a.bak", True),
            ("*.bak", "a.bak2", False),
            ("a?c", "abc", True),
            ("a?c", "ac", False),
            ("build", "build", True),
            ("*", ".hidden", False),
            ("[ab]", "a", False),
        ],
    )
    def test_wx_match_wild(self, pattern, name, result):
        assert np._wild_match(pattern, name) == result


def template(root: Path) -> Path:
    tpl = root / "MyTpl"
    write(tpl / "MyTpl.kicad_pro", "{}")
    write(tpl / "MyTpl.kicad_sch")
    write(tpl / "MyTpl.kicad_pcb")
    write(tpl / "MyTpl-cache.lib")
    write(tpl / "MyTpl.lib")
    write(tpl / "MyTpl.dcm")
    write(tpl / "MyTpl.kicad_wks")
    write(tpl / "MyTpl.pretty" / "MyTpl_fp.kicad_mod")
    write(tpl / "MyTpl-sheets" / "MyTpl_power.kicad_sch")
    write(tpl / "meta" / "info.html")
    write(tpl / "metadata" / "a.txt")
    write(tpl / "old-backups" / "a.zip")
    write(tpl / "fp-info-cache")
    write(tpl / "_autosave-MyTpl.kicad_sch")
    write(tpl / "~MyTpl.kicad_sch.lck")
    write(tpl / ".hidden")
    write(tpl / ".gitignore", "# comment\n*.bak\nbuild/\nnotes\n")
    write(tpl / ".gitattributes")
    write(tpl / "x.bak")
    write(tpl / "build" / "out.txt")
    write(tpl / "notes" / "n.txt")
    return tpl


def names(entries: list[Path], base: Path) -> set[str]:
    return {str(p.relative_to(base)).replace("\\", "/") for p in entries}


@pytest.mark.skipif(sys.platform == "win32", reason="a name with a dot is not hidden on Windows")
class TestTemplateEntries:
    def test_kicad_8(self, tmp_path):
        tpl = template(tmp_path)
        found = names(np.template_entries(np.rules("8.0.9"), tpl), tpl)
        assert "meta" not in found and "metadata" not in found and "old-backups" not in found
        assert "fp-info-cache" not in found and ".hidden" not in found
        assert "_autosave-MyTpl.kicad_sch" not in found
        assert "~MyTpl.kicad_sch.lck" not in found  # the prefix "~" (from 8.0.8)
        assert {".gitignore", ".gitattributes", "x.bak", "build", "build/out.txt"} <= found
        assert {"notes", "notes/n.txt", "MyTpl-sheets/MyTpl_power.kicad_sch"} <= found

    def test_kicad_8_0_7_skips_lck_by_extension(self, tmp_path):
        tpl = template(tmp_path)
        write(tpl / "~other.txt")
        found = names(np.template_entries(np.rules("8.0.7"), tpl), tpl)
        assert "~MyTpl.kicad_sch.lck" not in found and "~other.txt" in found

    def test_kicad_10_reads_the_gitignore(self, tmp_path):
        tpl = template(tmp_path)
        found = names(np.template_entries(np.rules("10.0.6"), tpl), tpl)
        # *.bak: files; build/: folders only; notes: a file or a folder.
        assert "x.bak" not in found and "build" not in found and "notes" not in found
        assert {".gitignore", ".gitattributes"} <= found
        # KiCad 10 does not skip the autosave files.
        assert "_autosave-MyTpl.kicad_sch" in found

    def test_a_gitignore_applies_to_its_own_folder(self, tmp_path):
        tpl = template(tmp_path)
        write(tpl / "sub" / "y.bak")
        found = names(np.template_entries(np.rules("10.0.6"), tpl), tpl)
        assert "sub/y.bak" in found


class TestTemplateCopies:
    def targets(self, version, tmp_path, project="proj"):
        tpl = template(tmp_path)
        target = tmp_path / "out"
        copies = np.template_copies(np.rules(version), tpl, target, project)
        return {str(c.target.relative_to(target)).replace("\\", "/") for c in copies}

    @pytest.mark.skipif(
        sys.platform == "win32", reason="a name with a dot is not hidden on Windows"
    )
    def test_names_of_kicad_8(self, tmp_path):
        found = self.targets("8.0.9", tmp_path)
        assert {"proj.kicad_pro", "proj.kicad_sch", "proj.kicad_pcb", "proj-cache.lib"} <= found
        # Not renamed: drawing sheets, legacy libraries, footprint libraries.
        assert {
            "MyTpl.kicad_wks",
            "MyTpl.lib",
            "MyTpl.dcm",
            "MyTpl.pretty/MyTpl_fp.kicad_mod",
        } <= found
        # KiCad 8 does not rename the folders, but the files in them.
        assert "MyTpl-sheets/proj_power.kicad_sch" in found

    @pytest.mark.skipif(
        sys.platform == "win32", reason="a name with a dot is not hidden on Windows"
    )
    def test_kicad_10_renames_the_folders(self, tmp_path):
        found = self.targets("10.0.6", tmp_path)
        assert "proj-sheets/proj_power.kicad_sch" in found
        assert "MyTpl.pretty/MyTpl_fp.kicad_mod" in found

    def test_folders_above_the_project_folder_stay(self, tmp_path):
        """KiCad 10 replaces "/<template>/" in the full path: the server only in the
        project folder."""
        tpl = write(tmp_path / "default" / "default.kicad_pro").parent
        target = tmp_path / "default" / "work" / "proj"
        (copy,) = np.template_copies(np.rules("10.0.6"), tpl, target, "proj")
        assert copy.target == target / "proj.kicad_pro"

    def test_more_project_files_give_the_folder_name(self, tmp_path):
        tpl = tmp_path / "Board"
        write(tpl / "a.kicad_pro")
        write(tpl / "b.kicad_pro")
        write(tpl / "Board.kicad_sch")
        copies = np.template_copies(np.rules("9.0.9"), tpl, tmp_path / "out", "proj")
        assert {c.target.name for c in copies} == {"a.kicad_pro", "b.kicad_pro", "proj.kicad_sch"}


class TestSearch:
    def test_project_file_search_order(self, kicad_home, monkeypatch):
        rule = np.rules("9.0.9")
        user = kicad_home / "user"
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(user))
        env = kicad_environment("9.0.9")
        stock = library_tables._stock_dir()
        paths = np.project_file_search_path(rule, env, stock)
        assert paths[0] == user
        assert paths[1] == kicad_documents_dir("9.0.9") / "template"
        assert paths[2] == stock / "template"
        write(stock / "template" / "kicad.kicad_pro")
        assert np.project_file_template(rule, env, stock) == stock / "template" / "kicad.kicad_pro"
        write(user / "kicad.kicad_pro")
        assert np.project_file_template(rule, env, stock) == user / "kicad.kicad_pro"

    def test_kicad_10_expands_the_template_variable(self, kicad_home, monkeypatch):
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", "${MYROOT}/tpl")
        monkeypatch.setenv("MYROOT", str(kicad_home))
        env = kicad_environment("10.0.6")
        assert np.user_template_dir(np.rules("10.0.6"), env) == kicad_home / "tpl"
        # KiCad 8 and 9 use the value as it is.
        env = kicad_environment("9.0.9")
        assert np.user_template_dir(np.rules("9.0.9"), env) == Path("${MYROOT}/tpl")

    def test_default_template_folder(self, kicad_home, monkeypatch):
        assert np.default_template_dir(np.rules("9.0.9"), kicad_environment("9.0.9")) is None
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(kicad_home / "user"))
        # 10.0.0 to 10.0.4: in KICAD_USER_TEMPLATE_DIR. From 10.0.5: in the documents.
        env = kicad_environment("10.0.4")
        assert np.default_template_dir(np.rules("10.0.4"), env) == kicad_home / "user" / "default"
        env = kicad_environment("10.0.5")
        assert np.default_template_dir(np.rules("10.0.5"), env) == (
            kicad_documents_dir("10.0.5") / "template" / "default"
        )

    def test_find_template(self, kicad_home, monkeypatch):
        rule = np.rules("9.0.9")
        stock = library_tables._stock_dir()
        write(stock / "template" / "Uno" / "Uno.kicad_pro")
        user = kicad_home / "user"
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(user))
        env = kicad_environment("9.0.9")
        assert np.find_template(rule, env, "Uno") == stock / "template" / "Uno"
        with pytest.raises(np.NewProjectError, match="Templates: Uno"):
            np.find_template(rule, env, "Mega")
        write(user / "Uno" / "Uno.kicad_pro")
        with pytest.raises(np.NewProjectError, match="More than one template"):
            np.find_template(rule, env, "Uno")

    def test_a_page_with_meta_is_one_template(self, kicad_home, monkeypatch):
        user = kicad_home / "One"
        write(user / "meta" / "info.html")
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(user))
        env = kicad_environment("9.0.9")
        assert np.find_template(np.rules("9.0.9"), env, "One") == user


class TestPlan:
    def test_legacy_project_file(self, kicad_home, tmp_path):
        tpl = tmp_path / "Old"
        write(tpl / "Old.pro")
        rule, env = np.rules("9.0.9"), kicad_environment("9.0.9")
        with pytest.raises(np.NewProjectError, match="legacy project file"):
            np.plan_project(rule, env, None, tmp_path / "out", "proj", tpl)

    def test_a_brd_file_gives_no_board_stub(self, kicad_home, tmp_path):
        tpl = tmp_path / "Old"
        write(tpl / "Old.kicad_pro")
        write(tpl / "Old.brd")
        rule, env = np.rules("9.0.9"), kicad_environment("9.0.9")
        plan = np.plan_project(rule, env, None, tmp_path / "out", "proj", tpl)
        assert not plan.write_board and plan.write_schematic and not plan.write_project_file

    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_kicad_10_0_7_makes_the_copy_writable(self, tmp_path):
        source = write(tmp_path / "a.txt")
        source.chmod(0o444)
        (tmp_path / "old").mkdir()
        (tmp_path / "new").mkdir()
        np.copy_file(np.rules("10.0.6"), source, tmp_path / "old" / "a.txt")
        np.copy_file(np.rules("10.0.7"), source, tmp_path / "new" / "a.txt")
        assert not os.access(tmp_path / "old" / "a.txt", os.W_OK)
        assert os.access(tmp_path / "new" / "a.txt", os.W_OK)


class TestVersionDetails:
    """The rules that change in the patch releases of KiCad 10."""

    def test_pages_of_kicad_10_0_0_to_10_0_3_use_the_process_environment(
        self, kicad_home, monkeypatch
    ):
        monkeypatch.setenv("MYROOT", str(kicad_home))
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", "${MYROOT}/tpl")
        env = kicad_environment("10.0.3")
        assert np.user_template_dir(np.rules("10.0.3"), env) == kicad_home / "tpl"
        # The search path of kicad.kicad_pro uses the value as it is before 10.0.4.
        paths = np.project_file_search_path(np.rules("10.0.3"), env, None)
        assert paths[0] == Path("${MYROOT}/tpl")
        assert np.project_file_search_path(np.rules("10.0.4"), env, None)[0] == kicad_home / "tpl"

    def test_kicad_10_0_4_with_an_unresolved_variable_has_no_default(self, kicad_home, monkeypatch):
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", "${NOT_SET_ANYWHERE}/tpl")
        env = kicad_environment("10.0.4")
        assert np.default_template_dir(np.rules("10.0.4"), env) is None
        assert np.default_template_dir(np.rules("10.0.3"), env) == Path(
            "${NOT_SET_ANYWHERE}/tpl/default"
        )

    def test_templates_need_meta_from_10_0_1(self, kicad_home):
        stock = library_tables._stock_dir()
        write(stock / "template" / "Plain" / "Plain.kicad_pro")
        write(stock / "template" / "Real" / "meta" / "info.html")
        for version, names in (
            ("9.0.9", ["Plain", "Real"]),
            ("10.0.0", ["Plain", "Real"]),
            ("10.0.1", ["Real"]),
        ):
            env = kicad_environment(version)
            assert np.list_templates(np.rules(version), env) == names, version

    def test_the_default_page_from_10_0_5(self, kicad_home, monkeypatch):
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(kicad_home / "custom"))
        default = kicad_documents_dir("10.0.5") / "template" / "default"
        write(default / "meta" / "info.html")
        env = kicad_environment("10.0.5")
        assert np.find_template(np.rules("10.0.5"), env, "default", default) == default
        env = kicad_environment("10.0.4")
        assert "default" not in np.list_templates(np.rules("10.0.4"), env, default)

    def test_versioned_variable_of_kicad_9_takes_the_user_template_dir(
        self, kicad_home, monkeypatch
    ):
        """No KICAD9_TEMPLATE_DIR: KiCad 9 takes the first name that matches
        "KICAD*_TEMPLATE_DIR", KiCad 10 only a name with digits."""
        monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(kicad_home / "user"))
        env = kicad_environment("9.0.9")
        assert np.system_template_dir(np.rules("9.0.9"), env) == kicad_home / "user"
        env = kicad_environment("10.0.6")
        assert np.system_template_dir(np.rules("10.0.6"), env) is None


class TestCopyErrors:
    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_kicad_copies_the_other_items_and_writes_no_stubs(self, kicad_home, tmp_path):
        tpl = tmp_path / "T"
        write(tpl / "T.kicad_pro", "{}")
        write(tpl / "a.txt")
        bad = write(tpl / "b.txt")
        bad.chmod(0)
        rule, env = np.rules("9.0.9"), kicad_environment("9.0.9")
        plan = np.plan_project(rule, env, None, tmp_path / "out", "proj", tpl)
        try:
            with pytest.raises(np.NewProjectError, match="b.txt"):
                np.write_project(rule, plan)
        finally:
            bad.chmod(0o644)
        names = sorted(p.name for p in (tmp_path / "out").iterdir())
        assert names == ["a.txt", "proj.kicad_pro"]

    @pytest.mark.skipif(sys.platform == "win32", reason="symbolic links of POSIX")
    def test_a_link_is_replaced_not_its_target(self, tmp_path):
        shared = write(tmp_path / "shared.txt", "shared")
        target = tmp_path / "out" / "a.txt"
        target.parent.mkdir()
        target.symlink_to(shared)
        np.copy_file(np.rules("9.0.9"), write(tmp_path / "src.txt", "new"), target)
        assert not target.is_symlink() and target.read_text(encoding="utf-8") == "new"
        assert shared.read_text(encoding="utf-8") == "shared"


class TestKiCadOnErrors:
    @pytest.mark.skipif(sys.platform == "win32", reason="wxCopyFile of POSIX")
    def test_a_file_copied_onto_itself_keeps_its_content(self, tmp_path):
        """wxCopyFile opens the source before it removes the target."""
        same = write(tmp_path / "a.txt", "content")
        np.copy_file(np.rules("9.0.9"), same, same)
        assert same.read_text(encoding="utf-8") == "content"

    def test_a_trailing_dot_stays(self, tmp_path):
        tpl = tmp_path / "T"
        write(tpl / "T.kicad_pro")
        write(tpl / "T.")
        copies = np.template_copies(np.rules("9.0.9"), tpl, tmp_path / "out", "proj")
        assert {c.target.name for c in copies} == {"proj.kicad_pro", "proj."}

    def test_a_folder_that_cannot_be_made_is_skipped(self, kicad_home, tmp_path):
        """CreateProject: a Mkdir error skips the items, and the project continues."""
        tpl = tmp_path / "T"
        write(tpl / "T.kicad_pro", "{}")
        write(tpl / "sub" / "a.txt")
        out = tmp_path / "out"
        write(out / "sub", "a file in the place of the folder")
        rule, env = np.rules("9.0.9"), kicad_environment("9.0.9")
        plan = np.plan_project(rule, env, None, out, "proj", tpl)
        written, warnings = np.write_project(rule, plan)
        assert any("cannot make the folder" in w for w in warnings)
        assert {p.name for p in written} == {"proj.kicad_pro", "proj.kicad_sch", "proj.kicad_pcb"}

    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_a_project_file_that_cannot_be_copied(self, kicad_home, tmp_path):
        """CreateNewProject writes the empty project file when wxCopyFile fails."""
        source = write(tmp_path / "kicad.kicad_pro", "{}")
        source.chmod(0)
        try:
            rule, env = np.rules("9.0.9"), kicad_environment("9.0.9")
            plan = np.plan_project(rule, env, None, tmp_path / "out", "proj", None)
            plan.project_file_source = source
            written, warnings = np.write_project(rule, plan)
        finally:
            source.chmod(0o644)
        assert (tmp_path / "out" / "proj.kicad_pro").read_bytes() == b"{\n}\n"
        assert any("cannot copy" in w for w in warnings)

    def test_an_error_of_make_writeable_is_a_warning(self, tmp_path, monkeypatch):
        """CreateProject ignores the result of MakeWriteable."""
        source = write(tmp_path / "a.txt")

        def chmod(self, mode):
            raise PermissionError("EPERM")

        monkeypatch.setattr(Path, "chmod", chmod)
        warning = np.copy_file(np.rules("10.0.7"), source, tmp_path / "b.txt")
        if sys.platform != "win32":
            assert warning and "not writable" in warning
        assert (tmp_path / "b.txt").is_file()

    def test_a_default_template_that_cannot_be_made(self, kicad_home):
        write(kicad_documents_dir("10.0.6") / "template", "a file in the place of the folder")
        folder, made, reason = np.ensure_default_template(
            np.rules("10.0.6"), kicad_environment("10.0.6")
        )
        assert folder is None and reason and "cannot be made" in reason


class TestDefaultPage:
    def test_the_default_page_only_when_kicad_made_it(self, kicad_home, monkeypatch):
        # A user page that is not the folder of "default".
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(kicad_home / "custom"))
        default = kicad_documents_dir("10.0.6") / "template" / "default"
        write(default / "meta" / "info.html")
        rule, env = np.rules("10.0.6"), kicad_environment("10.0.6")
        assert "default" not in np.list_templates(rule, env, None)
        assert np.find_template(rule, env, "default", default) == default

    def test_a_tilde_in_the_documents_folder(self, kicad_home, monkeypatch):
        monkeypatch.setenv("KICAD_DOCUMENTS_HOME", "~/kd")
        folder = np.default_template_dir(np.rules("10.0.7"), kicad_environment("10.0.7"))
        assert "~" not in str(folder) and folder.is_absolute()
