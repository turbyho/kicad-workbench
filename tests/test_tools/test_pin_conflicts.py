"""Tests of detect_pin_conflicts: the pin items of the ERC report of kicad-cli.

The tests give the tool an ERC result (validation._erc_result): they need no KiCad.
The ERC of kicad-cli itself is in test_check_reports.py (TestKicadCliIntegration) and in
spike/kicad-versions/pin_conflicts_check.py.
"""

import asyncio

from kicad_workbench.models.types import ERCError
from kicad_workbench.tools import pin_analysis, validation

SCH = '(kicad_sch (version 20260306) (generator "eeschema"))\n'


def _error(kind: str, severity: str = "error", excluded: bool = False) -> ERCError:
    return ERCError(
        severity=severity,
        type=kind,
        description=f"text of {kind}",
        components=["U1"],
        excluded=excluded,
    )


def _answer(tmp_path, monkeypatch, errors, annotated=True, severity="", kicad="10.0.6") -> str:
    sch = tmp_path / "p.kicad_sch"
    sch.write_text(SCH, encoding="utf-8")

    async def version():
        return kicad

    async def root(path):
        return path, ""

    async def result(report, root):
        return validation._ErcResult(errors, [], True, not annotated)

    monkeypatch.setattr(validation, "_erc_root", root)
    monkeypatch.setattr(validation, "_report_is_stale", lambda *args: False)
    monkeypatch.setattr(validation, "_erc_result", result)
    monkeypatch.setattr(pin_analysis, "get_kicad_version", version)
    return asyncio.run(pin_analysis.detect_pin_conflicts(str(sch), severity))


def test_only_the_pin_items(tmp_path, monkeypatch):
    errors = [
        _error("pin_to_pin"),
        _error("pin_to_pin", "warning"),
        _error("pin_not_driven"),
        _error("power_pin_not_driven"),
        _error("pin_not_connected", "warning"),
        _error("duplicate_pins"),
        _error("label_dangling"),
        _error("unannotated"),
    ]
    out = _answer(tmp_path, monkeypatch, errors)
    assert out.startswith("# ERC Violations"), out
    assert "**Count:** 6" in out
    for kind in ("pin_to_pin", "pin_not_driven", "power_pin_not_driven", "duplicate_pins"):
        assert f"| {kind} |" in out
    assert "label_dangling" not in out and "| unannotated |" not in out


def test_other_violations_are_no_pin_conflict(tmp_path, monkeypatch):
    out = _answer(tmp_path, monkeypatch, [_error("label_dangling")])
    assert out.startswith("✅ **No Matching Violations**"), out
    assert "type = pin_to_pin, pin_not_driven" in out


def test_an_excluded_pin_item_does_not_count(tmp_path, monkeypatch):
    out = _answer(tmp_path, monkeypatch, [_error("pin_to_pin", excluded=True)])
    assert out.startswith("✅ **No Matching Violations**"), out
    assert "**Excluded (not counted):** 1" in out
    assert "| ❌ error (excluded) | pin_to_pin |" in out
    out = _answer(
        tmp_path, monkeypatch, [_error("pin_to_pin", excluded=True), _error("pin_not_driven")]
    )
    assert "**Count:** 1\n**Excluded (not counted):** 1" in out, out


def test_an_invalid_severity(tmp_path, monkeypatch):
    out = _answer(tmp_path, monkeypatch, [_error("pin_to_pin")], severity="Error")
    assert out.startswith("❌ Invalid severity 'Error'"), out


def test_an_erc_that_kicad_cli_cannot_run(tmp_path, monkeypatch):
    # No report: the answer is the answer of the ERC run.
    sch = tmp_path / "p.kicad_sch"
    sch.write_text(SCH, encoding="utf-8")

    async def root(path):
        return path, ""

    async def run(path, results, target=None):
        return "❌ kicad-cli stopped with the exit code 1: cannot load"

    monkeypatch.setattr(validation, "_erc_root", root)
    monkeypatch.setattr(validation, "_report_is_stale", lambda *args: True)
    monkeypatch.setattr(validation, "_run_erc", run)
    monkeypatch.setattr(validation, "_report_path", lambda *args: tmp_path / "none.json")

    async def version():
        return "10.0.6"

    monkeypatch.setattr(pin_analysis, "get_kicad_version", version)
    out = asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))
    assert out == "❌ kicad-cli stopped with the exit code 1: cannot load"


def test_a_warning_for_kicad_9(tmp_path, monkeypatch):
    out = _answer(tmp_path, monkeypatch, [_error("pin_to_pin")], kicad="9.0.9")
    assert out.endswith("an item of an other type is not in the answer."), out


def test_no_pass_when_not_fully_annotated(tmp_path, monkeypatch):
    out = _answer(tmp_path, monkeypatch, [_error("label_dangling")], annotated=False)
    assert out.startswith("⚠️ **No Matching Violations**"), out


def test_the_severity_filter(tmp_path, monkeypatch):
    errors = [_error("pin_to_pin"), _error("pin_to_pin", "warning")]
    out = _answer(tmp_path, monkeypatch, errors, severity="warning")
    assert "**Count:** 1" in out and "| ⚠️ warning | pin_to_pin |" in out, out
