"""Tests of the error answers of the tools (CLAUDE.md, "Honest output").

An error starts with "❌". An expected error gives the message, an other
exception (a defect of the tool) also the traceback. An error of kicad-cli
gives its exit code and its output, not a guess.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.parsers.kicad_versions import KicadFileError
from kicad_workbench.parsers.netlist_parser import NetlistParser
from kicad_workbench.tools import (
    hierarchical_analysis,
    netlist,
    pin_analysis,
    schematic,
    schematic_editor,
    schematic_search,
)
from kicad_workbench.utils.kicad_cli import CliExport, KicadCliError
from kicad_workbench.utils.sch_hierarchy import AmbiguousRootError
from kicad_workbench.utils.tool_errors import error_answer

SCH = '(kicad_sch (version 20250114) (generator "eeschema"))\n'


def _answer(error: Exception) -> str:
    try:
        raise error
    except Exception as e:
        return error_answer(e, "Error doing it")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("File not found: a.kicad_sch"),
        PermissionError("[Errno 13] Permission denied: 'a.kicad_sch'"),
        KicadCliError("kicad-cli did not stop in 60 s."),
        subprocess.TimeoutExpired(["kicad-cli", "pcb", "render"], 180),
        ValueError("a value"),
        KicadFileError("a file version"),
        AmbiguousRootError(Path("s.kicad_sch"), [Path("a.kicad_sch"), Path("b.kicad_sch")]),
        netlist.NetlistError("no netlist"),
    ],
)
def test_an_expected_error_gives_the_message(error):
    assert _answer(error) == f"❌ {error}"


def test_an_other_exception_gives_the_traceback():
    answer = _answer(KeyError("x"))
    assert answer.startswith("❌ Error doing it: 'x'\n\nTraceback (most recent call last):")
    assert "KeyError: 'x'" in answer


class FakeExport:
    """export_with_kicad_cli of the netlist tools: the data of a netlist, or an exception."""

    def __init__(self, data=b"", error=None):
        self.data, self.error, self.calls = data, error, []

    def __call__(self, command, source, file_name, timeout):
        self.calls.append((command, source, timeout))
        if self.error is not None:
            raise self.error
        return CliExport(self.data, [])


NETLIST = b'<export version="E"><components/><nets/></export>'


@pytest.fixture
def sch(tmp_path):
    # A name with no netlist next to it or in the temporary directory.
    path = tmp_path / f"errors_{tmp_path.name}.kicad_sch"
    path.write_text(SCH, encoding="utf-8")
    yield path
    netlist.netlist_cache_path(path).unlink(missing_ok=True)


class TestWriteNetlist:
    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("kicad-cli stopped with the exit code 3: Failed to load schematic"),
            RuntimeError("kicad-cli did not stop in 120 s."),
            FileNotFoundError("kicad-cli not found"),
        ],
    )
    def test_kicad_cli_that_writes_no_netlist(self, monkeypatch, sch, error):
        monkeypatch.setattr(netlist, "export_with_kicad_cli", FakeExport(error=error))
        with pytest.raises(netlist.NetlistError) as e:
            asyncio.run(netlist.write_netlist(sch))
        assert str(e.value) == f"The server cannot make the netlist of {sch}: {error}"

    def test_the_netlist(self, monkeypatch, sch):
        # kicad-cli writes into its own folder (export_with_kicad_cli): the cache file
        # gets the data in one step, also when it has an older netlist.
        netlist.netlist_cache_path(sch).write_bytes(b"<export/>")
        export = FakeExport(NETLIST)
        monkeypatch.setattr(netlist, "export_with_kicad_cli", export)
        path, root = asyncio.run(netlist.write_netlist(sch))
        assert path == netlist.netlist_cache_path(sch) and root is None
        assert path.read_bytes() == NETLIST
        assert export.calls == [
            (["sch", "export", "netlist", "--format", "kicadxml"], sch, netlist.NETLIST_TIMEOUT)
        ]


def test_a_netlist_that_is_not_xml(tmp_path):
    path = tmp_path / "bad.xml"
    path.write_text("not XML", encoding="utf-8")
    with pytest.raises(ValueError, match=f"The netlist {path} is not valid XML: syntax error"):
        NetlistParser(str(path)).get_nets()
    result = asyncio.run(netlist.get_netlist_nets(str(path)))
    assert result.startswith(f"❌ The netlist {path} is not valid XML: syntax error"), result


class TestNetlistErrorsOfTheTools:
    """A netlist that kicad-cli does not write is an error with its reason."""

    @pytest.fixture(autouse=True)
    def failing_cli(self, monkeypatch):
        error = RuntimeError("kicad-cli stopped with the exit code 1: cannot load")
        monkeypatch.setattr(netlist, "export_with_kicad_cli", FakeExport(error=error))

    def test_generate_netlist(self, sch):
        result = asyncio.run(netlist.generate_netlist(str(sch)))
        assert result == (
            f"❌ The server cannot make the netlist of {sch}: kicad-cli stopped with the exit code 1: cannot load"
        )

    @pytest.mark.parametrize(
        "tool", ["analyze_pin_functions", "detect_pin_conflicts", "extract_pinmux_config"]
    )
    def test_pin_tools(self, sch, tool):
        result = asyncio.run(getattr(pin_analysis, tool)(str(sch)))
        assert result.startswith(
            f"❌ The server cannot make the netlist of {sch}: kicad-cli stopped"
        )
        assert "Traceback" not in result

    def test_search_tells_that_it_did_not_use_the_nets(self, sch):
        result = asyncio.run(schematic_search.search_components_by_type(str(sch), "OLED"))
        assert result.startswith("# No OLED components found"), result
        assert (
            "⚠️ The search did not use the nets of the components: The server cannot make "
            f"the netlist of {sch}: kicad-cli stopped with the exit code 1: cannot load"
        ) in result


def test_hierarchical_nets_with_a_filter_that_is_not_a_regular_expression(monkeypatch, sch):
    # The tool ignored such a filter and gave all nets.
    path = sch.with_suffix(".xml")
    path.write_text('<export version="E"><components/><nets/></export>', encoding="utf-8")

    async def fake_ensure_netlist(_sch_path):
        return path

    monkeypatch.setattr(pin_analysis, "_ensure_netlist", fake_ensure_netlist)
    result = asyncio.run(hierarchical_analysis.analyze_hierarchical_nets(str(sch), "(GND"))
    assert result.startswith("❌ The filter pattern '(GND' is not a regular expression:"), result


def test_trace_of_a_reference_that_is_not_there(sch):
    result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(str(sch), "U9"))
    assert result.startswith("❌ Component 'U9' not found in schematic hierarchy."), result


@pytest.mark.parametrize(
    "tool", ["list_schematic_components", "list_schematic_nets", "get_schematic_info"]
)
def test_schematic_tools_with_a_file_that_is_not_there(tmp_path, tool):
    path = tmp_path / "none.kicad_sch"
    result = asyncio.run(getattr(schematic, tool)(str(path)))
    assert result == f"❌ File not found: {path}", result


def test_symbol_details_of_a_reference_that_is_not_there(sch):
    result = asyncio.run(schematic.get_symbol_details(str(sch), "U9"))
    assert result.startswith("❌ Component 'U9' not found in schematic."), result


def test_editor_with_a_file_that_is_not_there(tmp_path):
    path = tmp_path / "none.kicad_sch"
    result = asyncio.run(schematic_editor.add_wire(str(path), [(0, 0), (10, 0)]))
    assert result == f"❌ The file {path} does not exist."
