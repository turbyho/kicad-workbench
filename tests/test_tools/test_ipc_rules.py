"""Tests of the parts of the IPC client that need no kicad-python: the rules of each
KiCad version, the install check, the socket, and the texts of the IPC tools."""

import pytest

from kicad_workbench.tools import ipc
from kicad_workbench.utils import ipc_client
from kicad_workbench.utils.ipc_client import IpcError


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


def test_ipc_status_with_no_kicad_python(monkeypatch):
    import asyncio

    monkeypatch.setattr(ipc_client, "kicad_python_version", lambda: None)
    text = asyncio.run(ipc.ipc_status())
    assert text.startswith("❌ kicad-python is not installed.")
    assert "Traceback" not in text


def test_the_socket_of_kicad_api_socket(monkeypatch):
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///x/api.sock")
    assert ipc_client.socket_path() == ("ipc:///x/api.sock", "KICAD_API_SOCKET")
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


def test_mm():
    assert ipc._mm(1_500_000) == "1.5"
    assert ipc._mm(-2_000_000) == "-2"
    assert ipc._mm(100_000_001) == "100.000001"
    assert ipc._mm(-1) == "-0.000001"
    assert ipc._mm(0) == "0"
