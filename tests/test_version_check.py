"""Tests of the notes about a newer release and a restart of the client."""

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.utilities.types import Image

from kicad_workbench import __version__
from kicad_workbench import version_check as vc

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f"
    b"\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """The state file goes into a temporary directory."""
    monkeypatch.setattr(vc, "cache_home", lambda: tmp_path / "cache")


def enable(monkeypatch, installed="0.1.0"):
    """The check applies: no stop in the environment, an install from an index."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "installed_version", lambda: installed)
    monkeypatch.setattr(vc, "_told", set())


def pypi(monkeypatch, version="0.2.0"):
    """A PyPI that gives version. The list gets the URL of each request."""
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        return Answer(json.dumps({"info": {"version": version}}).encode())

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    return calls


def write_state(latest: str) -> None:
    vc.state_path().parent.mkdir(parents=True, exist_ok=True)
    vc.state_path().write_text(
        json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "latest": latest})
    )


def test_newer_release_from_pypi(monkeypatch):
    enable(monkeypatch)
    calls = pypi(monkeypatch)
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    vc.refresh_latest_version(now)
    vc.refresh_latest_version(now + timedelta(hours=1))  # younger than the interval: no request
    assert calls == [vc.PYPI_URL]
    assert vc.newer_release() == ("0.2.0", "0.1.0")
    notes = vc.version_notes()
    assert any(
        "kicad-workbench 0.2.0 is available on PyPI" in n and "Do not upgrade it yourself" in n
        for n in notes
    )
    assert vc.version_notes() == []  # one time for each server process
    vc.refresh_latest_version(now + timedelta(hours=25))
    assert len(calls) == 2
    # A time before the stored request (a clock that moved back) does not stop the check.
    vc.refresh_latest_version(now - timedelta(hours=1))
    assert len(calls) == 3


def test_failed_request_writes_nothing(monkeypatch):
    enable(monkeypatch)

    def urlopen(request, timeout):
        raise OSError("no network")

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    vc._refresh_in_background()  # the error goes to the debug log only
    assert not vc.state_path().exists()
    pypi(monkeypatch, "not a version")
    with pytest.raises(ValueError):
        vc.refresh_latest_version()
    assert not vc.state_path().exists()


def test_no_check_when_stopped(monkeypatch):
    enable(monkeypatch)
    write_state("9.0")
    assert vc.newer_release() == ("9.0", "0.1.0")
    monkeypatch.setenv(vc.DISABLE_ENV, "1")
    assert vc.newer_release() is None and vc.start_update_check() is None
    monkeypatch.setenv(vc.DISABLE_ENV, "0")  # "0" does not stop the check
    assert vc.newer_release() == ("9.0", "0.1.0")
    monkeypatch.delenv(vc.DISABLE_ENV)
    monkeypatch.setattr(vc, "is_editable_install", lambda: True)  # an editable install: no check
    assert vc.newer_release() is None and vc.start_update_check() is None
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    write_state("0.1.0")  # not newer
    assert vc.newer_release() is None
    vc.state_path().write_text("{broken")
    assert vc.newer_release() is None


def test_no_check_with_no_metadata(monkeypatch):
    """A source tree in PYTHONPATH: no installed version, no request."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "DISTRIBUTION", "kicad-workbench-not-installed")
    calls = pypi(monkeypatch)
    assert vc.is_editable_install() is None
    assert not vc.check_enabled() and vc.start_update_check() is None
    assert calls == []


def test_a_defect_in_the_thread_is_not_hidden(monkeypatch):
    """Only the errors of a request go to the debug log."""

    def refresh():
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "refresh_latest_version", refresh)
    with pytest.raises(RuntimeError):
        vc._refresh_in_background()


def test_start_update_check(monkeypatch):
    enable(monkeypatch)
    calls = pypi(monkeypatch)
    thread = vc.start_update_check()
    assert thread is not None and thread.daemon
    thread.join(5)
    assert calls == [vc.PYPI_URL]
    assert vc.read_state()[1] == "0.2.0"


def test_restart_note(monkeypatch):
    enable(monkeypatch, installed="0.2.0rc1")
    assert "restart the LLM client" in vc.restart_reason("0.1.0")
    assert vc.restart_reason("0.2.0-rc1") is None  # the same version in a different spelling
    monkeypatch.setattr(vc, "is_editable_install", lambda: True)  # its metadata is old
    assert vc.restart_reason("0.1.0") is None
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "installed_version", lambda: None)
    assert vc.restart_reason("0.1.0") is None


def server() -> FastMCP:
    mcp = FastMCP(name="test")
    mcp.add_middleware(vc.VersionNotes())

    @mcp.tool()
    async def text_tool() -> str:
        return "✅ done"

    @mcp.tool()
    async def image_tool() -> list[str | Image] | str:
        # The return type of render_library_item: no output schema.
        return ["picture", Image(data=PNG, format="png")]

    return mcp


async def test_middleware_adds_the_notes_one_time(monkeypatch):
    enable(monkeypatch)
    write_state("0.2.0")
    async with Client(server()) as client:
        first = await client.call_tool("text_tool", {})
        second = await client.call_tool("text_tool", {})
    text = first.content[0].text
    assert text.startswith("✅ done\n\nℹ️ kicad-workbench 0.2.0 is available on PyPI")
    # The structured content and the content have the same text.
    assert first.structured_content == {"result": text}
    assert first.data == text
    assert second.content[0].text == "✅ done"
    assert second.structured_content == {"result": "✅ done"}


async def test_middleware_with_an_image(monkeypatch):
    # An upgrade while the server runs: the restart note.
    enable(monkeypatch, installed=f"{__version__}.post1")
    async with Client(server()) as client:
        result = await client.call_tool("image_tool", {})
    kinds = [item.type for item in result.content]
    assert kinds == ["text", "image", "text"]
    assert result.content[-1].text.startswith(
        f"⚠️ The installed kicad-workbench is {__version__}.post1"
    )


def test_no_check_after_a_note(monkeypatch):
    """After a note of a kind, version_notes does not do its check again."""
    enable(monkeypatch)
    calls = []

    def reason():
        calls.append("restart")
        return "restart"

    def release():
        calls.append("release")
        return ("0.2.0", "0.1.0")

    monkeypatch.setattr(vc, "restart_reason", reason)
    monkeypatch.setattr(vc, "newer_release", release)
    assert len(vc.version_notes()) == 2
    assert vc.version_notes() == []
    assert calls == ["restart", "release"]


async def test_a_defect_of_the_check_gives_a_note(monkeypatch):
    """The tool result is complete, and a note tells about the defect one time."""
    enable(monkeypatch)

    def release():
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "newer_release", release)
    async with Client(server()) as client:
        first = await client.call_tool("text_tool", {})
        second = await client.call_tool("text_tool", {})
    assert first.data.startswith("✅ done\n\n⚠️ The release check of kicad-workbench failed")
    assert "RuntimeError: defect" in first.data
    assert second.data == "✅ done"


def test_server_has_the_middleware():
    from kicad_workbench.server import mcp

    assert any(isinstance(m, vc.VersionNotes) for m in mcp.middleware)


def test_one_source_of_the_version():
    """The build reads the version of the metadata from __version__: the restart
    note compares the two."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11 or later

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
    path = pyproject["tool"]["hatch"]["version"]["path"]
    assert f'__version__ = "{__version__}"' in (root / path).read_text(encoding="utf-8")
