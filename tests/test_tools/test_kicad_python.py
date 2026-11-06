"""Tests of the Python of KiCad as a subprocess (utils/kicad_python.py)."""

import sys

import pytest

from kicad_workbench.utils import kicad_python
from kicad_workbench.utils.kicad_python import find_kicad_python

from .locale_env import can_name, non_utf8_env, run_python


class TestFindKicadPython:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(kicad_python, "_cached_python", None)
        monkeypatch.delenv("KICAD_PYTHON", raising=False)
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: None)

    def test_env_override_wins(self, monkeypatch, tmp_path):
        fake = tmp_path / "python.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("KICAD_PYTHON", str(fake))
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: None)
        assert find_kicad_python() == str(fake)

    def test_current_interpreter_when_pcbnew_importable(self, monkeypatch):
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: object())
        assert find_kicad_python() == sys.executable

    def test_windows_bundled_python(self, monkeypatch, tmp_path):
        exe = tmp_path / "bin" / "python.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: (tmp_path, "10.0"))
        assert find_kicad_python() == str(exe)

    def test_macos_bundled_python(self, monkeypatch, tmp_path):
        contents = tmp_path / "KiCad.app" / "Contents"
        shared_support = contents / "SharedSupport"
        shared_support.mkdir(parents=True)
        exe = (
            contents
            / "Frameworks"
            / "Python.framework"
            / "Versions"
            / "Current"
            / "bin"
            / "python3"
        )
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: (shared_support, "macos"))
        assert find_kicad_python() == str(exe)

    def test_nothing_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kicad_python, "find_kicad_install", lambda: None)
        assert find_kicad_python() is None


class TestRunKicadPython:
    # Text in different scripts. On Linux and macOS an argument holds only the
    # characters of the locale encoding, thus the test uses the texts that
    # the encoding can hold.
    TEXTS = ["电源板", "لوحة", "Плата", "řídicí", "𠀀"]
    CODE = (
        "from kicad_workbench.utils import kicad_python\n"
        "kicad_python.find_kicad_python = lambda: sys.executable\n"
        "script = 'import sys; print(sys.argv[1]); sys.stderr.write(sys.argv[1])'\n"
        "r = kicad_python.run_kicad_python_sync(['-c', script, ARGS[0]])\n"
        "result([r.returncode, r.stdout.decode('utf-8', 'replace'),\n"
        "        r.stderr.decode('utf-8', 'replace')])\n"
    )

    @pytest.mark.parametrize("utf8_mode", [False, True])
    def test_child_uses_utf8_in_a_locale_that_is_not_utf8(self, utf8_mode):
        # The server and the child Python run in the same locale, as on a
        # system that does not use UTF-8. The child must read its arguments
        # as the server writes them, and write its output as UTF-8.
        env, encoding = non_utf8_env()
        texts = [text for text in self.TEXTS if can_name(text, encoding, utf8_mode)]
        assert texts, encoding
        for text in texts:
            returncode, stdout, stderr = run_python(self.CODE, env, utf8_mode, text)
            assert returncode == 0, stderr
            assert stdout.strip() == text
            assert stderr == text
