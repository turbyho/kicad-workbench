"""The one thread for the pcbnew of the server process.

pcbnew is not thread-safe: the settings manager and the project of KiCad are global
objects of the process. Thus each use of pcbnew in the server process (the import, the
check of its version, PCBParserKiCad) runs in this one thread, one call at a time,
through in_pcbnew_thread. It is not the thread of the event loop: a large board does not
block the server, a long call becomes a background job (background_jobs.py), and the
pool of asyncio.to_thread stays free for the other tools.

pcbnew of KiCad 8.0.9, 9.0.9 and 10.0.6 on Linux imports and loads a board in such a
thread (spike/kicad-versions/pcbnew_thread_check.py). Limits:

- A cancel does not stop a call that runs (a thread cannot be stopped): a later call
  waits until it ends, also at the end of the server process.
- The server process imports pcbnew only when its Python has pcbnew (the system Python
  of a KiCad package on Linux). Windows and macOS (the text reader) are not measured.
"""

import asyncio
import concurrent.futures
import contextvars
import functools
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_PCBNEW_THREAD = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcbnew")


async def in_pcbnew_thread(function: Callable[..., T], *args, **kwargs) -> T:
    """Run a function in the pcbnew thread, with the context of the call (for example
    the version of kicad-cli that with_board_versions keeps), as asyncio.to_thread
    does."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, function, *args, **kwargs)
    return await loop.run_in_executor(_PCBNEW_THREAD, call)
