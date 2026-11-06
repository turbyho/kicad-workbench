"""The errors that the server expects.

A tool gives "❌ <message>" for such an error (utils/tool_errors.error_answer).
An other exception, also a plain ValueError, is a defect of the tool: the
answer then also gives the traceback.
"""


class WorkbenchError(Exception):
    """An argument, a file or a program that the server or KiCad cannot use.

    The message tells the user why. Each error of a module that the tools
    expect is a WorkbenchError.
    """


class InputError(WorkbenchError, ValueError):
    """An argument or a file that the server or KiCad cannot use.

    It is also a ValueError: the code that catches ValueError gets it too.
    """
