"""The KiCad of the tests that need no KiCad (CI has no KiCad).

The tools need the version of the installed KiCad (its rules), also when a test gives
them the output of kicad-cli. These tests give the version of KiCad 10.0.6 as the
version of the call (kicad_cli.set_call_version), no check of the file versions
(utils/file_versions.py) and no save of the libraries in kicad-cli
(utils/library_saved.py): the fixtures of tests/test_tools/conftest.py, and
child_preamble for a test that runs a tool in a child process.
"""

import inspect
import textwrap

TEST_KICAD = "10.0.6"


def saved_symbols_stub(chain, kicad):
    """library_saved._saved_symbols with no kicad-cli, for KiCad 10.0: the symbols of the
    chain in the format of the save (KICAD10.symbol_library). The stub cannot convert a
    text: a symbol that a conversion of the load changes (symbol_library_problem) needs
    the save in kicad-cli (the marker kicad_save), and the stub refuses it. The stub does
    not do the other changes of the load (the blanks and the names of the fields): most
    fixtures are not files that KiCad saved.
    """
    from dataclasses import replace

    from kicad_workbench.parsers.kicad_versions import KICAD10, symbol_library_problem

    written = KICAD10.symbol_library
    for symbol in chain:
        problem = symbol_library_problem(symbol.node, symbol.version, written)
        if problem is not None:
            raise AssertionError(
                f"The stub of the save cannot convert the symbol {symbol.name!r}: {problem} "
                "Give the test the marker kicad_save."
            )
    return {symbol.name: replace(symbol, version=written) for symbol in chain}


def child_preamble(version: str = repr(TEST_KICAD)) -> str:
    """The start of the script of a child process: the same as the fixtures of
    conftest.py.

    Args:
        version: A Python expression for the version of the call (after the import of
            kicad_cli), for example the version of the installed KiCad for a test
            that needs its libraries.
    """
    return f"""
import sys
from kicad_workbench.utils import file_versions, kicad_cli
kicad_cli.set_call_version({version})
for _check in ("check_schematic_versions", "check_board_file_version"):
    setattr(file_versions, _check, lambda path, write: file_versions.FileVersions("", ""))
file_versions.library_versions = lambda kind, libraries, kicad: []
file_versions.check_library_versions = (
    lambda kind, libraries, kicad: file_versions.LibraryCheck([], [], set())
)
from kicad_workbench.utils import library_saved
{_STUB_SOURCE}
library_saved._saved_symbols = saved_symbols_stub
"""


_STUB_SOURCE = textwrap.dedent(inspect.getsource(saved_symbols_stub))
