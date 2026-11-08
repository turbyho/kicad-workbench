"""The KiCad of the tests that need no KiCad (CI has no KiCad).

The tools need the version of the installed KiCad (its rules), also when a test gives
them the output of kicad-cli. These tests give the version of KiCad 10.0.6 as the
version of the call (kicad_cli.set_call_version), and no check of the file versions
(utils/file_versions.py): the fixtures of tests/test_tools/conftest.py, and
child_preamble for a test that runs a tool in a child process.
"""

TEST_KICAD = "10.0.6"


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
"""
