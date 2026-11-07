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


class KicadFileError(WorkbenchError):
    """KiCad cannot read the file, or this server has no rules for its version."""


class KicadParseError(KicadFileError):
    """The parser of KiCad refuses the text of a file (THROW_PARSE_ERROR).

    KiCad then does not load the file: a sub-sheet is an empty sheet, a
    top-level sheet stops the load.
    """


class LocaleNumberError(KicadFileError):
    """KiCad reads a text of the file as a number only in some locales.

    wcstol skips the white space at the start with iswspace of the C library,
    which depends on the locale and the system. The server does not know them.
    """
