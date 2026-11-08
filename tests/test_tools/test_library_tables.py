"""Tests for the library tables of KiCad (utils/library_tables.py)."""

import json
import os
import re
import subprocess

import pytest

from kicad_workbench.utils import library_tables
from kicad_workbench.utils.kicad_cli import find_kicad_cli
from kicad_workbench.utils.library_tables import (
    LibraryTableError,
    active_rows,
    expand,
    find_library,
    kicad_environment,
    library_rows,
)


def table(kind: str, *rows: str) -> str:
    head = {"symbol": "sym_lib_table", "footprint": "fp_lib_table"}[kind]
    return f"({head}\n\t(version 7)\n" + "".join(f"\t{row}\n" for row in rows) + ")\n"


def lib(name: str, uri: str, kind_type: str = "KiCad", flags: str = "") -> str:
    return (
        f'(lib (name "{name}")(type "{kind_type}")(uri "{uri}")(options "")'
        f'(descr "{name} symbols"){flags})'
    )


@pytest.fixture(autouse=True)
def no_kicad_variables(monkeypatch):
    """Remove the path variables of the developer from the environment."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KISYS3DMOD|KIPRJMOD|MYLIBS|LIBS|NESTED", name):
            monkeypatch.delenv(name)


@pytest.fixture
def kicad(tmp_path, monkeypatch):
    """A KiCad 10.0 configuration and installation in tmp_path."""
    config = tmp_path / "config"
    stock = tmp_path / "stock"
    (config / "10.0").mkdir(parents=True)
    (stock / "template").mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: stock)
    # The default global table of KiCad 10: one nested table, the table of
    # the installation.
    stock_table = stock / "template" / "sym-lib-table"
    stock_table.write_text(
        table(
            "symbol",
            lib("Device", "${KICAD10_SYMBOL_DIR}/Device.kicad_sym"),
            lib("Old", "${KICAD8_SYMBOL_DIR}/Old.kicad_sym"),
        ),
        encoding="utf-8",
    )
    (config / "10.0" / "sym-lib-table").write_text(
        table("symbol", lib("KiCad", str(stock_table), "Table")), encoding="utf-8"
    )
    return {"config": config / "10.0", "stock": stock, "root": tmp_path}


def names(rows):
    return [(row.name, row.scope) for row in rows]


class TestTables:
    def test_nested_global_table(self, kicad):
        rows, problems = library_rows("symbol", "10.0.6")
        assert problems == []
        assert names(rows) == [("Device", "global"), ("Old", "global")]
        assert rows[0].path == kicad["stock"] / "symbols" / "Device.kicad_sym"
        assert rows[0].description == "Device symbols"
        # A variable of KiCad 8 gives the value of KiCad 10, as in KiCad.
        assert rows[1].path == kicad["stock"] / "symbols" / "Old.kicad_sym"

    def test_project_library_replaces_the_global_library(self, kicad):
        project = kicad["root"] / "board"
        project.mkdir()
        (project / "board.kicad_pro").write_text(
            json.dumps({"text_variables": {"LIBS": "${KIPRJMOD}/libs"}}), encoding="utf-8"
        )
        (project / "sym-lib-table").write_text(
            table("symbol", lib("Device", "${LIBS}/${PROJECTNAME}.kicad_sym")), encoding="utf-8"
        )
        rows, _ = library_rows("symbol", "10.0", project / "board.kicad_pro")
        # The sequence of KiCad: the global table, then the project table.
        assert names(rows) == [("Device", "global"), ("Old", "global"), ("Device", "project")]
        found, _ = find_library("symbol", "Device", "10.0", project / "board.kicad_pro")
        assert found.scope == "project"
        assert found.path == project / "libs" / "board.kicad_sym"
        assert names(active_rows(rows)) == [("Device", "project"), ("Old", "global")]

    def test_last_row_with_a_name_is_active(self, kicad):
        # Also a disabled or hidden row replaces an earlier row, and the place
        # of the first row stays (LIBRARY_MANAGER::Rows of KiCad 10).
        (kicad["config"] / "sym-lib-table").write_text(
            table(
                "symbol",
                lib("A", "/libs/A1.kicad_sym"),
                lib("B", "/libs/B.kicad_sym"),
                lib("A", "/libs/A2.kicad_sym", "KiCad", "(disabled)"),
            ),
            encoding="utf-8",
        )
        rows, problems = library_rows("symbol", "10.0")
        assert problems == []
        assert [(row.name, row.uri) for row in active_rows(rows)] == [
            ("A", "/libs/A2.kicad_sym"),
            ("B", "/libs/B.kicad_sym"),
        ]
        found, problems = find_library("symbol", "A", "10.0")
        assert (found.uri, found.disabled, problems) == ("/libs/A2.kicad_sym", True, [])
        assert find_library("symbol", "Missing", "10.0") == (None, [])

    def test_find_library_with_a_table_that_cannot_be_read(self, kicad):
        # KiCad uses no row of the table, and shows the problem.
        nested = kicad["root"] / "bad-table"
        nested.write_text("(sym_lib_table (lib", encoding="utf-8")
        (kicad["config"] / "sym-lib-table").write_text(
            table("symbol", lib("A", "/libs/A.kicad_sym"), lib("T", str(nested), "Table")),
            encoding="utf-8",
        )
        found, problems = find_library("symbol", "A", "10.0")
        assert found.uri == "/libs/A.kicad_sym"
        assert len(problems) == 1 and "cannot be read" in problems[0]

    @pytest.mark.parametrize("content", [b"", b"\n"])
    def test_table_of_one_byte_has_no_rows(self, kicad, content):
        (kicad["config"] / "sym-lib-table").write_bytes(content)
        assert library_rows("symbol", "10.0") == ([], [])

    def test_hidden_and_disabled_nested_tables(self, kicad):
        nested = kicad["root"] / "nested-table"
        nested.write_text(table("symbol", lib("A", "/libs/A.kicad_sym")), encoding="utf-8")
        off = kicad["root"] / "off-table"
        off.write_text(table("symbol", lib("B", "/libs/B.kicad_sym")), encoding="utf-8")
        (kicad["config"] / "sym-lib-table").write_text(
            table(
                "symbol",
                lib("Nested", str(nested), "Table", "(hidden)"),
                lib("Off", str(off), "Table", "(disabled)"),
                lib("C", "/libs/C.kicad_sym", "KiCad", "(disabled)"),
            ),
            encoding="utf-8",
        )
        rows, _ = library_rows("symbol", "10.0")
        # A disabled nested table gives no libraries. A disabled library stays
        # in the list with its flag.
        assert [(row.name, row.hidden, row.disabled) for row in rows] == [
            ("A", True, False),
            ("C", False, True),
        ]

    def test_relative_nested_table_and_relative_library(self, kicad):
        (kicad["config"] / "more").mkdir()
        (kicad["config"] / "more" / "sym-lib-table").write_text(
            table("symbol", lib("Rel", "libs/Rel.kicad_sym")), encoding="utf-8"
        )
        (kicad["config"] / "sym-lib-table").write_text(
            table("symbol", lib("More", "more/sym-lib-table", "Table")), encoding="utf-8"
        )
        (row,), problems = library_rows("symbol", "10.0")
        assert problems == []
        # KiCad makes a relative library path absolute from its working
        # directory, thus the path is not known.
        assert row.path is None and "relative path" in row.problems[0]

    def test_loop_of_nested_tables(self, kicad):
        own = kicad["config"] / "sym-lib-table"
        own.write_text(table("symbol", lib("Self", str(own), "Table")), encoding="utf-8")
        rows, problems = library_rows("symbol", "10.0")
        assert rows == [] and "already exists" in problems[0]

    def test_relative_project_path(self, kicad, monkeypatch):
        project = kicad["root"] / "board"
        project.mkdir()
        (project / "sym-lib-table").write_text(
            table("symbol", lib("P", "${KIPRJMOD}/P.kicad_sym")), encoding="utf-8"
        )
        monkeypatch.chdir(kicad["root"])
        rows, _ = library_rows("symbol", "10.0", library_tables.Path("board/board.kicad_pro"))
        assert names(rows)[-1] == ("P", "project")
        assert rows[-1].path == project / "P.kicad_sym"

    @pytest.mark.parametrize(
        "content", ["[]", '{"environment": "x"}', '{"environment": {"vars": [1]}}']
    )
    def test_settings_of_an_other_shape(self, kicad, content):
        (kicad["config"] / "kicad_common.json").write_text(content, encoding="utf-8")
        with pytest.raises(LibraryTableError, match="not a JSON object"):
            library_rows("symbol", "10.0")

    def test_variable_with_no_value(self, kicad):
        (kicad["config"] / "sym-lib-table").write_text(
            table("symbol", lib("X", "${MYLIBS}/X.kicad_sym")), encoding="utf-8"
        )
        (row,), problems = library_rows("symbol", "10.0")
        assert row.path is None and "${MYLIBS}" in row.problems[0]
        assert problems == []

    def test_stock_folder_that_is_not_known(self, kicad, monkeypatch):
        # Linux with a second installation: the variables of the stock folder
        # have no value, and the problems tell what to set.
        monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
        monkeypatch.setattr(library_tables.sys, "platform", "linux")
        rows, problems = library_rows("symbol", "10.0")
        assert all(row.path is None for row in rows)
        (problem,) = problems
        assert "does not know the stock library folder" in problem
        assert "KICAD10_SYMBOL_DIR" in problem and "On Linux the build" in problem
        assert kicad_environment("10.0").stock_dir is None

        # The variable in the environment gives the folder, as in KiCad. The alias
        # KICAD8_SYMBOL_DIR gets the value of KICAD10_SYMBOL_DIR.
        monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(kicad["stock"] / "symbols"))
        rows, problems = library_rows("symbol", "10.0")
        assert [row.path for row in rows] == [
            kicad["stock"] / "symbols" / "Device.kicad_sym",
            kicad["stock"] / "symbols" / "Old.kicad_sym",
        ]
        assert problems == []

    def test_stock_folder_of_the_installation(self, kicad):
        assert kicad_environment("10.0").stock_dir == kicad["stock"]

    @pytest.mark.parametrize(
        "uri, stock",
        [
            ("${KICAD10_SYMBOL_DIR}/a.kicad_sym", True),
            ("$(KICAD10_DESIGN_BLOCK_DIR)/a.kicad_blocks", True),
            ("${KICAD8_FOOTPRINT_DIR}/a.pretty", True),  # an alias of KICAD10_FOOTPRINT_DIR
            ("${KICAD_SYMBOL_DIR}/a.kicad_sym", True),  # an alias of KICAD10_SYMBOL_DIR
            ("${KISYS3DMOD}/a.step", True),
            ("${KICAD_FOOTPRINT_DIR}/a.pretty", False),  # not a variable of KiCad
            ("${KICAD9_TEMPLATE_DIR}/a", False),  # KiCad has no alias for it
            ("${KICAD10_3RD_PARTY}/a.kicad_sym", False),
            ("${MYLIBS}/a.kicad_sym", False),
        ],
    )
    def test_variables_of_the_stock_folder(self, kicad, uri, stock):
        env = kicad_environment("10.0")
        assert library_tables._uses_stock_variable(uri, env) is stock

    def test_stock_folder_that_is_not_known_on_windows(self, kicad, monkeypatch, tmp_path):
        from kicad_workbench.utils import kicad_cli

        monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
        # The test sets the platform of the full process: shutil.which of Windows
        # cannot run on Linux, thus kicad-cli is a path of the test.
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: "C:/KiCad/bin/kicad-cli.exe")
        monkeypatch.setattr(library_tables.sys, "platform", "win32")
        env = kicad_environment("10.0")
        assert "set KICAD_CLI to kicad-cli.exe" in library_tables._unknown_stock_problem(env)
        monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(tmp_path / "missing"))
        problem = library_tables._unknown_stock_problem(env)
        assert f"KICAD_STOCK_DATA_HOME does not exist: {tmp_path / 'missing'}" in problem

    def test_stock_folder_that_is_not_known_on_macos(self, kicad, monkeypatch):
        monkeypatch.setattr(library_tables, "_stock_dir", lambda: None)
        monkeypatch.setattr(library_tables.sys, "platform", "darwin")
        problem = library_tables._unknown_stock_problem(kicad_environment("10.0"))
        assert "KiCad.app/Contents/MacOS" in problem

    @pytest.mark.parametrize(
        "text",
        ["(sym_lib_table (lib (name x)", "(fp_lib_table (version 7))", "not a table"],
    )
    def test_table_that_cannot_be_read(self, kicad, text):
        (kicad["config"] / "sym-lib-table").write_text(text, encoding="utf-8")
        rows, problems = library_rows("symbol", "10.0")
        assert rows == []
        assert "cannot be read" in problems[0] or "is a 'fp_lib_table'" in problems[0]

    def test_no_global_table(self, kicad):
        (kicad["config"] / "sym-lib-table").unlink()
        rows, problems = library_rows("symbol", "10.0")
        assert rows == [] and "does not exist" in problems[0]

    def test_version_with_no_minor_number(self):
        with pytest.raises(LibraryTableError):
            library_rows("symbol", "10")


class TestVariables:
    def test_precedence(self, kicad, monkeypatch):
        # The environment, then kicad_common.json, then the installation.
        (kicad["config"] / "kicad_common.json").write_text(
            json.dumps(
                {
                    "environment": {
                        "vars": {"KICAD10_SYMBOL_DIR": "/json/symbols", "MYLIBS": "/json/mine"}
                    }
                }
            ),
            encoding="utf-8",
        )
        env = kicad_environment("10.0.6")
        assert env.variables["KICAD10_SYMBOL_DIR"] == "/json/symbols"
        assert env.variables["MYLIBS"] == "/json/mine"
        assert env.variables["KICAD10_FOOTPRINT_DIR"] == str(kicad["stock"] / "footprints")
        assert env.variables["KICAD10_3RD_PARTY"] == str(
            kicad["root"] / "documents" / library_tables_folder() / "10.0" / "3rdparty"
        )
        monkeypatch.setenv("KICAD10_SYMBOL_DIR", "/env/symbols")
        assert kicad_environment("10.0").variables["KICAD10_SYMBOL_DIR"] == "/env/symbols"
        # An empty environment variable does not replace a KiCad variable.
        monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", "")
        env = kicad_environment("10.0")
        assert env.variables["KICAD10_FOOTPRINT_DIR"] == str(kicad["stock"] / "footprints")
        # An other defined variable gives its value, also when it is empty.
        monkeypatch.setenv("MYLIBS", "")
        assert expand("${MYLIBS}/a", env) == "/json/mine/a"
        monkeypatch.setenv("LIBS", "")
        assert expand("x${LIBS}/a", env) == "x/a"

    def test_settings_with_comments(self, kicad):
        """KiCad reads kicad_common.json with comments and a byte order mark
        (nlohmann::json with ignore_comments, JSON_SETTINGS::LoadFromFile)."""
        common = kicad["root"] / "config" / "10.0" / "kicad_common.json"
        common.write_bytes(
            '\ufeff{"environment": {"vars": {"MYLIBS": "/c"}} /* a */ } // b\n'.encode()
        )
        assert kicad_environment("10.0.6").variables["MYLIBS"] == "/c"

    def test_no_appdata_on_windows(self, monkeypatch):
        monkeypatch.setattr(library_tables.sys, "platform", "win32")
        monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        with pytest.raises(LibraryTableError, match="APPDATA"):
            kicad_environment("10.0")

    @pytest.mark.parametrize(
        ("text", "expanded"),
        [
            ("$(KICAD10_SYMBOL_DIR)/a", "/s/a"),
            ("${KICAD10_SYMBOL_DIR}/a", "/s/a"),
            ("$KICAD10_SYMBOL_DIR/a", "/s/a"),
            ("${KISYS3DMOD}/m.step", "/3d/m.step"),
            ("${KICAD_SYMBOL_DIR}/a", "/s/a"),
            # An unknown variable and an escaped "$" stay.
            ("${NOPE}/a", "${NOPE}/a"),
            ("\\$KICAD10_SYMBOL_DIR", "$KICAD10_SYMBOL_DIR"),
            # KiCad expands a result with brackets again, thus "\${...}" is
            # expanded in the second pass.
            ("\\${KICAD10_SYMBOL_DIR}", "/s"),
            # A value with a variable is expanded again.
            ("${NESTED}/a", "/s/sub/a"),
            # KiCad also expands a variable with no closing bracket.
            ("${KICAD10_SYMBOL_DIR/a", "/s/a"),
        ],
    )
    def test_expand(self, text, expanded):
        env = library_tables.KicadEnvironment(
            10,
            0,
            library_tables.Path("/config"),
            {
                "KICAD10_SYMBOL_DIR": "/s",
                "KICAD10_3DMODEL_DIR": "/3d",
                "NESTED": "${KICAD10_SYMBOL_DIR}/sub",
            },
        )
        assert expand(text, env) == expanded

    def test_windows_percent_form(self, monkeypatch):
        monkeypatch.setattr(library_tables.sys, "platform", "win32")
        env = library_tables.KicadEnvironment(10, 0, library_tables.Path("/c"), {"LIBS": "D:/libs"})
        assert expand("%LIBS%/a", env) == "D:/libs/a"
        assert expand("100\\%", env) == "100%"


def library_tables_folder() -> str:
    return "KiCad" if library_tables.sys.platform in ("win32", "darwin") else "kicad"


def _installed_version() -> str | None:
    cli = find_kicad_cli()
    if cli is None:
        return None
    try:
        out = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


INSTALLED_VERSION = _installed_version()


@pytest.mark.skipif(INSTALLED_VERSION is None, reason="kicad-cli is not installed")
def test_libraries_of_the_installation_exist():
    # The libraries of the table of the installation (share/kicad/template),
    # through the global table of the user. The own libraries of the user do
    # not count.
    share = library_tables.kicad_stock_dir()
    for kind in ("symbol", "footprint"):
        rows, problems = library_rows(kind, INSTALLED_VERSION)
        if any("does not exist" in problem for problem in problems):
            pytest.skip("KiCad has no global library table yet (first start)")
        stock = [row for row in rows if share and row.table.is_relative_to(share)]
        missing = [row.name for row in stock if row.path is None or not row.path.exists()]
        assert stock and not missing, (kind, missing[:5], problems)
