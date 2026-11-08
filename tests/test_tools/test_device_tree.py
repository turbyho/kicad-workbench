"""Tests for the output file of generate_device_tree."""

import asyncio
import sys

import pytest

from kicad_workbench.tools import device_tree, pin_analysis
from kicad_workbench.tools import netlist as netlist_tools
from kicad_workbench.utils.kicad_cli import CliExport

from .kicad_stub import child_preamble
from .locale_env import can_name, non_utf8_env, run_python

# The tests give the tools the netlist and the version of KiCad: they need no KiCad.
pytestmark = pytest.mark.usefixtures("kicad_version")

SCH = '(kicad_sch (version 20250114) (generator "eeschema"))\n'
NETLIST = '<?xml version="1.0"?>\n<export version="E"><components/><nets/></export>\n'


@pytest.fixture
def no_kicad(tmp_path, monkeypatch):
    """Give the tool a netlist without kicad-cli."""
    netlist = tmp_path / "board.xml"
    netlist.write_text(NETLIST, encoding="utf-8")

    monkeypatch.setattr(
        netlist_tools, "export_with_kicad_cli", lambda *args: CliExport(netlist.read_bytes(), [])
    )


def _generate(sch, output):
    return asyncio.run(device_tree.generate_device_tree(str(sch), "stm32f4", str(output)))


# Board names in different scripts. No Windows code page holds all of them,
# and Devanagari has no code page. "𠀀" (CJK Extension B) has 4 bytes in UTF-8
# and is a surrogate pair in UTF-16.
NAMES = [
    "řídicí_Ω",  # Czech and Greek: cp1250, cp1253
    "Плата_питания",  # Russian: cp1251
    "电源板",  # Chinese: cp936
    "電源ボード",  # Japanese: cp932
    "전원보드",  # Korean: cp949
    "لوحة_التحكم",  # Arabic, right to left: cp1256
    "לוח_בקרה",  # Hebrew, right to left: cp1255
    "แผงวงจร",  # Thai: cp874
    "बोर्ड",  # Devanagari: no code page
    "𠀀_板",  # CJK Extension B: 4 bytes in UTF-8
]


@pytest.mark.parametrize("name", NAMES)
def test_output_is_utf8(tmp_path, no_kicad, name):
    # The board name comes from the file name. On Windows the locale encoding
    # is the code page of the region: it cannot encode the names of other
    # scripts, and for its own script it gives bytes that are not UTF-8.
    sch = tmp_path / f"{name}.kicad_sch"
    sch.write_text(SCH, encoding="utf-8")
    output = tmp_path / "out" / "board.dts"
    result = _generate(sch, output)
    assert "Device Tree Generated Successfully" in result, result
    assert f'model = "{name}";'.encode() in output.read_bytes()


@pytest.mark.skipif(sys.platform != "linux", reason="needs a file name that is not UTF-8")
def test_text_that_is_not_valid_does_not_empty_the_output(tmp_path, no_kicad):
    # On Linux a file name can hold a byte that is not UTF-8. Python gives it
    # as a lone surrogate, which UTF-8 cannot encode. open("w") emptied the
    # output file before the encode error.
    sch = tmp_path / "board\udcff.kicad_sch"
    try:
        sch.write_text(SCH, encoding="utf-8")
    except OSError:
        pytest.skip("the file system accepts only UTF-8 file names")
    output = tmp_path / "board.dts"
    output.write_text("/* old */\n", encoding="utf-8")
    result = _generate(sch, output)
    assert result.startswith("❌ The text for board"), result
    assert "surrogates not allowed" in result and "Traceback" not in result
    assert output.read_text(encoding="utf-8") == "/* old */\n"


_IN_LOCALE_RUN = """
import asyncio
from pathlib import Path
from kicad_workbench.tools import device_tree
from kicad_workbench.tools import netlist as netlist_tools
from kicad_workbench.utils.kicad_cli import CliExport

directory, name, sch_text, netlist_text = ARGS
sch = Path(directory) / f"{name}.kicad_sch"
sch.write_text(sch_text, encoding="utf-8")
netlist = Path(directory) / "board.xml"
netlist.write_text(netlist_text, encoding="utf-8")
output = Path(directory) / "board.dts"

netlist_tools.export_with_kicad_cli = lambda *args: CliExport(netlist.read_bytes(), [])
message = asyncio.run(device_tree.generate_device_tree(str(sch), "stm32f4", str(output)))
result([message, output.read_bytes().hex() if output.exists() else ""])
"""
_IN_LOCALE = child_preamble() + _IN_LOCALE_RUN


@pytest.mark.parametrize("utf8_mode", [False, True])
def test_output_is_utf8_in_a_locale_that_is_not_utf8(tmp_path, utf8_mode):
    # The server runs in a locale that is not UTF-8, as on a system with such
    # a locale. The process makes the schematic file itself, thus the file
    # name uses the encoding of that locale.
    env, encoding = non_utf8_env()
    names = [name for name in NAMES if can_name(name, encoding, utf8_mode)]
    assert names, encoding
    for index, name in enumerate(names):
        directory = tmp_path / str(index)
        directory.mkdir()
        message, data = run_python(_IN_LOCALE, env, utf8_mode, str(directory), name, SCH, NETLIST)
        assert "Device Tree Generated Successfully" in message, message
        assert f'model = "{name}";'.encode() in bytes.fromhex(data)


def _conflicts(tmp_path, monkeypatch, nets: str) -> str:
    netlist = tmp_path / "c.xml"
    netlist.write_text(
        '<export version="E"><design><tool>Eeschema 10.0.6</tool></design><components>'
        '<comp ref="U1"><value>U</value><libsource lib="L" part="U"/></comp>'
        '<comp ref="U2"><value>U</value><libsource lib="L" part="U"/></comp>'
        f"</components><nets>{nets}</nets></export>",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        netlist_tools, "export_with_kicad_cli", lambda *args: CliExport(netlist.read_bytes(), [])
    )
    sch = tmp_path / "c.kicad_sch"
    sch.write_text(SCH, encoding="utf-8")
    return asyncio.run(pin_analysis.detect_pin_conflicts(str(sch)))


UNCONNECTED = (
    '<net code="1" name="unconnected-(U1-A-Pad1)">'
    '<node ref="U1" pin="1" pintype="input+no_connect"/></net>'
)
DRIVERS = (
    '<net code="2" name="N"><node ref="U1" pin="2" pintype="output"/>'
    '<node ref="U2" pin="2" pintype="output"/></net>'
)


def test_only_unconnected_pins_are_no_conflict(tmp_path, monkeypatch):
    # An unconnected pin is information: no "❌" (CLAUDE.md, honest output).
    result = _conflicts(tmp_path, monkeypatch, UNCONNECTED)
    assert result.startswith("✅ **No Pin Conflicts Detected**"), result
    assert "- ℹ️ 1 unconnected pin(s), listed below" in result
    assert "| ℹ️ info | unconnected_pin |" in result


def test_two_drivers_are_an_error(tmp_path, monkeypatch):
    result = _conflicts(tmp_path, monkeypatch, UNCONNECTED + DRIVERS)
    assert result.startswith("❌ **Pin Conflicts Detected**"), result
    assert "- Errors: 1\n- Warnings: 0\n- Unconnected pins (info): 1" in result
    assert "| ❌ error | multiple_outputs |" in result


def _validate(tmp_path, monkeypatch, nets: str) -> str:
    _conflicts(tmp_path, monkeypatch, nets)  # the netlist and the schematic
    return asyncio.run(device_tree.validate_pin_configuration(str(tmp_path / "c.kicad_sch")))


def test_a_warning_does_not_stop_the_device_tree(tmp_path, monkeypatch):
    # The tool looked for the letter "X" in the conflict report (the sign of the
    # fork for an error): the name of the net XTAL stopped the device tree.
    xtal = '<net code="3" name="XTAL"><node ref="U1" pin="3" pintype="input"/></net>'
    result = _validate(tmp_path, monkeypatch, xtal)
    assert result.startswith("⚠️ **No MCU Component Found**"), result


def test_two_drivers_stop_the_device_tree(tmp_path, monkeypatch):
    result = _validate(tmp_path, monkeypatch, DRIVERS)
    assert result.startswith("❌ **Pin Configuration Validation Failed**"), result
    assert "| ❌ error | multiple_outputs | N |" in result
