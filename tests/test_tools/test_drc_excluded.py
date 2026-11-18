"""Tests of the excluded violations of get_drc_violations: they show in the table and
do not count, as in get_erc_violations and in the counts of errors and warnings of
run_drc. A fake kicad-cli (fake_drc) writes the report."""

import asyncio

import pytest

from kicad_workbench.tools import validation


def _violation(excluded: bool) -> dict:
    return {
        "type": "clearance",
        "description": "Clearance violation",
        "severity": "error",
        "excluded": excluded,
        "items": [],
    }


@pytest.fixture
def board(fake_drc):
    """A board, and a fake kicad-cli that writes the violations of the list."""
    violations: list[dict] = []
    fake_drc.violations = lambda args: violations
    return fake_drc.pcb, violations


def test_an_excluded_violation_does_not_count(board):
    pcb, violations = board
    violations += [_violation(False), _violation(True)]
    out = asyncio.run(validation.get_drc_violations(str(pcb)))
    assert out.startswith("# DRC Violations"), out
    assert "**Count:** 1\n**Excluded (not counted):** 1" in out, out
    assert out.count("| clearance |") == 2, out
    assert "(excluded)" in out


def test_only_excluded_violations_pass(board):
    # As run_drc: a report with only excluded violations is a pass.
    pcb, violations = board
    violations += [_violation(True), _violation(True)]
    out = asyncio.run(validation.get_drc_violations(str(pcb)))
    assert out.startswith("✅ **No Matching Violations**"), out
    assert "**Excluded (not counted):** 2" in out, out
    assert out.count("| clearance |") == 2, out
    passed = asyncio.run(validation.run_drc(str(pcb)))
    assert passed.startswith("✅"), passed


def test_a_filter_with_no_match_is_no_pass(board):
    # An active violation of an other type: no "✅" for a filter that matches nothing.
    pcb, violations = board
    violations += [_violation(False)]
    out = asyncio.run(validation.get_drc_violations(str(pcb), violation_type="clearence"))
    assert out.startswith("⚠️ **No Matching Violations**"), out
    assert "The board has 1 active violation(s) of other types." in out, out
