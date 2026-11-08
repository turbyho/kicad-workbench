"""Tests for utils/model_paths.py, utils/library_footprint.py and the tool get_library_footprint."""

import os
import re
import sys
from pathlib import Path

import pytest

from kicad_workbench.tools import library as library_tools
from kicad_workbench.utils.kicad_python import find_kicad_python, result_line
from kicad_workbench.utils.library_footprint import (
    FootprintItems,
    FootprintReadError,
    PadNumber,
    footprint_info,
    read_footprints,
    read_library_footprint,
)
from kicad_workbench.utils.library_tables import KicadEnvironment
from kicad_workbench.utils.model_paths import ModelFile, resolve_model

from .test_library_search import make_tables, table_rows


@pytest.fixture
def places(tmp_path, monkeypatch):
    """Directories of models, a project, a library and an alias, with no variable from the environment."""
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD|MYLIB|NOPE", name):
            monkeypatch.delenv(name)
    dirs = {name: tmp_path / name for name in ("models", "project", "library", "alias")}
    for path in dirs.values():
        path.mkdir()
    dirs["project_file"] = dirs["project"] / "board.kicad_pro"
    dirs["project_file"].write_text("{}", encoding="utf-8")
    return dirs


def environment(places, major=10, **more) -> KicadEnvironment:
    variables = {
        f"KICAD{major}_3DMODEL_DIR": str(places["models"]),
        f"KICAD{major}_FOOTPRINT_DIR": str(places["alias"]),
        "MYLIB": str(places["alias"]),
        **more,
    }
    return KicadEnvironment(major, 0, places["models"], variables)


def write(path, text="model"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def resolve(places, name, major=10, project=True, embedded=(), **more):
    project_file = places["project_file"] if project else None
    variables = {"KIPRJMOD": str(places["project"])} if project else {}
    env = environment(places, major, **more)
    return resolve_model(
        name, major, env, project_file, variables, places["library"], set(embedded)
    )


class TestModelPaths:
    def test_variable(self, places):
        model = write(places["models"] / "Package.3dshapes" / "a.step")
        assert resolve(places, "${KICAD10_3DMODEL_DIR}/Package.3dshapes/a.step") == ModelFile(
            "found", model
        )
        assert resolve(places, "$(KICAD10_3DMODEL_DIR)/Package.3dshapes/a.step").path == model

    def test_variable_of_an_other_version(self, places):
        # KiCad gives the 3D model directory of its own version (KIwxExpandEnvVars).
        model = write(places["models"] / "a.step")
        assert resolve(places, "${KICAD9_3DMODEL_DIR}/a.step").path == model
        assert resolve(places, "${KISYS3DMOD}/a.step").path == model

    def test_name_with_a_variable_is_not_found_at_other_places(self, places):
        write(places["library"] / "a.step")
        write(places["project"] / "a.step")
        found = resolve(places, "${KICAD10_3DMODEL_DIR}/a.step")
        assert found == ModelFile("not found")

    def test_variable_with_no_value(self, places):
        assert resolve(places, "${NOPE}/a.step") == ModelFile(
            "not found", note="The path variable NOPE has no value."
        )
        found = resolve(places, "${KIPRJMOD}/a.step", project=False)
        assert (
            found.note
            == "The path variable KIPRJMOD has no value. Give the project to set KIPRJMOD."
        )

    def test_absolute_path(self, places):
        model = write(places["alias"] / "a.step")
        assert resolve(places, str(model)) == ModelFile("found", model)
        assert resolve(places, str(places["alias"] / "b.step")) == ModelFile("not found")

    def test_relative_path_sequence(self, places):
        note = "KiCad first looks for a relative path in its working directory"
        in_models = write(places["models"] / "m" / "a.step")
        found = resolve(places, "m/a.step")
        assert found.path == in_models and found.note.startswith(note)
        in_library = write(places["library"] / "m" / "a.step")
        assert resolve(places, "m/a.step").path == in_library
        in_project = write(places["project"] / "m" / "a.step")
        assert resolve(places, "m/a.step").path == in_project
        # With no project, the project directory is not searched.
        assert resolve(places, "m/a.step", project=False).path == in_library

    def test_relative_project_path(self, places, monkeypatch):
        model = write(places["project"] / "m" / "a.step")
        monkeypatch.chdir(places["project"].parent)
        env = environment(places)
        found = resolve_model(
            "m/a.step", 10, env, places["project_file"].relative_to(places["project"].parent),
            {}, None, set(),
        )  # fmt: skip
        assert found.path == model

    def test_absolute_path_with_no_file_below_the_project(self, places):
        # KiCad joins the project directory and the absolute name as text
        # (model_paths_check.py: KiCad 8, 9 and 10 find this file on Linux).
        name = places["alias"] / "x.step"
        if os.name == "nt":
            # "C:\\project\\C:\\...": a second volume makes a name that is not a
            # file, thus the joined name finds nothing. This follows from the join
            # as text (FILENAME_RESOLVER::ResolvePath, wxFileName). The check did
            # not run KiCad on Windows.
            assert resolve(places, str(name)).state == "not found"
            return
        model = write(Path(f"{places['project']}{os.sep}{name}"))
        assert resolve(places, str(name)).path == model

    def test_relative_path_not_found(self, places):
        found = resolve(places, "m/a.step")
        assert found.state == "not found" and "working directory" in found.note

    @pytest.mark.parametrize("name", ["MYLIB:sub/a.step", ":MYLIB:sub/a.step"])
    def test_alias(self, places, name):
        model = write(places["alias"] / "sub" / "a.step")
        assert resolve(places, name).path == model

    def test_alias_of_a_footprint_directory_or_a_url(self, places):
        write(places["alias"] / "a.step")
        # GetKicadPaths: no KICAD*_FOOTPRINT_DIR, no KICAD_PTEMPLATES, no URL.
        assert resolve(places, "KICAD10_FOOTPRINT_DIR:a.step").state == "not found"
        assert resolve(
            places, "KICAD_PTEMPLATES:a.step", KICAD_PTEMPLATES=str(places["alias"])
        ).state == ("not found")
        assert resolve(places, "WEB:a.step", WEB="https://example.com").state == "not found"
        assert resolve(places, "NONE:a.step").state == "not found"

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps the backslash")
    def test_backslash(self, places):
        model = write(places["library"] / "sub" / "a.step")
        assert resolve(places, "sub\\a.step").path == model
        # KiCad 8 does not replace the backslash.
        assert resolve(places, "sub\\a.step", major=8).state == "not found"

    def test_embedded_file(self, places):
        assert resolve(places, "kicad-embed://m.step", embedded={"m.step"}) == ModelFile("embedded")
        assert resolve(places, "kicad-embed://x.step", embedded={"m.step"}) == ModelFile(
            "not found", note='The footprint has no embedded file "x.step".'
        )
        # KiCad 8 has no embedded files.
        assert resolve(places, "kicad-embed://m.step", major=8, embedded={"m.step"}).state == (
            "not found"
        )

    def test_embedded_file_of_the_board(self, places):
        # A footprint of a board: KiCad looks in the files of the footprint, then of the board.
        env = environment(places)
        args = (10, env, None, {}, None)
        assert resolve_model("kicad-embed://b.step", *args, {"m.step"}, {"b.step"}) == (
            ModelFile("embedded")
        )
        assert resolve_model("kicad-embed://x.step", *args, set(), {"b.step"}) == ModelFile(
            "not found", note='The footprint and the board have no embedded file "x.step".'
        )

    def test_no_file_name(self, places):
        assert resolve(places, "") == ModelFile("no file name")


def pad(**values) -> dict:
    data = {
        "number": "1",
        "type": "smd",
        "shape": "rectangle",
        "size": [1000000, 500000],
        "offset": [0, 0],
        "roundrect_ratio": 0.25,
        "chamfer_ratio": 0.2,
        "chamfer_positions": 0,
        "delta": [0, 0],
        "anchor_shape": "circle",
        "position": [-1000000, 250000],
        "orientation": 0.0,
        "drill": [0, 0],
        "drill_shape": "circle",
        "layers": ["F.Cu", "F.Mask", "F.Paste"],
        "on_copper": True,
        "pin_function": "",
        "pin_type": "",
        "clearance": None,
        "solder_mask_margin": None,
        "solder_paste_margin": None,
        "solder_paste_ratio": None,
        "zone_connection": "inherited",
        "padstack": "normal",
        "layer_shapes": [],
    }
    data.update(values)
    return data


def script_result(**values) -> dict:
    """The JSON of the pcbnew script, with the text of the KiCad 10 writer."""
    data = {
        "kicad": "10.0.6",
        "description": "Test footprint",
        "keywords": "test smd",
        "fields": [
            {"name": "Reference", "text": "REF**", "visible": True, "layer": "F.Silkscreen"},
            {"name": "Value", "text": "Test", "visible": True, "layer": "F.Fab"},
        ],
        "clearance": None,
        "solder_mask_margin": None,
        "solder_paste_margin": None,
        "solder_paste_ratio": None,
        "zone_connection": "inherited",
        "box": [-1500000, -1000000, 1500000, 1000000],
        "courtyard": [
            {
                "layer": "F.Courtyard",
                "outlines": 1,
                "box": [-1745000, -1245000, 1745000, 1245000],
                "area": 8690100000000.0,
                "malformed": False,
            },
            {"layer": "B.Courtyard", "outlines": 0, "box": None, "area": 0.0, "malformed": False},
        ],
        "pads": [pad()],
        "models": [],
        "text": '(footprint "Test"(layer "F.Cu" )(attr smd)(duplicate_pad_numbers_are_jumpers no))',
    }
    data.update(values)
    return data


WRITER_TEXT = (
    '(footprint "Test"(layer "F.Cu" )(descr "x")'
    "(attr smd board_only exclude_from_bom allow_missing_courtyard dnp)"
    '(private_layers "User.1" "User.2")(net_tie_pad_groups "1, 2" "3,4")'
    '(duplicate_pad_numbers_are_jumpers yes)(jumper_pad_groups ("1" "2" ) ("5" "6" "7" ))'
    '(embedded_fonts no)(embedded_files (file (name "m.step")(type model)(checksum "0")))'
    '(model "kicad-embed://m.step"(offset (xyz 0 0 0))(scale (xyz 1 1 1))(rotate (xyz 0 0 0))))'
)


class TestFootprintInfo:
    def test_items_of_the_writer_text(self):
        info = footprint_info(script_result(text=WRITER_TEXT))
        assert info.attributes == (
            "smd",
            "board_only",
            "exclude_from_bom",
            "allow_missing_courtyard",
            "dnp",
        )
        assert info.private_layers == ("User.1", "User.2")
        assert info.net_tie_pad_groups == ("1, 2", "3,4")
        assert info.duplicate_pad_numbers_are_jumpers
        assert info.jumper_pad_groups == (("1", "2"), ("5", "6", "7"))
        assert info.embedded_files == (("m.step", "model"),)

    def test_text_of_kicad_8(self):
        # KiCad 8 writes no duplicate_pad_numbers_are_jumpers and no embedded files.
        info = footprint_info(
            script_result(text='(footprint "Test" (layer "F.Cu")\n  (attr smd)\n)')
        )
        assert not info.duplicate_pad_numbers_are_jumpers
        assert info.jumper_pad_groups == () and info.embedded_files == ()

    def test_values_of_pcbnew(self):
        shape = pad(shape="circle", size=[1500000, 1500000], layers=["In1.Cu", "In2.Cu"])
        info = footprint_info(script_result(pads=[pad(padstack="custom", layer_shapes=[shape])]))
        assert info.pads[0].shape.size == (1000000, 500000)
        assert info.pads[0].layer_shapes[0].shape == "circle"
        assert info.pads[0].layer_shapes[0].layers == ("In1.Cu", "In2.Cu")
        assert info.courtyard[0].box == (-1745000, -1245000, 1745000, 1245000)
        assert info.courtyard[1].box is None

    def test_writer_text_that_cannot_be_parsed(self):
        with pytest.raises(FootprintReadError, match="cannot be parsed"):
            footprint_info(script_result(text='(footprint "Test"'))


class FakePython:
    """A fake run_kicad_python: it gives one result line and records the arguments."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.result = type(
            "Result",
            (),
            {"stdout": stdout.encode(), "stderr": stderr.encode(), "returncode": returncode},
        )()
        self.args = None
        self.input = None

    async def __call__(self, args, timeout, input=None):
        self.args, self.input = args, input
        return self.result


class TestReadLibraryFootprint:
    async def test_result(self, monkeypatch, tmp_path):
        import json

        from kicad_workbench.utils import library_footprint

        fake = FakePython("noise\nFOOTPRINT_JSON=" + json.dumps(script_result()) + "\n")
        monkeypatch.setattr(library_footprint, "run_kicad_python", fake)
        version, info = await read_library_footprint(tmp_path, "Test")
        assert version == "10.0.6" and info.pads[0].number == "1"
        assert fake.args[2:] == [str(tmp_path), "Test"]

    async def test_footprint_that_kicad_does_not_load(self, monkeypatch, tmp_path):
        from kicad_workbench.utils import library_footprint

        fake = FakePython('FOOTPRINT_JSON={"kicad": "9.0.9"}\n')
        monkeypatch.setattr(library_footprint, "run_kicad_python", fake)
        assert await read_library_footprint(tmp_path, "Test") == ("9.0.9", None)

    @pytest.mark.parametrize(
        "fake, message",
        [
            (
                FakePython(stderr="Segmentation fault", returncode=139),
                "exit code 139:\nSegmentation fault",
            ),
            (FakePython("nothing\n"), "gave no result"),
            (FakePython("FOOTPRINT_JSON={\n"), "not JSON"),
        ],
    )
    async def test_errors(self, monkeypatch, tmp_path, fake, message):
        from kicad_workbench.utils import library_footprint

        monkeypatch.setattr(library_footprint, "run_kicad_python", fake)
        with pytest.raises(FootprintReadError, match=re.escape(message)):
            await read_library_footprint(tmp_path, "Test")


def make_footprints(tmp_path, monkeypatch) -> dict:
    """Library tables with a footprint library, and a fake pcbnew that gives a result."""
    tables = make_tables(tmp_path, monkeypatch)
    pretty = tables["libs"] / "Lib.pretty"
    pretty.mkdir()
    (pretty / "Test.kicad_mod").write_text(
        '(footprint "Test"\n\t(version 20241229)\n\t(layer "F.Cu")\n)\n', encoding="utf-8"
    )
    (tables["libs"] / "File.pretty").write_text("", encoding="utf-8")
    (tables["libs"] / "x.lbr").write_text("", encoding="utf-8")
    lib = tables["lib"]
    (tables["config"] / "10.0" / "fp-lib-table").write_text(
        "(fp_lib_table\n\t(version 7)\n"
        + "\n".join(
            [
                lib("Lib", pretty),
                lib("File", tables["libs"] / "File.pretty"),
                lib("Eagle", tables["libs"] / "x.lbr", kind_type="Eagle"),
            ]
        )
        + "\n)\n",
        encoding="utf-8",
    )
    result = {"value": ("10.0.6", footprint_info(script_result()))}

    async def read(library, name):
        result["args"] = (library, name)
        return result["value"]

    monkeypatch.setattr(library_tools, "read_library_footprint", read)
    tables.update(pretty=pretty, result=result)
    return tables


@pytest.fixture
def footprints(tmp_path, monkeypatch):
    return make_footprints(tmp_path, monkeypatch)


def section(out: str, heading: str) -> str:
    """The text of one section of the output, from its heading to the next heading."""
    start = out.index(f"## {heading}")
    end = out.find("\n## ", start + 1)
    return out[start : end if end >= 0 else len(out)]


class TestGetLibraryFootprint:
    async def test_footprint(self, footprints):
        out = await library_tools.get_library_footprint("Lib:Test")
        assert out.startswith("# Library footprint Lib:Test"), out
        assert footprints["result"]["args"] == (footprints["pretty"], "Test")
        for line in [
            f"**File**: {footprints['pretty'] / 'Test.kicad_mod'}",
            "**File version**: 20241229",
            "**Read by**: pcbnew of KiCad 10.0.6",
            "**Type**: SMD",
            "**Attributes**: none",
            "**Size**: 3 × 2 mm, from (-1.5, -1) to (1.5, 1) (all items, no text)",
        ]:
            assert line in out
        assert table_rows(section(out, "Courtyard")) == [
            [
                "F.Courtyard",
                "ok",
                "1",
                "3.49 × 2.49",
                "(-1.745, -1.245)",
                "(1.745, 1.245)",
                "8.6901",
            ],
            ["B.Courtyard", "none", "0", "", "", "", ""],
        ]
        assert table_rows(section(out, "Pads")) == [
            ["1", "SMD", "rectangle", "-1", "0.25", "0", "1 × 0.5", "", "F.Cu F.Mask F.Paste", ""]
        ]
        assert "## 3D models" not in out

    async def test_attributes_and_local_settings(self, footprints):
        footprints["result"]["value"] = (
            "10.0.6",
            footprint_info(
                script_result(
                    text=WRITER_TEXT,
                    clearance=200000,
                    solder_mask_margin=50000,
                    solder_paste_margin=-30000,
                    solder_paste_ratio=-0.1,
                    zone_connection="tht_thermal",
                )
            ),
        )
        out = await library_tools.get_library_footprint("Lib:Test")
        for line in [
            "**Attributes**: board_only, exclude_from_bom, allow_missing_courtyard, dnp",
            "**Net tie pad groups**: 1, 2; 3,4",
            "**Duplicate pad numbers are jumpers**: yes",
            "**Jumper pad groups**: 1, 2; 5, 6, 7",
            "**Private layers**: User.1, User.2",
            "**Local settings**: clearance 0.2 mm, solder mask margin 0.05 mm, solder paste margin "
            "-0.03 mm, solder paste ratio -0.1, zone connection tht thermal",
            "**Embedded files**: m.step (model)",
        ]:
            assert line in out

    async def test_pad_details(self, footprints):
        inner = pad(
            shape="circle", size=[1500000, 1500000], layers=[f"In{i}.Cu" for i in range(1, 31)]
        )
        pads = [
            pad(shape="roundrect"),
            pad(
                number="2",
                shape="chamfered_rect",
                roundrect_ratio=0.1,
                chamfer_ratio=0.3,
                chamfer_positions=9,
            ),
            pad(number="3", shape="trapezoid", delta=[200000, 0]),
            pad(number="4", shape="custom", anchor_shape="rectangle", offset=[100000, -50000]),
            pad(
                number="5",
                type="pth",
                shape="oval",
                drill=[1200000, 600000],
                drill_shape="oblong",
                layers=["*.Cu", "F.Mask", "B.Mask"],
                orientation=90.0,
                pin_function="GND",
                pin_type="passive",
                clearance=100000,
                zone_connection="full",
            ),
            pad(
                number="",
                type="npth",
                shape="circle",
                drill=[1000000, 1000000],
                padstack="front_inner_back",
                layer_shapes=[pad(layers=["F.Cu"]), inner],
            ),
        ]
        footprints["result"]["value"] = ("10.0.6", footprint_info(script_result(pads=pads)))
        rows = table_rows(section(await library_tools.get_library_footprint("Lib:Test"), "Pads"))
        assert [(row[0], row[1], row[5], row[7], row[9]) for row in rows] == [
            ("1", "SMD", "0", "", "corner radius ratio 0.25"),
            (
                "2",
                "SMD",
                "0",
                "",
                "corner radius ratio 0.1; chamfer ratio 0.3 (top left, bottom right)",
            ),
            ("3", "SMD", "0", "", "delta (0.2, 0) mm"),
            ("4", "SMD", "0", "", "anchor rectangle; offset (0.1, -0.05) mm"),
            (
                "5",
                "PTH",
                "90",
                "1.2 × 0.6 oblong",
                "pin function GND; pin type passive; clearance 0.1 mm; zone connection full",
            ),
            (
                "",
                "NPTH",
                "0",
                "1",
                "padstack front/inner/back: F.Cu: rectangle 1 × 0.5 mm; "
                "In1.Cu to In30.Cu (30 layers): circle 1.5 × 1.5 mm",
            ),
        ]

    @pytest.mark.parametrize(
        "courtyard, attributes, note",
        [
            ([True, False], "smd", "The DRC of KiCad reports a malformed courtyard"),
            ([False, False], "smd", "its default severity is ignore"),
            ([False, False], "smd allow_missing_courtyard", "allow_missing_courtyard lets the DRC"),
        ],
    )
    async def test_courtyard_drc(self, footprints, courtyard, attributes, note):
        layers = [
            {"layer": layer, "outlines": 0, "box": None, "area": 0.0, "malformed": malformed}
            for layer, malformed in zip(("F.Courtyard", "B.Courtyard"), courtyard, strict=True)
        ]
        text = f'(footprint "Test"(attr {attributes}))'
        footprints["result"]["value"] = (
            "10.0.6",
            footprint_info(script_result(courtyard=layers, text=text)),
        )
        out = await library_tools.get_library_footprint("Lib:Test")
        assert note in out
        assert table_rows(section(out, "Courtyard"))[0][1] == (
            "malformed" if courtyard[0] else "none"
        )

    async def test_models(self, footprints, monkeypatch):
        model = footprints["libs"] / "models" / "a.step"
        model.parent.mkdir()
        model.write_text("model", encoding="utf-8")
        monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(model.parent))
        models = [
            {
                "file": "${KICAD10_3DMODEL_DIR}/a.step",
                "shown": True,
                "opacity": 1.0,
                "offset": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
                "rotation": [0.0, 0.0, 0.0],
            },
            {
                "file": "${KIPRJMOD}/b.step",
                "shown": False,
                "opacity": 0.5,
                "offset": [1.0, -2.5, 0.25],
                "scale": [1.0, 1.0, 2.0],
                "rotation": [0.0, 0.0, -90.0],
            },
        ]
        footprints["result"]["value"] = ("10.0.6", footprint_info(script_result(models=models)))
        out = await library_tools.get_library_footprint("Lib:Test")
        assert table_rows(section(out, "3D models")) == [
            [
                "${KICAD10_3DMODEL_DIR}/a.step",
                "yes",
                "found",
                str(model),
                "(0, 0, 0)",
                "(1, 1, 1)",
                "(0, 0, 0)",
                "1",
            ],
            [
                "${KIPRJMOD}/b.step",
                "no",
                "not found: The path variable KIPRJMOD has no value. Give the project to set KIPRJMOD.",
                "",
                "(1, -2.5, 0.25)",
                "(1, 1, 2)",
                "(0, 0, -90)",
                "0.5",
            ],
        ]
        # The project sets KIPRJMOD.
        (footprints["project"] / "b.step").write_text("model", encoding="utf-8")
        out = await library_tools.get_library_footprint("Lib:Test", str(footprints["project"]))
        assert table_rows(section(out, "3D models"))[1][2:4] == [
            "found",
            str(footprints["project"] / "b.step"),
        ]

    @pytest.mark.parametrize(
        "lib_id, message",
        [
            ("Test", 'is not "<library>:<footprint>"'),
            ("Nothing:Test", 'no footprint library "Nothing" is in the global library table'),
            ("Eagle:Test", 'has the type "Eagle"'),
            ("File:Test", "is not a directory"),
            ("Lib:Other", 'has no footprint "Other"'),
            # KiCad finds a footprint by the name of its file, with the same case.
            ("Lib:test", 'has no footprint "test"'),
            # The name of a file of the library has no path separator.
            ("Lib:../Lib.pretty/Test", 'has no footprint "../Lib.pretty/Test"'),
        ],
    )
    async def test_errors(self, footprints, lib_id, message):
        out = await library_tools.get_library_footprint(lib_id)
        assert out.startswith("❌") and message in out, out

    async def test_footprint_that_kicad_cannot_read(self, footprints):
        footprints["result"]["value"] = ("10.0.6", None)
        out = await library_tools.get_library_footprint("Lib:Test")
        assert out.startswith(
            "❌ Cannot read the footprint Lib:Test: KiCad 10.0.6 cannot read the file"
        )
        assert "footprint editor" in out

    async def test_pcbnew_of_an_other_kicad(self, footprints):
        # The reader prints the version of the pcbnew that read the footprint.
        footprints["result"]["value"] = ("9.0.9", footprint_info(script_result(kicad="9.0.9")))
        out = await library_tools.get_library_footprint("Lib:Test")
        assert out.startswith(
            "❌ Cannot read the footprint Lib:Test: kicad-cli is KiCad 10.0.6, but the Python "
            "with pcbnew is KiCad 9.0.9."
        ), out
        assert "KICAD_PYTHON" in out

    async def test_pcbnew_error(self, footprints, monkeypatch):
        async def read(library, name):
            raise FootprintReadError("No Python with KiCad's pcbnew module was found.")

        monkeypatch.setattr(library_tools, "read_library_footprint", read)
        out = await library_tools.get_library_footprint("Lib:Test")
        assert (
            out
            == "❌ Cannot read the footprint Lib:Test: No Python with KiCad's pcbnew module was found."
        )


FEATURE = """(footprint "feature"
\t(version 20240108)
\t(generator "pcbnew")
\t(layer "F.Cu")
\t(descr "Test \\"quoted\\" description")
\t(tags "a b")
\t(solder_mask_margin 0.05)
\t(clearance 0.2)
\t(zone_connect 2)
\t(attr smd exclude_from_bom allow_missing_courtyard dnp)
\t(net_tie_pad_groups "1, 2")
\t(property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
\t(property "Value" "feature" (at 0 2 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
\t(fp_line (start -2 -1.5) (end 2 -1.5) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
\t(fp_rect (start -1 -1) (end 1 1) (stroke (width 0.05) (type solid)) (fill none) (layer "B.CrtYd"))
\t(pad "1" smd roundrect (at -1 0) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
\t(pad "2" smd roundrect (at 1 0 90) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t(roundrect_rratio 0.2) (chamfer_ratio 0.3) (chamfer top_left bottom_right))
\t(pad "3" thru_hole oval (at 0 1.5) (size 2 1) (drill oval 1.2 0.6) (layers "*.Cu" "*.Mask")
\t\t(clearance 0.1) (zone_connect 1))
\t(pad "4" smd rect (at 0 -1.5) (size 0.5 0.5) (layers "F.Paste"))
\t(model "${KIPRJMOD}/x.step" (hide yes) (opacity 0.5) (offset (xyz 1 2 3)) (scale (xyz 1 1 2))
\t\t(rotate (xyz 0 0 90)))
)
"""


@pytest.mark.skipif(find_kicad_python() is None, reason="no Python with pcbnew")
class TestWithKiCad:
    """The real pcbnew of the installed KiCad (8.0 and later)."""

    async def test_footprint(self, tmp_path):
        (tmp_path / "feature.kicad_mod").write_text(FEATURE, encoding="utf-8")
        version, info = await read_library_footprint(tmp_path, "feature")
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        assert info.description == 'Test "quoted" description' and info.keywords == "a b"
        assert info.attributes == ("smd", "exclude_from_bom", "allow_missing_courtyard", "dnp")
        assert info.net_tie_pad_groups == ("1, 2",)
        assert (info.clearance, info.solder_mask_margin, info.zone_connection) == (
            200000,
            50000,
            "full",
        )
        assert [(c.layer, c.outlines, c.malformed) for c in info.courtyard] == [
            ("F.Courtyard", 0, True),
            ("B.Courtyard", 1, False),
        ]
        assert [
            (p.number, p.type, p.shape.shape, p.position, p.orientation) for p in info.pads
        ] == [
            ("1", "smd", "roundrect", (-1000000, 0), 0.0),
            ("2", "smd", "chamfered_rect", (1000000, 0), 90.0),
            ("3", "pth", "oval", (0, 1500000), 0.0),
            # An aperture pad (no copper layer) gets no number (PAD::CanHaveNumber).
            ("", "smd", "rectangle", (0, -1500000), 0.0),
        ]
        assert info.pads[1].shape.chamfer_positions == 9
        assert info.pads[2].drill == (1200000, 600000) and info.pads[2].drill_shape == "oblong"
        assert info.pads[2].layers[0] == "*.Cu"
        assert [p.on_copper for p in info.pads] == [True, True, True, False]
        assert (info.pads[2].clearance, info.pads[2].zone_connection) == (100000, "thermal")
        model = info.models[0]
        assert (model.file, model.shown, model.opacity) == ("${KIPRJMOD}/x.step", False, 0.5)
        assert (model.offset, model.scale, model.rotation) == ((1, 2, 3), (1, 1, 2), (0, 0, 90))

    async def test_items_of_footprints(self, tmp_path):
        (tmp_path / "feature.kicad_mod").write_text(FEATURE, encoding="utf-8")
        (tmp_path / "broken.kicad_mod").write_text(
            '(footprint "broken" (version 20240108) (pad "1" smd rect (at 0 0) (size 1 x)))\n',
            encoding="utf-8",
        )
        version, items = await read_footprints(
            [(tmp_path, "broken"), (tmp_path, "feature"), (tmp_path, "missing")]
        )
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        # The cache of the library has no broken file, and the other files.
        assert items[0] is None and items[2] is None
        assert items[1] == FootprintItems(
            pads=(
                PadNumber("1", True),
                PadNumber("2", True),
                PadNumber("3", True),
                PadNumber("", False),
            ),
            models=(("${KIPRJMOD}/x.step", False),),
            embedded_files=None,
        )
        assert await read_footprints([]) == ("", [])

    async def test_embedded_files_of_a_footprint(self, tmp_path):
        import base64
        import hashlib
        import json

        zstd = pytest.importorskip("compression.zstd")
        from kicad_workbench.utils import library_footprint
        from kicad_workbench.utils.kicad_python import run_kicad_python

        data = bytes(range(256)) * 400
        encoded = base64.b64encode(zstd.compress(data)).decode("ascii")
        (tmp_path / "emb.kicad_mod").write_text(
            '(footprint "emb" (version 20241229) (layer "F.Cu")\n'
            '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            f'  (embedded_files (file (name "e.step") (type model) (data |{encoded}|)\n'
            f'    (checksum "{hashlib.sha256(data).hexdigest()}")))\n'
            '  (model "kicad-embed://e.step"))\n',
            encoding="utf-8",
        )
        version, items = await read_footprints([(tmp_path, "emb")], embedded=True)
        if version.startswith("8."):
            pytest.skip("KiCad 8 has no embedded files")
        assert items[0].models == (("kicad-embed://e.step", True),)
        assert items[0].embedded_files == ("e.step",)
        assert (await read_footprints([(tmp_path, "emb")]))[1][0].embedded_files is None
        # The writer (CTL_FOR_BOARD) gives the names of the files with no data.
        request = {"items": [[str(tmp_path), "emb"]], "embedded": True}
        result = await run_kicad_python(
            ["-c", library_footprint._ITEMS_SCRIPT], input=json.dumps(request).encode()
        )
        text = json.loads(
            result_line(result.stdout, library_footprint._ITEMS_PREFIX)
        )["items"][0]["text"]
        assert '(name "e.step")' in text and "(data" not in text and encoded[:40] not in text

    async def test_footprint_that_kicad_cannot_read(self, tmp_path):
        (tmp_path / "broken.kicad_mod").write_text(
            '(footprint "broken" (version 20240108) (pad "1" smd rect (at 0 0) (size 1 x)))\n',
            encoding="utf-8",
        )
        version, info = await read_library_footprint(tmp_path, "broken")
        assert info is None and version
        assert (await read_library_footprint(tmp_path, "missing"))[1] is None
