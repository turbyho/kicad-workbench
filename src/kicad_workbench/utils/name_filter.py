"""The regular expression that a tool gets from the agent as a filter of names.

The module re has no time limit for a search, and a thread with a search that does
not end cannot be stopped (a pattern with catastrophic backtracking, for example
"(a*)*b" on a long name): it holds a CPU core and a worker of the executor, and the
server process cannot end while it runs. Thus the searches run in a child process of
the Python of the server (python -I -S: no site module, thus no .pth file of the
venv runs) with a time limit: at the limit, subprocess.run
kills the process. On Linux the process also has a memory limit (RLIMIT_AS); macOS
does not enforce RLIMIT_AS, and Windows has no such limit.

The compile runs in the server process: re.compile does not expand the repeats (the
module regex does, and a short pattern then takes gigabytes), and it raises its errors
at once.
"""

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import TypeVar

from ..errors import InputError, WorkbenchError

# The time limit of the search process (seconds), with the start of the interpreter
# (some 10 to 30 ms, more on a loaded system) and the transfer of the texts. Below
# JOB_WAIT (background_jobs.py): a tool with no other long step answers before its
# call becomes a job.
SEARCH_TIME = 15.0
# The memory limit of the search process on Linux (bytes).
SEARCH_MEMORY = 1024**3
# The length of the pattern in an error message.
_SHOWN = 100

T = TypeVar("T")

# The search process. Its argument is the memory limit. It reads {"pattern", "flags",
# "texts": [[text, ...], ...]} on stdin (ASCII JSON), and writes {"found": [the indexes
# of the items with a match]} or {"error": "memory"}. A pattern that compiles in the
# server compiles here too (the same Python).
_SEARCH = """
import json, re, sys
try:
    import resource
    limit = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
except (ImportError, ValueError, OSError):
    pass
try:
    data = json.loads(sys.stdin.buffer.read().decode("ascii"))
    flt = re.compile(data["pattern"], data["flags"])
    found = [i for i, texts in enumerate(data["texts"]) if any(flt.search(t) for t in texts)]
    answer = {"found": found}
except MemoryError:
    answer = {"error": "memory"}
sys.stdout.write(json.dumps(answer))
"""


def _shown(pattern: str) -> str:
    text = repr(pattern)
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}... ({len(pattern)} characters)"


def compile_filter(pattern: str, name: str = "filter pattern") -> re.Pattern[str] | None:
    """Return the regular expression of a filter (no difference of case), or None for
    an empty pattern. name is the name of the argument in the error messages.

    re.compile raises re.error for a wrong pattern, OverflowError for a repetition
    count that is too large ("a{4294967296}") and RecursionError for a pattern with a
    deep nesting: each one is an error that the agent can correct.

    Raises:
        InputError: The pattern is not a regular expression of the module re.
    """
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except (re.error, OverflowError, RecursionError) as e:
        raise InputError(f"The {name} {_shown(pattern)} is not a regular expression: {e}") from e


def _search_sync(flt: re.Pattern[str], texts: list[list[str]], name: str) -> list[int]:
    """Run the searches in the search process, with the time limit SEARCH_TIME. The
    function blocks: run it off the event loop.

    Raises:
        InputError: The search process takes longer than SEARCH_TIME, or more memory
            than SEARCH_MEMORY.
        WorkbenchError: The server has no path of its Python, or the system stopped
            the search process (for example for lack of memory).
        OSError: The search process does not start.
        RuntimeError: The search process ended with an error (a defect).
    """
    if not sys.executable:
        raise WorkbenchError(
            "The server cannot start the search process of a filter: Python gives no path "
            "of its program (sys.executable)."
        )
    data = json.dumps({"pattern": flt.pattern, "flags": flt.flags, "texts": texts})
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _SEARCH, str(SEARCH_MEMORY)],
            input=data.encode("ascii"),
            capture_output=True,
            timeout=SEARCH_TIME,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise InputError(
            f"The search with the {name} {_shown(flt.pattern)} takes longer than "
            f"{SEARCH_TIME:g} s (with the start of the search process). A pattern with "
            'nested repeats (catastrophic backtracking, for example "(a*)*b") can take this '
            "long: give a simpler pattern. A simple pattern on a very loaded system can "
            "also take this long: then try again later."
        ) from e
    errors = done.stderr.decode("utf-8", "replace").strip()
    if done.returncode == 1:
        # An exception of the search script (Python ends with 1): a defect.
        raise RuntimeError(f"The search process ended with the exit code 1: {errors}")
    if done.returncode != 0:
        # The system stopped the process: a signal on Linux and macOS (a negative code,
        # for example the kill for lack of memory), an other exit code on Windows.
        raise WorkbenchError(
            f"The system stopped the search process of the {name} (exit code "
            f"{done.returncode}). {errors}".strip()
        )
    answer = json.loads(done.stdout.decode("ascii"))
    if answer.get("error") == "memory":
        raise InputError(
            f"The search with the {name} {_shown(flt.pattern)} needs more memory than the "
            f"search process can get (on Linux {SEARCH_MEMORY // 1024**2} MB): give a "
            "simpler pattern."
        )
    return answer["found"]


async def select(
    flt: re.Pattern[str] | None,
    items: list[T],
    texts: Callable[[T], Iterable[str]],
    name: str = "filter pattern",
) -> list[T]:
    """Return the items with a text that the filter finds (a part of the text, as
    re.search), in their order; all items with no filter. The searches run in a
    child process with the time limit SEARCH_TIME (see the module).

    Raises:
        InputError: The search process takes longer than SEARCH_TIME, or more memory
            than SEARCH_MEMORY.
        WorkbenchError: The server has no path of its Python, or the system stopped
            the search process.
        OSError: The search process does not start.
    """
    if flt is None:
        return list(items)
    if not items:
        return []
    found = await asyncio.to_thread(_search_sync, flt, [list(texts(item)) for item in items], name)
    return [items[index] for index in found]
