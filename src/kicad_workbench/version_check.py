"""Tell the user about a newer release, and about a server that does not run the
installed code (the method of datasheet-lens and fw-context-mcp).

- The server does not run the installed code. The MCP server reads __version__
  one time, when it starts. When the user installs a different version while
  the server runs, the server keeps the old code. Only a restart of the LLM
  client helps: the MCP server is a child process of the client. This check
  reads only the local package metadata. The build reads the version of the
  metadata from __version__ (pyproject.toml, [tool.hatch.version]): the two
  values have one source.
- A newer release is on PyPI. One background thread for each server process
  gets the JSON document of the package from PyPI, and writes the newest
  version into a state file in the cache directory of the user. After a
  request that succeeded, no request goes out for CHECK_INTERVAL. A request
  that failed writes nothing, thus a machine with no network access tries again
  at the next start. The tools read only the state file, never the network: no
  answer waits for PyPI.

The network check is on by default. A value in DISABLE_ENV stops it. An
editable install (PEP 610 direct_url.json) gets no check: its metadata does not
follow the source tree, and a comparison with PyPI can report a release that
the source tree has already. A server with no package metadata (a source tree
in PYTHONPATH) gets no check: it has no installed version to compare.

VersionNotes adds the notes to the result of each tool, each note one time for
each server process. After a note, the server does not do its check again.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from packaging.version import InvalidVersion, Version

from . import __version__
from .utils.user_files import cache_home, write_atomic

log = logging.getLogger(__name__)

DISTRIBUTION = "kicad-workbench"
PYPI_URL = "https://pypi.org/pypi/kicad-workbench/json"
# A value other than "" and "0" stops the network check (for CI and tests).
DISABLE_ENV = "KICAD_WORKBENCH_NO_UPDATE_CHECK"
# One request a day is enough for releases that come after days or weeks.
CHECK_INTERVAL = timedelta(hours=24)
TIMEOUT = 5.0
STATE_FILE = "update_check.json"
# The errors of a request to PyPI: no network or an HTTP error (OSError), a
# document that is not JSON or has no valid version (ValueError, KeyError,
# TypeError), a cache directory that is not writable (OSError).
_REQUEST_ERRORS = (OSError, ValueError, KeyError, TypeError)


def installed_version() -> str | None:
    """The version in the package metadata on disk now (it follows an upgrade
    while the process runs), or None when the package is not installed."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def _same_version(a: str, b: str) -> bool:
    """True for one version in two spellings (0.2.0-rc1 and 0.2.0rc1)."""
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def restart_reason(running: str = __version__) -> str | None:
    """The text that tells the user to restart the LLM client, or None: the
    installed version is not the version that this process loaded. Not for an
    editable install: its metadata does not follow the source tree."""
    installed = installed_version()
    if installed is None or _same_version(installed, running) or is_editable_install() is not False:
        return None
    return (
        f"The installed kicad-workbench is {installed}, but this MCP server runs {running}, "
        "the version of the start of the LLM client. Tell the user to restart the LLM client "
        "(Claude Code or the client in use): the MCP server is a child process of the client."
    )


def is_editable_install() -> bool | None:
    """True for an editable install (PEP 610). None when the install type is not
    known (no check): the package has no metadata, or direct_url.json is not
    JSON. The function reads the metadata at each call: the user can change the
    install while the server runs."""
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if text is None:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def check_enabled() -> bool:
    """True when the user lets kicad-workbench ask PyPI, and the install is from
    an index (not editable, with metadata)."""
    if os.environ.get(DISABLE_ENV, "") not in ("", "0"):
        return False
    return is_editable_install() is False


def state_path() -> Path:
    return cache_home() / STATE_FILE


def read_state() -> tuple[datetime, str] | None:
    """(time of the request, newest version) of the last request that
    succeeded, or None. A file that cannot be read gives None."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["checked_at"])
        latest = str(data["latest"])
        Version(latest)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (checked, latest) if checked.tzinfo is not None else None


def refresh_latest_version(now: datetime | None = None) -> None:
    """Ask PyPI for the newest version and write it into the state file. No
    request when the stored result is younger than CHECK_INTERVAL. The errors
    go to the caller."""
    current = now or datetime.now(timezone.utc)
    state = read_state()
    # A time in the future (a clock that moved back) does not stop the check.
    if state is not None and timedelta(0) <= current - state[0] < CHECK_INTERVAL:
        return
    request = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        latest = str(json.loads(response.read().decode("utf-8"))["info"]["version"])
    Version(latest)
    write_atomic(state_path(), json.dumps({"checked_at": current.isoformat(), "latest": latest}))


def _refresh_in_background() -> None:
    """A failed request is a fact about the network: it goes to the debug log,
    never into a tool answer. A different error is a defect: the thread stops
    with it, and threading.excepthook writes it to stderr (the log of the
    client)."""
    try:
        refresh_latest_version()
    except _REQUEST_ERRORS:
        log.debug("update check failed", exc_info=True)


def start_update_check() -> threading.Thread | None:
    """Start the background request to PyPI when the check applies. The thread
    never keeps the process alive (daemon)."""
    if not check_enabled():
        return None
    thread = threading.Thread(
        target=_refresh_in_background, daemon=True, name="kicad-workbench-update-check"
    )
    thread.start()
    return thread


def newer_release() -> tuple[str, str] | None:
    """(newest, installed) when PyPI has a newer release, from the state file
    only."""
    if not check_enabled():
        return None
    installed, state = installed_version(), read_state()
    if installed is None or state is None:
        return None
    try:
        newer = Version(state[1]) > Version(installed)
    except InvalidVersion:  # installed metadata with no valid version
        return None
    return (state[1], installed) if newer else None


_told: set[str] = set()  # the kinds of note that this server process gave already
_told_lock = threading.Lock()


def _once(notes: list[tuple[str, str]]) -> list[str]:
    """The texts of the notes of a kind that this process did not give."""
    with _told_lock:
        new = [(kind, text) for kind, text in notes if kind not in _told]
        _told.update(kind for kind, _text in new)
    return [text for _kind, text in new]


def version_notes() -> list[str]:
    """The lines for the LLM: a restart of the client, or a newer release. Each
    kind of note one time for each server process: the agent tells the user one
    time. After a note of a kind, the function does not do its check again."""
    with _told_lock:
        pending = {"restart", "release"} - _told
    notes = []
    if "restart" in pending:
        reason = restart_reason()
        if reason:
            notes.append(("restart", f"⚠️ {reason}"))
    if "release" in pending:
        found = newer_release()
        if found:
            notes.append(
                (
                    "release",
                    f"ℹ️ kicad-workbench {found[0]} is available on PyPI, and this machine has "
                    f"{found[1]}. Tell the user one time in this session. Do not upgrade it "
                    "yourself: the user upgrades it with the tool that installed it, then "
                    "restarts the LLM client.",
                )
            )
    return _once(notes)


def add_notes(result: ToolResult, notes: list[str]) -> ToolResult:
    """The result with the notes after its text. A tool that returns a text has
    the text two times: in the content and in the structured content
    {"result": text} (FastMCP wraps a return value that is not an object). The
    two get the same notes. A different result gets the notes as one more text
    content item. model_copy keeps the other fields of the result."""
    if not notes:
        return result
    block = "\n\n" + "\n".join(notes)
    structured = result.structured_content
    text = structured.get("result") if isinstance(structured, dict) else None
    content = list(result.content)
    index = next(
        (
            i
            for i, item in enumerate(content)
            if isinstance(text, str) and isinstance(item, mt.TextContent) and item.text == text
        ),
        None,
    )
    if index is None:
        content.append(mt.TextContent(type="text", text=block.lstrip("\n")))
    else:
        content[index] = content[index].model_copy(update={"text": text + block})
        structured = {**structured, "result": text + block}
    return result.model_copy(update={"content": content, "structured_content": structured})


class VersionNotes(Middleware):
    """Adds the version notes to the result of a tool."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        if not isinstance(result, ToolResult):
            # A task of the MCP task protocol: the result comes later, through a
            # different request.
            return result
        try:
            # version_notes reads the package metadata and the state file.
            notes = await asyncio.to_thread(version_notes)
        except Exception as error:
            # A defect of the check must not stop the tool: the tool result is
            # complete. The log and the note tell the user about the defect.
            log.warning("release check failed", exc_info=True)
            notes = _once(
                [
                    (
                        "error",
                        f"⚠️ The release check of kicad-workbench failed "
                        f"({type(error).__name__}: {error}). The result of the tool is "
                        "complete. Tell the user one time in this session.",
                    )
                ]
            )
        return add_notes(result, notes)
