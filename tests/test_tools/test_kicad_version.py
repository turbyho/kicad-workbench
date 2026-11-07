"""Tests for the stock library folder of a KiCad installation (utils/kicad_version)."""

from pathlib import Path

import pytest

from kicad_workbench.utils import kicad_version
from kicad_workbench.utils.kicad_version import stock_library_dir, system_data_dir


def program(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("KICAD_STOCK_DATA_HOME", raising=False)


class TestLinux:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch):
        monkeypatch.setattr(kicad_version.sys, "platform", "linux")

    def test_default_layout_of_the_build(self, tmp_path):
        # CMakeLists.txt: KICAD_BIN is <prefix>/bin, KICAD_DATA <prefix>/share/kicad.
        cli = program(tmp_path / "usr" / "bin" / "kicad-cli")
        (tmp_path / "usr" / "share" / "kicad" / "template").mkdir(parents=True)
        assert stock_library_dir(str(cli)) == tmp_path / "usr" / "share" / "kicad"

    def test_symbolic_link_to_the_program(self, tmp_path):
        cli = program(tmp_path / "opt" / "kicad" / "bin" / "kicad-cli")
        (tmp_path / "opt" / "kicad" / "share" / "kicad" / "template").mkdir(parents=True)
        link = tmp_path / "links" / "kicad-cli"
        link.parent.mkdir()
        link.symlink_to(cli)
        assert stock_library_dir(str(link)) == tmp_path / "opt" / "kicad" / "share" / "kicad"

    def test_start_script_of_a_second_installation_is_not_known(self, tmp_path):
        # The start script of the KiCad nightly package is /usr/bin/kicad-cli-nightly,
        # its data are in /usr/share/kicad-nightly: the build has the folder.
        script = program(tmp_path / "usr" / "bin" / "kicad-cli-nightly")
        (tmp_path / "usr" / "share" / "kicad" / "template").mkdir(parents=True)
        assert stock_library_dir(str(script)) is None

    def test_program_outside_a_bin_folder_is_not_known(self, tmp_path):
        cli = program(tmp_path / "build" / "kicad" / "kicad-cli")
        (tmp_path / "build" / "share" / "kicad").mkdir(parents=True)
        assert stock_library_dir(str(cli)) is None

    def test_missing_folder(self, tmp_path):
        assert stock_library_dir(str(program(tmp_path / "usr" / "bin" / "kicad-cli"))) is None

    def test_folder_with_no_template_is_not_known(self, tmp_path):
        # A folder share/kicad of an other program (no project template of KiCad).
        cli = program(tmp_path / "usr" / "bin" / "kicad-cli")
        (tmp_path / "usr" / "share" / "kicad" / "symbols").mkdir(parents=True)
        assert stock_library_dir(str(cli)) is None

    def test_system_data_folder_is_the_stock_folder(self, tmp_path):
        # SystemDirsAppend adds GetStockEDALibraryPath on Linux.
        cli = program(tmp_path / "usr" / "bin" / "kicad-cli")
        (tmp_path / "usr" / "share" / "kicad" / "template").mkdir(parents=True)
        assert system_data_dir(str(cli)) == tmp_path / "usr" / "share" / "kicad"

    def test_override_of_the_data_path_does_not_apply(self, tmp_path, monkeypatch):
        # Linux: GetStockEDALibraryPath ignores KICAD_STOCK_DATA_HOME.
        cli = program(tmp_path / "usr" / "bin" / "kicad-cli")
        (tmp_path / "usr" / "share" / "kicad" / "template").mkdir(parents=True)
        (tmp_path / "other").mkdir()
        monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(tmp_path / "other"))
        assert stock_library_dir(str(cli)) == tmp_path / "usr" / "share" / "kicad"

    def test_no_program(self):
        assert stock_library_dir(None) is None


class TestWindows:
    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch):
        monkeypatch.setattr(kicad_version.sys, "platform", "win32")

    def test_share_folder_above_the_program_folder(self, tmp_path):
        # getWindowsKiCadRoot: the folder above the folder of the program.
        cli = program(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        stock = tmp_path / "KiCad" / "10.0" / "share" / "kicad"
        stock.mkdir(parents=True)
        assert stock_library_dir(str(cli)) == stock

    def test_override_of_the_data_path(self, tmp_path, monkeypatch):
        cli = program(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        (tmp_path / "KiCad" / "10.0" / "share" / "kicad").mkdir(parents=True)
        (tmp_path / "data").mkdir()
        monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(tmp_path / "data"))
        assert stock_library_dir(str(cli)) == tmp_path / "data"

    def test_missing_folder(self, tmp_path):
        cli = program(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        assert stock_library_dir(str(cli)) is None

    def test_relative_path_of_the_program(self, tmp_path, monkeypatch):
        # KICAD_CLI="kicad-cli.exe" in the folder bin: the path is made absolute first.
        program(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        stock = tmp_path / "KiCad" / "10.0" / "share" / "kicad"
        stock.mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "KiCad" / "10.0" / "bin")
        assert stock_library_dir("kicad-cli.exe") == stock

    def test_system_data_folder_has_no_override(self, tmp_path, monkeypatch):
        # SystemDirsAppend searches the folder above the folder of the program, not
        # GetStockDataPath: KICAD_STOCK_DATA_HOME does not change it.
        cli = program(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
        stock = tmp_path / "KiCad" / "10.0" / "share" / "kicad"
        stock.mkdir(parents=True)
        (tmp_path / "data").mkdir()
        monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(tmp_path / "data"))
        assert system_data_dir(str(cli)) == stock
        assert stock_library_dir(str(cli)) == tmp_path / "data"


class TestMacos:
    @pytest.fixture(autouse=True)
    def _macos(self, monkeypatch):
        monkeypatch.setattr(kicad_version.sys, "platform", "darwin")

    def test_shared_support_of_the_bundle(self, tmp_path):
        cli = program(tmp_path / "KiCad" / "KiCad.app" / "Contents" / "MacOS" / "kicad-cli")
        stock = tmp_path / "KiCad" / "KiCad.app" / "Contents" / "SharedSupport"
        stock.mkdir()
        assert stock_library_dir(str(cli)) == stock

    def test_auxiliary_bundle_in_the_main_bundle(self, tmp_path):
        main = tmp_path / "KiCad.app"
        cli = program(
            main / "Contents" / "Applications" / "pcbnew.app" / "Contents" / "MacOS" / "x"
        )
        (main / "Contents" / "SharedSupport").mkdir(parents=True)
        assert stock_library_dir(str(cli)) == main / "Contents" / "SharedSupport"

    def test_override_of_the_data_path_does_not_apply(self, tmp_path, monkeypatch):
        # macOS: GetStockEDALibraryPath is GetOSXKicadMachineDataDir, with no override.
        cli = program(tmp_path / "KiCad.app" / "Contents" / "MacOS" / "kicad-cli")
        stock = tmp_path / "KiCad.app" / "Contents" / "SharedSupport"
        stock.mkdir()
        (tmp_path / "data").mkdir()
        monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(tmp_path / "data"))
        assert stock_library_dir(str(cli)) == stock
