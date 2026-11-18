"""Fixtures of the tests of the tools.

long64 and long32 set the size of a C long, thus a test gives the same result on
each system: a C long has 64 bits on Linux and macOS and 32 bits on Windows
(parsers/c_numbers.py).

Before each tool, the server compares the schematic files, the board and the
libraries of the project with the version that the installed KiCad writes
(utils/file_versions.py): an older file
gives a warning, or an error for a tool that writes. The tests of a tool test its rules
on the fixtures as they are, files of KiCad 8, 9 and 10: thus each test gets no check,
except a test with the marker file_versions (the tests of the check;
spike/kicad-versions/schematic_versions_check.py compares it with KiCad).
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kicad_workbench.parsers import c_numbers
from kicad_workbench.parsers.sub_symbols import sub_symbol_unit
from kicad_workbench.utils import file_versions, kicad_cli

from .kicad_stub import TEST_KICAD


@pytest.fixture(autouse=True)
def _no_file_version_check(request, monkeypatch):
    if request.node.get_closest_marker("file_versions") is None:
        for check in ("check_schematic_versions", "check_board_file_version"):
            monkeypatch.setattr(
                file_versions, check, lambda path, write: file_versions.FileVersions("", "")
            )
        monkeypatch.setattr(file_versions, "library_versions", lambda kind, libraries, kicad: [])
        monkeypatch.setattr(
            file_versions,
            "check_library_versions",
            lambda kind, libraries, kicad: file_versions.LibraryCheck([], [], set()),
        )


@pytest.fixture
def kicad_version():
    """The version of the installed KiCad for a test that runs no kicad-cli
    (kicad_stub.TEST_KICAD as the version of the call, see kicad_stub)."""
    token = kicad_cli.set_call_version(TEST_KICAD)
    yield TEST_KICAD
    kicad_cli.reset_call_version(token)


def _long(monkeypatch, bits: int):
    monkeypatch.setattr(c_numbers, "LONG_MIN", -(2 ** (bits - 1)))
    monkeypatch.setattr(c_numbers, "LONG_MAX", 2 ** (bits - 1) - 1)
    # The cache of sub_symbol_unit holds the results of the long of the system.
    sub_symbol_unit.cache_clear()
    yield
    sub_symbol_unit.cache_clear()


@pytest.fixture
def long64(monkeypatch):
    """The long of Linux and macOS (LP64)."""
    yield from _long(monkeypatch, 64)


@pytest.fixture
def long32(monkeypatch):
    """The long of Windows (LLP64, the MSVC build of KiCad)."""
    yield from _long(monkeypatch, 32)


@dataclass
class FakeDrc:
    """A board and a fake kicad-cli for the DRC tools (tools/validation.py)."""

    pcb: Path
    calls: list[list[str]] = field(default_factory=list)
    # The violations of the report for the arguments of kicad-cli.
    violations: Callable[[list[str]], list[dict]] = lambda args: []


@pytest.fixture
def fake_drc(tmp_path, monkeypatch):
    """A board with a zone, a fake kicad-cli that writes the report of
    FakeDrc.violations (and keeps its calls), KiCad 10.0.6, and the reports in tmp_path."""
    from kicad_workbench.tools import validation

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20260206) (generator "pcbnew") (zone (net 0) (layer "F.Cu")))\n',
        encoding="utf-8",
    )
    fake = FakeDrc(pcb)

    async def kicad_cli(args, *rest, **kwargs):
        fake.calls.append(args)
        output = Path(args[args.index("--output") + 1])
        report = {
            "violations": fake.violations(args),
            "unconnected_items": [],
            "schematic_parity": [],
        }
        output.write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    async def version():
        return "10.0.6"

    monkeypatch.setattr(validation, "run_kicad_cli", kicad_cli)
    monkeypatch.setattr(validation, "get_kicad_version", version)
    monkeypatch.setattr(validation, "_temp_dir", lambda: tmp_path)
    return fake
