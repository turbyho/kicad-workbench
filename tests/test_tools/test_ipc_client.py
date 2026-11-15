"""Tests of the IPC session (utils/ipc_client.py) and of the IPC tools with kicad-python.

The tests use kicad-python 0.8.0 (the extra "test") with a fake client: the fake KiCad
answers the protobuf commands as KiCad 10.0.6 answered them in the checks
(spike/ipc/probe_3a.py, spike/kicad-versions/ipc_client_check.py and
ipc_selection_check.py, 2026-11-15). test_ipc_rules.py has the tests that need no
kicad-python.
"""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("kipy")

from google.protobuf.any_pb2 import Any  # noqa: E402
from kipy.errors import ApiError, ConnectionError  # noqa: E402
from kipy.proto.board import board_types_pb2  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: E402
from kipy.proto.common.types import DocumentType, KiCadObjectType  # noqa: E402

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
        response = response_type()
        if name == "GetItems":
            assert list(command.types) == [KiCadObjectType.KOT_PCB_FOOTPRINT]
            response.items.extend(self.footprints)
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
        elif name == "GetBoundingBox":
            assert list(command.items) == []
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

    def new_client(k):
        real = real_new_client(k)
        client = FakeClient(fake, real._socket_path, real._client_name)
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
        r"reply from KiCad: Timed out\)\. KiCad waits 2\.0 s .* KiCad stopped or closed",
    ):
        with ipc_client.open_session():
            pass


def test_kicad_busy(kicad):
    kicad.failure = ApiError("KiCad returned error: busy", code=ipc_client.AS_BUSY)
    with pytest.raises(IpcError, match="KiCad is busy: a dialog is open"):
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
        assert session.state.write_problem() is None
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
            IpcError, match="No open editor of KiCad answers the request GetItems. The PCB"
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
        assert "KiCad bug #24966" in session.state.write_problem()
        with pytest.raises(IpcError, match="close the schematic editor"):
            session.check_write()


def test_the_guard_reads_the_state_again(kicad):
    """The user opens the schematic editor after the start of the session."""
    with ipc_client.open_session() as session:
        kicad.schematic_open = True
        with pytest.raises(IpcError, match="KiCad bug #24966"):
            session.check_write()


# The items of the board.


def test_describe_items(kicad):
    k = ipc_client._import_kipy()
    item = ipc_client.describe_item(k, footprint("fp-1", "R1"))
    assert item == ipc_client.BoardItem(
        "FootprintInstance", "fp-1", "R1", "", "F.Cu", (1_500_000, -2_000_000)
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
    assert "- KiCad busy: no" in text
    assert "- Writes: ❌ not possible now. KiCad 10.0.6 stops on a commit" in text
    assert "fixed in KiCad 10.0.7" in text
    assert "## Limits of KiCad 10.0.6" in text
    assert "| KiCad 11.0 (PlaceFootprintFromLibrary, not released) |" in text
    assert "The API gives no data for tables, table cells and reference points." in text


def test_ipc_status_writes_possible(kicad):
    text = run(ipc.ipc_status())
    assert "- Writes: ✅ possible now" in text
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
    assert "✅" not in text


def test_ipc_status_with_a_dialog(kicad):
    """A modal dialog: GetVersion and GetOpenDocuments answer, GetBoundingBox gives
    AS_BUSY (handleGetBoundingBox calls checkForBusy first)."""
    kicad.busy = True
    text = run(ipc.ipc_status())
    assert "- Board: ❌ KiCad is busy" in text
    assert "- Writes: ❌ not possible now. KiCad is busy" in text


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
