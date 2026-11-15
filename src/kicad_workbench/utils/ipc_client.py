"""The client of the IPC API of KiCad (phase 3): the only module that imports kipy.

The IPC API lets the tools change the board that is open in the PCB editor of a running
KiCad. KiCad itself does each change: one commit is one undo step in the editor.

The server uses kicad-python (kipy, MIT) and its generated protobuf code. The .proto
files of KiCad are GPL-3: the server does not use them.

Each KiCad version has its own rules (IpcRules): the limits of the version, and the
first version that solves each limit (Limit). A tool asks the rules, not the version
number. A KiCad version gets rules only after a check against that version
(spike/kicad-versions/ipc_client_check.py, CLAUDE.md rule 5).
"""

from __future__ import annotations

import os
import platform
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING

from ..errors import WorkbenchError
from ..parsers.kicad_versions import (
    is_development_build,
    is_release_candidate,
    version_triple,
)

if TYPE_CHECKING:
    from kipy import KiCad
    from kipy.board import Board

# The version of kicad-python that the rules of the server use (the extra "ipc").
KICAD_PYTHON_VERSION = "0.8.0"

INSTALL_STEP = (
    'Tell the user: install the IPC dependency of the server: pip install "kicad-workbench[ipc]" '
    f"(kicad-python {KICAD_PYTHON_VERSION}), then restart the MCP client."
)

# The time that the client waits for each answer of KiCad (the default of kicad-python).
TIMEOUT_MS = 2000

# The status codes of ApiResponseStatus (common/envelope.proto of KiCad 10).
AS_UNHANDLED = 5
AS_BUSY = 7


class IpcError(WorkbenchError):
    """KiCad or kicad-python cannot do the IPC request. The message tells the user why,
    and the step that solves it."""


@dataclass(frozen=True)
class Limit:
    """A limit of the IPC API of a KiCad version, and the first version that solves it."""

    key: str
    text: str
    solved_in: str
    source: str


# The limits of the IPC API (plans/roadmap.md, "Limits by KiCad version"). The KiCad 11
# items come from branch master at 52b43fb8fc (2026-11-15): master is not released, and
# a command can change before KiCad 11.
LIMITS = {
    limit.key: limit
    for limit in (
        Limit(
            "schematic_editor_commit",
            "A commit while the schematic editor is open stops KiCad (KiCad bug #24966). "
            "The tools refuse each write while the schematic editor is open.",
            "10.0.7",
            "API_HANDLER_SCH: the member m_frame of API_HANDLER_EDITOR stays null, and "
            "handleBeginCommit calls checkForBusy first (10.0.6); fix 6545d4a28d",
        ),
        Limit(
            "footprint_from_library",
            "No command loads a footprint from a library.",
            "11.0 (PlaceFootprintFromLibrary, not released)",
            "ParseAndCreateItemsFromString gives an empty answer (10.0.6, 10.0.8)",
        ),
        Limit(
            "update_from_schematic",
            "Update PCB from Schematic opens a modal dialog that blocks the API. "
            "The tools do not start it.",
            "11.0 (ImportNetlist, not released)",
            "plans/ipc-spike.md, question 4",
        ),
        Limit(
            "modified_state",
            "No command tells if the board in the editor has changes that are not saved. "
            "The file tools read the file on disk.",
            "11.0 (GetDocumentModifiedState, not released)",
            "editor_commands.proto of 10.0.6 and master",
        ),
        Limit(
            "gui_only",
            "The IPC API needs the GUI of KiCad.",
            "11.0 (kicad-cli api-server, not released)",
            "plans/ipc-spike.md, group G",
        ),
    )
}


@dataclass(frozen=True)
class IpcRules:
    """The IPC rules of one KiCad version."""

    version: tuple[int, int, int]
    limits: tuple[str, ...]
    source: str

    @property
    def name(self) -> str:
        """The version as a text, for example "10.0.6"."""
        return ".".join(map(str, self.version))

    def has_limit(self, key: str) -> bool:
        """True when this KiCad version has the limit."""
        return key in self.limits


# The KiCad versions with IPC rules. The user decision of 2026-11-15: KiCad 10 only;
# start on 10.0.6, then the transition to 10.0.7 and 10.0.8 (step 3F of phase 3).
IPC_RULES = {
    rules.version: rules
    for rules in (
        IpcRules(
            (10, 0, 6),
            (
                "schematic_editor_commit",
                "footprint_from_library",
                "update_from_schematic",
                "modified_state",
                "gui_only",
            ),
            "spike/kicad-versions/ipc_client_check.py with KiCad 10.0.6 (2026-11-15)",
        ),
    )
}


def known_versions() -> str:
    """The KiCad versions with IPC rules, as a text."""
    return ", ".join(rules.name for rules in IPC_RULES.values())


def rules_for(version: str) -> IpcRules:
    """Return the IPC rules of a running KiCad.

    Args:
        version: The full version of the running KiCad (GetVersion), for example "10.0.6".

    Raises:
        IpcError: A version with no IPC rules: a release candidate, a development build,
            a nightly build, or a version that the server did not compare with its rules.
    """
    triple = version_triple(version)
    rules = IPC_RULES.get(triple) if triple else None
    if rules is None or is_release_candidate(version) or is_development_build(version):
        raise IpcError(
            f"The server has no IPC rules for KiCad {version}. It knows only KiCad "
            f"{known_versions()}. Tell the user: the IPC tools need one of these versions."
        )
    return rules


def kicad_python_version() -> str | None:
    """Return the installed version of kicad-python, or None when it is not installed."""
    try:
        return metadata.version("kicad-python")
    except metadata.PackageNotFoundError:
        return None


def _import_kipy():
    """Import kipy, and refuse a kicad-python of an other version than the rules."""
    installed = kicad_python_version()
    if installed is None:
        raise IpcError(f"kicad-python is not installed. {INSTALL_STEP}")
    if installed != KICAD_PYTHON_VERSION:
        raise IpcError(
            f"The server needs kicad-python {KICAD_PYTHON_VERSION}, but {installed} is "
            f"installed. {INSTALL_STEP}"
        )
    try:
        import kipy
        import kipy.client
        import kipy.errors
        from kipy.proto.common.types import DocumentType
    except ImportError as error:
        raise IpcError(f"kicad-python cannot be imported: {error}. {INSTALL_STEP}") from None
    return kipy, kipy.errors, DocumentType


def socket_path() -> tuple[str, str]:
    """Return the socket of the client, and where it comes from.

    The rules of the default socket of kicad-python 0.8.0 (kipy/kicad.py,
    _default_socket_path): KICAD_API_SOCKET when it is set (also an empty value), on
    Windows "kicad/api.sock" in the temporary folder, else the socket of the Flatpak
    package of KiCad when it exists, else /tmp/kicad/api.sock. The client gives this
    path to kicad-python, thus the messages name the socket that the client dials.
    """
    path = os.environ.get("KICAD_API_SOCKET")
    if path is not None:
        return path, "KICAD_API_SOCKET"
    if platform.system() == "Windows":
        return f"ipc://{gettempdir()}\\kicad\\api.sock", "the default socket of Windows"
    home = os.environ.get("HOME")
    if home is not None:
        flatpak = f"{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
        if os.path.exists(flatpak):
            return f"ipc://{flatpak}", "the socket of the Flatpak package of KiCad"
    return "ipc:///tmp/kicad/api.sock", "the default socket"


def socket_text() -> str:
    """The socket of the client, as a text for the user."""
    path, source = socket_path()
    return f"`{path}` ({source})"


# The texts of ConnectionError of kicad-python 0.8.0 (kipy/client.py): "Failed to
# connect to KiCad" when the dial of the socket fails. The send and the answer give
# other texts ("Failed to send command", "Error receiving reply"): a timeout, or a
# connection that stopped.
_DIAL_FAILED = "Failed to connect to KiCad"

NOT_RUNNING = (
    "KiCad does not answer on {socket}: KiCad is not running, or its API server is off. "
    "Tell the user: start KiCad and open the board in the PCB editor. If KiCad runs, "
    'turn on "Enable KiCad API" in Preferences > Preferences > Plugins.'
)
NO_ANSWER = (
    "KiCad did not answer the request {request} ({error}). KiCad waits {seconds} s for "
    "each answer. Possible causes: KiCad stopped or closed, or a dialog that KiCad "
    "opened for an action blocks the API. Tell the user: make sure that KiCad runs, close "
    "an open dialog in KiCad, then try again."
)
BUSY = (
    "KiCad is busy: a dialog is open, or the user does an interactive operation in the "
    "editor (CanAcceptApiCommands). Tell the user: close the dialog or finish the "
    "operation, then try again."
)
NO_PCB_EDITOR = (
    "The PCB editor of KiCad is not open. Tell the user: open the board in the PCB editor."
)


def _translate(error: Exception, errors, request: str) -> IpcError:
    """Return the IpcError for a ConnectionError or an ApiError of kicad-python."""
    if isinstance(error, errors.ConnectionError):
        if str(error).startswith(_DIAL_FAILED):
            return IpcError(NOT_RUNNING.format(socket=socket_text()))
        return IpcError(NO_ANSWER.format(request=request, error=error, seconds=TIMEOUT_MS / 1000))
    if error.code == AS_BUSY:
        return IpcError(BUSY)
    if error.code == AS_UNHANDLED:
        # No open editor has a handler for the request (measured with KiCad 10.0.6).
        return IpcError(f"No open editor of KiCad answers the request {request}. {NO_PCB_EDITOR}")
    return IpcError(f"KiCad refused the request {request}: {error}")


def _same_file(a: Path, b: Path) -> bool:
    """True when two paths name the same file (symbolic links resolved, the case of
    the file system of Windows)."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@dataclass(frozen=True)
class KicadState:
    """The state of the running KiCad at the start of a session."""

    version: str
    rules: IpcRules
    boards: tuple[Path, ...]
    schematic_open: bool

    @property
    def pcb_editor_open(self) -> bool:
        return bool(self.boards)

    def write_problem(self) -> str | None:
        """Return why the tools must not write now, or None."""
        if self.rules.has_limit("schematic_editor_commit") and self.schematic_open:
            solved_in = LIMITS["schematic_editor_commit"].solved_in
            return (
                f"KiCad {self.version} stops on a commit while the schematic editor is open "
                f"(KiCad bug #24966, fixed in KiCad {solved_in}). Tell the user: close the "
                "schematic editor, then try again."
            )
        return None


class IpcSession:
    """A connection to the running KiCad for one tool call (open_session)."""

    def __init__(self, kicad: KiCad, errors, document_type, state: KicadState):
        self.kicad = kicad
        self._errors = errors
        self._document_type = document_type
        self.state = state

    def call(self, request: str, function, *args, **kwargs):
        """Call a function of kicad-python, and give an IpcError for its errors.

        Args:
            request: The name of the request for the message, for example "GetItems".
        """
        try:
            return function(*args, **kwargs)
        except (self._errors.ConnectionError, self._errors.ApiError) as error:
            raise _translate(error, self._errors, request) from None

    def board(self, board_path: str | Path) -> Board:
        """Return the board of the PCB editor, when it is the board of the tool argument.

        Raises:
            IpcError: The PCB editor is not open, or it has a different board.
        """
        # The documents come again from KiCad: the user can close the PCB editor after
        # the start of the session.
        documents = self.call(
            "GetOpenDocuments",
            _open_documents,
            self.kicad,
            self._errors,
            self._document_type.DOCTYPE_PCB,
        )
        if not documents:
            raise IpcError(NO_PCB_EDITOR)
        board = self.call("GetOpenDocuments", self.kicad.get_board)
        document = board.document
        open_board = Path(document.project.path) / document.board_filename
        if not _same_file(open_board, Path(board_path)):
            raise IpcError(
                f"The PCB editor has the board {open_board}, not {board_path}. Tell the "
                "user: open the board in the PCB editor, or give the path of the open board."
            )
        return board

    def check_write(self) -> None:
        """Refuse a write that the running KiCad cannot do safely.

        The state comes again from KiCad: the user can open the schematic editor after
        the start of the session.

        Raises:
            IpcError: The reason (KicadState.write_problem).
        """
        state = _read_state(self.kicad, self._errors, self._document_type, self.state.version)
        self.state = state
        problem = state.write_problem()
        if problem:
            raise IpcError(problem)


def _open_documents(kicad: KiCad, errors, document_type) -> list:
    """Return the open documents of a type. A KiCad with no editor of that type gives
    AS_UNHANDLED ("no handler available", measured with KiCad 10.0.6)."""
    try:
        return list(kicad.get_open_documents(document_type))
    except errors.ApiError as error:
        if error.code == AS_UNHANDLED:
            return []
        raise


def _read_state(kicad: KiCad, errors, document_type, version: str | None = None) -> KicadState:
    """Read the version and the open editors of the running KiCad."""
    try:
        if version is None:
            version = kicad.get_version().full_version
        rules = rules_for(version)
        boards = tuple(
            Path(d.project.path) / d.board_filename
            for d in _open_documents(kicad, errors, document_type.DOCTYPE_PCB)
        )
        schematic_open = bool(_open_documents(kicad, errors, document_type.DOCTYPE_SCHEMATIC))
    except (errors.ConnectionError, errors.ApiError) as error:
        raise _translate(error, errors, "GetVersion and GetOpenDocuments") from None
    return KicadState(version, rules, boards, schematic_open)


# One IPC session at a time in the server process: kicad-python is not thread-safe, and
# KiCad does the requests of all clients one after the other. Reentrant: a session in a
# session of the same thread gets its own connection.
_SESSION_LOCK = threading.RLock()


@contextmanager
def open_session() -> Iterator[IpcSession]:
    """Connect to the running KiCad, and read its version and its open editors.

    Call it in a thread (asyncio.to_thread): each request waits for KiCad.

    Each session has its own connection and its own client name: KiCad keeps a commit
    for each client name (API_HANDLER_EDITOR::m_commits), thus a commit that a session
    could not end does not go to the next session. The session closes the socket at
    its end: the pynng socket and its dialer refer to each other, thus only the cyclic
    garbage collector would close it.

    Raises:
        IpcError: kicad-python is not installed, KiCad does not answer, or the server
            has no IPC rules for the running KiCad.
    """
    kipy, errors, document_type = _import_kipy()
    with _SESSION_LOCK:
        client = kipy.client.KiCadClient(
            socket_path()[0],
            f"kicad-workbench-{os.getpid()}-{uuid.uuid4().hex[:8]}",
            # The token of kicad-python 0.8.0 (kipy/kicad.py, _default_kicad_token).
            os.environ.get("KICAD_API_TOKEN", ""),
            TIMEOUT_MS,
        )
        try:
            kicad = kipy.KiCad.from_client(client)
            state = _read_state(kicad, errors, document_type)
            yield IpcSession(kicad, errors, document_type, state)
        finally:
            _close(client)


def _close(client) -> None:
    """Close the socket of a client of kicad-python 0.8.0.

    KiCadClient has no close method: its socket is the attribute _conn while the
    property connected is true (kipy/client.py). The extra "ipc" pins this version.
    """
    if client.connected:
        client._conn.close()
