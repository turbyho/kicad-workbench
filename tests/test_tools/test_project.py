"""Tests for create_kicad_project (tools/project.py)."""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import schematic_version
from kicad_workbench.parsers.sexpr import parse
from kicad_workbench.tools import project
from kicad_workbench.tools.project import _set_title_block, create_kicad_project
from kicad_workbench.utils import kicad_python
from kicad_workbench.utils.kicad_cli import find_kicad_cli
from kicad_workbench.utils.kicad_python import find_kicad_python

from .locale_env import can_name, non_utf8_env, run_python

ROOT_UUID = "e63e39d7-6ac0-4ffd-8aa3-1841a4541b55"


class _FakeResult:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _kicad_version(monkeypatch):
    async def fake_version():
        return "10.0.6"

    monkeypatch.setattr(project, "get_kicad_version", fake_version)


@pytest.fixture
def template_base(tmp_path, monkeypatch):
    """A fake KiCad template directory shaped like template/Arduino_Mega."""
    base = tmp_path / "templates"
    tpl = base / "Arduino_Mega"
    (tpl / "meta").mkdir(parents=True)
    (tpl / "meta" / "info.html").write_text("<html/>", encoding="utf-8")
    (tpl / "Arduino_MountingHole.pretty").mkdir()
    (tpl / "Arduino_MountingHole.pretty" / "MountingHole_3.2mm.kicad_mod").write_text(
        "(footprint)", encoding="utf-8"
    )
    (tpl / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Arduino_MountingHole")'
        '(uri "${KIPRJMOD}/Arduino_MountingHole.pretty")))',
        encoding="utf-8",
    )
    (tpl / "Arduino_Mega.kicad_pcb").write_text("(kicad_pcb (version 20260206))", encoding="utf-8")
    (tpl / "Arduino_Mega.kicad_sch").write_text(
        f'(kicad_sch\n\t(version 20250114)\n\t(uuid "{ROOT_UUID}")\n\t(paper "A4")\n'
        f'\t(symbol (instances (project "Arduino_Mega" (path "/{ROOT_UUID}" (reference "J1")))))\n)\n',
        encoding="utf-8",
    )
    (tpl / "Arduino_Mega.kicad_pro").write_text(
        json.dumps(
            {"meta": {"filename": "Arduino_Mega.kicad_pro"}, "sheets": [[ROOT_UUID, "Root"]]}
        ),
        encoding="utf-8",
    )
    (tpl / "custom.kicad_wks").write_text("(kicad_wks)", encoding="utf-8")
    monkeypatch.setattr(project, "_kicad_template_dirs", lambda: [base])
    return base


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
    @pytest.fixture
    def fake_kicad(self, monkeypatch):
        """Stub pcbnew and kicad-cli; record their arguments."""
        calls = {}

        calls["pcbnew_version"] = "10.0.6"

        def fake_python(args, timeout=120.0, input=None):
            calls["python"] = args
            pcb = Path(args[2])
            pcb.write_text("(kicad_pcb (version 20260206))", encoding="utf-8")
            pcb.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
            pcb.with_suffix(".kicad_prl").write_text("{}", encoding="utf-8")
            return _FakeResult(stdout=f"KICAD_VERSION={calls['pcbnew_version']}\n".encode())

        def fake_cli(args, timeout=60.0, english=False):
            calls["cli"] = args
            return _FakeResult()

        monkeypatch.setattr(project, "find_kicad_python", lambda: sys.executable)
        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", fake_python)
        monkeypatch.setattr(project, "run_kicad_cli_sync", fake_cli)
        return calls

    def test_is_the_default_and_uses_kicad_tooling(self, tmp_path, fake_kicad):
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "blank", "My Board", "ACME"))

        py_args = fake_kicad["python"]
        assert py_args[0] == "-c" and "pcbnew.NewBoard" in py_args[1]
        # The tool makes the files in a temporary folder in the project folder.
        assert Path(py_args[2]).name == "blank.kicad_pcb"
        assert Path(py_args[2]).parent.parent == out_dir
        assert py_args[3:5] == ["My Board", "ACME"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", py_args[5])
        assert fake_kicad["cli"][:2] == ["sch", "upgrade"]
        assert Path(fake_kicad["cli"][2]).name == "blank.kicad_sch"
        # Only the files of the project stay: no temporary folder.
        assert sorted(p.name for p in out_dir.iterdir()) == [
            "blank.kicad_pcb",
            "blank.kicad_prl",
            "blank.kicad_pro",
            "blank.kicad_sch",
        ]

        sch = (out_dir / "blank.kicad_sch").read_text(encoding="utf-8")
        assert sch.startswith("(kicad_sch")
        assert '(title "My Board")' in sch and '(company "ACME")' in sch
        assert re.search(r'\(uuid "[0-9a-f-]{36}"\)', sch)
        assert "(symbol" not in sch

        assert "KiCad 10.0.6 Project Created" in result
        assert "9.0+" not in result
        assert "**Template:** empty" in result
        for name in ("blank.kicad_pro", "blank.kicad_prl", "blank.kicad_sch", "blank.kicad_pcb"):
            assert f"- {name}" in result
        assert "Warnings" not in result

    def test_title_defaults_to_project_name(self, tmp_path, fake_kicad):
        asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert fake_kicad["python"][3] == "blank"
        assert fake_kicad["python"][4] == ""
        sch = (tmp_path / "blank.kicad_sch").read_text(encoding="utf-8")
        assert '(title "blank")' in sch and "(company" not in sch

    def test_upgrade_failure_is_a_warning(self, tmp_path, fake_kicad, monkeypatch):
        def failing_cli(args, timeout=60.0, english=False):
            return _FakeResult(returncode=1, stderr=b"boom")

        monkeypatch.setattr(project, "run_kicad_cli_sync", failing_cli)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert "Project Created" in result
        assert "Warnings" in result and "boom" in result
        assert (tmp_path / "blank.kicad_sch").exists()

    @pytest.mark.parametrize(
        "version, file_version, generator, fonts, upgrade",
        [
            # KiCad 8 and 9 have no "kicad-cli sch upgrade": their own format.
            ("8.0.9", 20231120, "8.0", False, False),
            ("9.0.9", 20250114, "9.0", True, False),
            # KiCad 10 converts a schematic of KiCad 9.
            ("10.0.6", 20250114, "9.0", True, True),
            # A development build: kicad-cli tells if it can convert it.
            ("9.99.0", 20250114, "9.0", True, True),
            ("10.99.0", 20250114, "9.0", True, True),
        ],
    )
    def test_schematic_of_the_installed_kicad(
        self, tmp_path, fake_kicad, monkeypatch, version, file_version, generator, fonts, upgrade
    ):
        async def fake_version():
            return version

        monkeypatch.setattr(project, "get_kicad_version", fake_version)
        fake_kicad["pcbnew_version"] = version
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank", "T"))
        assert "Warnings" not in result
        root = parse((tmp_path / "blank.kicad_sch").read_text(encoding="utf-8"))
        assert schematic_version(root) == file_version
        assert root.first("generator_version").value(0) == generator
        assert (root.first("embedded_fonts") is not None) == fonts
        assert root.first("title_block").first("title").value(0) == "T"
        assert ("cli" in fake_kicad) == upgrade

    @pytest.mark.parametrize(
        "pcbnew, file_version, warning",
        [("10.0.6", 20250114, True), ("9.0.9", 20250114, False), ("8.0.9", 20231120, False)],
    )
    def test_no_kicad_cli_version(
        self, tmp_path, fake_kicad, monkeypatch, pcbnew, file_version, warning
    ):
        """The format of the KiCad of pcbnew, with no conversion. A warning only when
        the format is not the own format of that KiCad."""

        async def no_version():
            return None

        monkeypatch.setattr(project, "get_kicad_version", no_version)
        fake_kicad["pcbnew_version"] = pcbnew
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        root = parse((tmp_path / "blank.kicad_sch").read_text(encoding="utf-8"))
        assert schematic_version(root) == file_version
        assert "cli" not in fake_kicad
        assert ("kicad-cli gave no version (it is not found, or it failed)" in result) == warning
        assert ("Warnings" in result) == warning

    def test_a_version_text_that_the_server_cannot_read(self, tmp_path, fake_kicad, monkeypatch):
        async def odd_version():
            return "build abc"

        monkeypatch.setattr(project, "get_kicad_version", odd_version)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert "cli" not in fake_kicad
        assert "kicad-cli gave the version text 'build abc'" in result

    def test_pcbnew_and_kicad_cli_of_different_kicad(self, tmp_path, fake_kicad):
        """KICAD_PYTHON of KiCad 8 and kicad-cli of KiCad 10: no file of the project."""
        fake_kicad["pcbnew_version"] = "8.0.9"
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "blank"))
        assert result.startswith("❌ kicad-cli is KiCad 10.0.6, but the Python with pcbnew is ")
        # No file, and no folder: the tool made the folder, and removes it.
        assert not out_dir.exists()

    def test_all_new_folders_go_after_an_error(self, tmp_path, fake_kicad):
        fake_kicad["pcbnew_version"] = "8.0.9"
        result = asyncio.run(create_kicad_project(str(tmp_path / "a" / "b" / "proj"), "blank"))
        assert result.startswith("❌") and list(tmp_path.iterdir()) == []

    def test_a_failed_move_moves_the_files_back(self, tmp_path, fake_kicad, monkeypatch):
        """All files or none: the files that moved go back."""
        replace = project.os.replace
        count = {"moves": 0}

        def failing_replace(source, target):
            if Path(target).parent == tmp_path:
                count["moves"] += 1
                if count["moves"] == 3:
                    raise PermissionError("denied")
            replace(source, target)

        monkeypatch.setattr(project.os, "replace", failing_replace)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert result.startswith(f"❌ The tool cannot move the files into {tmp_path}: denied")
        assert list(tmp_path.iterdir()) == []

    def test_pcbnew_of_kicad_7_is_refused(self, tmp_path, fake_kicad, monkeypatch):
        async def no_version():
            return None

        monkeypatch.setattr(project, "get_kicad_version", no_version)
        fake_kicad["pcbnew_version"] = "7.0.11"
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert result.startswith("❌ pcbnew is KiCad 7.0.11. The server supports KiCad 8")
        assert list(tmp_path.iterdir()) == []

    def test_kicad_cli_of_kicad_7_is_refused(self, tmp_path, fake_kicad, monkeypatch):
        async def old_version():
            return "7.0.11"

        monkeypatch.setattr(project, "get_kicad_version", old_version)
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "blank"))
        assert result.startswith("❌ KiCad 7.0.11 is installed.")
        assert "python" not in fake_kicad and not out_dir.exists()

    def test_an_upgrade_to_a_version_with_no_rules(self, tmp_path, fake_kicad, monkeypatch):
        """A nightly build newer than the rules of the server: a warning."""

        async def new_version():
            return "11.99.0"

        def upgrade(args, timeout=60.0, english=False):
            fake_kicad["cli"] = args
            path = Path(args[2])
            path.write_text(
                path.read_text(encoding="utf-8").replace("20250114", "20991231"), encoding="utf-8"
            )
            return _FakeResult()

        monkeypatch.setattr(project, "get_kicad_version", new_version)
        monkeypatch.setattr(project, "run_kicad_cli_sync", upgrade)
        fake_kicad["pcbnew_version"] = "11.99.0"
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert "The new schematic:" in result and "The other tools cannot read it." in result

    def test_pcbnew_failure_is_reported(self, tmp_path, fake_kicad, monkeypatch):
        def failing_python(args, timeout=120.0, input=None):
            return _FakeResult(returncode=1, stderr=b"ImportError: pcbnew")

        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", failing_python)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert result.startswith("❌") and "ImportError: pcbnew" in result
        assert not (tmp_path / "blank.kicad_sch").exists()

    def test_pcbnew_timeout_is_reported(self, tmp_path, fake_kicad, monkeypatch):
        def slow_python(args, timeout=120.0, input=None):
            raise subprocess.TimeoutExpired("python", timeout)

        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", slow_python)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert result == "❌ pcbnew did not stop in 120 s. The tool wrote no file."
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "error, text",
        [
            (subprocess.TimeoutExpired("kicad-cli", 120), "it did not stop in 120 s"),
            (RuntimeError("kicad-cli cannot read a file path that is not ASCII"), "not ASCII"),
            (FileNotFoundError("kicad-cli not found"), "kicad-cli not found"),
        ],
    )
    def test_an_upgrade_error_keeps_the_project(
        self, tmp_path, fake_kicad, monkeypatch, error, text
    ):
        """The schematic of KiCad 9 is valid: a warning, not an error."""

        def failing(args, timeout=60.0, english=False):
            raise error

        monkeypatch.setattr(project, "run_kicad_cli_sync", failing)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert "Project Created" in result and "`kicad-cli sch upgrade` failed" in result
        assert text in result
        root = parse((tmp_path / "blank.kicad_sch").read_text(encoding="utf-8"))
        assert schematic_version(root) == 20250114

    def test_a_file_that_an_other_call_made(self, tmp_path, fake_kicad, monkeypatch):
        """A file of the project that appears during the work: no file changes."""
        original = kicad_python.run_kicad_python_sync

        def python(args, timeout=120.0, input=None):
            (tmp_path / "blank.kicad_sch").write_text("other", encoding="utf-8")
            return original(args, timeout, input)

        monkeypatch.setattr(kicad_python, "run_kicad_python_sync", python)
        result = asyncio.run(create_kicad_project(str(tmp_path), "blank"))
        assert result.startswith("❌ blank.kicad_sch now exist in ")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["blank.kicad_sch"]
        assert (tmp_path / "blank.kicad_sch").read_text(encoding="utf-8") == "other"

    def test_missing_kicad_python(self, tmp_path, monkeypatch):
        monkeypatch.setattr(project, "find_kicad_python", lambda: None)
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "blank"))
        assert "pcbnew" in result and "KICAD_PYTHON" in result
        assert not out_dir.exists()


class TestTemplateProject:
    def test_copies_directories_and_renames_project_files(self, tmp_path, template_base):
        out_dir = tmp_path / "proj"
        result = asyncio.run(
            create_kicad_project(str(out_dir), "shield", "Shield", template="Arduino_Mega")
        )

        assert sorted(p.name for p in out_dir.iterdir()) == [
            "Arduino_MountingHole.pretty",
            "custom.kicad_wks",
            "fp-lib-table",
            "shield.kicad_pcb",
            "shield.kicad_pro",
            "shield.kicad_sch",
        ]
        # The footprint library the fp-lib-table points at must come along.
        assert (out_dir / "Arduino_MountingHole.pretty" / "MountingHole_3.2mm.kicad_mod").is_file()
        assert "KiCad 10.0.6 Project Created" in result
        assert "9.0+" not in result
        assert "Arduino_Mega" in result and "- Arduino_MountingHole.pretty/" in result

    def test_root_uuid_is_replaced_consistently(self, tmp_path, template_base):
        asyncio.run(create_kicad_project(str(tmp_path), "shield", template="Arduino_Mega"))

        sch = (tmp_path / "shield.kicad_sch").read_text(encoding="utf-8")
        new_uuid = re.search(r'\(uuid "([^"]*)"\)', sch).group(1)
        assert new_uuid != ROOT_UUID and ROOT_UUID not in sch
        assert f'(path "/{new_uuid}"' in sch
        assert '(title "shield")' in sch

        pro = json.loads((tmp_path / "shield.kicad_pro").read_text(encoding="utf-8"))
        assert pro["meta"]["filename"] == "shield.kicad_pro"
        assert pro["sheets"] == [[new_uuid, "Root"]]

    @pytest.mark.parametrize("field", ["project_name", "title", "company"])
    def test_text_that_is_not_valid_changes_no_file(self, tmp_path, template_base, field):
        # A JSON string can hold a lone surrogate, which UTF-8 cannot encode.
        args = {"project_name": "shield", "title": "T", "company": "C", field: "A\ud800"}
        result = asyncio.run(create_kicad_project(str(tmp_path), template="Arduino_Mega", **args))
        assert f"The {field} is not valid text" in result, result
        assert list(tmp_path.iterdir()) == [tmp_path / "templates"]

    def test_project_file_is_utf8_text(self, tmp_path, template_base):
        # KiCad writes the project file as UTF-8 text, not with \u escapes.
        asyncio.run(create_kicad_project(str(tmp_path), "电源板_لوحة", template="Arduino_Mega"))
        pro = (tmp_path / "电源板_لوحة.kicad_pro").read_text(encoding="utf-8")
        assert '"filename": "电源板_لوحة.kicad_pro"' in pro

    def test_template_by_path(self, tmp_path, template_base, monkeypatch):
        monkeypatch.setattr(project, "_kicad_template_dirs", lambda: [])
        out_dir = tmp_path / "proj"
        asyncio.run(
            create_kicad_project(
                str(out_dir), "shield", template=str(template_base / "Arduino_Mega")
            )
        )
        assert (out_dir / "shield.kicad_pro").is_file()

    def test_unknown_template_lists_available(self, tmp_path, template_base):
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "x", template="Nope"))
        assert "'Nope' not found" in result and "Arduino_Mega" in result
        assert not out_dir.exists()

    def test_kicad_7_is_refused(self, tmp_path, template_base, monkeypatch):
        """The server supports KiCad 8 and later, also for a template project."""

        async def old_version():
            return "7.0.11"

        monkeypatch.setattr(project, "get_kicad_version", old_version)
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "Mega", template="Arduino_Mega"))
        assert result.startswith("❌ KiCad 7.0.11 is installed.") and not out_dir.exists()

    def test_a_template_schematic_that_the_parser_cannot_read(self, tmp_path, template_base):
        """No (paper ...) and no title block: an error, and no file of the template."""
        (template_base / "Arduino_Mega" / "Arduino_Mega.kicad_sch").write_text(
            '(kicad_sch (version 20250114) (uuid "x"))', encoding="utf-8"
        )
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "Mega", template="Arduino_Mega"))
        assert result.startswith("❌ The template "), result
        assert "The tool wrote no file." in result
        assert not out_dir.exists()

    def test_a_template_project_file_as_kicad_reads_it(self, tmp_path, template_base):
        """A byte order mark and comments: KiCad reads the file (read_kicad_json)."""
        pro = template_base / "Arduino_Mega" / "Arduino_Mega.kicad_pro"
        pro.write_text("\ufeff// a comment\n" + pro.read_text(encoding="utf-8"), encoding="utf-8")
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "Mega", template="Arduino_Mega"))
        assert "Project Created" in result, result
        data = json.loads((out_dir / "Mega.kicad_pro").read_text(encoding="utf-8"))
        assert data["meta"]["filename"] == "Mega.kicad_pro"

    def test_a_template_project_file_that_is_not_json(self, tmp_path, template_base):
        (template_base / "Arduino_Mega" / "Arduino_Mega.kicad_pro").write_text("{", "utf-8")
        out_dir = tmp_path / "proj"
        result = asyncio.run(create_kicad_project(str(out_dir), "Mega", template="Arduino_Mega"))
        assert result.startswith("❌ The template ") and not out_dir.exists()

    def test_refuses_to_overwrite_existing_project(self, tmp_path, template_base):
        existing = tmp_path / "shield.kicad_sch"
        existing.write_text("mine", encoding="utf-8")
        result = asyncio.run(create_kicad_project(str(tmp_path), "shield", template="Arduino_Mega"))
        assert "already exists" in result
        assert existing.read_text(encoding="utf-8") == "mine"
        assert not (tmp_path / "shield.kicad_pcb").exists()


class TestFindKicadPython:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(kicad_python, "_cached_python", None)
        monkeypatch.delenv("KICAD_PYTHON", raising=False)
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: None)

    def test_env_override_wins(self, monkeypatch, tmp_path):
        fake = tmp_path / "python.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("KICAD_PYTHON", str(fake))
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: None)
        assert find_kicad_python() == str(fake)

    def test_current_interpreter_when_pcbnew_importable(self, monkeypatch):
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: object())
        assert find_kicad_python() == sys.executable

    def test_windows_bundled_python(self, monkeypatch, tmp_path):
        exe = tmp_path / "bin" / "python.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: (tmp_path, "10.0"))
        assert find_kicad_python() == str(exe)

    def test_macos_bundled_python(self, monkeypatch, tmp_path):
        contents = tmp_path / "KiCad.app" / "Contents"
        shared_support = contents / "SharedSupport"
        shared_support.mkdir(parents=True)
        exe = (
            contents
            / "Frameworks"
            / "Python.framework"
            / "Versions"
            / "Current"
            / "bin"
            / "python3"
        )
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: (shared_support, "macos"))
        assert find_kicad_python() == str(exe)

    def test_nothing_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: None)
        assert find_kicad_python() is None


class TestRunKicadPython:
    # Text in different scripts. On Linux and macOS an argument holds only the
    # characters of the locale encoding, thus the test uses the texts that
    # the encoding can hold.
    TEXTS = ["电源板", "لوحة", "Плата", "řídicí", "𠀀"]
    CODE = (
        "from kicad_workbench.utils import kicad_python\n"
        "kicad_python.find_kicad_python = lambda: sys.executable\n"
        "script = 'import sys; print(sys.argv[1]); sys.stderr.write(sys.argv[1])'\n"
        "r = kicad_python.run_kicad_python_sync(['-c', script, ARGS[0]])\n"
        "result([r.returncode, r.stdout.decode('utf-8', 'replace'),\n"
        "        r.stderr.decode('utf-8', 'replace')])\n"
    )

    @pytest.mark.parametrize("utf8_mode", [False, True])
    def test_child_uses_utf8_in_a_locale_that_is_not_utf8(self, utf8_mode):
        # The server and the child Python run in the same locale, as on a
        # system that does not use UTF-8. The child must read its arguments
        # as the server writes them, and write its output as UTF-8.
        env, encoding = non_utf8_env()
        texts = [text for text in self.TEXTS if can_name(text, encoding, utf8_mode)]
        assert texts, encoding
        for text in texts:
            returncode, stdout, stderr = run_python(self.CODE, env, utf8_mode, text)
            assert returncode == 0, stderr
            assert stdout.strip() == text
            assert stderr == text


def _kicad_available() -> bool:
    python = find_kicad_python()
    if python is None or find_kicad_cli() is None:
        return False
    probe = subprocess.run([python, "-c", "import pcbnew"], capture_output=True, timeout=60)
    return probe.returncode == 0


@pytest.mark.skipif(not _kicad_available(), reason="KiCad with pcbnew not installed")
class TestWithRealKicad:
    @pytest.fixture(autouse=True)
    def _kicad_version(self):
        """Use the real version lookup (overrides the module-level stub)."""

    def test_empty_project_is_native_and_passes_erc(self, tmp_path):
        from kicad_workbench.utils.kicad_cli import run_kicad_cli_sync

        result = asyncio.run(create_kicad_project(str(tmp_path), "blank", company="ACME"))
        assert "Project Created" in result and "Warnings" not in result

        pcb = (tmp_path / "blank.kicad_pcb").read_text(encoding="utf-8")
        sch = (tmp_path / "blank.kicad_sch").read_text(encoding="utf-8")
        assert "(footprint " not in pcb
        assert '(company "ACME")' in pcb and '(company "ACME")' in sch
        assert (tmp_path / "blank.kicad_pro").is_file()

        # Both files carry the installed KiCad's own generator version.
        pcb_gen = re.search(r'\(generator_version "([^"]*)"\)', pcb).group(1)
        sch_gen = re.search(r'\(generator_version "([^"]*)"\)', sch).group(1)
        assert pcb_gen == sch_gen

        report = tmp_path / "erc.rpt"
        erc = run_kicad_cli_sync(
            ["sch", "erc", "-o", str(report), str(tmp_path / "blank.kicad_sch")], timeout=120
        )
        assert erc.returncode == 0
        assert "ERC messages: 0" in report.read_text(encoding="utf-8")

    def test_arduino_mega_template_includes_footprint_library(self, tmp_path):
        if "Arduino_Mega" not in project._list_kicad_templates():
            pytest.skip("Arduino_Mega template not installed")

        asyncio.run(create_kicad_project(str(tmp_path), "shield", template="Arduino_Mega"))

        fp_lib_table = (tmp_path / "fp-lib-table").read_text(encoding="utf-8")
        for lib_dir in re.findall(r"\$\{KIPRJMOD\}/([^)\"]+)", fp_lib_table):
            assert (tmp_path / lib_dir).is_dir(), lib_dir
        assert not (tmp_path / "meta").exists()
