"""Tests for the encoding of the KiCad files that the tools read and write.

KiCad reads and writes its files as UTF-8 on each platform. The tools used the
locale encoding, which on Windows is the code page of the region (for example
cp1252, cp936 for Chinese or cp1256 for Arabic). The tests run the tool in a
Python process whose locale encoding is not UTF-8: the code page on Windows,
ASCII on Linux.
"""

import asyncio
import codecs
import json
import os
import subprocess
import sys
import uuid as uuidlib

import pytest

from kicad_workbench.parsers.pcb_parser import PCBParser
from kicad_workbench.parsers.schematic_parser import SchematicParser
from kicad_workbench.tools.schematic_editor import add_label
from kicad_workbench.utils.kicad_text import (
    TOKEN,
    KiCadEncodingError,
    read_as_kicad,
    read_for_write,
    write_kicad_text,
)

from .installed_kicad import as_installed, installed_device_library

SCH = """(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "%s")
\t(paper "A4")
\t(lib_symbols)
\t(label "Ω_OLD" (at 10 10 0)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "%s")
\t)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
\t(embedded_fonts no)
)
"""

# Runs in the child process. argv: schematic path, tool name, name as JSON.
# The name goes as ASCII JSON, because the ASCII locale cannot decode a
# non-ASCII argument.
_CHILD = """
import asyncio, json, locale, sys
from kicad_workbench.tools import schematic_editor as se
path, tool, name = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
if tool == "add_label":
    result = asyncio.run(se.add_label(path, name, 50, 50))
elif tool == "add_wire":
    result = asyncio.run(se.add_wire(path, [(0, 0), (10, 0)]))
elif tool == "add_component_from_library":
    result = asyncio.run(se.add_component_from_library(path, "Device", "R", "R1", name))
else:
    result = asyncio.run(getattr(se, tool)(path, "", "", name, x=60, y=60))
print(json.dumps({"encoding": locale.getpreferredencoding(False), "result": result}))
"""

_needs_symbols = pytest.mark.skipif(
    installed_device_library() is None, reason="the global library table has no library Device"
)

TOOLS = [
    "add_label",
    "add_global_label",
    "add_hierarchical_label",
    "add_wire",
    pytest.param("add_component_from_library", marks=_needs_symbols),
]


@pytest.fixture
def sch(tmp_path):
    path = tmp_path / "t.kicad_sch"
    path.write_bytes(as_installed(SCH % (uuidlib.uuid4(), uuidlib.uuid4())).encode("utf-8"))
    return path


def _run_tool(sch, tool, name="µ_NEW"):
    """Run a tool in a Python process with an ASCII locale and no UTF-8 mode."""
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    env.pop("PYTHONUTF8", None)
    child = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _CHILD, str(sch), tool, json.dumps(name)],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
    out = json.loads(child.stdout)
    # For example Windows with the "Use Unicode UTF-8" option gives "cp65001".
    if codecs.lookup(out["encoding"]).name == "utf-8":
        pytest.skip("the child process has a UTF-8 locale encoding")
    return out["result"]


@pytest.mark.parametrize("tool", TOOLS)
def test_write_keeps_utf8(sch, tool):
    result = _run_tool(sch, tool)
    assert "✅" in result, result
    data = sch.read_bytes()
    assert '(label "Ω_OLD"'.encode() in data
    if tool != "add_wire":
        assert '"µ_NEW"'.encode() in data
    data.decode("utf-8")


@pytest.mark.parametrize("tool", TOOLS)
def test_invalid_utf8_is_not_written(sch, tool):
    # A byte that is not valid UTF-8 in a label. Before, the label tools read it
    # as U+FFFD and wrote U+FFFD back, so the byte was lost.
    sch.write_bytes(sch.read_bytes().replace("Ω".encode(), b"\xff"))
    before = sch.read_bytes()
    result = _run_tool(sch, tool, name="NEW")
    offset = before.index(b"\xff")
    assert f"byte 0xff at offset {offset}" in result, result
    assert "✅" not in result
    assert sch.read_bytes() == before


def test_lone_surrogate_does_not_empty_the_file(sch):
    # A JSON string can hold a lone surrogate. write_text() emptied the file
    # before the encode error.
    before = sch.read_bytes()
    result = asyncio.run(add_label(str(sch), "A\ud800", 50, 50))
    assert result.startswith("❌ The text for ") and "surrogates not allowed" in result, result
    assert "Traceback" not in result
    assert sch.read_bytes() == before


class TestReadAsKicad:
    def test_string_with_invalid_byte_is_empty(self, tmp_path):
        path = tmp_path / "t.kicad_sch"
        path.write_bytes(b'(kicad_sch (title "a\xffb") (label "ok \\" \xff"))\n')
        assert read_as_kicad(path) == '(kicad_sch (title "") (label ""))\n'

    def test_invalid_byte_outside_a_string(self, tmp_path):
        path = tmp_path / "t.kicad_sch"
        path.write_bytes(b"(kicad_sch (at 1\xff 2))\n")
        with pytest.raises(KiCadEncodingError, match="byte 0xff at offset 16"):
            read_as_kicad(path)

    def test_error_gives_the_byte_outside_a_string(self, tmp_path):
        # The first invalid byte is in a string, which KiCad accepts.
        path = tmp_path / "t.kicad_sch"
        data = b'(kicad_sch (title "\xe9") (at 1\xff 2))\n'
        path.write_bytes(data)
        offset = data.index(b"\xff")
        with pytest.raises(KiCadEncodingError, match=f"byte 0xff at offset {offset}"):
            read_as_kicad(path)

    def test_quote_in_a_bare_token_does_not_start_a_string(self, tmp_path):
        # DSNLEXER ends a bare token only at a space or a parenthesis.
        path = tmp_path / "t.kicad_sch"
        path.write_bytes(b'(kicad_sch (x a"b) (title "\xff") (y "c"))\n')
        assert read_as_kicad(path) == '(kicad_sch (x a"b) (title "") (y "c"))\n'
        assert [m.group() for m in TOKEN.finditer('(x a"b "c d")')] == [
            "(",
            "x",
            'a"b',
            '"c d"',
            ")",
        ]

    def test_crlf_is_read_as_lf(self, tmp_path):
        path = tmp_path / "t.kicad_sch"
        path.write_bytes(b'(kicad_sch\r\n  (title "\xff")\r\n)\r\n')
        assert read_as_kicad(path) == '(kicad_sch\n  (title "")\n)\n'
        path.write_bytes(b"(kicad_sch\r\n)\r\n")
        assert read_for_write(path) == "(kicad_sch\n)\n"

    def test_write_uses_the_line_ends_of_kicad(self, tmp_path):
        # KiCad writes in text mode: CRLF on Windows, LF on other systems.
        path = tmp_path / "t.kicad_sch"
        write_kicad_text(path, '(kicad_sch\n  (title "µ")\n)\n')
        expected = '(kicad_sch\n  (title "µ")\n)\n'.replace("\n", os.linesep)
        assert path.read_bytes() == expected.encode("utf-8")

    def test_parsers_read_as_kicad(self, tmp_path):
        # Before, the parsers read such a file as latin-1 and gave wrong text.
        sch = tmp_path / "t.kicad_sch"
        sch.write_bytes(b'(kicad_sch (version 20250114) (title_block (title "\xff")))\n')
        assert SchematicParser(str(sch)).get_title_block().get("title", "") == ""
        pcb = tmp_path / "t.kicad_pcb"
        pcb.write_bytes(b'(kicad_pcb (version 20240108) (title_block (title "\xff")))\n')
        PCBParser(str(pcb))._parse_file()
