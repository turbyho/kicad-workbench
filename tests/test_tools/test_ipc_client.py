"""Tests of the IPC client (utils/ipc_client.py) and of ipc_status, with a fake kipy.

The answers of the fake KiCad are the answers that KiCad 10.0.6 gave to
spike/ipc/probe_3a.py (2026-11-15): spike/kicad-versions/ipc_client_check.py compares
the client with the real KiCad.
"""

import asyncio
import types
from pathlib import Path

import pytest

from kicad_workbench.utils import ipc_client
from kicad_workbench.utils.ipc_client import IpcError


class FakeConnectionError(Exception):
    pass


class FakeApiError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


ERRORS = types.SimpleNamespace(ConnectionError=FakeConnectionError, ApiError=FakeApiError)
DOCUMENT_TYPE = types.SimpleNamespace(DOCTYPE_SCHEMATIC=1, DOCTYPE_PCB=3)
NO_HANDLER = FakeApiError(
    "KiCad returned error: no handler available for request of type "
    "kiapi.common.commands.GetOpenDocuments",
    ipc_client.AS_UNHANDLED,
)


def document(path: Path):
    return types.SimpleNamespace(
        board_filename=path.name,
        project=types.SimpleNamespace(name=path.stem, path=str(path.parent)),
    )


class FakeClient:
    """The KiCadClient of kicad-python 0.8.0: the socket is _conn while connected."""

    clients: list = []

    def __init__(self, socket_path, client_name, kicad_token, timeout_ms):
        self.socket_path = socket_path
        self.client_name = client_name
        self.connected = True
        self._conn = types.SimpleNamespace(closed=False)
        self._conn.close = lambda: setattr(self._conn, "closed", True)
        FakeClient.clients.append(self)


class FakeKiCad:
    """A KiCad with an optional board in the PCB editor and an optional schematic editor."""

    board_path: Path | None = None
    schematic_open = False
    version = "10.0.6"
    failure: Exception | None = None
    footprints_failure: Exception | None = None

    @classmethod
    def from_client(cls, client):
        kicad = cls()
        kicad.client = client
        return kicad

    def get_version(self):
        if self.failure:
            raise self.failure
        return types.SimpleNamespace(full_version=self.version)

    def get_open_documents(self, doc_type):
        if self.failure:
            raise self.failure
        if doc_type == DOCUMENT_TYPE.DOCTYPE_PCB and self.board_path:
            return [document(self.board_path)]
        if doc_type == DOCUMENT_TYPE.DOCTYPE_SCHEMATIC and self.schematic_open:
            # KiCad 10.0.6 gives the file name of the schematic and no project.
            return [types.SimpleNamespace(board_filename="x.kicad_sch", project=None)]
        raise NO_HANDLER

    def get_board(self):
        def get_footprints():
            if self.footprints_failure:
                raise self.footprints_failure
            return [object()] * 56

        return types.SimpleNamespace(
            document=document(self.board_path), get_footprints=get_footprints
        )


@pytest.fixture
def kicad(monkeypatch, tmp_path):
    """The fake KiCad of open_session, with the board tmp_path/board.kicad_pcb open."""

    class Instance(FakeKiCad):
        board_path = tmp_path / "board.kicad_pcb"

    Instance.board_path.write_text("(kicad_pcb)", encoding="utf-8")
    FakeClient.clients = []
    kipy = types.SimpleNamespace(
        KiCad=Instance, client=types.SimpleNamespace(KiCadClient=FakeClient)
    )
    monkeypatch.setattr(ipc_client, "_import_kipy", lambda: (kipy, ERRORS, DOCUMENT_TYPE))
    return Instance


def test_rules_of_the_known_version():
    rules = ipc_client.rules_for("10.0.6")
    assert rules.name == "10.0.6"
    assert rules.has_limit("schematic_editor_commit")
    assert all(key in ipc_client.LIMITS for key in rules.limits)


@pytest.mark.parametrize(
    "version", ["10.0.7", "10.0.8", "9.0.9", "10.99.0", "10.0.6-rc1", "10.0.6-12-g1234abc"]
)
def test_a_version_with_no_rules(version):
    with pytest.raises(IpcError, match=f"no IPC rules for KiCad {version}. It knows only"):
        ipc_client.rules_for(version)


def test_no_kicad_python(monkeypatch):
    monkeypatch.setattr(ipc_client, "kicad_python_version", lambda: None)
    with pytest.raises(IpcError, match=r"kicad-python is not installed\. Tell the user: install"):
        ipc_client._import_kipy()


def test_an_other_kicad_python(monkeypatch):
    monkeypatch.setattr(ipc_client, "kicad_python_version", lambda: "0.9.0")
    with pytest.raises(IpcError, match="needs kicad-python 0.8.0, but 0.9.0 is installed"):
        ipc_client._import_kipy()


def test_kicad_not_running(kicad):
    kicad.failure = FakeConnectionError("Failed to connect to KiCad: Connection refused")
    with pytest.raises(IpcError, match="KiCad is not running, or its API server is off"):
        with ipc_client.open_session():
            pass


def test_kicad_does_not_answer(kicad):
    kicad.failure = FakeConnectionError("Error receiving reply from KiCad: Timed out")
    with pytest.raises(
        IpcError,
        match=r"did not answer the request GetVersion and GetOpenDocuments \(Error receiving "
        r"reply from KiCad: Timed out\)\. KiCad waits 2\.0 s .* KiCad stopped or closed",
    ):
        with ipc_client.open_session():
            pass


def test_kicad_busy(kicad):
    kicad.failure = FakeApiError("KiCad returned error: KiCad is busy", ipc_client.AS_BUSY)
    with pytest.raises(IpcError, match="KiCad is busy: a dialog is open"):
        with ipc_client.open_session():
            pass


def test_an_other_api_error(kicad):
    kicad.failure = FakeApiError("KiCad returned error: bad", 3)
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
        assert session.kicad.client.client_name.startswith("kicad-workbench-")


def test_each_session_has_its_own_client_and_closes_it(kicad):
    """KiCad keeps a commit for each client name: a later session must not get it."""
    with ipc_client.open_session():
        with ipc_client.open_session():  # reentrant in the same thread
            pass
    first, second = FakeClient.clients
    assert first.client_name != second.client_name
    assert first._conn.closed and second._conn.closed


def test_the_session_closes_the_client_on_an_error(kicad):
    kicad.failure = FakeApiError("busy", ipc_client.AS_BUSY)
    with pytest.raises(IpcError):
        with ipc_client.open_session():
            pass
    assert FakeClient.clients[0]._conn.closed


def test_the_socket_of_kicad_api_socket(kicad, monkeypatch):
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///x/api.sock")
    with ipc_client.open_session():
        pass
    assert FakeClient.clients[0].socket_path == "ipc:///x/api.sock"
    assert ipc_client.socket_text() == "`ipc:///x/api.sock` (KICAD_API_SOCKET)"


@pytest.mark.parametrize(
    "system, flatpak, expected",
    [
        ("Linux", False, "ipc:///tmp/kicad/api.sock"),
        ("Darwin", False, "ipc:///tmp/kicad/api.sock"),
        ("Linux", True, "ipc://{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"),
        ("Windows", False, "ipc://{temp}\\kicad\\api.sock"),
    ],
)
def test_the_default_socket(monkeypatch, tmp_path, system, flatpak, expected):
    """The rules of _default_socket_path of kicad-python 0.8.0."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ipc_client.platform, "system", lambda: system)
    monkeypatch.setattr(ipc_client, "gettempdir", lambda: "T")
    if flatpak:
        socket = tmp_path / ".var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
        socket.parent.mkdir(parents=True)
        socket.touch()
    path, _ = ipc_client.socket_path()
    assert path == expected.format(home=tmp_path, temp="T")


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
    def unhandled():
        raise NO_HANDLER

    with ipc_client.open_session() as session:
        with pytest.raises(
            IpcError, match="No open editor of KiCad answers the request GetItems. The PCB"
        ):
            session.call("GetItems", unhandled)


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


def test_session_call_translates_the_errors(kicad):
    def busy():
        raise FakeApiError("busy", ipc_client.AS_BUSY)

    with ipc_client.open_session() as session:
        assert session.call("GetItems", lambda: 56) == 56
        with pytest.raises(IpcError, match="KiCad is busy"):
            session.call("GetItems", busy)


def test_ipc_status(kicad, tmp_path):
    from kicad_workbench.tools.ipc import ipc_status

    kicad.schematic_open = True
    text = asyncio.run(ipc_status(str(tmp_path / "board.kicad_pcb")))
    assert "- KiCad: 10.0.6 (running)" in text
    assert f"- PCB editor: open, board `{kicad.board_path}`" in text
    assert "- Schematic editor: open" in text
    assert "✅ open in the PCB editor" in text
    assert "- Footprints on the board: 56" in text
    assert "- Writes: ❌ not possible now. KiCad 10.0.6 stops on a commit" in text
    assert "fixed in KiCad 10.0.7" in text
    assert "## Limits of KiCad 10.0.6" in text
    assert "| KiCad 11.0 (PlaceFootprintFromLibrary, not released) |" in text


def test_ipc_status_writes_possible(kicad):
    from kicad_workbench.tools.ipc import ipc_status

    text = asyncio.run(ipc_status())
    assert "- Footprints on the board: 56" in text
    assert "- Writes: ✅ possible now" in text
    assert "Known KiCad versions of the IPC tools: 10.0.6." in text


def test_ipc_status_with_no_pcb_editor(kicad):
    from kicad_workbench.tools.ipc import ipc_status

    kicad.board_path = None
    text = asyncio.run(ipc_status())
    assert "- PCB editor: not open" in text
    assert "- Writes: ❌ not possible now. no board is open in the PCB editor" in text


def test_ipc_status_with_an_other_board(kicad, tmp_path):
    from kicad_workbench.tools.ipc import ipc_status

    text = asyncio.run(ipc_status(str(tmp_path / "other.kicad_pcb")))
    assert "other.kicad_pcb`: ❌ The PCB editor has the board" in text
    assert "- Writes: ❌ not possible now. The PCB editor has the board" in text
    assert "✅" not in text


def test_ipc_status_with_a_dialog(kicad):
    """A modal dialog: GetVersion and GetOpenDocuments answer, GetItems gives AS_BUSY
    (measured with KiCad 10.0.6)."""
    from kicad_workbench.tools.ipc import ipc_status

    kicad.footprints_failure = FakeApiError("busy", ipc_client.AS_BUSY)
    text = asyncio.run(ipc_status())
    assert "- Board: ❌ KiCad is busy" in text
    assert "- Writes: ❌ not possible now. KiCad is busy" in text


def test_ipc_status_with_no_kicad(kicad):
    from kicad_workbench.tools.ipc import ipc_status

    kicad.failure = FakeConnectionError("Failed to connect to KiCad: Connection refused")
    text = asyncio.run(ipc_status())
    assert text.startswith("❌ KiCad does not answer on ")
    assert "Traceback" not in text
