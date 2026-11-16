"""Tests of the IPC session (utils/ipc_client.py) and of the IPC tools with kicad-python.

The tests use kicad-python 0.8.0 (the extra "test") with a fake client: the fake KiCad
answers the protobuf commands as KiCad 10.0.6 answered them in the checks
(spike/ipc/probe_3a.py, spike/kicad-versions/ipc_client_check.py and
ipc_selection_check.py, 2026-11-15). test_ipc_rules.py has the tests that need no
kicad-python.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("kipy")

from google.protobuf.any_pb2 import Any  # noqa: E402
from kipy.errors import ApiError, ConnectionError  # noqa: E402
from kipy.proto.board import (
    board_commands_pb2,  # noqa: E402
    board_types_pb2,  # noqa: E402
)
from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: E402
from kipy.proto.common.commands import editor_commands_pb2  # noqa: E402
from kipy.proto.common.types import (  # noqa: E402
    DocumentType,
    KiCadObjectType,
    base_types_pb2,
)

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
        if name == "GetItems" and list(command.types) == [KiCadObjectType.KOT_PCB_ZONE]:
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


def test_ipc_status_writes_possible(kicad):
    text = run(ipc.ipc_status())
    assert "- Writes: ✅ the board is open" in text
    assert "- Commits: ✅ possible now" in text
    assert "Known KiCad versions of the IPC tools: 10.0.6." in text


def test_ipc_status_with_no_pcb_editor(kicad):
    kicad.board_path = None
    text = run(ipc.ipc_status())
    assert "- PCB editor: not open" in text
    assert "- Writes: ❌ not possible now. no board is open in the PCB editor" in text


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


@pytest.fixture
def flip_setting(monkeypatch, tmp_path):
    """The settings folder of KiCad with pcbnew.json (editing.flip_left_right)."""

    def write(value):
        folder = tmp_path / "config" / "10.0"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pcbnew.json").write_text(
            json.dumps({"editing": {"flip_left_right": value}}), encoding="utf-8"
        )

    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    return write


def test_place_a_footprint_on_the_other_side(kicad, flip_setting):
    flip_setting(True)
    text = place(kicad, [{"reference": "R1", "side": "bottom"}, {"reference": "U3", "side": "top"}])
    assert text.startswith('✅ Flipped 1 footprint in one undo step ("Flipped items via API").')
    assert "Flip: top/bottom around the position of the footprint (editing.flip_left_right" in text
    assert "| R1 | F.Cu | B.Cu | 1.5, -2 |  |" in text
    assert "| U3 | F.Cu | F.Cu |" in text
    assert layer_of(kicad, "R1") == "BL_B_Cu"
    assert kicad.flip_directions == [board_commands_pb2.BFD_TOP_BOTTOM]


def test_the_flip_direction_of_the_setting(kicad, flip_setting):
    """KiCad stores LEFT_RIGHT (0) as false: the name of the key says the reverse."""
    flip_setting(False)
    place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert kicad.flip_directions == [board_commands_pb2.BFD_LEFT_RIGHT]


def test_the_flip_direction_with_no_setting(kicad, monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "none"))
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert kicad.flip_directions == [board_commands_pb2.BFD_LEFT_RIGHT]
    assert "(the default of KiCad (" in text


def test_the_flip_direction_of_the_argument(kicad, flip_setting):
    flip_setting(False)
    text = place(kicad, [{"reference": "R1", "side": "bottom"}], flip_direction="top_bottom")
    assert kicad.flip_directions == [board_commands_pb2.BFD_TOP_BOTTOM]
    assert "(the argument flip_direction)" in text


def test_no_flip_when_the_side_is_the_same(kicad, flip_setting):
    flip_setting(True)
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


def test_place_with_the_schematic_editor_open(kicad, flip_setting):
    """FlipItems has no handler in API_HANDLER_SCH of KiCad 10.0.6: it needs no guard
    (measured with the schematic editor open, ipc_write_check.py)."""
    flip_setting(True)
    kicad.schematic_open = True
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert text.startswith("✅ Flipped 1 footprint")


def test_a_flip_that_kicad_refuses(kicad, flip_setting, monkeypatch):
    flip_setting(True)
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
        "the tool has no rules for them yet: The API has no command"
    )


def locked_footprint(item_id, reference):
    message = board_types_pb2.FootprintInstance()
    footprint(item_id, reference).Unpack(message)
    message.locked = 2  # LS_LOCKED
    return packed(message)


def test_a_locked_footprint(kicad, flip_setting):
    """The Flip of the PCB editor leaves out a locked footprint, except with "Override
    locks" (FilterCollectorForLockedItems of KiCad 10.0.6)."""
    flip_setting(True)
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


def test_no_setting_read_with_no_flip(kicad, monkeypatch):
    def fail(version):
        raise AssertionError("no read of pcbnew.json")

    monkeypatch.setattr(ipc, "flip_direction_setting", fail)
    assert place(kicad, [{"reference": "R1", "side": "top"}]).startswith("✅ No change")


def test_a_setting_file_that_is_not_json(kicad, monkeypatch, tmp_path):
    """JSON_SETTINGS::LoadFromFile keeps the defaults for a file that it cannot parse."""
    folder = tmp_path / "config" / "10.0"
    folder.mkdir(parents=True)
    (folder / "pcbnew.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    text = place(kicad, [{"reference": "R1", "side": "bottom"}])
    assert kicad.flip_directions == [board_commands_pb2.BFD_LEFT_RIGHT]
    assert "the default of KiCad, which cannot read" in text


def test_a_partial_flip(kicad, flip_setting):
    flip_setting(True)
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


def test_flip_results_in_an_other_order(kicad, flip_setting):
    """board_commands.proto gives no order of the results: the item of each result
    gives the ID."""
    flip_setting(True)
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


def test_a_flip_with_an_error_status(kicad, flip_setting):
    """A documented error of the overall status (ItemRequestStatus) stops the tool."""
    flip_setting(True)
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


def test_a_footprint_with_no_lock_state(kicad, flip_setting):
    """LS_UNKNOWN: the tool cannot know if the PCB editor flips it."""
    flip_setting(True)
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


def test_a_flip_result_that_the_tool_cannot_read(kicad, flip_setting, monkeypatch):
    """An ISC_OK result with an item that the tool cannot read: the board changed, but
    the tool cannot tell which footprint."""
    flip_setting(True)
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
