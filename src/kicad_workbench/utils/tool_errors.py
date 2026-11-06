"""The answer of a tool for an exception (CLAUDE.md, "Honest output")."""

import subprocess
import traceback

from ..parsers.kicad_versions import KicadFileError
from .kicad_cli import KicadCliError
from .sch_hierarchy import AmbiguousRootError

# The errors that the tools expect: a file that cannot be read or written
# (OSError: also a file that is not there), a value or a file that the server or
# KiCad cannot read (ValueError: also SExprError, KiCadEncodingError,
# LibraryTableError, NetlistError and the other errors of the modules), a sheet
# with more than one root schematic, and a kicad-cli that cannot run or does not
# stop in time.
EXPECTED_ERRORS = (
    OSError,
    ValueError,
    KicadFileError,
    AmbiguousRootError,
    KicadCliError,
    subprocess.TimeoutExpired,
)


def error_answer(error: Exception, failure: str) -> str:
    """Return the answer of a tool for an exception.

    Call this function in the except block: the answer of an unexpected error
    has the traceback of the exception that the block handles.

    Args:
        error: The exception.
        failure: The text for an unexpected error, for example "Error parsing
            schematic".

    Returns:
        "❌ <message>" for an expected error. For an other exception (a defect
        of the tool): "❌ <failure>: <message>" and the traceback.
    """
    if isinstance(error, EXPECTED_ERRORS):
        return f"❌ {error}"
    return f"❌ {failure}: {error}\n\n{traceback.format_exc()}"
