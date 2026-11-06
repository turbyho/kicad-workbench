"""The answer of a tool for an exception (CLAUDE.md, "Honest output")."""

import subprocess
import traceback

from ..errors import WorkbenchError

# The errors that the tools expect: a file that cannot be read or written
# (OSError: also a file that is not there), an error of the modules
# (WorkbenchError: an argument, a file or a program that the server or KiCad
# cannot use), and a kicad-cli that does not stop in time. A plain ValueError is
# a defect of the tool.
EXPECTED_ERRORS = (OSError, WorkbenchError, subprocess.TimeoutExpired)


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
