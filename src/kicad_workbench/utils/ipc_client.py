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
from typing import TYPE_CHECKING, Any

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

# The time that the client waits for each answer of KiCad. KiCad does the requests one
# after the other, and a large board needs time: GetItems of the 1125 footprints of the
# demo jetson-agx-thor-baseboard took 983 ms (KiCad 10.0.8, 2026-11-16). The 2 s of
# kicad-python are too short for a larger board.
TIMEOUT_MS = 30_000

# The status codes of ApiResponseStatus (common/envelope.proto of KiCad 10).
AS_NOT_READY = 4
AS_UNHANDLED = 5
AS_BUSY = 7


class IpcError(WorkbenchError):
    """KiCad or kicad-python cannot do the IPC request. The message tells the user why,
    and the step that solves it.

    busy is True when KiCad answered AS_BUSY: "KiCad is busy performing an operation and
    can't accept API commands" (ApiStatusCode of envelope.proto). no_answer is True when
    the answer did not come (a timeout, or a broken connection after the dial).
    """

    def __init__(self, message: str, *, busy: bool = False, no_answer: bool = False):
        super().__init__(message)
        self.busy = busy
        self.no_answer = no_answer


@dataclass(frozen=True)
class Limit:
    """A limit of the IPC API of a KiCad version, and the first version that solves it.

    solved_in is the text for the user: "KiCad <version> ..." or "No version (...)".
    """

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
            "A commit (BeginCommit) while the schematic editor is open stops KiCad (KiCad "
            "bug #24966). The tools refuse a commit while the schematic editor is open. A "
            "request with no commit (FlipItems, SaveDocument, RefillZones) operates.",
            "KiCad 10.0.7",
            "API_HANDLER_SCH: the member m_frame of API_HANDLER_EDITOR stays null, and "
            "handleBeginCommit calls checkForBusy first (10.0.6); fix 6545d4a28d",
        ),
        Limit(
            "footprint_from_library",
            "No command loads a footprint from a library.",
            "KiCad 11.0 (PlaceFootprintFromLibrary, not released)",
            "ParseAndCreateItemsFromString gives an empty answer (10.0.6, 10.0.8)",
        ),
        Limit(
            "update_from_schematic",
            "The API has no command that updates the board from the schematic (Update PCB "
            "from Schematic). The user starts it in the PCB editor (F8).",
            "KiCad 11.0 (ImportNetlist, not released)",
            "board_commands.proto of 10.0.6: no such command; master: ImportNetlist",
        ),
        Limit(
            "modified_state",
            "No command tells if the board in the editor has changes that are not saved. "
            "The file tools read the file on disk.",
            "KiCad 11.0 (GetDocumentModifiedState, not released)",
            "editor_commands.proto of 10.0.6 and master",
        ),
        Limit(
            "gui_only",
            "The IPC API needs the GUI of KiCad.",
            "KiCad 11.0 (kicad-cli api-server, not released)",
            "plans/ipc-spike.md, group G",
        ),
        Limit(
            "no_data_tables_points",
            "The API gives no data for tables, table cells and reference points.",
            "KiCad 10.0.7 (kicad-python 0.8.0 does not know these types)",
            "PCB_TABLE, PCB_TABLECELL and PCB_POINT have no Serialize in 10.0.6; "
            "commits 15eda6b8ec and d41d1e81f9 of 10.0.7",
        ),
        Limit(
            "no_data_markers_patterns_targets",
            "The API gives no data for DRC markers, tuning patterns and targets.",
            "KiCad 11.0 for DRC markers and tuning patterns (not released); no version for "
            "targets (master 2026-11-15)",
            "PCB_MARKER, PCB_TUNING_PATTERN and PCB_TARGET have no Serialize in 10.0.6 to "
            "10.0.8; master serializes PCB_MARKER and PCB_TUNING_PATTERN",
        ),
        Limit(
            "selection_filter",
            "The API cannot read or use the selection filter of the PCB editor: the tools "
            "select the items with no filter.",
            "No version (master 2026-11-15)",
            "SELECTION_TOOL::AddItemsToSel selects with no itemPassesFilter (10.0.6); no "
            "selection filter in the .proto files of master",
        ),
        Limit(
            "footprint_update_loses_data",
            "An update of a footprint (UpdateItems) makes a new footprint from the API "
            "data, and KiCad 10.0.6 loses the units of the footprint, the teardrop settings "
            "of its pads and the default line style of its shapes. The tools do not move, "
            "rotate or lock a footprint.",
            "KiCad 10.0.8 (measured in Docker: the round trip keeps all data, also through the "
            "classes of kicad-python 0.8.0)",
            "handleCreateUpdateItemsInternal removes the footprint and adds a new one "
            "(10.0.6); spike/ipc/probe_update_roundtrip.py: all 56 footprints lose "
            "(units ...), 12 pads lose (teardrops ...), 6 shapes change from default to solid",
        ),
        Limit(
            "footprint_move_children",
            "The API has no command that moves or rotates a footprint. The API gives the "
            "items of a footprint with absolute positions: a change of the position of the "
            "footprint alone moves only its anchor. The tools do not move or rotate a "
            "footprint.",
            "No version (master 2026-11-15: only InteractiveMoveItems)",
            "spike/ipc/probe_update_raw.py with KiCad 10.0.8: a footprint moved 1 mm by its "
            "position; its pads, shapes and fields stayed at their place",
        ),
        Limit(
            "flip_direction_setting",
            "The API cannot read the flip direction of the PCB editor: the tools need the "
            "direction as an argument.",
            "No version (master 2026-11-15)",
            "board_commands.proto of 10.0.6 and master: FlipItems takes the direction of the "
            "request; no request reads the settings of the editor",
        ),
        Limit(
            "refill_progress",
            "RefillZones gives no result: KiCad answers AS_BUSY until the fill has completed "
            "(board_commands.proto), or the next request waits for the end of the fill "
            "(measured with KiCad 10.0.6 and 10.0.8). The tools cannot see if the user "
            "cancelled the fill. The tools refill all zones.",
            "KiCad 11.0 (StartRefillZonesTask of board_tasks.proto, not released)",
            "board_commands.proto of 10.0.6 and master; measured with 10.0.6: a list of "
            "zones gives AS_UNIMPLEMENTED (handleRefillZones)",
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
                "no_data_tables_points",
                "no_data_markers_patterns_targets",
                "selection_filter",
                "footprint_update_loses_data",
                "footprint_move_children",
                "flip_direction_setting",
                "refill_progress",
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


@dataclass(frozen=True)
class _Kipy:
    """The modules and the names of kicad-python that the client uses."""

    module: Any
    client_class: Any
    errors: Any
    document_type: Any
    object_type: Any
    editor_commands: Any
    board_commands: Any
    unpack_any: Any
    canonical_name: Any
    item_ok: Any
    item_request_status: Any
    zone_copper: Any
    locked_state: Any
    unlocked_state: Any


def _import_kipy() -> _Kipy:
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
        from kipy.proto.board import board_commands_pb2, board_types_pb2
        from kipy.proto.common.commands import editor_commands_pb2
        from kipy.proto.common.types import DocumentType, KiCadObjectType, base_types_pb2
        from kipy.util.board_layer import canonical_name
        from kipy.util.proto import unpack_any
    except ImportError as error:
        raise IpcError(f"kicad-python cannot be imported: {error}. {INSTALL_STEP}") from None
    return _Kipy(
        module=kipy,
        client_class=kipy.client.KiCadClient,
        errors=kipy.errors,
        document_type=DocumentType,
        object_type=KiCadObjectType,
        editor_commands=editor_commands_pb2,
        board_commands=board_commands_pb2,
        unpack_any=unpack_any,
        canonical_name=canonical_name,
        item_ok=editor_commands_pb2.ISC_OK,
        item_request_status=base_types_pb2.ItemRequestStatus,
        zone_copper=board_types_pb2.ZT_COPPER,
        locked_state=base_types_pb2.LS_LOCKED,
        unlocked_state=base_types_pb2.LS_UNLOCKED,
    )


def socket_path() -> tuple[str, str]:
    """Return the socket of the client, and where it comes from.

    A limit of kicad-python 0.8.0, not of the IPC API: the documented way to make a
    client for raw requests is KiCadClient(socket_path, ...) and KiCad.from_client, and
    KiCadClient needs the socket path. Only the constructor KiCad() finds the default
    socket, with the private function _default_socket_path, and it gives its client
    only through the private attribute _client. Thus this function is a copy of the
    rules of _default_socket_path of kicad-python 0.8.0 (kipy/kicad.py):
    KICAD_API_SOCKET when it is set (also an empty value; documented in kicad-dev-docs),
    on Windows "kicad/api.sock" in the temporary folder, else the socket of the Flatpak
    package of KiCad when it exists, else /tmp/kicad/api.sock. Compare it with each new
    release of kicad-python. The client gives this path to kicad-python, thus the
    messages name the socket that the client dials.
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
# The text of nng for an address that it cannot use, for example a path of a Unix socket
# that is longer than sockaddr_un.sun_path (measured with a path of more than 108 bytes).
_ADDRESS_INVALID = "Address invalid"

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
BAD_ADDRESS = (
    "The socket {socket} is not a valid address for nng ({error}). The path of a socket "
    "must be short (Linux: less than 108 bytes). Tell the user: set KICAD_API_SOCKET to a "
    "shorter path."
)
NOT_READY = (
    'KiCad is not ready to answer: it starts, or a dialog of its first start ("Welcome '
    'to KiCad") is open. Tell the user: finish the dialog in KiCad, then try again.'
)
BUSY = (
    "KiCad is busy: a dialog is open, or the user does an interactive operation in the "
    "editor (CanAcceptApiCommands). Tell the user: close the dialog or finish the "
    "operation, then try again."
)
NO_PCB_EDITOR = (
    "The PCB editor of KiCad is not open. Tell the user: open the board in the PCB editor."
)


def _translate(error: Exception, errors, request: str, timeout_ms: int = TIMEOUT_MS) -> IpcError:
    """Return the IpcError for a ConnectionError or an ApiError of kicad-python."""
    if isinstance(error, errors.ConnectionError):
        if str(error).startswith(_DIAL_FAILED) and _ADDRESS_INVALID in str(error):
            return IpcError(BAD_ADDRESS.format(socket=socket_text(), error=error))
        if str(error).startswith(_DIAL_FAILED):
            return IpcError(NOT_RUNNING.format(socket=socket_text()))
        return IpcError(
            NO_ANSWER.format(request=request, error=error, seconds=timeout_ms / 1000),
            no_answer=True,
        )
    if error.code == AS_BUSY:
        return IpcError(BUSY, busy=True)
    if error.code == AS_NOT_READY:
        # KICAD_API_SERVER answers AS_NOT_READY until SetReadyToReply at the end of the
        # start (single_top.cpp, kicad.cpp of KiCad 10.0.8); the first-run wizard holds it.
        return IpcError(NOT_READY)
    if error.code == AS_UNHANDLED:
        # envelope.proto: "The request was not handled by KiCad". Measured with KiCad
        # 10.0.6: no open editor has a handler for the request.
        return IpcError(
            f"KiCad did not handle the request {request} (AS_UNHANDLED): no open editor of "
            f"KiCad has a handler for it. {NO_PCB_EDITOR}"
        )
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

    def commit_problem(self) -> str | None:
        """Return why the tools must not start a commit (BeginCommit) now, or None.

        Only BeginCommit and EndCommit go to the schematic handler with no document check
        first (bug #24966). FlipItems, SaveDocument and RefillZones have no handler in
        API_HANDLER_SCH of KiCad 10.0.6: measured with the schematic editor open
        (spike/kicad-versions/ipc_write_check.py)."""
        if self.rules.has_limit("schematic_editor_commit") and self.schematic_open:
            solved_in = LIMITS["schematic_editor_commit"].solved_in
            return (
                f"KiCad {self.version} stops on a commit while the schematic editor is open "
                f"(KiCad bug #24966, fixed in {solved_in}). Tell the user: close the "
                "schematic editor, then try again."
            )
        return None


@dataclass(frozen=True)
class BoardItem:
    """One item of the board as KiCad gives it through the API: a summary for the user.

    kind is the name of the protobuf message (for example "FootprintInstance", "Track",
    "Via", "Pad", "Zone"), or "" when KiCad gave no data. problem tells why the client
    cannot read the item, or it is "".
    """

    kind: str
    id: str = ""
    text: str = ""
    net: str = ""
    layer: str = ""
    position: tuple[int, int] | None = None
    problem: str = ""
    # The locked state of the item itself (LS_LOCKED), None for an item with no state.
    locked: bool | None = None


# KiCad gives an empty message for an item type with no Serialize function
# (SERIALIZABLE::Serialize of KiCad 10.0.6 only asserts).
NO_DATA = (
    "KiCad gave no data for this item: the API of this KiCad version cannot serialize "
    "its type (for example a table, a reference point or a DRC marker)"
)


def describe_item(k: _Kipy, message) -> BoardItem:
    """Return the summary of an item of an answer of KiCad (a google.protobuf.Any)."""
    if not message.type_url:
        return BoardItem("", problem=NO_DATA)
    kind = message.type_url.rsplit(".", 1)[-1]
    try:
        item = k.unpack_any(message)
    except NotImplementedError:
        return BoardItem(
            kind,
            problem=f"kicad-python {KICAD_PYTHON_VERSION} does not know the type "
            f"{message.type_url}",
        )
    except ValueError as error:
        return BoardItem(kind, problem=f"kicad-python cannot read the item: {error}")
    fields = item.DESCRIPTOR.fields_by_name
    item_id = ""
    if "id" in fields and fields["id"].message_type.full_name == "kiapi.common.types.KIID":
        item_id = item.id.value
    if kind == "FootprintInstance":
        text = item.reference_field.text.text.text
    elif kind == "Pad":
        text = item.number
    elif kind in ("Zone", "Group"):
        text = item.name
    elif kind == "Field":
        text = f"{item.name}: {item.text.text.text}"
    elif kind in ("BoardText", "Dimension"):
        text = item.text.text
    elif kind == "BoardTextBox":
        text = item.textbox.text
    elif kind == "Barcode":
        text = item.text
    else:
        text = ""
    if "net" in fields:
        net = item.net.name
    elif kind == "Zone":
        # The net of a copper zone (CopperZoneSettings).
        net = item.copper_settings.net.name
    else:
        net = ""
    if "layer" in fields:
        layers = [item.layer]
    elif "layers" in fields:
        layers = list(item.layers)
    elif "pad_stack" in fields:
        # The layers of a via or a pad (PadStack).
        layers = list(item.pad_stack.layers)
    else:
        layers = []
    layer = ", ".join(k.canonical_name(layer) for layer in layers)
    if "position" in fields:
        position = (item.position.x_nm, item.position.y_nm)
    elif "start" in fields:
        position = (item.start.x_nm, item.start.y_nm)
    else:
        position = None
    locked = None
    if "locked" in fields and fields["locked"].enum_type is not None:
        # LockedState: LS_LOCKED or LS_UNLOCKED, else not known (LS_UNKNOWN).
        locked = {k.locked_state: True, k.unlocked_state: False}.get(item.locked)
    return BoardItem(kind, item_id, text, net, layer, position, locked=locked)


class IpcSession:
    """A connection to the running KiCad for one tool call (open_session)."""

    def __init__(self, k: _Kipy, client, kicad: KiCad, state: KicadState, timeout_ms: int):
        self._k = k
        self.client = client
        self.kicad = kicad
        self.state = state
        self.timeout_ms = timeout_ms

    def call(self, request: str, function, *args, **kwargs):
        """Call a function of kicad-python, and give an IpcError for its errors.

        Args:
            request: The name of the request for the message, for example "GetItems".
        """
        try:
            return function(*args, **kwargs)
        except (self._k.errors.ConnectionError, self._k.errors.ApiError) as error:
            raise _translate(error, self._k.errors, request, self.timeout_ms) from None

    def send(self, command, response_type):
        """Send a protobuf command to KiCad, and return its answer.

        The item commands go directly to the client: the wrappers of kicad-python 0.8.0
        stop on an item that KiCad gives with no data (unpack_any refuses an empty
        message), and describe_item reads each item."""
        return self.call(type(command).__name__, self.client.send, command, response_type)

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
            self._k.errors,
            self._k.document_type.DOCTYPE_PCB,
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

    def check_commit(self) -> None:
        """Refuse a commit (BeginCommit) that the running KiCad cannot do safely.

        The state comes again from KiCad: the user can open the schematic editor after
        the start of the session.

        Raises:
            IpcError: The reason (KicadState.commit_problem).
        """
        state = _read_state(self.kicad, self._k, self.state.version, self.timeout_ms)
        self.state = state
        problem = state.commit_problem()
        if problem:
            raise IpcError(problem)

    # The items of the board.

    def footprints(self, board: Board) -> list[BoardItem]:
        """Return the footprints of the board (GetItems)."""
        command = self._k.editor_commands.GetItems()
        command.header.document.CopyFrom(board.document)
        command.types.append(self._k.object_type.KOT_PCB_FOOTPRINT)
        answer = self.send(command, self._k.editor_commands.GetItemsResponse)
        return [describe_item(self._k, item) for item in answer.items]

    def net_names(self, board: Board) -> list[str]:
        """Return the names of the nets of the board (GetNets)."""
        command = self._k.board_commands.GetNets()
        command.board.CopyFrom(board.document)
        answer = self.send(command, self._k.board_commands.NetsResponse)
        return [net.name for net in answer.nets]

    def net_items(self, board: Board, nets: list[str]) -> list[BoardItem]:
        """Return the tracks, arcs, vias and shapes of the nets: the item types that
        "Select All Tracks in Net" selects (PCB_SELECTION_TOOL::SelectAllItemsOnNet of
        KiCad 10.0.6), through GetItemsByNet. The selection filter of the editor does not
        apply (limit selection_filter)."""
        types = self._k.object_type
        command = self._k.board_commands.GetItemsByNet()
        command.header.document.CopyFrom(board.document)
        command.types.extend(
            [types.KOT_PCB_TRACE, types.KOT_PCB_ARC, types.KOT_PCB_VIA, types.KOT_PCB_SHAPE]
        )
        for name in nets:
            command.nets.add().name = name
        answer = self.send(command, self._k.editor_commands.GetItemsResponse)
        return [describe_item(self._k, item) for item in answer.items]

    def flip_items(
        self, board: Board, ids: list[str], direction: str
    ) -> tuple[dict[str, BoardItem], list[str], int]:
        """Flip items to the other side of the board, each around its position
        (FlipItems: "Flips one or more board items to the opposite side of the board").
        With no open commit, KiCad makes one commit for the request: one undo step
        "Flipped items via API" (handleFlipItems of KiCad 10.0.6). KiCad flips also a
        locked item.

        board_commands.proto gives no order of the results and no ID in a result: the
        item of a result (only with ISC_OK) gives the ID of the flipped item.

        Args:
            direction: "left_right" (around the Y axis) or "top_bottom" (around the X axis).

        Returns:
            The flipped items by ID, the messages of KiCad for the results that are not
            ISC_OK, and the number of ISC_OK results with an item that the tool cannot
            read (no ID: the tool cannot tell which item it is).

        Raises:
            IpcError: KiCad refused the request (an error of the overall status).
        """
        k = self._k
        commands = k.board_commands
        command = commands.FlipItems()
        command.header.document.CopyFrom(board.document)
        command.direction = (
            commands.BFD_LEFT_RIGHT if direction == "left_right" else commands.BFD_TOP_BOTTOM
        )
        for item_id in ids:
            command.items.add().value = item_id
        answer = self.send(command, commands.FlipItemsResponse)
        # The overall status: "may return IRS_OK even if no items were flipped". KiCad
        # 10.0.6 does not set it (IRS_UNKNOWN, measured in handleFlipItems): only the
        # documented error values stop the tool.
        statuses = k.item_request_status
        if answer.status not in (statuses.Value("IRS_OK"), statuses.Value("IRS_UNKNOWN")):
            raise IpcError(
                f"KiCad refused the flip ({statuses.Name(answer.status)}). The board did "
                "not change."
            )
        flipped: dict[str, BoardItem] = {}
        messages: list[str] = []
        unreadable = 0
        for result in answer.flipped_items:
            if result.status.code == k.item_ok:
                item = describe_item(k, result.item)
                if item.id:
                    flipped[item.id] = item
                else:
                    unreadable += 1
            else:
                messages.append(result.status.error_message or f"status {result.status.code}")
        return flipped, messages, unreadable

    def project_file(self, board: Board) -> Path | None:
        """Return the project file of the board, from the project of its document
        (ProjectSpecifier: name, "the path to the project directory"), or None when
        KiCad gives no project."""
        project = board.document.project
        if not project.path or not project.name:
            return None
        return Path(project.path) / f"{project.name}.kicad_pro"

    def save_board(self, board: Board) -> None:
        """Save the board in its file (SaveDocument, PCB_EDIT_FRAME::SaveBoard)."""
        command = self._k.editor_commands.SaveDocument()
        command.document.CopyFrom(board.document)
        self.send(command, _empty())

    def refill_zones(self, board: Board) -> None:
        """Start the refill of all zones (RefillZones).

        board_commands.proto: "This is a blocking operation; it will return Empty
        immediately, but KiCad will return ApiStatusCode.AS_BUSY to all future API
        requests until the zone fill has completed." The caller must repeat a request
        while it gets AS_BUSY. (Measured with KiCad 10.0.6 and 10.0.8: the next request
        waits for the end of the fill and gets no AS_BUSY.)"""
        command = self._k.board_commands.RefillZones()
        command.board.CopyFrom(board.document)
        self.send(command, _empty())

    def copper_zones(self, board: Board) -> tuple[int, int]:
        """Return the number of the copper zones, and of the filled copper zones
        (GetItems: the field filled of each zone of the type ZT_COPPER)."""
        k = self._k
        command = k.editor_commands.GetItems()
        command.header.document.CopyFrom(board.document)
        command.types.append(k.object_type.KOT_PCB_ZONE)
        answer = self.send(command, k.editor_commands.GetItemsResponse)
        copper = filled = 0
        for item in answer.items:
            zone = k.unpack_any(item)
            if zone.type == k.zone_copper:
                copper += 1
                filled += bool(zone.filled)
        return copper, filled

    # The selection of the PCB editor. The selection is no change of the board: no
    # commit, no undo step.

    def selection(self, board: Board) -> list[BoardItem]:
        """Return the selected items of the PCB editor (GetSelection)."""
        command = self._k.editor_commands.GetSelection()
        command.header.document.CopyFrom(board.document)
        answer = self.send(command, self._k.editor_commands.SelectionResponse)
        return [describe_item(self._k, item) for item in answer.items]

    def clear_selection(self, board: Board) -> None:
        """Clear the selection of the PCB editor (ClearSelection)."""
        command = self._k.editor_commands.ClearSelection()
        command.header.document.CopyFrom(board.document)
        self.send(command, _empty())

    def add_to_selection(self, board: Board, ids: list[str]) -> list[BoardItem]:
        """Add items to the selection (AddToSelection), and return the new selection.

        KiCad ignores an ID that it does not find (handleAddToSelection)."""
        return self._change_selection(self._k.editor_commands.AddToSelection(), board, ids)

    def remove_from_selection(self, board: Board, ids: list[str]) -> list[BoardItem]:
        """Remove items from the selection (RemoveFromSelection), and return the new
        selection. KiCad ignores an ID that it does not find."""
        return self._change_selection(self._k.editor_commands.RemoveFromSelection(), board, ids)

    def _change_selection(self, command, board: Board, ids: list[str]) -> list[BoardItem]:
        command.header.document.CopyFrom(board.document)
        for item_id in ids:
            command.items.add().value = item_id
        answer = self.send(command, self._k.editor_commands.SelectionResponse)
        return [describe_item(self._k, item) for item in answer.items]


def _empty():
    """The class of the empty answer (google.protobuf.Empty)."""
    from google.protobuf.empty_pb2 import Empty

    return Empty


def _open_documents(kicad: KiCad, errors, document_type) -> list:
    """Return the open documents of a type. A KiCad with no editor of that type gives
    AS_UNHANDLED ("no handler available", measured with KiCad 10.0.6)."""
    try:
        return list(kicad.get_open_documents(document_type))
    except errors.ApiError as error:
        if error.code == AS_UNHANDLED:
            return []
        raise


def _read_state(
    kicad: KiCad, k: _Kipy, version: str | None = None, timeout_ms: int = TIMEOUT_MS
) -> KicadState:
    """Read the version and the open editors of the running KiCad."""
    errors = k.errors
    try:
        if version is None:
            version = kicad.get_version().full_version
        rules = rules_for(version)
        boards = tuple(
            Path(d.project.path) / d.board_filename
            for d in _open_documents(kicad, errors, k.document_type.DOCTYPE_PCB)
        )
        schematic_open = bool(_open_documents(kicad, errors, k.document_type.DOCTYPE_SCHEMATIC))
    except (errors.ConnectionError, errors.ApiError) as error:
        raise _translate(error, errors, "GetVersion and GetOpenDocuments", timeout_ms) from None
    return KicadState(version, rules, boards, schematic_open)


# One IPC session at a time in the server process: kicad-python is not thread-safe, and
# KiCad does the requests of all clients one after the other. Reentrant: a session in a
# session of the same thread gets its own connection.
_SESSION_LOCK = threading.RLock()


def _new_client(k: _Kipy, timeout_ms: int):
    """Make the client of kicad-python for one session: its own client name, the socket
    of socket_path, and the token of kicad-python 0.8.0 (kipy/kicad.py,
    _default_kicad_token)."""
    return k.client_class(
        socket_path()[0],
        f"kicad-workbench-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        os.environ.get("KICAD_API_TOKEN", ""),
        timeout_ms,
    )


@contextmanager
def open_session(timeout_ms: int = TIMEOUT_MS) -> Iterator[IpcSession]:
    """Connect to the running KiCad, and read its version and its open editors.

    Call it in a thread (asyncio.to_thread): each request waits for KiCad, up to
    timeout_ms for each answer.

    Each session has its own connection and its own client name: KiCad keeps a commit
    for each client name (API_HANDLER_EDITOR::m_commits), thus a commit that a session
    could not end does not go to the next session. The session closes the socket at
    its end: the pynng socket and its dialer refer to each other, thus only the cyclic
    garbage collector would close it.

    Raises:
        IpcError: kicad-python is not installed, KiCad does not answer, or the server
            has no IPC rules for the running KiCad.
    """
    k = _import_kipy()
    with _SESSION_LOCK:
        client = _new_client(k, timeout_ms)
        try:
            kicad = k.module.KiCad.from_client(client)
            state = _read_state(kicad, k, timeout_ms=timeout_ms)
            yield IpcSession(k, client, kicad, state, timeout_ms)
        finally:
            _close(client)


def _close(client) -> None:
    """Close the socket of a client of kicad-python 0.8.0.

    A limit of kicad-python 0.8.0, not of the IPC API: KiCadClient has no documented
    close method, thus the client uses the private attribute _conn (the socket, while
    the property connected is true, kipy/client.py). Without the close, the socket stays
    open (measured in the review of step 3A). kicad-python master has KiCadClient.close
    and KiCad.close: use them, and remove this function, with the first release that
    has them. The extra "ipc" pins 0.8.0.
    """
    if client.connected:
        client._conn.close()
