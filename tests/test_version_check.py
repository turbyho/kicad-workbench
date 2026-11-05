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


REAL_RUNS = vc.runs_installed_code


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """The state file goes into a temporary directory. Each test starts with no
    notes and no defects of an earlier test."""
    monkeypatch.setattr(vc, "cache_home", lambda: tmp_path / "cache")
    monkeypatch.setattr(vc, "_told", set())
    monkeypatch.setattr(vc, "_defects", {})


def enable(monkeypatch, installed="0.1.0"):
    """The check applies: no stop in the environment, an install from an index."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "runs_installed_code", lambda: True)
    monkeypatch.setattr(vc, "installed_version", lambda: installed)
    monkeypatch.setattr(vc, "_told", set())
    monkeypatch.setattr(vc, "_defects", {})


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
    state = vc.state_path().read_text()
    vc.state_path().write_text(state.replace("0.2.0", "0.3.0"))
    assert any("kicad-workbench 0.3.0 is available" in n for n in vc.version_notes())  # a new text
    vc.state_path().write_text(state)
    vc.refresh_latest_version(now + timedelta(hours=25))
    assert len(calls) == 2
    # A time before the stored request (a clock that moved back) does not stop the check.
    vc.refresh_latest_version(now - timedelta(hours=1))
    assert len(calls) == 3


@pytest.mark.parametrize(
    "error",
    [
        OSError("no network"),
        vc.http.client.IncompleteRead(b"{"),
        ValueError("proxy URL with no authority: 'http://'"),
    ],
)
def test_failed_request_writes_nothing(monkeypatch, caplog, error):
    """No network, a stop in the answer, a proxy variable that is not a URL."""
    enable(monkeypatch)

    def urlopen(request, timeout):
        raise error

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()
    vc._refresh_in_background()  # the debug log only
    assert not vc.state_path().exists()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert vc.version_notes() == []


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"info": {"version": "not a version"}}).encode(),
        json.dumps({"info": {"version": None}}).encode(),
        b"\xff",
        b"<html>",
        b"[]",
        b'{"info": []}',
        b"{}",
        b"[" * 100000,
    ],
)
def test_an_answer_with_no_version_is_a_request_error(monkeypatch, caplog, body):
    enable(monkeypatch)
    monkeypatch.setattr(vc.urllib.request, "urlopen", lambda request, timeout: Answer(body))
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()
    vc._refresh_in_background()  # the debug log only
    assert not vc.state_path().exists()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_an_http_error_is_closed(monkeypatch):
    enable(monkeypatch)
    closed = []

    class Error(vc.urllib.error.HTTPError):
        def close(self):
            closed.append(True)
            super().close()

    def urlopen(request, timeout):
        raise Error(vc.PYPI_URL, 503, "unavailable", {}, None)

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()
    assert closed


def test_a_write_error_is_a_defect(monkeypatch):
    """The state file cannot be written: a defect of this machine, not of the network."""
    enable(monkeypatch, installed=__version__)
    pypi(monkeypatch)

    def write(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(vc, "write_atomic", write)
    vc._refresh_in_background()
    (note,) = vc.version_notes()
    assert "failed (OSError: [Errno 28] No space left on device)" in note


def test_a_deep_state_file_cannot_be_read(monkeypatch):
    enable(monkeypatch)
    vc.state_path().parent.mkdir(parents=True, exist_ok=True)
    vc.state_path().write_text("[" * 100000)
    assert vc.read_state() is None


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
    assert not vc.runs_installed_code()
    assert not vc.check_enabled() and vc.start_update_check() is None
    assert calls == []


def test_install_type_read_at_each_call(monkeypatch):
    """The user can change the install while the server runs: no cache. A
    direct_url.json that is not UTF-8 gives an unknown install type."""
    answers = iter(
        [
            '{"dir_info": {"editable": true}}',
            None,
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        ]
    )

    class Distribution:
        def read_text(self, name):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert vc.is_editable_install() is True
    assert vc.is_editable_install() is False
    assert vc.is_editable_install() is None


def test_a_source_tree_in_front_of_an_install(monkeypatch):
    """The metadata of an installed package that does not run: no check."""
    enable(monkeypatch)
    monkeypatch.setattr(vc, "runs_installed_code", REAL_RUNS)
    site = "/elsewhere/site-packages"

    class Distribution:
        def locate_file(self, path):
            return f"{site}/{path}"

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert not vc.runs_installed_code()
    assert not vc.check_enabled()
    assert vc.restart_reason("0.0.1") is None
    site = str(Path(vc.__file__).resolve().parent.parent)  # the metadata of the code that runs
    assert vc.runs_installed_code()


def test_a_defect_in_the_thread_is_not_hidden(monkeypatch, caplog):
    """Only the errors of a request go to the debug log. A defect gives a note,
    and one warning."""
    enable(monkeypatch, installed=__version__)

    def refresh():
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "refresh_latest_version", refresh)
    vc._refresh_in_background()
    vc._refresh_in_background()
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert vc.version_notes() == [
        "⚠️ The version check of kicad-workbench failed (RuntimeError: defect). The result of "
        "the tool is complete. Tell the user one time in this session."
    ]


def test_a_defect_does_not_stop_the_start(monkeypatch):
    enable(monkeypatch)

    def enabled():
        raise RuntimeError("no home directory")

    monkeypatch.setattr(vc, "check_enabled", enabled)
    assert vc.start_update_check() is None
    (note,) = vc.version_notes()  # start and release have the same defect: one note
    assert "failed (RuntimeError: no home directory)" in note


def test_later_defects_go_to_the_debug_log(monkeypatch, caplog):
    """One note and one warning for each check. A later defect of the check, also
    with a different text, goes to the debug log."""
    enable(monkeypatch)
    caplog.set_level("DEBUG", logger=vc.log.name)
    for text in ("a", "b", "a"):
        vc._record("release", RuntimeError(text))
    assert [r.levelname for r in caplog.records] == ["WARNING", "DEBUG", "DEBUG"]
    assert vc._defects == {"release": "RuntimeError: a"}


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


async def test_a_defect_of_one_check_keeps_the_other_notes(monkeypatch):
    """The tool result is complete. Each check has its own guard, and a defect gives
    one note, also when its text is different at each call."""
    enable(monkeypatch)
    count = iter(range(100))

    def reason():
        raise RuntimeError(f"defect {next(count)}")

    monkeypatch.setattr(vc, "restart_reason", reason)
    monkeypatch.setattr(vc, "newer_release", lambda: ("9.0", "0.1.0"))
    async with Client(server()) as client:
        first = await client.call_tool("text_tool", {})
        second = await client.call_tool("text_tool", {})
    lines = first.data.split("\n")
    assert lines[0] == "✅ done"
    assert lines[2].startswith("ℹ️ kicad-workbench 9.0 is available on PyPI")
    assert lines[3].startswith(
        "⚠️ The version check of kicad-workbench failed (RuntimeError: defect 0)"
    )
    assert second.data == "✅ done"


async def test_a_defect_of_the_middleware_keeps_the_result(monkeypatch, caplog):
    enable(monkeypatch)

    def add(result, notes):
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "add_notes", add)
    async with Client(server()) as client:
        result = await client.call_tool("text_tool", {})
    assert result.data == "✅ done"
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_a_distribution_with_no_files_on_disk(monkeypatch):
    class Distribution:
        def locate_file(self, path):
            raise NotImplementedError

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert not vc.runs_installed_code()


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
