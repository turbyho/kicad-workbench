"""Tests of the error answers of the tools (CLAUDE.md, "Honest output").

An error starts with "❌". An expected error gives the message, an other
exception (a defect of the tool) also the traceback. An error of kicad-cli
gives its exit code and its output, not a guess.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from kicad_workbench.errors import InputError, WorkbenchError
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
        InputError("an argument"),
        KicadFileError("a file version"),
        AmbiguousRootError(Path("s.kicad_sch"), [Path("a.kicad_sch"), Path("b.kicad_sch")]),
        netlist.NetlistError("no netlist"),
    ],
)
def test_an_expected_error_gives_the_message(error):
    assert _answer(error) == f"❌ {error}"


@pytest.mark.parametrize(
    "error, text",
    [
        (KeyError("x"), "'x'"),
        # A plain ValueError is a defect, for example int() of a text in the code.
        (ValueError("invalid literal for int() with base 10: 'A1'"), None),
    ],
)
def test_an_other_exception_gives_the_traceback(error, text):
    answer = _answer(error)
    text = text or str(error)
    assert answer.startswith(f"❌ Error doing it: {text}\n\nTraceback (most recent call last):")
    assert f"{type(error).__name__}: {text}" in answer


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
            KicadCliError("kicad-cli stopped with the exit code 3: Failed to load schematic"),
            KicadCliError("kicad-cli did not stop in 120 s."),
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
        error = KicadCliError("kicad-cli stopped with the exit code 1: cannot load")
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

    monkeypatch.setattr(
        netlist, "export_with_kicad_cli", lambda *args: CliExport(path.read_bytes(), [])
    )
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


def _error_classes():
    """The exception classes of the package, less the errors of the version check."""
    import importlib
    import inspect
    import pkgutil

    import kicad_workbench

    found = []
    for info in pkgutil.walk_packages(kicad_workbench.__path__, "kicad_workbench."):
        if info.name in ("kicad_workbench.__main__", "kicad_workbench.version_check"):
            continue
        module = importlib.import_module(info.name)
        for _name, value in inspect.getmembers(module, inspect.isclass):
            if issubclass(value, Exception) and value.__module__ == info.name:
                found.append(value)
    return found


def test_each_error_of_the_modules_is_a_workbench_error():
    # A new error class of a module must be a WorkbenchError: else the tools
    # give it as a defect (with the traceback).
    classes = _error_classes()
    assert len(classes) >= 15
    # _RefusedError of tools/project.py holds the full answer of the tool, and the
    # tool catches it itself.
    others = [c.__qualname__ for c in classes if not issubclass(c, WorkbenchError)]
    assert others == ["_RefusedError"]


# The raises of a built-in exception that are no message to the user: the checks of
# the code. A plain ValueError gives the traceback (a defect). A message to the user
# raises an error of errors.py (InputError) or an other WorkbenchError.
_CHECKS_OF_THE_CODE = {
    ("parsers/annotation.py", "placed_symbols_after_load", "ValueError"),
    ("parsers/annotation.py", "check_annotation", "ValueError"),
    ("parsers/sexpr.py", "edited", "ValueError"),
}


def _builtin_raises():
    """The raises of a built-in exception, with the name of the function (or
    "<module>"): "raise X(...)" and "raise X". The test does not see a ValueError
    of the standard library, for example of int()."""
    import ast
    import builtins

    import kicad_workbench

    root = Path(kicad_workbench.__file__).parent
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                owner[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = exc.id if isinstance(exc, ast.Name) else None
            kind = getattr(builtins, name, None) if name else None
            if not isinstance(kind, type) or issubclass(kind, OSError):
                continue
            scope = owner.get(node)
            while scope is not None and not isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                scope = owner.get(scope)
            function = scope.name if scope is not None else "<module>"
            found.add((path.relative_to(root).as_posix(), function, name))
    return found


def test_a_message_to_the_user_is_no_built_in_exception():
    # An OSError (also FileNotFoundError) is an expected error. Each other raise of a
    # built-in exception must be a check of the code.
    assert _builtin_raises() == _CHECKS_OF_THE_CODE


def test_numbers_of_more_than_4300_digits():
    # int() refuses a text of more than 4300 digits: the readers give the value of
    # KiCad (LONG_MAX, out of range, or the order of the numbers) with no int().
    from kicad_workbench.parsers.annotation import split_reference
    from kicad_workbench.parsers.bus_names import _to_long
    from kicad_workbench.parsers.sheet_list import to_long
    from kicad_workbench.parsers.symbol_info import str_num_cmp

    many = "9" * 5000
    assert _to_long(many) == 2**63 - 1 and _to_long("0" * 5000 + "7") == 7
    assert to_long(many) is None and to_long("0" * 5000 + "7") == 7
    assert split_reference("R" + many).number == split_reference("R" + "9" * 20).number
    assert split_reference("R" + "0" * 5000 + "7").number == 7
    assert str_num_cmp("a" + many, "a" + "1" * 5000) == 1
    assert str_num_cmp("a" + "0" * 5000 + "2", "a10") == -1


@pytest.mark.parametrize("pos", [{"x": "abc", "y": 0}, {"x": 1.0, "y": None}, {"x": True, "y": 0}])
def test_a_report_coordinate_that_is_not_a_number(pos):
    from kicad_workbench.parsers.check_report_parser import (
        parse_drc_item,
        parse_drc_report,
        parse_erc_item,
    )

    item = {"uuid": "u", "description": "x", "pos": pos}
    for parse in (parse_erc_item, parse_drc_item):
        with pytest.raises(InputError, match="not a number"):
            parse(item)
    # The check of a report gives the place.
    report = {"violations": [{"type": "t", "description": "d", "severity": "error",
                              "items": [item]}], "unconnected_items": [], "schematic_parity": []}  # fmt: skip
    with pytest.raises(InputError, match="of 'pos' of item 1 of violation 1 of 'violations'"):
        parse_drc_report(report)
