"""Tests for the kicad-cli wrapper (utils/kicad_cli)."""

import asyncio
import json
import locale
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_workbench.utils import kicad_cli
from kicad_workbench.utils.kicad_cli import (
    KicadCliError,
    decode_output,
    find_kicad_cli,
    kicad_cli_env,
    run_kicad_cli,
    run_kicad_cli_sync,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Start every test with a clean executable cache and no override."""
    monkeypatch.setattr(kicad_cli, "_cached_exe", None)
    monkeypatch.delenv("KICAD_CLI", raising=False)
    yield
    # monkeypatch restores _cached_exe's original value (None) on teardown.


class TestFindKicadCli:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        fake = tmp_path / "kicad-cli.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("KICAD_CLI", str(fake))
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: None)
        assert find_kicad_cli() == str(fake)

    def test_env_override_ignored_when_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KICAD_CLI", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: None)
        assert find_kicad_cli() is None

    def test_path_lookup(self, monkeypatch, tmp_path):
        on_path = tmp_path / "kicad-cli"
        monkeypatch.setattr(
            kicad_cli.shutil, "which", lambda name: str(on_path) if name == "kicad-cli" else None
        )
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: None)
        assert find_kicad_cli() == str(on_path)

    def test_install_dir_fallback_windows_layout(self, monkeypatch, tmp_path):
        """bin/kicad-cli(.exe) next to the detected share/kicad directory."""
        # The wrapper looks for kicad-cli.exe only on Windows; the bare name
        # elsewhere. Mirror that so the test exercises the branch its platform
        # actually uses (the CI matrix covers both).
        exe_name = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
        exe = tmp_path / "bin" / exe_name
        exe.parent.mkdir()
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: (tmp_path, "10.0"))
        assert find_kicad_cli() == str(exe)

    def test_install_dir_fallback_macos_layout(self, monkeypatch, tmp_path):
        """macOS: install_path is .../SharedSupport, binary in sibling MacOS/."""
        contents = tmp_path / "KiCad.app" / "Contents"
        shared_support = contents / "SharedSupport"
        shared_support.mkdir(parents=True)
        exe = contents / "MacOS" / "kicad-cli"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: (shared_support, "macos"))
        assert find_kicad_cli() == str(exe)

    def test_install_dir_without_binary_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: (tmp_path, "10.0"))
        assert find_kicad_cli() is None

    def test_nothing_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: None)
        assert find_kicad_cli() is None


class TestRunKicadCli:
    def test_sync_raises_when_not_found(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: None)
        with pytest.raises(FileNotFoundError):
            run_kicad_cli_sync(["version"])

    def test_async_raises_when_not_found(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: None)
        with pytest.raises(FileNotFoundError):
            asyncio.run(run_kicad_cli(["version"]))

    def test_not_found_is_kicad_not_found_error(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: None)
        with pytest.raises(kicad_cli.KicadNotFoundError) as error:
            run_kicad_cli_sync(["version"])
        assert str(error.value) == kicad_cli.NO_KICAD
        assert "Tell the user: install KiCad 8 or later" in kicad_cli.NO_KICAD

    def test_a_removed_kicad_cli_in_the_cache(self, monkeypatch, tmp_path):
        """KiCad was removed while the server runs: the cache gives no file."""
        gone = tmp_path / "kicad-cli"
        gone.write_text("", encoding="utf-8")
        monkeypatch.setattr(kicad_cli, "_cached_exe", str(gone))
        gone.unlink()
        monkeypatch.setattr(kicad_cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(kicad_cli, "find_kicad_install", lambda: None)
        assert find_kicad_cli() is None
        with pytest.raises(kicad_cli.KicadNotFoundError):
            run_kicad_cli_sync(["version"])

    def test_the_message_of_a_kicad_cli_that_fails(self, monkeypatch):
        """kicad-cli is there but gives no version: the message gives the reason, not
        the step to install KiCad."""
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        monkeypatch.setattr(
            kicad_cli,
            "run_kicad_cli_sync",
            lambda args, timeout: subprocess.CompletedProcess(args, 2, b"", b"no display"),
        )
        assert kicad_cli.kicad_cli_version_sync() is None
        message = kicad_cli.no_kicad_message()
        assert message.startswith(
            f"kicad-cli did not give its version: {sys.executable} gave the exit code 2: "
            "no display."
        )
        assert "install KiCad" not in message
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: None)
        monkeypatch.setattr(kicad_cli, "run_kicad_cli_sync", run_kicad_cli_sync)
        assert kicad_cli.kicad_cli_version_sync() is None
        assert kicad_cli.no_kicad_message() == kicad_cli.NO_KICAD

    def test_runs_executable_off_event_loop(self, monkeypatch):
        """End-to-end subprocess execution using the current interpreter."""
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        code = "import sys; sys.stdout.write('wrapper-ok')"
        result = asyncio.run(run_kicad_cli(["-c", code], timeout=30))
        assert result.returncode == 0
        assert b"wrapper-ok" in result.stdout

    def test_nonzero_exit_is_surfaced(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        result = asyncio.run(run_kicad_cli(["-c", "import sys; sys.exit(3)"], timeout=30))
        assert result.returncode == 3

    @pytest.mark.skipif(
        not Path("C:/Program Files/KiCad").is_dir(), reason="KiCad not installed"
    )
    def test_real_kicad_cli_version(self):
        """On this machine the wrapper must find and run the real kicad-cli."""
        result = run_kicad_cli_sync(["version"], timeout=30)
        assert result.returncode == 0
        assert result.stdout.decode(errors="replace").strip()


class TestCharacterSet:
    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        # The tests replace child_ctype, thus keep the cache_clear of the
        # function of the module.
        caches = [kicad_cli.child_ctype.cache_clear, kicad_cli._locale_exists.cache_clear]
        for cache_clear in caches:
            cache_clear()
        yield
        for cache_clear in caches:
            cache_clear()

    def test_no_change_on_windows(self, monkeypatch):
        # kicad-cli uses the UTF-8 code page of its manifest and Unicode
        # arguments on Windows.
        monkeypatch.setattr(kicad_cli.sys, "platform", "win32")
        assert kicad_cli_env() is None
        assert kicad_cli_env(english=True) is None
        assert decode_output("电源 لوحة".encode()) == "电源 لوحة"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_english_removes_the_locale_of_the_user(self, monkeypatch):
        # "C" exists on each system. LC_NUMERIC of the user would give a
        # decimal comma.
        monkeypatch.setattr(kicad_cli, "child_ctype", lambda: "zh_CN.UTF-8")
        monkeypatch.setenv("LC_ALL", "cs_CZ.UTF-8")
        monkeypatch.setenv("LC_NUMERIC", "de_DE.UTF-8")
        monkeypatch.setenv("LC_MESSAGES", "zh_CN.UTF-8")
        monkeypatch.setenv("LANGUAGE", "zh_CN")
        env = kicad_cli_env(english=True)
        assert env["LANG"] == "C" and env["LANGUAGE"] == ""
        assert env["LC_CTYPE"] == "zh_CN.UTF-8"
        assert not [name for name in env if name.startswith("LC_") and name != "LC_CTYPE"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_same_character_set_keeps_the_environment(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "child_ctype", lambda: locale.setlocale(locale.LC_CTYPE))
        assert kicad_cli_env() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_other_character_set_replaces_lc_all(self, monkeypatch):
        # LC_ALL overrides LC_CTYPE. Its value stays for the other categories.
        monkeypatch.setattr(kicad_cli, "child_ctype", lambda: "C.UTF-8")
        monkeypatch.setattr(kicad_cli.locale, "setlocale", lambda *args: "zh_CN.GBK")
        monkeypatch.setenv("LC_ALL", "zh_CN.GBK")
        env = kicad_cli_env()
        assert "LC_ALL" not in env
        assert env["LANG"] == "zh_CN.GBK" and env["LC_CTYPE"] == "C.UTF-8"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_ctype_in_utf8_mode_with_a_locale_that_is_not_utf8(self, monkeypatch):
        # This process writes the paths as UTF-8, thus kicad-cli needs a
        # UTF-8 locale that exists.
        monkeypatch.setattr(kicad_cli, "fs_encoding", lambda: "utf-8")
        monkeypatch.setattr(kicad_cli.locale, "setlocale", lambda *args: "zh_CN.GBK")
        monkeypatch.setattr(kicad_cli.locale, "nl_langinfo", lambda item: "GBK")
        monkeypatch.setattr(kicad_cli, "_locale_exists", lambda name: name == "C.utf8")
        assert kicad_cli.child_ctype() == "C.utf8"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_no_utf8_locale(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "fs_encoding", lambda: "utf-8")
        monkeypatch.setattr(kicad_cli.locale, "setlocale", lambda *args: "zh_CN.GBK")
        monkeypatch.setattr(kicad_cli.locale, "nl_langinfo", lambda item: "GBK")
        monkeypatch.setattr(kicad_cli, "_locale_exists", lambda name: False)
        assert kicad_cli.child_ctype() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_no_utf8_locale_refuses_a_path_that_is_not_ascii(self, monkeypatch):
        # The path bytes would be UTF-8, but kicad-cli would read them as GBK.
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        monkeypatch.setattr(kicad_cli, "child_ctype", lambda: None)
        with pytest.raises(KicadCliError, match="no UTF-8 locale"):
            run_kicad_cli_sync(["-c", "pass", "电源板.kicad_sch"], timeout=30)
        # ASCII is the same in the two character sets.
        assert run_kicad_cli_sync(["-c", "pass", "board.kicad_sch"], timeout=30).returncode == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_locale_probe_that_cannot_run(self, monkeypatch):
        monkeypatch.setattr(kicad_cli.sys, "executable", "")
        assert kicad_cli._locale_exists("C.UTF-8") is False
        monkeypatch.setattr(kicad_cli.sys, "executable", "/nonexistent/python")
        assert kicad_cli._locale_exists("en_US.UTF-8") is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_ctype_is_kept_when_the_character_sets_agree(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "fs_encoding", lambda: "gbk")
        monkeypatch.setattr(kicad_cli.locale, "setlocale", lambda *args: "zh_CN.GBK")
        monkeypatch.setattr(kicad_cli.locale, "nl_langinfo", lambda item: "GBK")
        assert kicad_cli.child_ctype() == "zh_CN.GBK"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX locales")
    def test_output_uses_the_encoding_of_the_paths(self, monkeypatch):
        monkeypatch.setattr(kicad_cli, "fs_encoding", lambda: "gbk")
        assert decode_output("电源".encode("gbk")) == "电源"

    def test_no_output(self):
        assert decode_output(None) == ""
        assert decode_output(b"") == ""


class TestEnglishSettings:
    """kicad-cli takes its language from system.language of kicad_common.json
    (PGM_BASE::SetLanguage), thus an English kicad-cli gets a copy of the
    settings with the language English."""

    @pytest.fixture
    def settings(self, monkeypatch, tmp_path):
        home = tmp_path / "user"
        version_dir = home / "10.0"
        (version_dir / "colors").mkdir(parents=True)
        (version_dir / "colors" / "theme.json").write_text("{}", encoding="utf-8")
        (version_dir / "sym-lib-table").write_text("(sym_lib_table)", encoding="utf-8")
        common = {"system": {"language": "Čeština", "x": 1}, "environment": {"vars": {"A": "b"}}}
        (version_dir / "kicad_common.json").write_text(
            json.dumps(common, ensure_ascii=False), encoding="utf-8"
        )
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(home))
        return version_dir

    @pytest.mark.parametrize(
        ("platform", "language"),
        [("linux", "Default"), ("darwin", "English"), ("win32", "English")],
    )
    def test_copy_with_the_language_english(
        self, settings, tmp_path, monkeypatch, platform, language
    ):
        # Unix: "Default", thus LANG=C of kicad_cli_env and the LC_CTYPE of the paths
        # apply (a language of the settings sets LC_CTYPE too).
        monkeypatch.setattr(kicad_cli.sys, "platform", platform)
        copy = tmp_path / "copy"
        kicad_cli.english_settings(copy, "10.0.6")
        common = json.loads((copy / "10.0" / "kicad_common.json").read_text(encoding="utf-8"))
        assert common == {
            "system": {"language": language, "x": 1},
            "environment": {"vars": {"A": "b"}},
        }
        assert (copy / "10.0" / "sym-lib-table").read_text(encoding="utf-8") == "(sym_lib_table)"
        assert (copy / "10.0" / "colors" / "theme.json").is_file()
        # The settings of the user do not change.
        user = json.loads((settings / "kicad_common.json").read_text(encoding="utf-8"))
        assert user["system"]["language"] == "Čeština"

    def test_settings_that_cannot_be_read(self, settings, tmp_path):
        # No default settings in place of the settings of the user: an error.
        (settings / "kicad_common.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="kicad_common.json is not valid JSON"):
            kicad_cli.english_settings(tmp_path / "a", "10.0.6")
        (settings / "kicad_common.json").write_text('{"system": 3}', encoding="utf-8")
        with pytest.raises(ValueError, match="system is not a JSON object"):
            kicad_cli.english_settings(tmp_path / "b", "10.0.6")

    def test_settings_as_kicad_reads_them(self, settings, tmp_path):
        """Comments and a byte order mark (nlohmann::json with ignore_comments)."""
        (settings / "kicad_common.json").write_bytes(
            '\ufeff{"environment": {"vars": {"A": "b"}} // user\n /* x */}'.encode()
        )
        kicad_cli.english_settings(tmp_path / "a", "10.0.6")
        common = json.loads((tmp_path / "a" / "10.0" / "kicad_common.json").read_text("utf-8"))
        assert common["environment"] == {"vars": {"A": "b"}}

    @pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need rights")
    def test_symbolic_links(self, settings, tmp_path):
        """A file of a dotfile manager is a link: the copy holds the file, thus a
        write cannot change the settings of the user. A link to no file is left out."""
        real = tmp_path / "dotfiles" / "kicad_common.json"
        real.parent.mkdir()
        (settings / "kicad_common.json").rename(real)
        (settings / "kicad_common.json").symlink_to(real)
        (settings / "fp-lib-table").symlink_to(tmp_path / "missing")
        kicad_cli.english_settings(tmp_path / "copy", "10.0.6")
        copy = tmp_path / "copy" / "10.0"
        assert not (copy / "kicad_common.json").is_symlink()
        assert json.loads(real.read_text("utf-8"))["system"]["language"] == "Čeština"
        assert not (copy / "fp-lib-table").exists()

    def test_no_version(self, settings, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: None)
        with pytest.raises(KicadCliError, match="kicad-cli did not give its version"):
            run_kicad_cli_sync(["-c", "pass"], timeout=30, english=True)

    def test_no_settings_of_the_version(self, settings, tmp_path):
        kicad_cli.english_settings(tmp_path / "copy", "9.0.9")
        assert [p.name for p in (tmp_path / "copy" / "9.0").iterdir()] == ["kicad_common.json"]

    def test_run_gives_the_copy_to_kicad_cli(self, settings, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.6")
        code = (
            "import json, os, pathlib; home = pathlib.Path(os.environ['KICAD_CONFIG_HOME']); "
            "print(home); print(json.loads((home / '10.0' / 'kicad_common.json')"
            ".read_text(encoding='utf-8'))['system']['language'])"
        )
        result = run_kicad_cli_sync(["-c", code], timeout=30, english=True)
        home, language = decode_output(result.stdout).split()
        assert language == kicad_cli._english_language() and home != str(settings.parent)
        assert not Path(home).exists()  # the copy is removed after the run
        # Without english, kicad-cli gets the settings of the user.
        result = run_kicad_cli_sync(["-c", code], timeout=30)
        assert decode_output(result.stdout).split() == [str(settings.parent), "Čeština"]

    def test_settings_that_cannot_be_copied(self, settings, monkeypatch):
        monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: sys.executable)
        monkeypatch.setattr(kicad_cli, "kicad_cli_version_sync", lambda: "10.0.6")
        (settings / "kicad_common.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(KicadCliError, match="The settings of KiCad cannot be copied"):
            run_kicad_cli_sync(["-c", "pass"], timeout=30, english=True)

    @pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli is not installed")
    def test_real_kicad_cli_in_an_other_language(self, settings, tmp_path):
        """A user with Czech in KiCad: the report of kicad-cli is in English."""
        version = decode_output(run_kicad_cli_sync(["version"], timeout=30).stdout).strip()
        major, minor = version.split(".")[:2]
        settings.rename(settings.parent / f"{major}.{minor}")
        sheet = tmp_path / "deska č.kicad_sch"
        sheet.write_text(
            '(kicad_sch (version 20231120) (generator "eeschema") (uuid "a") (paper "A4")\n'
            '  (text "x" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
            "  (wire (pts (xy 10 10) (xy 20 10))))\n",
            encoding="utf-8",
        )
        report = tmp_path / "erc.json"
        args = ["sch", "erc", "--format", "json", "--output", str(report), str(sheet)]
        result = run_kicad_cli_sync(args, timeout=60, english=True)
        assert result.returncode == 0, decode_output(result.stderr)
        text = decode_output(result.stdout)
        assert "Found" in text and "Nalezeno" not in text, text
        violations = json.loads(report.read_text(encoding="utf-8"))["sheets"][0]["violations"]
        assert violations and all(v["description"].isascii() for v in violations), violations
