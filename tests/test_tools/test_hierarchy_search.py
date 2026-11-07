"""Tests of the search of the components of a full sheet hierarchy.

trace_hierarchical_connection and search_components_by_type read the root
schematic and one level of sub-sheets (SchematicParser of each file): they did
not find a component in a deeper sheet, and they gave a sheet that the
hierarchy uses two times one reference. search_components_by_type also said "No
... components found" before it searched the sub-sheets. Now the two tools read
the netlist of kicad-cli: each component of each sheet path, with the reference
of that path.
"""

import asyncio

import pytest

from kicad_workbench.tools import hierarchical_analysis, netlist, schematic_search
from kicad_workbench.utils.kicad_cli import CliExport

ROOT, MID, LEAF = (
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
)
# The sheet symbols: Mid in the root, Leaf in Mid, Leaf2 in the root (the same file).
S_MID, S_LEAF, S_LEAF2 = (
    "44444444-4444-4444-4444-444444444444",
    "55555555-5555-5555-5555-555555555555",
    "66666666-6666-6666-6666-666666666666",
)


def _sheet(uuid: str, name: str, file: str) -> str:
    return (
        f'  (sheet (at 10 10) (size 20 20) (uuid "{uuid}")\n'
        f'    (property "Sheetname" "{name}" (at 10 9 0))\n'
        f'    (property "Sheetfile" "{file}" (at 10 31 0)))\n'
    )


def _schematic(uuid: str, *sheets: str) -> str:
    return (
        f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{uuid}") (paper "A4")\n'
        + "".join(sheets)
        + ")\n"
    )


def _comp(ref: str, value: str, names: str, tstamps: str) -> str:
    return (
        f'<comp ref="{ref}"><value>{value}</value><footprint>Package_SO:SOIC-8</footprint>'
        f'<libsource lib="L" part="P"/><sheetpath names="{names}" tstamps="{tstamps}"/></comp>'
    )


def _design_sheet(names: str, tstamps: str, source: str) -> str:
    return (
        f'<sheet number="1" name="{names}" tstamps="{tstamps}">'
        f"<title_block><source>{source}</source></title_block></sheet>"
    )


SHEETS = [
    ("/", "/", "root.kicad_sch"),
    ("/Mid/", f"/{S_MID}/", "mid.kicad_sch"),
    ("/Leaf2/", f"/{S_LEAF2}/", "leaf.kicad_sch"),
    ("/Mid/Leaf/", f"/{S_MID}/{S_LEAF}/", "leaf.kicad_sch"),
]
COMPONENTS = [
    ("R1", "10k", "/", "/"),
    ("U1", "LM358", "/Mid/", f"/{S_MID}/"),
    ("U2", "SSD1306 OLED", "/Leaf2/", f"/{S_LEAF2}/"),
    ("U3", "SSD1306 OLED", "/Mid/Leaf/", f"/{S_MID}/{S_LEAF}/"),
]


def netlist_xml(sheets=SHEETS, components=COMPONENTS, nets="") -> bytes:
    return (
        '<export version="E"><design><tool>Eeschema 10.0.6</tool>'
        + "".join(_design_sheet(*s) for s in sheets)
        + "</design><components>"
        + "".join(_comp(*c) for c in components)
        + '</components><nets><net code="1" name="/Mid/Leaf/SDA">'
        '<node ref="U3" pin="1" pintype="bidirectional"/></net>'
        '<net code="2" name="/Leaf2/SDA"><node ref="U2" pin="1" pintype="bidirectional"/>'
        '<node ref="R1" pin="2" pintype="passive"/></net>' + nets + "</nets></export>"
    ).encode()


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "root.kicad_sch").write_text(
        _schematic(ROOT, _sheet(S_MID, "Mid", "mid.kicad_sch"),
                   _sheet(S_LEAF2, "Leaf2", "leaf.kicad_sch")),
        encoding="utf-8",
    )  # fmt: skip
    (tmp_path / "mid.kicad_sch").write_text(
        _schematic(MID, _sheet(S_LEAF, "Leaf", "leaf.kicad_sch")), encoding="utf-8"
    )
    (tmp_path / "leaf.kicad_sch").write_text(_schematic(LEAF), encoding="utf-8")
    # A root schematic is a schematic with the project file of its name.
    (tmp_path / "root.kicad_pro").write_text("{}\n", encoding="utf-8")
    state = {"xml": netlist_xml(), "sources": []}

    def export(command, source, file_name, timeout):
        state["sources"].append(source)
        return CliExport(state["xml"], [])

    monkeypatch.setattr(netlist, "export_with_kicad_cli", export)
    state["dir"] = tmp_path
    return state


def search(path, kind="OLED", below=True):
    return asyncio.run(schematic_search.search_components_by_type(str(path), kind, below))


def found(result: str) -> list[str]:
    return [line.split()[2] for line in result.splitlines() if line.startswith("### ")]


def test_trace_finds_a_component_two_levels_down(project):
    root = project["dir"] / "root.kicad_sch"
    result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(str(root), "U3"))
    assert "**Sheet:** /Mid/Leaf/ (`leaf.kicad_sch`)" in result, result
    assert "**Net:** /Mid/Leaf/SDA" in result


def test_trace_of_a_sub_sheet_uses_the_netlist_of_the_root(project):
    leaf = project["dir"] / "leaf.kicad_sch"
    result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(str(leaf), "U2"))
    assert "**Sheet:** /Leaf2/ (`leaf.kicad_sch`)" in result, result
    assert "  - R1:2" in result
    assert project["sources"] == [project["dir"] / "root.kicad_sch"]


def test_search_of_the_root_finds_each_level(project):
    # The root has no match: the old tool stopped with "No OLED components found".
    result = search(project["dir"] / "root.kicad_sch")
    assert found(result) == ["U2", "U3"], result
    assert "**Searched:** 4 components of the file and the sheets below it" in result
    assert "**Sheet:** /Mid/Leaf/" in result


def test_search_of_the_root_file_only(project):
    result = search(project["dir"] / "root.kicad_sch", "10k", below=False)
    assert found(result) == ["R1"], result
    assert "**Searched:** 1 components of the file" in result


def test_search_of_a_sub_sheet_and_the_sheets_below_it(project):
    result = search(project["dir"] / "mid.kicad_sch")
    assert found(result) == ["U3"], result  # not U2: /Leaf2/ is not below Mid


def test_search_of_a_sheet_that_the_hierarchy_uses_two_times(project):
    result = search(project["dir"] / "leaf.kicad_sch", below=False)
    assert found(result) == ["U2", "U3"], result  # one reference for each sheet path


def test_a_sheet_of_the_netlist_that_the_hierarchy_does_not_have(project):
    sheets = [*SHEETS, ("/Other/", "/77777777-7777-7777-7777-777777777777/", "other.kicad_sch")]
    project["xml"] = netlist_xml(sheets=sheets)
    result = search(project["dir"] / "mid.kicad_sch")
    assert found(result) == ["U3"], result
    assert "⚠️ The netlist has sheets that the tool does not find in the hierarchy of" in result
    assert ": /Other/. The tool cannot tell if their components are in the file" in result


def test_a_sheet_name_with_a_text_variable(project):
    # KiCad expands the text variables of a sheet name in "names"
    # (GetShownText): the tool uses only "tstamps".
    renamed = [(n.replace("Mid", "main_io"), t, f) for n, t, f in SHEETS]
    components = [(r, v, n.replace("Mid", "main_io"), t) for r, v, n, t in COMPONENTS]
    project["xml"] = netlist_xml(sheets=renamed, components=components)
    result = search(project["dir"] / "mid.kicad_sch")
    assert found(result) == ["U3"], result
    assert "⚠️" not in result


def test_a_root_schematic_with_no_uuid(project):
    # KiCad gives such a root a new UUID at each load; the netlist does not use it.
    root = project["dir"] / "root.kicad_sch"
    root.write_text(root.read_text(encoding="utf-8").replace(f' (uuid "{ROOT}")', ""), "utf-8")
    result = search(project["dir"] / "leaf.kicad_sch", below=False)
    assert found(result) == ["U2", "U3"], result


def test_two_sheets_with_one_path_are_not_in_the_result(project):
    # kicad-cli of KiCad 10.0.7 and later loads all top-level sheets: two of them
    # have the path "/".
    sheets = [*SHEETS, ("/Extra/", "/", "extra.kicad_sch")]
    components = [*COMPONENTS, ("U9", "SSD1306 OLED", "/Extra/", "/")]
    project["xml"] = netlist_xml(sheets=sheets, components=components)
    result = search(project["dir"] / "root.kicad_sch")
    assert found(result) == ["U2", "U3"], result
    # "/" of the root is known by its names; "/Extra/" is not in the hierarchy.
    assert ": /Extra/. The tool cannot tell if their components are in the file" in result


def test_a_type_of_the_table_in_lower_case(project):
    # The look-up in upper case did not find 'sensor': only the word itself.
    components = [*COMPONENTS, ("U5", "BME280 humidity", "/Mid/", f"/{S_MID}/")]
    project["xml"] = netlist_xml(components=components)
    result = search(project["dir"] / "root.kicad_sch", "sensor")
    assert found(result) == ["U5"], result
    assert "**Searched patterns:** sensor, temp, humidity" in result


@pytest.mark.parametrize("tool", ["trace", "search", "nets"])
def test_a_file_that_is_not_there(project, tool):
    path = project["dir"] / "typo.kicad_sch"
    if tool == "trace":
        result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(str(path), "U1"))
    elif tool == "search":
        result = search(path)
    else:
        result = asyncio.run(hierarchical_analysis.analyze_hierarchical_nets(str(path)))
    assert result == f"❌ File not found: {path}", result
    assert project["sources"] == []


def test_the_nets_tool_counts_each_sheet_path(project):
    root = project["dir"] / "root.kicad_sch"
    result = asyncio.run(hierarchical_analysis.analyze_hierarchical_nets(str(root)))
    assert "**Sheet paths:** 4" in result, result
    assert "- /Mid/Leaf/ (`leaf.kicad_sch`)" in result


def test_the_root_search_leaves_out_a_sheet_that_the_hierarchy_does_not_have(project):
    # The warning says that the components of such a sheet are not in the result:
    # the prefix "/" of the root matched them.
    other = "/77777777-7777-7777-7777-777777777777/"
    sheets = [*SHEETS, ("/Other/", other, "other.kicad_sch")]
    components = [*COMPONENTS, ("U8", "SSD1306 OLED", "/Other/", other)]
    project["xml"] = netlist_xml(sheets=sheets, components=components)
    result = search(project["dir"] / "root.kicad_sch")
    assert found(result) == ["U2", "U3"], result
    assert "the hierarchy of" in result and ": /Other/." in result


def test_a_reference_of_two_components(project):
    components = [*COMPONENTS, ("U3", "SSD1306 OLED", "/Leaf2/", f"/{S_LEAF2}/")]
    project["xml"] = netlist_xml(components=components)
    result = search(project["dir"] / "root.kicad_sch")
    assert (
        "⚠️ The annotation is not complete: the netlist has more than one component "
        "with the reference U3. The result has one of them." in result
    ), result
    root = project["dir"] / "root.kicad_sch"
    trace = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(str(root), "U3"))
    assert "⚠️ The annotation is not complete: the netlist has more than one component U3" in trace


def test_the_pins_of_a_symbol_with_no_library_symbol(project):
    nets = '<net code="3" name="N3"><node ref="X1" pin="1" pintype="passive"/></net>'
    project["xml"] = netlist_xml(nets=nets)
    result = search(project["dir"] / "root.kicad_sch", "N3")
    assert found(result) == [], result
    assert "⚠️ The netlist has the pins of X1 but no component" in result


def test_trace_of_a_pin_on_two_nets_and_of_an_unknown_pin(project):
    # Pin 1 of U3 is also on /Mid/Leaf/SCL (two library pins "1"): each net, and the
    # warning. A pin that the netlist does not give U3 is an error.
    project["xml"] = netlist_xml(
        nets='<net code="3" name="/Mid/Leaf/SCL"><node ref="U3" pin="1" pintype="input"/></net>'
    )
    root = str(project["dir"] / "root.kicad_sch")
    result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(root, "U3", "1"))
    assert "**Net:** /Mid/Leaf/SDA" in result and "**Net:** /Mid/Leaf/SCL" in result, result
    assert "U3.1 (/Mid/Leaf/SDA, /Mid/Leaf/SCL)" in result
    result = asyncio.run(hierarchical_analysis.trace_hierarchical_connection(root, "U3", "9"))
    assert result == "❌ Pin 9 not found in component U3", result
