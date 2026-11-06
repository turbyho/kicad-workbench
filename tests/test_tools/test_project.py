"""Tests for create_kicad_project (tools/project.py).

The expected files are the files of the project manager of KiCad
(utils/new_project.py has the rules, test_new_project.py their tests).
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import project
from kicad_workbench.tools.project import _set_title_block, create_kicad_project
from kicad_workbench.utils import library_tables
from kicad_workbench.utils.kicad_cli import find_kicad_cli
from kicad_workbench.utils.library_tables import kicad_documents_dir

TEMPLATE_UUID = "e63e39d7-6ac0-4ffd-8aa3-1841a4541b55"
KICAD_PRO = '{"meta": {"filename": "kicad.kicad_pro"}}'


@pytest.fixture
def kicad(tmp_path, monkeypatch):
    """An isolated KiCad: settings, documents and the stock data folder in tmp_path.
    Call it with the installed version: kicad("9.0.9")."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KICAD", name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "docs"))
    stock = tmp_path / "share" / "kicad"
    (stock / "template").mkdir(parents=True)
    (stock / "template" / "kicad.kicad_pro").write_text(KICAD_PRO, encoding="utf-8")
    monkeypatch.setattr(library_tables, "_stock_dir", lambda version_dir: stock)

    def install(version):
        async def fake_version():
            return version

        monkeypatch.setattr(project, "get_kicad_version", fake_version)
        return stock

    return install


def make(path, name="blank", **kwargs) -> str:
    return asyncio.run(create_kicad_project(str(path), name, **kwargs))


def mega_template(stock: Path) -> Path:
    """A template in the system template folder, shaped like Arduino_Mega."""
    tpl = stock / "template" / "Arduino_Mega"
    (tpl / "meta").mkdir(parents=True)
    (tpl / "meta" / "info.html").write_text("<html/>", encoding="utf-8")
    (tpl / "Arduino_MountingHole.pretty").mkdir()
    (tpl / "Arduino_MountingHole.pretty" / "MountingHole_3.2mm.kicad_mod").write_text(
        "(footprint)", encoding="utf-8"
    )
    (tpl / "fp-lib-table").write_text("(fp_lib_table)", encoding="utf-8")
    (tpl / "Arduino_Mega.kicad_pcb").write_text("(kicad_pcb (version 20241229))", encoding="utf-8")
    (tpl / "Arduino_Mega.kicad_sch").write_text(
        f'(kicad_sch\n\t(version 20250114)\n\t(uuid "{TEMPLATE_UUID}")\n\t(paper "A4")\n'
        f'\t(symbol (instances (project "Arduino_Mega" (path "/{TEMPLATE_UUID}" '
        '(reference "J1")))))\n)\n',
        encoding="utf-8",
    )
    (tpl / "Arduino_Mega.kicad_pro").write_text(
        json.dumps(
            {"meta": {"filename": "Arduino_Mega.kicad_pro"}, "sheets": [[TEMPLATE_UUID, "Root"]]}
        ),
        encoding="utf-8",
    )
    (tpl / "custom.kicad_wks").write_text("(kicad_wks)", encoding="utf-8")
    return tpl


class TestSetTitleBlock:
    def test_creates_block_after_paper_when_absent(self):
        content = '(kicad_sch\n\t(uuid "x")\n\t(paper "A4")\n\t(lib_symbols)\n)\n'
        out = _set_title_block(content, "Board", "ACME", "2026-01-02")
        assert re.search(
            r'\(paper "A4"\)\s*\(title_block\s*\(title "Board"\)\s*'
            r'\(date "2026-01-02"\)\s*\(company "ACME"\)\s*\)',
            out,
        )

    def test_replaces_existing_fields(self):
        content = '(kicad_sch (paper "A4")\n(title_block\n(title "Old")\n(date "2000-01-01")\n))'
        out = _set_title_block(content, "New", "", "2026-01-02")
        assert '(title "New")' in out and "Old" not in out
        assert '(date "2026-01-02")' in out
        assert "(company" not in out

    def test_escapes_quotes_and_backslashes(self):
        out = _set_title_block('(kicad_sch (paper "A4"))', 'A "B" \\C', "", "2026-01-02")
        assert '(title "A \\"B\\" \\\\C")' in out
        # Re-applying must find and replace the escaped value, not add another.
        again = _set_title_block(out, "D", "", "2026-01-02")
        assert again.count("(title ") == 1 and '(title "D")' in again

    def test_order_of_kicad_and_other_fields_stay(self):
        """TITLE_BLOCK::Format: title, date, rev, company, comments. An empty value
        keeps the field of the file."""
        content = (
            '(kicad_sch (paper "A4") (title_block (comment 1 "c1") (rev "B") '
            '(company "Old Co") (comment 2 "c2")))'
        )
        out = _set_title_block(content, "T", "", "2026-01-02")
        block = parse(out).first("title_block")
        assert [c.head for c in block.children()] == [
            "title",
            "date",
            "rev",
            "company",
            "comment",
            "comment",
        ]
        assert block.first("company").value(0) == "Old Co"
        assert [c.value(1) for c in block.children("comment")] == ["c1", "c2"]

    def test_a_title_block_text_in_a_string_is_no_block(self):
        """The parser, not a text search: a text "(title_block" in a string."""
        content = '(kicad_sch (paper "A4") (text "(title_block" (at 0 0 0)))'
        out = _set_title_block(content, "T", "", "")
        root = parse(out)
        assert root.first("title_block").first("title").value(0) == "T"
        assert root.first("text").value(0) == "(title_block"


class TestEmptyProject:
    @pytest.mark.parametrize(
        "version, board, generator, old_stub",
        [
            ("8.0.9", 20240108, "8.0", True),
            ("9.0.1", 20241229, "9.0", True),
            ("9.0.9", 20241229, "9.0", False),
        ],
    )
    def test_kicad_8_and_9(self, tmp_path, kicad, version, board, generator, old_stub):
        """CreateNewProject: a copy of kicad.kicad_pro, and the stubs."""
        kicad(version)
        out = tmp_path / "proj"
        result = make(out, title="My Board", company="ACME")
        assert result.startswith(f"# ✅ KiCad {version} Project Created"), result
        assert sorted(p.name for p in out.iterdir()) == [
            "blank.kicad_pcb",
            "blank.kicad_pro",
            "blank.kicad_sch",
        ]
        assert (out / "blank.kicad_pro").read_text(encoding="utf-8") == KICAD_PRO
        assert (out / "blank.kicad_pcb").read_text(encoding="utf-8") == (
            f'(kicad_pcb (version {board}) (generator "pcbnew") (generator_version "{generator}")\n)'
        )
        sch = (out / "blank.kicad_sch").read_text(encoding="utf-8")
        assert ("(symbol_instances)" in sch) == old_stub
        assert ("(uuid " in sch) == (not old_stub)
        block = parse(sch).first("title_block")
        assert [c.value(0) for c in block.children()] == [
            "My Board",
            re.search(r'\(date "([^"]+)"\)', sch).group(1),
            "ACME",
        ]
        assert "copy of" in result and "kicad.kicad_pro" in result
        # No template "default" in KiCad 8 and 9.
        assert not (kicad_documents_dir(version) / "template").exists()

    def test_no_kicad_pro_gives_an_empty_project_file(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        (stock / "template" / "kicad.kicad_pro").unlink()
        result = make(tmp_path / "proj")
        assert (tmp_path / "proj" / "blank.kicad_pro").read_bytes() == b"{\n}\n"
        assert "KiCad finds no kicad.kicad_pro" in result

    @pytest.mark.parametrize(
        "version, text",
        [("10.0.6", "{}"), ("10.0.7", '{\n  "meta": {\n    "version": 1\n  }\n}\n')],
    )
    def test_kicad_10_uses_the_template_default(self, tmp_path, kicad, version, text):
        kicad(version)
        result = make(tmp_path / "proj")
        default = kicad_documents_dir(version) / "template" / "default"
        assert (default / "meta" / "info.html").is_file()
        assert (default / "default.kicad_pro").read_text(encoding="utf-8") == text
        assert 'makes the template "default"' in result
        assert (tmp_path / "proj" / "blank.kicad_pro").read_text(encoding="utf-8") == text
        sch = (tmp_path / "proj" / "blank.kicad_sch").read_text(encoding="utf-8")
        assert "(version 20260306)" in sch and '(generator_version "10.0")' in sch
        # The template is there now: KiCad and the tool make it one time.
        assert 'makes the template "default"' not in make(tmp_path / "proj2")

    def test_kicad_10_uses_the_default_template_of_the_user(self, tmp_path, kicad):
        kicad("10.0.6")
        default = kicad_documents_dir("10.0.6") / "template" / "default"
        (default / "meta").mkdir(parents=True)
        (default / "meta" / "info.html").write_text("<html/>", encoding="utf-8")
        (default / "default.kicad_pro").write_text('{"x": 1}', encoding="utf-8")
        (default / "default.kicad_sch").write_text(
            '(kicad_sch (version 20260306) (paper "A4"))', encoding="utf-8"
        )
        (default / "notes.txt").write_text("n", encoding="utf-8")
        make(tmp_path / "proj", title="T")
        out = tmp_path / "proj"
        assert sorted(p.name for p in out.iterdir()) == [
            "blank.kicad_pcb",
            "blank.kicad_pro",
            "blank.kicad_sch",
            "notes.txt",
        ]
        assert (out / "blank.kicad_pro").read_text(encoding="utf-8") == '{"x": 1}'
        assert '(title "T")' in (out / "blank.kicad_sch").read_text(encoding="utf-8")

    def test_kicad_10_0_4_uses_kicad_user_template_dir(self, tmp_path, kicad, monkeypatch):
        kicad("10.0.4")
        user = tmp_path / "my-templates"
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", str(user))
        make(tmp_path / "proj")
        assert (user / "default" / "default.kicad_pro").is_file()

    def test_a_folder_that_is_not_empty(self, tmp_path, kicad):
        """KiCad 8 and 9 ask before they use a folder that is not empty, then keep its files."""
        kicad("9.0.9")
        out = tmp_path / "proj"
        out.mkdir()
        (out / "blank.kicad_sch").write_text("mine", encoding="utf-8")
        (out / "readme.txt").write_text("r", encoding="utf-8")
        result = make(out)
        assert result.startswith(f"❌ The folder {out} is not empty: blank.kicad_sch, readme.txt.")
        assert sorted(p.name for p in out.iterdir()) == ["blank.kicad_sch", "readme.txt"]
        result = make(out, overwrite=True)
        assert "Project Created" in result
        assert (out / "blank.kicad_sch").read_text(encoding="utf-8") == "mine"
        assert (out / "blank.kicad_pro").is_file() and (out / "blank.kicad_pcb").is_file()

    def test_a_project_file_of_the_template_default(self, tmp_path, kicad):
        kicad("10.0.6")
        out = tmp_path / "proj"
        out.mkdir()
        (out / "blank.kicad_pro").write_text("mine", encoding="utf-8")
        assert make(out).startswith(f"❌ Files of the template exist in {out}: blank.kicad_pro.")
        assert "Project Created" in make(out, overwrite=True)
        assert (out / "blank.kicad_pro").read_text(encoding="utf-8") == "{}"

    def test_a_schematic_that_kicad_has_open(self, tmp_path, kicad):
        kicad("10.0.6")
        out = tmp_path / "proj"
        out.mkdir()
        for name in ("blank.kicad_pro", "blank.kicad_sch", "~blank.kicad_sch.lck"):
            (out / name).write_text("x", encoding="utf-8")
        default = kicad_documents_dir("10.0.6") / "template" / "default"
        (default / "meta").mkdir(parents=True)
        (default / "meta" / "info.html").write_text("<html/>", encoding="utf-8")
        (default / "default.kicad_pro").write_text("{}", encoding="utf-8")
        (default / "default.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        result = make(out, overwrite=True)
        # The text of utils/kicad_lock.py, as the other edit tools give it.
        assert result.startswith("Error: KiCad has the schematic open"), result
        assert (out / "blank.kicad_sch").read_text(encoding="utf-8") == "x"

    @pytest.mark.parametrize("version", [None, "7.0.11", "10.0.8", "10.99.0"])
    def test_a_version_with_no_rules(self, tmp_path, kicad, version):
        kicad(version)
        result = make(tmp_path / "proj")
        assert result.startswith("❌") and not (tmp_path / "proj").exists()

    def test_title_defaults_to_project_name(self, tmp_path, kicad):
        kicad("9.0.9")
        make(tmp_path, "board")
        sch = (tmp_path / "board.kicad_sch").read_text(encoding="utf-8")
        assert '(title "board")' in sch and "(company" not in sch

    @pytest.mark.parametrize("field", ["project_name", "title", "company"])
    def test_text_that_is_not_valid_changes_no_file(self, tmp_path, kicad, field):
        kicad("9.0.9")
        kwargs = {"project_name": "blank", "title": "", "company": ""}
        kwargs[field] = "\ud800"
        result = asyncio.run(create_kicad_project(str(tmp_path / "proj"), **kwargs))
        assert result.startswith(f"❌ The {field} is not valid text")
        assert not (tmp_path / "proj").exists()


class TestTemplateProject:
    def test_files_of_the_template_as_kicad_copies_them(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        tpl = mega_template(stock)
        out = tmp_path / "proj"
        result = make(out, "shield", template="Arduino_Mega", title="Shield")
        assert "Project Created" in result, result
        assert sorted(str(p.relative_to(out)) for p in out.rglob("*")) == sorted(
            [
                "Arduino_MountingHole.pretty",
                str(Path("Arduino_MountingHole.pretty") / "MountingHole_3.2mm.kicad_mod"),
                "custom.kicad_wks",
                "fp-lib-table",
                "shield.kicad_pcb",
                "shield.kicad_pro",
                "shield.kicad_sch",
            ]
        )
        # KiCad changes no file of the template: the project file, the UUID and the
        # instances stay. The tool writes only the title block.
        assert (out / "shield.kicad_pro").read_text(encoding="utf-8") == (
            tpl / "Arduino_Mega.kicad_pro"
        ).read_text(encoding="utf-8")
        sch = (out / "shield.kicad_sch").read_text(encoding="utf-8")
        assert sch.count(TEMPLATE_UUID) == 2
        assert parse(sch).first("title_block").first("title").value(0) == "Shield"
        assert (out / "shield.kicad_pcb").read_text(encoding="utf-8") == (
            tpl / "Arduino_Mega.kicad_pcb"
        ).read_text(encoding="utf-8")

    def test_missing_files_get_the_stubs(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        tpl = stock / "template" / "Only"
        tpl.mkdir()
        (tpl / "Only.kicad_pro").write_text("{}", encoding="utf-8")
        make(tmp_path / "proj", template="Only")
        out = tmp_path / "proj"
        assert (out / "blank.kicad_pro").read_text(encoding="utf-8") == "{}"
        assert (
            (out / "blank.kicad_pcb")
            .read_text(encoding="utf-8")
            .startswith("(kicad_pcb (version 20241229)")
        )
        assert "(kicad_sch" in (out / "blank.kicad_sch").read_text(encoding="utf-8")

    def test_template_by_path(self, tmp_path, kicad):
        kicad("9.0.9")
        tpl = tmp_path / "elsewhere" / "Mine"
        tpl.mkdir(parents=True)
        (tpl / "Mine.kicad_pro").write_text("{}", encoding="utf-8")
        result = make(tmp_path / "proj", template=str(tpl))
        assert "Project Created" in result and (tmp_path / "proj" / "blank.kicad_pro").is_file()

    def test_unknown_template_lists_available(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        mega_template(stock)
        result = make(tmp_path / "proj", template="Nope")
        assert result.startswith("❌ KiCad has no template 'Nope'") and "Arduino_Mega" in result
        assert not (tmp_path / "proj").exists()

    def test_files_of_the_template_exist(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        mega_template(stock)
        out = tmp_path / "proj"
        out.mkdir()
        (out / "fp-lib-table").write_text("mine", encoding="utf-8")
        (out / "shield.kicad_pcb").write_text("mine", encoding="utf-8")
        result = make(out, "shield", template="Arduino_Mega")
        assert result.startswith(
            f"❌ Files of the template exist in {out}: fp-lib-table, shield.kicad_pcb."
        )
        assert sorted(p.name for p in out.iterdir()) == ["fp-lib-table", "shield.kicad_pcb"]
        assert "Project Created" in make(out, "shield", template="Arduino_Mega", overwrite=True)
        assert (out / "fp-lib-table").read_text(encoding="utf-8") == "(fp_lib_table)"

    def test_a_template_with_only_a_legacy_project_file(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        tpl = stock / "template" / "Old"
        tpl.mkdir()
        (tpl / "Old.pro").write_text("", encoding="utf-8")
        result = make(tmp_path / "proj", template="Old")
        assert result.startswith("❌") and "legacy project file" in result
        assert not (tmp_path / "proj").exists()

    def test_a_template_schematic_that_the_parser_cannot_read(self, tmp_path, kicad):
        """The project is made as KiCad makes it; only the title block is missing."""
        stock = kicad("9.0.9")
        tpl = mega_template(stock)
        (tpl / "Arduino_Mega.kicad_sch").write_text(
            "(kicad_sch (version 20250114)", encoding="utf-8"
        )
        result = make(tmp_path / "proj", "shield", template="Arduino_Mega")
        assert "Project Created" in result
        assert "⚠️ The tool did not write the title block of shield.kicad_sch" in result


def _kicad_10_installed() -> bool:
    if find_kicad_cli() is None:
        return False
    from kicad_workbench.utils.kicad_cli import kicad_cli_version_sync

    version = kicad_cli_version_sync() or ""
    return version.startswith("10.0.")


@pytest.mark.skipif(not _kicad_10_installed(), reason="KiCad 10.0 not installed")
class TestWithRealKicad:
    @pytest.fixture(autouse=True)
    def _documents(self, tmp_path, monkeypatch):
        # The template "default" goes into the test folder, not to the user.
        monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "docs"))

    def test_empty_project_passes_erc_and_drc(self, tmp_path):
        from kicad_workbench.utils.kicad_cli import run_kicad_cli_sync

        out = tmp_path / "proj"
        result = make(out, company="ACME")
        assert "Project Created" in result, result
        erc = run_kicad_cli_sync(
            ["sch", "erc", "-o", str(tmp_path / "erc.rpt"), str(out / "blank.kicad_sch")],
            timeout=120,
        )
        assert erc.returncode == 0
        assert "ERC messages: 0" in (tmp_path / "erc.rpt").read_text(encoding="utf-8")
        drc = run_kicad_cli_sync(
            ["pcb", "drc", "-o", str(tmp_path / "drc.rpt"), str(out / "blank.kicad_pcb")],
            timeout=120,
        )
        assert drc.returncode == 0

    def test_arduino_mega_template_includes_footprint_library(self, tmp_path):
        result = make(tmp_path / "proj", "shield", template="Arduino_Mega")
        if "has no template" in result:
            pytest.skip("Arduino_Mega template not installed")
        out = tmp_path / "proj"
        fp_lib_table = (out / "fp-lib-table").read_text(encoding="utf-8")
        for lib_dir in re.findall(r"\$\{KIPRJMOD\}/([^)\"]+)", fp_lib_table):
            assert (out / lib_dir).is_dir(), lib_dir
        assert not (out / "meta").exists()


class TestRefusals:
    @pytest.mark.parametrize("name", ["", " ", ".", "..", "../other", "a/b", "a\\b"])
    def test_a_name_that_is_not_a_file_name(self, tmp_path, kicad, name):
        kicad("9.0.9")
        result = make(tmp_path / "proj", name)
        assert result.startswith("❌ The project_name"), result
        assert not (tmp_path / "proj").exists()

    def test_an_open_project_is_not_replaced(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        mega_template(stock)
        out = tmp_path / "proj"
        out.mkdir()
        (out / "shield.kicad_pro").write_text("mine", encoding="utf-8")
        (out / "~shield.kicad_pro.lck").write_text("{}", encoding="utf-8")
        result = make(out, "shield", template="Arduino_Mega", overwrite=True)
        assert result.startswith("Error: KiCad has the project open"), result
        assert (out / "shield.kicad_pro").read_text(encoding="utf-8") == "mine"

    def test_no_template_checks_no_lock_of_the_files_that_stay(self, tmp_path, kicad):
        """KiCad 8 and 9 keep the files of the folder: an open other schematic is no
        reason to refuse."""
        kicad("9.0.9")
        out = tmp_path / "proj"
        out.mkdir()
        (out / "old.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        (out / "~old.kicad_sch.lck").write_text("{}", encoding="utf-8")
        result = make(out, overwrite=True)
        assert "Project Created" in result, result


class TestKiCad10WithNoDefault:
    def test_10_0_4_with_no_usable_user_template_dir(self, tmp_path, kicad, monkeypatch):
        """No template "default": KiCad makes the project with no template."""
        kicad("10.0.4")
        monkeypatch.setenv("KICAD_USER_TEMPLATE_DIR", "${NOT_SET_ANYWHERE}/tpl")
        result = make(tmp_path / "proj")
        assert "Project Created" in result and 'has no template "default"' in result
        assert (tmp_path / "proj" / "blank.kicad_pro").read_text(encoding="utf-8") == KICAD_PRO
        # And it shows no template selector.
        result = make(tmp_path / "proj2", template="Arduino_Mega")
        assert result.startswith("❌") and "shows no template selector" in result

    def test_10_0_6_when_default_cannot_be_made(self, tmp_path, kicad):
        stock = kicad("10.0.6")
        mega_template(stock)
        (kicad_documents_dir("10.0.6")).mkdir(parents=True)
        (kicad_documents_dir("10.0.6") / "template").write_text("x", encoding="utf-8")
        result = make(tmp_path / "proj")
        assert result.startswith("❌") and "offers only the templates of its pages" in result
        result = make(tmp_path / "proj2", "shield", template="Arduino_Mega")
        assert "Project Created" in result, result


class TestNames:
    def test_the_extension_of_the_project_file_goes(self, tmp_path, kicad):
        kicad("9.0.9")
        make(tmp_path, "board.kicad_pro")
        assert (tmp_path / "board.kicad_pro").is_file()
        assert not (tmp_path / "board.kicad_pro.kicad_pro").exists()

    def test_a_template_path_that_is_no_template(self, tmp_path, kicad):
        kicad("9.0.9")
        folder = tmp_path / "all"
        (folder / "A").mkdir(parents=True)
        result = make(tmp_path / "proj", template=str(folder))
        assert result.startswith("❌") and "is no template" in result


class TestRoundThree:
    @pytest.mark.parametrize(
        "name, project",
        [
            ("board.kicad_pro", "board"),
            ("board.", "board"),
            ("board.v2", "board.v2"),
            (".kicad_pro", ".kicad_pro"),
        ],
    )
    def test_the_name_as_the_file_dialog_of_kicad(self, tmp_path, kicad, name, project):
        kicad("9.0.9")
        result = make(tmp_path / "p", name)
        assert "Project Created" in result, result
        assert (tmp_path / "p" / f"{project}.kicad_pro").is_file()

    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_a_read_only_template_schematic(self, tmp_path, kicad):
        """KiCad 9 keeps the mode of the template file: the title block is a warning."""
        stock = kicad("9.0.9")
        tpl = mega_template(stock)
        (tpl / "Arduino_Mega.kicad_sch").chmod(0o444)
        result = make(tmp_path / "proj", "shield", template="Arduino_Mega")
        assert "Project Created" in result, result
        assert "⚠️ The tool did not write the title block of shield.kicad_sch" in result

    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_a_folder_that_is_not_writable(self, tmp_path, kicad):
        kicad("9.0.9")
        out = tmp_path / "proj"
        out.mkdir()
        out.chmod(0o555)
        try:
            result = make(out)
        finally:
            out.chmod(0o755)
        assert result.startswith("❌ Insufficient permissions to write to folder"), result

    @pytest.mark.skipif(sys.platform == "win32", reason="the permission bits of POSIX")
    def test_no_copy_note_when_the_copy_failed(self, tmp_path, kicad):
        stock = kicad("9.0.9")
        (stock / "template" / "kicad.kicad_pro").chmod(0)
        try:
            result = make(tmp_path / "proj")
        finally:
            (stock / "template" / "kicad.kicad_pro").chmod(0o644)
        assert "KiCad cannot copy" in result and "is a copy of" not in result

    def test_kicad_10_0_1_needs_meta_for_a_template_path(self, tmp_path, kicad):
        kicad("10.0.6")
        folder = tmp_path / "proj_tpl"
        folder.mkdir()
        (folder / "proj_tpl.kicad_pro").write_text("{}", encoding="utf-8")
        result = make(tmp_path / "proj", template=str(folder))
        assert result.startswith("❌") and 'has no folder "meta"' in result
        kicad("10.0.0")
        assert "Project Created" in make(tmp_path / "proj2", template=str(folder))
