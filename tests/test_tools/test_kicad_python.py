"""Tests of the Python of KiCad as a subprocess (utils/kicad_python.py)."""

import sys

import pytest

from kicad_workbench.utils import kicad_python, kicad_version
from kicad_workbench.utils.kicad_python import find_kicad_python

from .locale_env import can_name, non_utf8_env, run_python


class TestFindKicadPython:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(kicad_python, "_cached_python", None)
        monkeypatch.delenv("KICAD_PYTHON", raising=False)
        monkeypatch.delenv("KICAD_CLI", raising=False)
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: None)

    def test_env_override_wins(self, monkeypatch, tmp_path):
        fake = tmp_path / "python.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("KICAD_PYTHON", str(fake))
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: pytest.fail("cli"))
        assert find_kicad_python() == str(fake)

    def test_a_wrong_override_is_an_error(self, monkeypatch, tmp_path):
        wrong = tmp_path / "none"
        monkeypatch.setenv("KICAD_PYTHON", str(wrong))
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: object())
        assert find_kicad_python() is None
        assert kicad_python.no_kicad_python_message().startswith(
            f"The environment variable KICAD_PYTHON gives {wrong}, but this path is not a file."
        )

    def test_current_interpreter_when_pcbnew_importable(self, monkeypatch):
        monkeypatch.setattr(kicad_python.importlib.util, "find_spec", lambda name: object())
        assert find_kicad_python() == sys.executable

    def test_windows_python_of_the_kicad_cli(self, monkeypatch, tmp_path):
        """The Python of the installation of kicad-cli: the same KiCad."""
        cli = tmp_path / "9.0" / "bin" / "kicad-cli.exe"
        cli.parent.mkdir(parents=True)
        (cli.parent / "python.exe").write_bytes(b"")
        monkeypatch.setattr(
            kicad_python, "kicad_python_of", lambda c: kicad_version.kicad_python_of(c, "win32")
        )
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: str(cli))
        assert find_kicad_python() == str(cli.parent / "python.exe")

    def test_macos_python_of_the_kicad_cli(self, monkeypatch, tmp_path):
        contents = tmp_path / "KiCad.app" / "Contents"
        cli = contents / "MacOS" / "kicad-cli"
        cli.parent.mkdir(parents=True)
        exe = contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin"
        exe.mkdir(parents=True)
        (exe / "python3").write_bytes(b"")
        monkeypatch.setattr(
            kicad_python, "kicad_python_of", lambda c: kicad_version.kicad_python_of(c, "darwin")
        )
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: str(cli))
        assert find_kicad_python() == str(exe / "python3")

    def test_linux_python_of_the_prefix(self, tmp_path):
        """<prefix>/bin/kicad-cli: the Python of the prefix. A start script with a different
        name (a second installation) has no known Python."""
        cli = tmp_path / "bin" / "kicad-cli"
        cli.parent.mkdir()
        cli.write_bytes(b"")
        assert kicad_version.kicad_python_of(cli, "linux") == [tmp_path / "bin" / "python3"]
        script = tmp_path / "bin" / "kicad-cli-nightly"
        script.write_bytes(b"")
        assert kicad_version.kicad_python_of(script, "linux") == []

    def test_a_removed_python_in_the_cache(self, monkeypatch, tmp_path):
        """KiCad was removed while the server runs: the cache gives no file."""
        gone = tmp_path / "python.exe"
        monkeypatch.setattr(kicad_python, "_cached_python", str(gone))
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: None)
        assert find_kicad_python() is None

    def test_no_kicad_cli_gives_no_python(self, monkeypatch):
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: None)
        assert find_kicad_python() is None

    def test_the_message_of_a_wrong_kicad_cli(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KICAD_CLI", str(tmp_path / "none"))
        monkeypatch.setattr(kicad_python, "find_kicad_cli", lambda: None)
        assert kicad_python.no_kicad_python_message().startswith(
            "The environment variable KICAD_CLI gives"
        )


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


class TestCheckSameKicad:
    def test_one_kicad(self):
        kicad_python.check_same_kicad("10.0.6", "pcbnew", "10.0.7")  # one major and minor

    def test_an_other_kicad(self):
        with pytest.raises(kicad_python.KicadPythonError) as error:
            kicad_python.check_same_kicad("9.0.9", "the Python with pcbnew", "10.0.6")
        assert str(error.value).startswith(
            "kicad-cli is KiCad 10.0.6, but the Python with pcbnew is KiCad 9.0.9."
        )
