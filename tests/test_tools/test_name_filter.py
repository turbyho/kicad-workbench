"""Tests of the name filters of the agent (utils/name_filter.py) and of the tools that
use them: get_netlist_nets, analyze_hierarchical_nets and search_symbols."""

import asyncio
import re
import subprocess

import pytest

from kicad_workbench.errors import InputError, WorkbenchError
from kicad_workbench.tools import hierarchical_analysis, netlist, schematic
from kicad_workbench.utils import name_filter
from kicad_workbench.utils.name_filter import compile_filter, select

BAD = [
    ("VCC(", "missing ), unterminated subpattern at position 3"),
    ("a{4294967296}", "the repetition number is too large"),
    ("(" * 2000 + ")" * 2000, "maximum recursion depth exceeded"),
]


@pytest.mark.parametrize("pattern, reason", BAD)
def test_a_pattern_that_is_not_a_regular_expression(pattern, reason):
    with pytest.raises(InputError, match=re.escape(reason)) as error:
        compile_filter(pattern)
    # A long pattern is cut in the message.
    assert len(str(error.value)) < 300


def test_an_empty_pattern_is_no_filter():
    assert compile_filter("") is None
    assert asyncio.run(select(None, ["a", "b"], lambda item: [item])) == ["a", "b"]


def test_the_syntax_of_re_and_no_difference_of_case():
    flt = compile_filter(r"^/power/v\w+")
    found = asyncio.run(select(flt, ["/Power/VIN", "/SDA"], lambda item: [item]))
    assert found == ["/Power/VIN"]


def test_a_search_that_takes_too_long(monkeypatch):
    # Catastrophic backtracking: the time limit kills the search process.
    monkeypatch.setattr(name_filter, "SEARCH_TIME", 2.0)
    # The control: a simple pattern ends well within the limit (with the start).
    simple = compile_filter("a")
    assert asyncio.run(select(simple, ["a" * 40], lambda item: [item])) == ["a" * 40]
    flt = compile_filter(r"(a*)*b")
    with pytest.raises(InputError, match=r"takes longer than 2 s \(with the start"):
        asyncio.run(select(flt, ["a" * 40], lambda item: [item]))


def test_a_nested_repeat_compiles_at_once():
    # The module regex expands the repeats at the compile (gigabytes); re does not.
    flt = compile_filter(r"(?:(?:(?:a{1000}){1000}){1000})")
    assert asyncio.run(select(flt, ["a", "b"], lambda item: [item])) == []


@pytest.mark.parametrize(
    "call",
    [
        lambda path: netlist.get_netlist_nets(str(path), filter_pattern="VCC("),
        lambda path: hierarchical_analysis.analyze_hierarchical_nets(
            str(path), filter_pattern="VCC("
        ),
    ],
)
def test_the_tools_refuse_the_pattern_first(tmp_path, call):
    # The error comes before the tool reads a file: the file is not there.
    result = asyncio.run(call(tmp_path / "missing.kicad_sch"))
    assert result.startswith(
        "❌ The filter pattern 'VCC(' is not a regular expression: missing )"
    ), result
    assert "Traceback" not in result


def test_search_symbols_names_its_argument(tmp_path):
    sch = tmp_path / "a.kicad_sch"
    sch.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    result = asyncio.run(schematic.search_symbols(str(sch), "("))
    assert result.startswith("❌ The pattern '(' is not a regular expression"), result
    assert "Traceback" not in result


def test_the_docstrings_give_the_time_limit():
    limit = f"takes longer than {name_filter.SEARCH_TIME:.0f} s"
    for tool in (
        netlist.get_netlist_nets,
        hierarchical_analysis.analyze_hierarchical_nets,
        schematic.search_symbols,
    ):
        assert limit in " ".join(tool.__doc__.split()), tool.__name__


def _ended(monkeypatch, returncode: int, stderr: bytes = b""):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, b"", stderr)

    monkeypatch.setattr(name_filter.subprocess, "run", run)


def test_the_system_stops_the_search_process(monkeypatch):
    # For example the kill of the OOM killer: an error of the system, with no traceback.
    for code in (-9, 0xC0000017):  # a signal of Linux, an exit code of Windows
        _ended(monkeypatch, code)
        with pytest.raises(WorkbenchError, match=rf"stopped the search .*\(exit code {code}\)"):
            asyncio.run(select(compile_filter("a"), ["a"], lambda item: [item]))


def test_an_error_of_the_search_process_is_a_defect(monkeypatch):
    _ended(monkeypatch, 1, b"Traceback ... TypeError")
    with pytest.raises(RuntimeError, match="exit code 1: Traceback ... TypeError"):
        asyncio.run(select(compile_filter("a"), ["a"], lambda item: [item]))


def test_no_path_of_the_python(monkeypatch):
    monkeypatch.setattr(name_filter.sys, "executable", "")
    with pytest.raises(WorkbenchError, match=r"gives no path of its program \(sys.executable\)"):
        asyncio.run(select(compile_filter("a"), ["a"], lambda item: [item]))
