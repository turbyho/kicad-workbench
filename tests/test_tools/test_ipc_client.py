"""Tests of the IPC session (utils/ipc_client.py) and of the IPC tools with kicad-python.

The tests use kicad-python 0.8.0 (the extra "test") with a fake client: the fake KiCad
answers the protobuf commands as KiCad 10.0.6 answered them in the checks
(spike/ipc/probe_3a.py, spike/kicad-versions/ipc_client_check.py and
ipc_selection_check.py, 2026-11-15). test_ipc_rules.py has the tests that need no
kicad-python.
"""

import asyncio
import os
from pathlib import Path
from typing import get_args

import pytest

pytest.importorskip("kipy")

from google.protobuf.any_pb2 import Any  # noqa: E402
from kipy.errors import ApiError, ConnectionError  # noqa: E402
from kipy.proto.board import (
    board_commands_pb2,  # noqa: E402
    board_pb2,  # noqa: E402
    board_types_pb2,  # noqa: E402
)
from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: E402
from kipy.proto.common.commands import editor_commands_pb2  # noqa: E402
from kipy.proto.common.types import (  # noqa: E402
    DocumentType,
    KiCadObjectType,
    base_types_pb2,
)
from kipy.util.proto import unpack_any  # noqa: E402

from kicad_workbench.tools import ipc  # noqa: E402
from kicad_workbench.utils import ipc_client  # noqa: E402
from kicad_workbench.utils.ipc_client import IpcError  # noqa: E402

NO_HANDLER = "KiCad returned error: no handler available for request of type {}"
KINDS = (
    board_types_pb2.FootprintInstance,
    board_types_pb2.Via,
    board_types_pb2.Track,
    board_types_pb2.Pad,
    board_types_pb2.Zone,
)
# The item types of PCB_SELECTION_TOOL::SelectAllItemsOnNet (KiCad 10.0.6).
NET_SELECTION_KINDS = ("Track", "Arc", "Via", "BoardGraphicShape")


def packed(message) -> Any:
    container = Any()
    container.Pack(message)
    return container


def footprint(item_id: str, reference: str, x_mm: float = 1.5) -> Any:
    item = board_types_pb2.FootprintInstance()
    item.id.value = item_id
    item.reference_field.text.text.text = reference
    item.layer = BoardLayer.BL_F_Cu
    item.position.x_nm = int(x_mm * 1_000_000)
    item.position.y_nm = -2_000_000
    # KiCad 10.0.6 gives the lock state of each footprint (measured: LS_UNLOCKED).
    item.locked = 1  # LS_UNLOCKED
    return packed(item)


def via(item_id: str, net: str) -> Any:
    item = board_types_pb2.Via()
    item.id.value = item_id
    item.net.name = net
    item.position.x_nm = 100_000_001
    item.pad_stack.layers.extend([BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu])
    return packed(item)


def track(item_id: str, net: str) -> Any:
    item = board_types_pb2.Track()
    item.id.value = item_id
    item.net.name = net
    item.layer = BoardLayer.BL_B_Cu
    item.start.x_nm = 3_000_000
    return packed(item)


def pad(item_id: str, net: str) -> Any:
    item = board_types_pb2.Pad()
    item.id.value = item_id
    item.number = "1"
    item.net.name = net
    return packed(item)


def zone(item_id: str, net: str) -> Any:
    item = board_types_pb2.Zone()
    item.id.value = item_id
    item.name = "fill | top"
    item.type = board_types_pb2.ZT_COPPER
    item.copper_settings.net.name = net
    item.layers.append(BoardLayer.BL_F_Cu)
    return packed(item)


def info(item: Any) -> tuple[str, str, str]:
    """The ID, the type and the net of a packed item."""
    for kind in KINDS:
        if item.Is(kind.DESCRIPTOR):
            message = kind()
            item.Unpack(message)
            net = message.copper_settings.net.name if kind is board_types_pb2.Zone else ""
            if hasattr(message, "net"):
                net = message.net.name
            return message.id.value, kind.DESCRIPTOR.name, net
    return "", "", ""


class FakeKiCad:
    """The state of a running KiCad 10.0.6 with a board in the PCB editor."""

    def __init__(self, board_path: Path):
        self.version = "10.0.6"
        self.board_path: Path | None = board_path
        self.schematic_open = False
        self.failure: Exception | None = None
        self.busy = False
        self.footprints = [footprint("fp-r1", "R1"), footprint("fp-u3", "U3", 2.25)]
        self.net_items = {
            "GND": [pad("pad-g", "GND"), via("via-1", "GND"), via("via-2", "GND")],
            "/SDA": [track("trk-1", "/SDA"), zone("zone-1", "/SDA")],
            "+3V3": [],
        }
        self.other_items = {"table-1": Any()}  # an item with no Serialize (no data)
        self.zones = [zone("zone-1", "/SDA"), zone("zone-2", "GND")]
        self.flip_directions: list[int] = []
        self.saves = 0
        self.save_version = 20260206
        self.refills = 0
        # The number of AS_BUSY answers after RefillZones (board_commands.proto: KiCad
        # gives AS_BUSY to each request until the fill has completed).
        self.busy_after_refill = 0
        # Step 3D: the shapes of the board, the commits (action, message), the created
        # items, and a status code for each created item that fails.
        self.shapes: list[Any] = []
        self.commits: list[tuple[int, str]] = []
        self.created: list[Any] = []
        self.deleted: list[str] = []
        self.create_status: int | None = None
        self.request_status = base_types_pb2.IRS_OK
        self.selected: list[str] = []
        self.clients: list = []
        self.requests: list[str] = []
        # A request that fails one time (its name and the error), for the restore of the
        # selection.
        self.fail_once: tuple[str, Exception] | None = None

    def all_items(self) -> list[Any]:
        return self.footprints + [i for items in self.net_items.values() for i in items]

    def item(self, wanted: str) -> Any | None:
        for item in self.all_items():
            if info(item)[0] == wanted:
                return item
        return self.other_items.get(wanted)

    def _footprint(self, wanted: str):
        for index, item in enumerate(self.footprints):
            message = board_types_pb2.FootprintInstance()
            item.Unpack(message)
            if message.id.value == wanted:
                return index, message
        return None

    def answer(self, command, response_type):
        name = type(command).__name__
        self.requests.append(name)
        if self.failure:
            raise self.failure
        if self.fail_once and name == self.fail_once[0]:
            error = self.fail_once[1]
            self.fail_once = None
            raise error
        if name == "GetVersion":
            response = response_type()
            response.version.full_version = self.version
            return response
        if name == "GetOpenDocuments":
            return self._open_documents(command, response_type)
        if self.board_path is None:
            raise ApiError(NO_HANDLER.format(name), code=ipc_client.AS_UNHANDLED)
        if self.busy and name != "GetSelection":
            # GetSelection does not call checkForBusy (KiCad 10.0.6).
            raise ApiError("KiCad returned error: busy", code=ipc_client.AS_BUSY)
        if self.refills and self.busy_after_refill and name != "RefillZones":
            self.busy_after_refill -= 1
            raise ApiError("KiCad returned error: busy", code=ipc_client.AS_BUSY)
        response = response_type()
        if name in STEP_3D_REQUESTS:
            return self._answer_3d(name, command, response)
        if name == "GetItems" and list(command.types) == [KiCadObjectType.KOT_PCB_SHAPE]:
            response.items.extend(self.shapes)
        elif name == "GetItems" and list(command.types) == [KiCadObjectType.KOT_PCB_ZONE]:
            response.items.extend(self.zones)
        elif name == "GetItems":
            assert list(command.types) == [KiCadObjectType.KOT_PCB_FOOTPRINT]
            response.items.extend(self.footprints)
        elif name == "FlipItems":
            self.flip_directions.append(command.direction)
            for item in command.items:
                result = response.flipped_items.add()
                found = self._footprint(item.value)
                if found is None:
                    result.status.code = editor_commands_pb2.ISC_NONEXISTENT
                    result.status.error_message = f"an item with UUID {item.value} does not exist"
                    continue
                index, message = found
                message.layer = (
                    BoardLayer.BL_B_Cu
                    if message.layer == BoardLayer.BL_F_Cu
                    else BoardLayer.BL_F_Cu
                )
                self.footprints[index] = packed(message)
                result.item.CopyFrom(packed(message))
                result.status.code = editor_commands_pb2.ISC_OK
        elif name == "SaveDocument":
            self.saves += 1
            self.board_path.write_text(
                f"(kicad_pcb (version {self.save_version}))", encoding="utf-8"
            )
        elif name == "RefillZones":
            self.refills += 1
            for index, item in enumerate(self.zones):
                message = board_types_pb2.Zone()
                item.Unpack(message)
                message.filled = True
                self.zones[index] = packed(message)
        elif name == "GetNets":
            for net in self.net_items:
                response.nets.add().name = net
        elif name == "GetItemsByNet":
            assert list(command.types) == [
                KiCadObjectType.KOT_PCB_TRACE,
                KiCadObjectType.KOT_PCB_ARC,
                KiCadObjectType.KOT_PCB_VIA,
                KiCadObjectType.KOT_PCB_SHAPE,
            ]
            for net in command.nets:
                response.items.extend(
                    item
                    for item in self.net_items.get(net.name, [])
                    if info(item)[1] in NET_SELECTION_KINDS
                )
        elif name == "ClearSelection":
            self.selected = []
        elif name in ("AddToSelection", "RemoveFromSelection", "GetSelection"):
            ids = [item.value for item in getattr(command, "items", [])]
            if name == "AddToSelection":
                # KiCad ignores an ID that it does not find (handleAddToSelection).
                self.selected += [i for i in ids if self.item(i) and i not in self.selected]
            if name == "RemoveFromSelection":
                self.selected = [i for i in self.selected if i not in ids]
            response.items.extend(self.item(i) for i in self.selected)
        else:
            raise AssertionError(f"no fake answer for {name}")
        return response

    def _answer_3d(self, name, command, response):
        if name == "GetBoardEnabledLayers":
            response.copper_layer_count = 2
            response.layers.extend(
                [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_Edge_Cuts]
            )
        elif name == "GetNetClassForNets":
            for net in command.net:
                netclass = response.classes[net.name]
                netclass.name = "Default"
                netclass.board.clearance.value_nm = 200_000
                netclass.board.track_width.value_nm = 250_000
                stack = netclass.board.via_stack
                stack.drill.diameter.x_nm = stack.drill.diameter.y_nm = 300_000
                layer = stack.copper_layers.add()
                layer.layer = BoardLayer.BL_F_Cu
                layer.size.x_nm = layer.size.y_nm = 600_000
        elif name == "GetGraphicsDefaults":
            layer = response.defaults.layers.add()
            layer.layer = board_pb2.BLC_EDGES
            layer.line_thickness.value_nm = 100_000
        elif name == "BeginCommit":
            response.id.value = "commit-1"
        elif name == "EndCommit":
            self.commits.append((command.action, command.message))
        elif name == "DeleteItems":
            response.status = self.request_status
            for item in command.item_ids:
                result = response.deleted_items.add()
                result.id.value = item.value
                result.status = editor_commands_pb2.IDS_OK
                self.deleted.append(item.value)
        elif name == "CreateItems":
            # KiCad 10.0.6 gives IRS_OK (measured, spike/ipc/probe_create.py).
            response.status = self.request_status
            for index, item in enumerate(command.items):
                result = response.created_items.add()
                if self.create_status is not None and index == len(command.items) - 1:
                    result.status.code = self.create_status
                    result.status.error_message = "refused"
                    continue
                message = unpack_any(item)
                message.id.value = f"new-{len(self.created) + 1}"
                created = packed(message)
                self.created.append(created)
                result.item.CopyFrom(created)
                result.status.code = editor_commands_pb2.ISC_OK
        return response

    def _open_documents(self, command, response_type):
        response = response_type()
        if command.type == DocumentType.DOCTYPE_PCB and self.board_path:
            document = response.documents.add()
            document.type = DocumentType.DOCTYPE_PCB
            document.board_filename = self.board_path.name
            document.project.name = self.board_path.stem
            document.project.path = str(self.board_path.parent)
            return response
        if command.type == DocumentType.DOCTYPE_SCHEMATIC and self.schematic_open:
            # KiCad 10.0.6 gives the file name of the schematic and no project.
            response.documents.add().board_filename = "x.kicad_sch"
            return response
        raise ApiError(NO_HANDLER.format("GetOpenDocuments"), code=ipc_client.AS_UNHANDLED)


class FakeClient:
    """The KiCadClient of kicad-python 0.8.0: send, connected and the socket _conn."""

    def __init__(self, kicad: FakeKiCad, socket_path, client_name):
        self.kicad = kicad
        self.socket_path = socket_path
        self.client_name = client_name
        self.connected = True
        self.closed = False
        self._conn = self

    def close(self):
        self.closed = True

    def send(self, command, response_type):
        return self.kicad.answer(command, response_type)


@pytest.fixture
def kicad(monkeypatch, tmp_path):
    """The fake KiCad of open_session, with the board tmp_path/board.kicad_pcb open."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    fake = FakeKiCad(board)
    real_new_client = ipc_client._new_client

    def new_client(k, timeout_ms):
        real = real_new_client(k, timeout_ms)
        client = FakeClient(fake, real._socket_path, real._client_name)
        client.timeout_ms = timeout_ms
        fake.clients.append(client)
        return client

    monkeypatch.setattr(ipc_client, "_new_client", new_client)
    monkeypatch.setattr(ipc_client, "kicad_python_version", lambda: "0.8.0")
    return fake


def run(coroutine) -> str:
    return asyncio.run(coroutine)


# The session.


def test_kicad_not_running(kicad):
    kicad.failure = ConnectionError("Failed to connect to KiCad: Connection refused")
    with pytest.raises(IpcError, match="KiCad is not running, or its API server is off"):
        with ipc_client.open_session():
            pass


def test_kicad_does_not_answer(kicad):
    kicad.failure = ConnectionError("Error receiving reply from KiCad: Timed out")
    with pytest.raises(
        IpcError,
        match=r"did not answer the request GetVersion and GetOpenDocuments \(Error receiving "
        r"reply from KiCad: Timed out\)\. KiCad waits 30\.0 s .* KiCad stopped or closed",
    ):
        with ipc_client.open_session():
            pass


def test_kicad_busy(kicad):
    kicad.failure = ApiError("KiCad returned error: busy", code=ipc_client.AS_BUSY)
    with pytest.raises(IpcError, match="KiCad is busy: a dialog is open"):
        with ipc_client.open_session():
            pass


def test_kicad_not_ready(kicad):
    """KiCad 10.0.8 answers AS_NOT_READY while the first-run wizard is open (measured)."""
    kicad.failure = ApiError("KiCad returned error: KiCad is not ready to reply", code=4)
    with pytest.raises(IpcError, match="KiCad is not ready to answer: it starts, or a dialog"):
        with ipc_client.open_session():
            pass


def test_a_socket_address_that_nng_refuses(kicad):
    """A path of a Unix socket of more than 108 bytes (measured with nng of pynng 0.9.0)."""
    kicad.failure = ConnectionError("Failed to connect to KiCad: Address invalid")
    with pytest.raises(IpcError, match=r"is not a valid address for nng .* must be short"):
        with ipc_client.open_session():
            pass


def test_an_other_api_error(kicad):
    kicad.failure = ApiError("KiCad returned error: bad", code=3)
    with pytest.raises(IpcError, match="KiCad refused the request GetVersion"):
        with ipc_client.open_session():
            pass


def test_the_running_version_with_no_rules(kicad):
    kicad.version = "10.0.8"
    with pytest.raises(IpcError, match="no IPC rules for KiCad 10.0.8"):
        with ipc_client.open_session():
            pass


def test_the_state_of_kicad(kicad):
    with ipc_client.open_session() as session:
        assert session.state.version == "10.0.6"
        assert session.state.boards == (kicad.board_path,)
        assert not session.state.schematic_open
        assert session.state.commit_problem() is None
        assert session.client.client_name.startswith("kicad-workbench-")


def test_each_session_has_its_own_client_and_closes_it(kicad):
    """KiCad keeps a commit for each client name: a later session must not get it."""
    with ipc_client.open_session():
        with ipc_client.open_session():  # reentrant in the same thread
            pass
    first, second = kicad.clients
    assert first.client_name != second.client_name
    assert first.closed and second.closed


def test_the_session_closes_the_client_on_an_error(kicad):
    kicad.failure = ApiError("busy", code=ipc_client.AS_BUSY)
    with pytest.raises(IpcError):
        with ipc_client.open_session():
            pass
    assert kicad.clients[0].closed


def test_the_session_uses_the_socket(kicad, monkeypatch):
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///x/api.sock")
    with ipc_client.open_session():
        pass
    assert kicad.clients[0].socket_path == "ipc:///x/api.sock"


def test_no_pcb_editor(kicad):
    kicad.board_path = None
    with ipc_client.open_session() as session:
        assert not session.state.pcb_editor_open
        with pytest.raises(IpcError, match="The PCB editor of KiCad is not open"):
            session.board("x.kicad_pcb")


def test_the_pcb_editor_closes_in_the_session(kicad):
    with ipc_client.open_session() as session:
        assert session.state.pcb_editor_open
        kicad.board_path = None
        with pytest.raises(IpcError, match="The PCB editor of KiCad is not open"):
            session.board("x.kicad_pcb")


def test_a_request_with_no_editor(kicad):
    with ipc_client.open_session() as session:
        board = session.board(kicad.board_path)
        kicad.board_path = None
        with pytest.raises(
            IpcError, match=r"KiCad did not handle the request GetItems \(AS_UNHANDLED\)"
        ):
            session.footprints(board)


def test_the_board_of_the_argument(kicad, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with ipc_client.open_session() as session:
        assert session.board("board.kicad_pcb").document.board_filename == "board.kicad_pcb"


def test_an_other_board(kicad, tmp_path):
    with ipc_client.open_session() as session:
        with pytest.raises(IpcError, match=r"The PCB editor has the board .*board\.kicad_pcb, not"):
            session.board(tmp_path / "other.kicad_pcb")


def test_the_guard_of_bug_24966(kicad):
    kicad.schematic_open = True
    with ipc_client.open_session() as session:
        assert "KiCad bug #24966" in session.state.commit_problem()
        with pytest.raises(IpcError, match="close the schematic editor"):
            session.check_commit()


def test_the_guard_reads_the_state_again(kicad):
    """The user opens the schematic editor after the start of the session."""
    with ipc_client.open_session() as session:
        kicad.schematic_open = True
        with pytest.raises(IpcError, match="KiCad bug #24966"):
            session.check_commit()


# The items of the board.


def test_describe_items(kicad):
    k = ipc_client._import_kipy()
    item = ipc_client.describe_item(k, footprint("fp-1", "R1"))
    assert item == ipc_client.BoardItem(
        "FootprintInstance", "fp-1", "R1", "", "F.Cu", (1_500_000, -2_000_000), locked=False
    )
    item = ipc_client.describe_item(k, track("trk-1", "/SDA"))
    assert (item.kind, item.net, item.layer, item.position) == (
        "Track",
        "/SDA",
        "B.Cu",
        (3_000_000, 0),
    )
    item = ipc_client.describe_item(k, via("via-1", "GND"))
    assert (item.net, item.layer) == ("GND", "F.Cu, B.Cu")
    item = ipc_client.describe_item(k, zone("zone-1", "/SDA"))
    assert (item.text, item.net, item.layer) == ("fill | top", "/SDA", "F.Cu")
    field = board_types_pb2.Field()
    field.name = "Value"
    field.text.text.text = "10k"
    assert ipc_client.describe_item(k, packed(field)).text == "Value: 10k"


def test_describe_an_item_with_no_data(kicad):
    """KiCad 10.0.6 gives an empty message for a type with no Serialize (a table)."""
    item = ipc_client.describe_item(ipc_client._import_kipy(), Any())
    assert item.kind == "" and item.problem == ipc_client.NO_DATA


def test_describe_an_unknown_type(kicad):
    unknown = Any(type_url="type.googleapis.com/kiapi.board.types.Table", value=b"")
    item = ipc_client.describe_item(ipc_client._import_kipy(), unknown)
    assert item.kind == "Table"
    assert "kicad-python 0.8.0 does not know the type" in item.problem


STEP_3D_REQUESTS = {
    "GetBoardEnabledLayers",
    "GetNetClassForNets",
    "GetGraphicsDefaults",
    "BeginCommit",
    "EndCommit",
    "CreateItems",
    "DeleteItems",
}


# ipc_status.


def test_ipc_status(kicad, tmp_path):
    kicad.schematic_open = True
    text = run(ipc.ipc_status(str(tmp_path / "board.kicad_pcb")))
    assert "- KiCad: 10.0.6 (running)" in text
    assert f"- PCB editor: open, board `{kicad.board_path}`" in text
    assert "- Schematic editor: open" in text
    assert "✅ open in the PCB editor" in text
    assert "- Writes: ✅ the board is open in the PCB editor. KiCad refuses each" in text
    assert "- Commits: ❌ not possible now. KiCad 10.0.6 stops on a commit" in text
    assert "fixed in KiCad 10.0.7" in text
    assert "## Limits of KiCad 10.0.6" in text
    assert "| KiCad 11.0 (PlaceFootprintFromLibrary, not released) |" in text
    assert "The API gives no data for tables, table cells and reference points." in text
    # RunAction is not an API for clients (editor_commands.proto): no limit names it.
    assert "RunAction" not in text


def test_ipc_status_writes_possible(kicad):
    text = run(ipc.ipc_status())
    assert "- Writes: ✅ the board is open" in text
    assert "- Commits: ✅ possible now" in text
    assert "Known KiCad versions of the IPC tools: 10.0.6." in text


def test_ipc_status_with_no_pcb_editor(kicad):
    kicad.board_path = None
    text = run(ipc.ipc_status())
    assert "- PCB editor: not open" in text
    assert "- Writes: ❌ not possible now. No board is open in the PCB editor." in text
    assert "| No version (master 2026-11-15) |" in text
    assert "KiCad no version" not in text


def test_ipc_status_with_an_other_board(kicad, tmp_path):
    text = run(ipc.ipc_status(str(tmp_path / "other.kicad_pcb")))
    assert "other.kicad_pcb`: ❌ The PCB editor has the board" in text
    assert "- Writes: ❌ not possible now. The PCB editor has the board" in text
    assert "Writes: ✅" not in text


def test_ipc_status_with_a_dialog(kicad):
    """A modal dialog: the API has no request that tells if KiCad is busy (each request
    can get AS_BUSY). The status uses no request of the editor for it, and tells that
    KiCad refuses a request while it is busy."""
    kicad.busy = True
    text = run(ipc.ipc_status())
    assert "KiCad refuses each request while it is busy" in text
    assert "GetBoundingBox" not in kicad.requests


def test_ipc_status_with_no_kicad(kicad):
    kicad.failure = ConnectionError("Failed to connect to KiCad: Connection refused")
    text = run(ipc.ipc_status())
    assert text.startswith("❌ KiCad does not answer on ")
    assert "Traceback" not in text


# ipc_get_selection.


def test_get_selection(kicad):
    kicad.selected = ["fp-r1", "via-1", "zone-1", "table-1"]
    text = run(ipc.ipc_get_selection(str(kicad.board_path)))
    assert text.startswith("# Selection in the PCB editor: 4 items")
    assert "Items: (no data) 1, FootprintInstance 1, Via 1, Zone 1" in text
    assert "| FootprintInstance | R1 |  | F.Cu | 1.5, -2 | fp-r1 |" in text
    assert "| Via |  | GND | F.Cu, B.Cu | 100.000001, 0 | via-1 |" in text
    assert "| Zone | fill \\| top | /SDA | F.Cu |  | zone-1 |" in text
    assert f"| (no data) | ⚠️ {ipc_client.NO_DATA} | | | | |" in text
    assert "⚠️ The tool cannot read 1 of the items (1 of them in the table)." in text


def test_get_selection_of_an_other_board(kicad, tmp_path):
    text = run(ipc.ipc_get_selection(str(tmp_path / "other.kicad_pcb")))
    assert text.startswith("❌ The PCB editor has the board")


# ipc_select_items.


def test_select_references_and_nets(kicad):
    kicad.selected = ["fp-u3"]
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1"], ["GND"]))
    assert text.startswith("✅ The selection has 3 items")
    # The pad of the net is not in the item types of "Select All Tracks in Net".
    assert kicad.selected == ["fp-r1", "via-1", "via-2"]
    assert "ℹ️ A net gives its tracks, arcs, vias and shapes" in text
    assert "The selection filter of the PCB editor does not apply" in text


def test_a_net_with_no_items(kicad):
    text = run(ipc.ipc_select_items(str(kicad.board_path), nets=["+3V3"]))
    assert text.startswith("✅ The selection has 0 items")
    assert "ℹ️ The net +3V3 has no tracks, arcs, vias or shapes." in text


def test_a_missing_reference_or_net_changes_nothing(kicad):
    kicad.selected = ["fp-u3"]
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1", "r9"], ["gnd"]))
    assert text.startswith("❌ The board has no reference r9, no net gnd. The tool did not")
    assert kicad.selected == ["fp-u3"]


def test_a_reference_on_two_footprints(kicad):
    kicad.footprints.append(footprint("fp-r1b", "R1"))
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1", "R1"]))
    assert "⚠️ 2 footprints have the reference R1." in text
    assert kicad.selected == ["fp-r1", "fp-r1b"]


def test_add_remove_and_clear(kicad):
    board = str(kicad.board_path)
    run(ipc.ipc_select_items(board, ["R1"]))
    text = run(ipc.ipc_select_items(board, ["R1", "U3"], mode="add"))
    assert text.startswith("✅ Added 1 item: the selection has 2 items")
    assert kicad.selected == ["fp-r1", "fp-u3"]
    text = run(ipc.ipc_select_items(board, nets=["/SDA"], mode="add"))
    assert text.startswith("✅ Added 1 item: the selection has 3 items")
    assert sorted(kicad.selected) == ["fp-r1", "fp-u3", "trk-1"]
    text = run(ipc.ipc_select_items(board, ["R1"], nets=["/SDA"], mode="remove"))
    assert text.startswith("✅ Removed 2 items: the selection has 1 item")
    assert kicad.selected == ["fp-u3"]
    text = run(ipc.ipc_select_items(board, ["R1"], mode="remove"))
    assert text.startswith("✅ Removed 0 items: the selection has 1 item")
    text = run(ipc.ipc_select_items(board, mode="clear"))
    assert text.startswith("✅ Cleared the selection (1 item): it has 0 items")
    assert kicad.selected == []


def test_add_keeps_the_items_with_no_id(kicad):
    kicad.selected = ["fp-u3", "table-1"]
    text = run(ipc.ipc_select_items(str(kicad.board_path), nets=["/SDA"], mode="add"))
    assert text.startswith("✅ Added 1 item: the selection has 3 items")
    assert kicad.selected == ["fp-u3", "table-1", "trk-1"]


def test_the_selection_comes_back_after_an_error(kicad):
    kicad.selected = ["fp-u3"]
    kicad.fail_once = ("AddToSelection", ConnectionError("Error receiving reply: Timed out"))
    text = run(ipc.ipc_select_items(str(kicad.board_path), nets=["GND"]))
    assert text.startswith("❌ KiCad did not answer the request AddToSelection")
    assert text.endswith("The tool set the selection of the start again.")
    assert kicad.selected == ["fp-u3"]


def test_the_selection_comes_back_after_an_unexpected_error(kicad):
    kicad.selected = ["fp-u3"]
    kicad.fail_once = ("AddToSelection", RuntimeError("decode"))
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1"]))
    assert text.startswith("❌ Error changing the selection: decode")
    assert kicad.selected == ["fp-u3"]


def test_the_restore_can_fail(kicad):
    kicad.selected = ["fp-u3"]
    kicad.fail_once = ("AddToSelection", ConnectionError("Error receiving reply: Timed out"))
    real = kicad.answer

    def answer(command, response_type):
        if type(command).__name__ == "ClearSelection" and kicad.selected == []:
            raise ConnectionError("Error receiving reply: Timed out")
        return real(command, response_type)

    kicad.answer = answer
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1"]))
    assert "it could not set the selection of the start again" in text


@pytest.mark.parametrize(
    "references, nets, mode, message",
    [
        (["R1"], [], "toggle", "mode must be one of replace, add, remove, clear"),
        (["R1"], [], "clear", 'mode "clear" takes no references and no nets'),
        ([], [], "add", 'mode "add" needs references, nets or the two'),
    ],
)
def test_select_arguments(kicad, references, nets, mode, message):
    text = run(ipc.ipc_select_items(str(kicad.board_path), references, nets, mode))
    assert text.startswith(f"❌ {message}")
    assert "Traceback" not in text


def test_select_when_kicad_is_busy(kicad):
    kicad.selected = ["fp-u3"]
    kicad.busy = True
    text = run(ipc.ipc_select_items(str(kicad.board_path), ["R1"]))
    assert text.startswith("❌ KiCad is busy")
    assert kicad.selected == ["fp-u3"]


# ipc_place_footprints.


def place(kicad, placements, **kwargs):
    kwargs.setdefault("flip_direction", "top_bottom")
    return run(
        ipc.ipc_place_footprints(
            str(kicad.board_path), [ipc.FootprintPlacement(**p) for p in placements], **kwargs
        )
    )


def layer_of(kicad, reference):
    for item in kicad.footprints:
        message = board_types_pb2.FootprintInstance()
        item.Unpack(message)
        if message.reference_field.text.text.text == reference:
            return BoardLayer.Name(message.layer)
    return None


def test_place_a_footprint_on_the_other_side(kicad):
    text = place(kicad, [{"reference": "R1", "side": "bottom"}, {"reference": "U3", "side": "top"}])
    assert text.startswith('✅ Flipped 1 footprint in one undo step ("Flipped items via API").')
    assert "Flip: top/bottom around the position of the footprint." in text
    assert "| R1 | F.Cu | B.Cu | 1.5, -2 |  |" in text
    assert "| U3 | F.Cu | F.Cu |" in text
    assert layer_of(kicad, "R1") == "BL_B_Cu"
    assert kicad.flip_directions == [board_commands_pb2.BFD_TOP_BOTTOM]


def test_the_flip_direction_left_right(kicad):
    text = place(kicad, [{"reference": "R1", "side": "bottom"}], flip_direction="left_right")
    assert kicad.flip_directions == [board_commands_pb2.BFD_LEFT_RIGHT]
    assert "Flip: left/right around the position of the footprint." in text


def test_the_flip_direction_is_required():
    """User decision 2026-11-16: the tool has no default direction (the API cannot read
    the direction of the PCB editor)."""
    import inspect

    parameter = inspect.signature(ipc.ipc_place_footprints).parameters["flip_direction"]
    assert parameter.default is inspect.Parameter.empty
    assert set(get_args(parameter.annotation)) == {"left_right", "top_bottom"}


def test_a_flip_direction_that_is_not_known(kicad):
    """The API cannot read the flip direction of the PCB editor: no "setting" value."""
    text = place(kicad, [{"reference": "R1", "side": "bottom"}], flip_direction="setting")
    assert text.startswith('❌ flip_direction must be "left_right" or "top_bottom"')
    assert "FlipItems" not in kicad.requests


def test_no_flip_when_the_side_is_the_same(kicad):
    text = place(kicad, [{"reference": "R1", "side": "top"}])
    assert text.startswith("✅ No change: each footprint is on the side of the request.")
    assert "FlipItems" not in kicad.requests


def test_a_position_needs_a_later_kicad(kicad):
    text = place(kicad, [{"reference": "R1", "side": "bottom"}, {"reference": "U3", "x_mm": 5}])
    assert text.startswith(
        "❌ The tool cannot change the position, the rotation or the lock of a footprint: "
        "KiCad 10.0.6 cannot change them with no loss of data"
    )
    assert "Asked: U3: x_mm." in text
    assert layer_of(kicad, "R1") == "BL_F_Cu"
    assert "FlipItems" not in kicad.requests


@pytest.mark.parametrize(
    "placements, message",
    [
        ([], "placements is empty."),
        (
            [{"reference": "R9", "side": "top"}],
            "The tool changed nothing: R9: the board has no footprint with this reference "
            "(references are case-sensitive).",
        ),
        (
            [{"reference": "R1", "side": "top"}, {"reference": "R1", "side": "bottom"}],
            "placements has more than one entry for R1.",
        ),
    ],
)
def test_place_arguments(kicad, placements, message):
    text = place(kicad, placements)
    assert text.startswith(f"❌ {message}")
    assert "FlipItems" not in kicad.requests


def test_place_a_reference_on_two_footprints(kicad):
    kicad.footprints.append(footprint("fp-r1b", "R1"))
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("❌ The tool changed nothing: R1: 2 footprints have this reference")


def test_place_with_the_schematic_editor_open(kicad):
    """FlipItems has no handler in API_HANDLER_SCH of KiCad 10.0.6: it needs no guard
    (measured with the schematic editor open, ipc_write_check.py)."""
    kicad.schematic_open = True
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("✅ Flipped 1 footprint")


def test_a_flip_that_kicad_refuses(kicad, monkeypatch):
    real = kicad.answer

    def answer(command, response_type):
        if type(command).__name__ == "FlipItems":
            response = response_type()
            result = response.flipped_items.add()
            result.status.code = editor_commands_pb2.ISC_INVALID_TYPE
            result.status.error_message = "items of type X cannot be flipped"
            return response
        return real(command, response_type)

    kicad.answer = answer
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("⚠️ KiCad did not flip 1 footprint.")
    assert "Messages of KiCad: items of type X cannot be flipped." in text
    assert "| R1 | F.Cu | F.Cu | 1.5, -2 | not flipped |" in text


# ipc_save_board.


def test_save_the_board(kicad):
    kicad.board_path.write_text("(kicad_pcb (version 20260206))", encoding="utf-8")
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert text.startswith(f"✅ KiCad saved the board `{kicad.board_path}`")
    assert "⚠️" not in text
    assert kicad.saves == 1


def test_save_a_board_of_an_older_version(kicad):
    """KiCad writes its own version: an older file gets the warning (CLAUDE.md rule 6)."""
    kicad.board_path.write_text("(kicad_pcb (version 20241229))", encoding="utf-8")
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert "⚠️ KiCad changed the file version from 20241229 to 20260206 (KiCad 10.0)." in text
    assert "KiCad 9.0 cannot open the board now." in text


def test_a_save_that_changes_no_file(kicad, monkeypatch):
    real = kicad.answer

    def answer(command, response_type):
        if type(command).__name__ == "SaveDocument":
            return response_type()
        return real(command, response_type)

    kicad.answer = answer
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert text.startswith("⚠️ KiCad answered the save, but the file")


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can write each file"
)
def test_save_into_a_read_only_file(kicad):
    """SavePcbFile opens an error dialog for a file that it cannot write: the tool checks
    the file first."""
    kicad.board_path.chmod(0o444)
    try:
        text = run(ipc.ipc_save_board(str(kicad.board_path)))
    finally:
        kicad.board_path.chmod(0o644)
    assert text.startswith(f"❌ {kicad.board_path} is not writable. The tool did not save.")
    assert kicad.saves == 0


# ipc_refill_zones.


def test_refill_the_zones(kicad):
    text = run(ipc.ipc_refill_zones(str(kicad.board_path)))
    assert text.startswith('ℹ️ KiCad ended "Fill All Zones" in ')
    assert "2 of 2 copper zones have a fill (before: 0)." in text
    assert "cannot see if the user cancelled the fill" in text
    assert kicad.refills == 1
    assert kicad.clients[0].timeout_ms == 120_000


def test_refill_with_a_timeout(kicad):
    """Each answer of the session waits the longer of TIMEOUT_MS and timeout_s."""
    run(ipc.ipc_refill_zones(str(kicad.board_path), timeout_s=5))
    assert kicad.clients[0].timeout_ms == ipc_client.TIMEOUT_MS
    run(ipc.ipc_refill_zones(str(kicad.board_path), timeout_s=600))
    assert kicad.clients[1].timeout_ms == 600_000
    text = run(ipc.ipc_refill_zones(str(kicad.board_path), timeout_s=0))
    assert text.startswith("❌ timeout_s must be 1 to 3600, not 0.")


def test_a_refill_with_as_busy(kicad, monkeypatch):
    """board_commands.proto: AS_BUSY to each request until the fill has completed. The
    tool repeats the request."""
    monkeypatch.setattr(ipc, "REFILL_POLL_S", 0)
    kicad.busy_after_refill = 3
    text = run(ipc.ipc_refill_zones(str(kicad.board_path)))
    assert text.startswith('ℹ️ KiCad ended "Fill All Zones" in ')
    assert "2 of 2 copper zones have a fill (before: 0)." in text
    assert kicad.requests.count("GetItems") == 2 + 3


def test_a_refill_that_stays_busy(kicad, monkeypatch):
    monkeypatch.setattr(ipc, "REFILL_POLL_S", 0.01)
    kicad.busy_after_refill = 10**9
    text = run(ipc.ipc_refill_zones(str(kicad.board_path), timeout_s=1))
    assert text.startswith("❌ KiCad started the fill, but it was still busy after 1 s")


def test_a_refill_when_kicad_is_busy(kicad):
    """A dialog: RefillZones gets AS_BUSY, and the tool starts nothing."""
    kicad.busy = True
    text = run(ipc.ipc_refill_zones(str(kicad.board_path)))
    assert text.startswith("❌ KiCad is busy")
    assert kicad.refills == 0


def test_a_refill_that_does_not_end_in_time(kicad):
    """KiCad 10.0.6 and 10.0.8: the request after RefillZones waits for the end of the
    fill; a fill longer than the timeout gives no answer."""
    real = kicad.answer
    calls = []

    def answer(command, response_type):
        calls.append(type(command).__name__)
        if type(command).__name__ == "GetItems" and "RefillZones" in calls:
            raise ConnectionError("Error receiving reply from KiCad: Timed out")
        return real(command, response_type)

    kicad.answer = answer
    text = run(ipc.ipc_refill_zones(str(kicad.board_path), timeout_s=3))
    assert text.startswith("❌ KiCad started the fill, but it gave no answer in 3 s.")
    assert "look at KiCad" in text


def test_a_position_with_no_limit_of_data_loss(kicad, monkeypatch):
    """A KiCad that keeps the data (10.0.8): the move still needs rules of the tool."""
    rules = ipc_client.IPC_RULES[(10, 0, 6)]
    without = ipc_client.IpcRules(
        rules.version, tuple(k for k in rules.limits if k != "footprint_update_loses_data"), ""
    )
    monkeypatch.setitem(ipc_client.IPC_RULES, (10, 0, 6), without)
    text = place(kicad, [{"reference": "U3", "rotation_deg": 90}])
    assert text.startswith(
        "❌ The tool cannot change the position, the rotation or the lock of a footprint: "
        "The API has no command that moves or rotates a footprint."
    )
    assert "lock" not in text.split("footprint: ", 1)[1].split(" Asked:")[0]
    text = place(kicad, [{"reference": "U3", "locked": True}])
    assert text.startswith(
        "❌ The tool cannot change the position, the rotation or the lock of a footprint: "
        "The tool cannot change the lock of a footprint with KiCad 10.0.6. Asked: U3: locked."
    )


def locked_footprint(item_id, reference):
    message = board_types_pb2.FootprintInstance()
    footprint(item_id, reference).Unpack(message)
    message.locked = 2  # LS_LOCKED
    return packed(message)


def test_a_locked_footprint(kicad):
    """The Flip of the PCB editor leaves out a locked footprint, except with "Override
    locks" (FilterCollectorForLockedItems of KiCad 10.0.6)."""
    kicad.footprints[0] = locked_footprint("fp-r1", "R1")
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("❌ The tool changed nothing: R1: the footprint is locked")
    assert "case-sensitive" not in text
    assert "FlipItems" not in kicad.requests
    text = place(kicad, [{"reference": "R1", "side": "bottom"}], override_locks=True)
    assert text.startswith("✅ Flipped 1 footprint")
    assert "| R1 | F.Cu | B.Cu | 1.5, -2 | locked (override_locks) |" in text


def test_a_locked_footprint_on_its_side(kicad):
    kicad.footprints[0] = locked_footprint("fp-r1", "R1")
    text = place(kicad, [{"reference": "R1", "side": "top"}])
    assert text.startswith("✅ No change")


def test_a_partial_flip(kicad):
    real = kicad.answer

    def answer(command, response_type):
        response = real(command, response_type)
        if type(command).__name__ == "FlipItems":
            response.flipped_items[1].status.code = editor_commands_pb2.ISC_INVALID_TYPE
            response.flipped_items[1].status.error_message = "refused"
        return response

    kicad.answer = answer
    placements = [{"reference": "R1", "side": "bottom"}, {"reference": "U3", "side": "bottom"}]
    text = place(kicad, placements)
    assert text.startswith(
        "⚠️ KiCad did not flip 1 footprint. It flipped 1 footprint in one undo step "
        '("Flipped items via API").'
    )
    assert "Messages of KiCad: refused." in text
    assert "| U3 | F.Cu | F.Cu | 2.25, -2 | not flipped |" in text


def test_flip_results_in_an_other_order(kicad):
    """board_commands.proto gives no order of the results: the item of each result
    gives the ID."""
    real = kicad.answer

    def answer(command, response_type):
        response = real(command, response_type)
        if type(command).__name__ == "FlipItems":
            results = list(response.flipped_items)[::-1]
            del response.flipped_items[:]
            response.flipped_items.extend(results)
        return response

    kicad.answer = answer
    placements = [{"reference": "R1", "side": "bottom"}, {"reference": "U3", "side": "bottom"}]
    text = place(kicad, placements)
    assert text.startswith("✅ Flipped 2 footprints")
    assert "| R1 | F.Cu | B.Cu | 1.5, -2 |  |" in text
    assert "| U3 | F.Cu | B.Cu | 2.25, -2 |  |" in text


def test_a_flip_with_an_error_status(kicad):
    """A documented error of the overall status (ItemRequestStatus) stops the tool."""
    real = kicad.answer

    def answer(command, response_type):
        response = real(command, response_type)
        if type(command).__name__ == "FlipItems":
            response.status = base_types_pb2.IRS_DOCUMENT_NOT_FOUND
        return response

    kicad.answer = answer
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("❌ KiCad refused the flip (IRS_DOCUMENT_NOT_FOUND).")
    assert "Traceback" not in text


def test_save_checks_the_project_file_of_the_document(kicad, tmp_path):
    """The project file comes from the project of the open document (ProjectSpecifier),
    not from the name of the board."""
    project = kicad.board_path.parent / f"{kicad.board_path.stem}.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    project.chmod(0o444)
    try:
        text = run(ipc.ipc_save_board(str(kicad.board_path)))
    finally:
        project.chmod(0o644)
    if os.geteuid() != 0:
        assert text.startswith(f"❌ {project} is not writable. The tool did not save.")
        assert kicad.saves == 0


def test_save_with_an_unreadable_version(kicad, monkeypatch):
    versions = iter([20240108, None])
    monkeypatch.setattr(ipc, "board_file_version", lambda path: next(versions))
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert text.startswith("✅ KiCad saved the board")
    assert "cannot read the file version of the saved board" in text
    assert "None" not in text


def test_a_footprint_with_no_lock_state(kicad):
    """LS_UNKNOWN: the tool cannot know if the PCB editor flips it."""
    message = board_types_pb2.FootprintInstance()
    footprint("fp-r1", "R1").Unpack(message)
    message.locked = 0  # LS_UNKNOWN
    kicad.footprints[0] = packed(message)
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("❌ The tool changed nothing: R1: KiCad gave no lock state")
    assert "FlipItems" not in kicad.requests
    text = place(kicad, [{"reference": "R1", "side": "bottom"}], override_locks=True)
    assert text.startswith("✅ Flipped 1 footprint")


def test_a_reference_with_an_unreadable_footprint(kicad, monkeypatch):
    real = ipc_client.describe_item

    def describe(k, item):
        result = real(k, item)
        if result.text == "R1":
            return ipc_client.BoardItem(kind="Footprint", problem="cannot read")
        return result

    monkeypatch.setattr(ipc_client, "describe_item", describe)
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith(
        "❌ The tool changed nothing: R1: no footprint that the tool can read has this "
        "reference (references are case-sensitive; KiCad gave 1 footprint that the tool "
        "cannot read)."
    )


def test_a_flip_result_that_the_tool_cannot_read(kicad, monkeypatch):
    """An ISC_OK result with an item that the tool cannot read: the board changed, but
    the tool cannot tell which footprint."""
    real = kicad.answer

    def answer(command, response_type):
        response = real(command, response_type)
        if type(command).__name__ == "FlipItems":
            response.flipped_items[0].item.Clear()
        return response

    kicad.answer = answer
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("⚠️ KiCad flipped 1 item that the tool cannot read")
    assert "The board did not change" not in text
    assert "| R1 | F.Cu | F.Cu | 1.5, -2 | not known |" in text


def test_save_a_new_board_file(kicad):
    """A board with no file yet: no version to compare."""
    kicad.board_path.unlink()
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert text.startswith("✅ KiCad saved the board")
    assert "changed the file version" not in text


def test_a_save_with_the_same_bytes_and_time(kicad):
    """A file system with coarse times: the same bytes and time, but a new inode."""
    real = kicad.answer

    def answer(command, response_type):
        if type(command).__name__ == "SaveDocument":
            status = kicad.board_path.stat()
            data = kicad.board_path.read_bytes()
            spare = kicad.board_path.with_suffix(".spare")
            spare.write_bytes(data)
            os.replace(spare, kicad.board_path)
            os.utime(kicad.board_path, ns=(status.st_atime_ns, status.st_mtime_ns))
            return response_type()
        return real(command, response_type)

    kicad.answer = answer
    text = run(ipc.ipc_save_board(str(kicad.board_path)))
    assert text.startswith("✅ KiCad saved the board")


# Step 3D: ipc_add_tracks, ipc_add_zone, ipc_add_board_outline.


def created_messages(kicad):
    return [unpack_any(item) for item in kicad.created]


def add_tracks(kicad, **kwargs):
    kwargs.setdefault("net", "GND")
    return run(ipc.ipc_add_tracks(str(kicad.board_path), **kwargs))


TRACK = {"start": {"x_mm": 0, "y_mm": 0}, "end": {"x_mm": 5, "y_mm": 0}, "layer": "F.Cu"}


def test_add_tracks_with_the_sizes_of_the_netclass(kicad):
    text = add_tracks(
        kicad,
        tracks=[ipc.TrackSegment(**TRACK)],
        vias=[ipc.Point(x_mm=5, y_mm=0)],
    )
    assert text.startswith(
        "✅ KiCad added 1 track and 1 via on the net `GND` in one undo step "
        '("kicad-workbench: tracks and vias on GND").'
    )
    assert "Track width: 0.25 mm (the netclass Default)." in text
    assert "Via: through, size 0.6 mm (the netclass Default), drill 0.3 mm" in text
    assert "⚠️ KiCad ran no DRC" in text
    track, via = created_messages(kicad)
    assert (track.net.name, track.width.value_nm, track.layer) == (
        "GND",
        250_000,
        BoardLayer.BL_F_Cu,
    )
    assert (via.net.name, via.type) == ("GND", board_types_pb2.VT_THROUGH)
    assert via.pad_stack.drill.diameter.x_nm == 300_000
    assert via.pad_stack.copper_layers[0].size.x_nm == 600_000
    # The drill of a through via, as the PCB_VIA constructor: F.Cu to B.Cu, a circle.
    drill = via.pad_stack.drill
    assert (drill.start_layer, drill.end_layer, drill.shape) == (
        BoardLayer.BL_F_Cu,
        BoardLayer.BL_B_Cu,
        board_types_pb2.DS_CIRCLE,
    )
    assert kicad.commits == [
        (editor_commands_pb2.CMA_COMMIT, "kicad-workbench: tracks and vias on GND")
    ]


def test_add_tracks_with_sizes_of_the_arguments(kicad):
    text = add_tracks(
        kicad,
        tracks=[ipc.TrackSegment(**TRACK)],
        vias=[ipc.Point(x_mm=5, y_mm=0)],
        width_mm=0.4,
        via_size_mm=0.8,
        via_drill_mm=0.4,
    )
    assert "Track width: 0.4 mm (the argument width_mm)." in text
    track, via = created_messages(kicad)
    assert track.width.value_nm == 400_000
    assert via.pad_stack.copper_layers[0].size.x_nm == 800_000


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "tracks and vias are empty."),
        ({"net": "gnd", "tracks": [TRACK]}, "The board has no net 'gnd'"),
        (
            {"tracks": [{**TRACK, "layer": "Edge.Cuts"}]},
            "Edge.Cuts: not a copper layer of the board",
        ),
        (
            {"tracks": [{**TRACK, "end": TRACK["start"]}]},
            "A track has the same start and end",
        ),
        (
            {"tracks": [TRACK], "vias": [{"x_mm": 5, "y_mm": 0}], "via_drill_mm": 0.6},
            "The via drill (0.6 mm) must be smaller than the via size (0.6 mm).",
        ),
        (
            {"tracks": [TRACK], "vias": [{"x_mm": 1, "y_mm": 1}]},
            "1 via not at an end of a new track (first: 1, 1): KiCad 10.0.6 cannot add a free via.",
        ),
        ({"tracks": [TRACK], "width_mm": 0}, "width_mm must be more than 0 mm, not 0."),
    ],
)
def test_add_tracks_arguments(kicad, kwargs, message):
    """Each check comes before the commit: CreateItems makes a new net for an unknown
    name (limit unknown_net)."""
    kwargs = dict(kwargs)
    kwargs["tracks"] = [ipc.TrackSegment(**t) for t in kwargs.get("tracks", [])]
    kwargs["vias"] = [ipc.Point(**v) for v in kwargs.get("vias", [])]
    text = add_tracks(kicad, **kwargs)
    assert text.startswith(f"❌ {message}")
    assert "BeginCommit" not in kicad.requests


def test_add_tracks_with_the_schematic_editor_open(kicad):
    """KiCad 10.0.6 stops on a commit while the schematic editor is open (bug #24966)."""
    kicad.schematic_open = True
    text = add_tracks(kicad, tracks=[ipc.TrackSegment(**TRACK)])
    assert text.startswith("❌ KiCad 10.0.6 stops on a commit while the schematic editor is open")
    assert "BeginCommit" not in kicad.requests


def test_an_item_that_kicad_refuses_drops_the_commit(kicad):
    kicad.create_status = editor_commands_pb2.ISC_INVALID_DATA
    text = add_tracks(kicad, tracks=[ipc.TrackSegment(**TRACK)], vias=[ipc.Point(x_mm=5, y_mm=0)])
    assert text.startswith("❌ KiCad created 1 of 2 items (ISC_INVALID_DATA: refused).")
    assert 'The tool dropped the commit (CMA_DROP: "Cancel this commit").' in text
    assert kicad.commits == [(editor_commands_pb2.CMA_DROP, "")]


ZONE = {
    "net": "GND",
    "layer": "F.Cu",
    "outline": [{"x_mm": 0, "y_mm": 0}, {"x_mm": 5, "y_mm": 0}, {"x_mm": 5, "y_mm": 5}],
    "min_thickness_mm": 0.25,
    "connection": "thermal",
    "island_removal": "always",
    "priority": 0,
    "border_style": "hatched_edge",
    "border_pitch_mm": 0.5,
    "thermal_gap_mm": 0.5,
    "thermal_spoke_width_mm": 0.5,
}


def add_zone(kicad, **changes):
    arguments = {**ZONE, **changes}
    arguments["outline"] = [ipc.Point(**p) for p in arguments["outline"]]
    return run(ipc.ipc_add_zone(str(kicad.board_path), **arguments))


def test_add_a_zone(kicad):
    text = add_zone(kicad)
    assert text.startswith(
        "✅ KiCad added a copper zone on the net `GND` in one undo step "
        '("kicad-workbench: zone on GND").'
    )
    assert "| Clearance | none of the zone (0): KiCad uses the current clearance" in text
    assert "ℹ️ The zone has no fill yet." in text
    (zone,) = created_messages(kicad)
    settings = zone.copper_settings
    assert (zone.type, list(zone.layers)) == (board_types_pb2.ZT_COPPER, [BoardLayer.BL_F_Cu])
    # No clearance (0): DRC_ENGINE::EvalRules uses the larger of it and the netclass.
    assert (settings.net.name, settings.clearance.value_nm) == ("GND", 0)
    assert "GetNetClassForNets" not in kicad.requests
    assert settings.connection.zone_connection == board_types_pb2.ZCS_THERMAL
    assert settings.connection.thermal_spokes.gap.value_nm == 500_000
    assert (settings.fill_mode, settings.island_mode) == (
        board_types_pb2.ZFM_SOLID,
        board_types_pb2.IRM_ALWAYS,
    )
    assert (zone.border.style, zone.border.pitch.value_nm) == (
        board_types_pb2.ZBS_DIAGONAL_EDGE,
        500_000,
    )
    assert len(zone.outline.polygons[0].outline.nodes) == 3


def test_add_a_zone_with_an_own_clearance(kicad):
    text = add_zone(kicad, clearance_mm=0.3, connection="solid")
    assert "| Clearance | 0.3 mm of the zone: KiCad uses the larger of it and" in text
    (zone,) = created_messages(kicad)
    assert zone.copper_settings.clearance.value_nm == 300_000
    # KiCad keeps the thermal settings also for a solid connection.
    assert zone.copper_settings.connection.thermal_spokes.gap.value_nm == 500_000


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"thermal_spoke_width_mm": 0.2},
            "Thermal spoke width cannot be smaller than the minimum width.",
        ),
        ({"min_thickness_mm": 0.01}, "min_thickness_mm must be 0.025 mm or more, not 0.01."),
        ({"border_pitch_mm": 3}, "border_pitch_mm must be 0.1 to 2 mm, not 3."),
        ({"clearance_mm": 101}, "clearance_mm must be 0 to 100 mm, not 101."),
        ({"name": "fill | top"}, "The board has a zone with the name 'fill | top'."),
        (
            {
                "outline": [
                    {"x_mm": 0, "y_mm": 0},
                    {"x_mm": 10, "y_mm": 10},
                    {"x_mm": 10, "y_mm": 0},
                    {"x_mm": 0, "y_mm": 10},
                ]
            },
            "outline intersects itself: the edges 1 and 3 have a common point.",
        ),
        ({"min_thickness_mm": float("inf")}, "min_thickness_mm is not a length that KiCad"),
        ({"outline": ZONE["outline"][:2]}, "outline must have 3 or more points, not 2."),
        ({"net": "NO"}, "The board has no net 'NO'"),
        ({"priority": -1}, "priority must be 0 to 4294967295, not -1."),
        ({"priority": 2**32}, "priority must be 0 to 4294967295"),
        ({"net": ""}, "net is empty"),
        (
            {"outline": [*ZONE["outline"], ZONE["outline"][0]]},
            "outline has the same point two times one after the other",
        ),
        (
            {"outline": [{"x_mm": 0, "y_mm": 0}, {"x_mm": 1, "y_mm": 1}, {"x_mm": 2, "y_mm": 2}]},
            "outline has no area",
        ),
        (
            {"outline": [{"x_mm": float("nan"), "y_mm": 0}, *ZONE["outline"][1:]]},
            "A point has no position that KiCad can store",
        ),
    ],
)
def test_add_zone_arguments(kicad, changes, message):
    text = add_zone(kicad, **changes)
    assert text.startswith(f"❌ {message}")
    assert "BeginCommit" not in kicad.requests


def edge_shape(item_id, locked=1):
    shape = board_types_pb2.BoardGraphicShape()
    shape.id.value = item_id
    shape.layer = BoardLayer.BL_Edge_Cuts
    shape.locked = locked  # 1 LS_UNLOCKED, 2 LS_LOCKED
    return packed(shape)


OUTLINE = [ipc.Point(x_mm=x, y_mm=y) for x, y in ((0, 0), (20, 0), (20, 10), (0, 10))]


def test_add_the_board_outline(kicad):
    text = run(ipc.ipc_add_board_outline(str(kicad.board_path), OUTLINE))
    assert text.startswith("✅ KiCad added the board outline (a polygon of 4 corners)")
    assert "Line width: 0.1 mm (the graphics defaults of the board for Edge.Cuts)." in text
    (shape,) = created_messages(kicad)
    assert shape.layer == BoardLayer.BL_Edge_Cuts
    assert shape.shape.attributes.stroke.width.value_nm == 100_000
    assert len(shape.shape.polygon.polygons[0].outline.nodes) == 4


def test_an_outline_on_a_board_with_an_outline(kicad):
    """The tools delete no items (limit delete_results): the step is the PCB editor."""
    kicad.shapes = [edge_shape("edge-1"), edge_shape("edge-2")]
    text = run(ipc.ipc_add_board_outline(str(kicad.board_path), OUTLINE))
    assert text.startswith("❌ The board has 2 shapes on Edge.Cuts. The tool adds an outline")
    assert "delete the Edge.Cuts shapes in the PCB editor" in text
    assert "BeginCommit" not in kicad.requests


def test_a_shape_that_the_tool_cannot_read(kicad):
    """The tool cannot tell the layer of a shape with no data: it changes nothing."""
    kicad.shapes = [Any()]
    text = run(ipc.ipc_add_board_outline(str(kicad.board_path), OUTLINE))
    assert text.startswith("❌ KiCad gave 1 shape of the board that the tool cannot read")
    assert "BeginCommit" not in kicad.requests


def test_a_netclass_with_no_via_stack(kicad, monkeypatch):
    real = ipc_client.IpcSession.netclass

    def netclass(self, net):
        sizes = real(self, net)
        return ipc_client.NetClassSizes(sizes.name, sizes.clearance, sizes.track_width, None, None)

    monkeypatch.setattr(ipc_client.IpcSession, "netclass", netclass)
    text = add_tracks(
        kicad,
        tracks=[ipc.TrackSegment(**TRACK)],
        vias=[ipc.Point(x_mm=5, y_mm=0)],
        via_size_mm=0.6,
        via_drill_mm=0.3,
    )
    assert text.startswith("❌ The netclass Default gives no via stack")
    assert "BeginCommit" not in kicad.requests


def test_an_end_of_commit_that_fails(kicad):
    kicad.fail_once = ("EndCommit", ConnectionError("Error receiving reply: Timed out"))
    text = add_tracks(kicad, tracks=[ipc.TrackSegment(**TRACK)])
    assert text.startswith("❌ KiCad did not answer the request EndCommit")
    assert "the commit can still be open" in text


def test_add_tracks_with_a_width_needs_no_netclass(kicad):
    text = add_tracks(kicad, tracks=[ipc.TrackSegment(**TRACK)], width_mm=0.3)
    assert "Track width: 0.3 mm (the argument width_mm)." in text
    assert "GetNetClassForNets" not in kicad.requests


def test_an_overall_status_that_is_not_ok(kicad):
    """Only IRS_OK passes: a status that KiCad did not set is no success."""
    kicad.request_status = base_types_pb2.IRS_UNKNOWN
    text = add_tracks(kicad, tracks=[ipc.TrackSegment(**TRACK)])
    assert text.startswith("❌ KiCad refused the items (IRS_UNKNOWN).")
    assert kicad.commits == [(editor_commands_pb2.CMA_DROP, "")]


def test_an_interrupt_stays_an_interrupt(kicad):
    """An interrupt during CreateItems, and a drop that fails: the interrupt goes on."""
    real = kicad.answer

    def answer(command, response_type):
        name = type(command).__name__
        if name == "CreateItems":
            raise KeyboardInterrupt
        if name == "EndCommit":
            raise ConnectionError("Error receiving reply: Timed out")
        return real(command, response_type)

    kicad.answer = answer
    with pytest.raises(KeyboardInterrupt):
        with ipc_client.open_session() as session:
            board = session.board(kicad.board_path)
            session.commit_items(board, [session.outline_message([(0, 0), (1, 0), (0, 1)], 1)], "x")
