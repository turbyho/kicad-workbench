"""Tests for utils/svg_image.py and the tool render_library_item."""

import zlib
from itertools import pairwise
from pathlib import Path

import pytest
from fastmcp.utilities.types import Image

from kicad_workbench.tools import library as library_tools
from kicad_workbench.utils.kicad_cli import find_kicad_cli
from kicad_workbench.utils.svg_image import (
    MIN_PX_PER_MM,
    SvgImageError,
    Tile,
    plan_images,
    png_size,
    read_svg,
    render_tile,
    required_scale,
)

from .test_library_search import make_tables, symbol, symbol_lib

# U: two units and the De Morgan body styles (a drawing of body style 2).
# Two: two units. UD: derived from U.
UNITS = (
    '\t(symbol "U"\n'
    '\t\t(symbol "U_1_1"\n\t\t\t(rectangle (start -1 1) (end 1 -1))\n\t\t)\n'
    '\t\t(symbol "U_1_2"\n\t\t\t(circle (center 0 0) (radius 1))\n\t\t)\n'
    '\t\t(symbol "U_2_1"\n\t\t\t(rectangle (start -1 1) (end 1 -1))\n\t\t)\n'
    "\t)\n"
    '\t(symbol "Two"\n'
    '\t\t(symbol "Two_1_1"\n\t\t\t(rectangle (start -1 1) (end 1 -1))\n\t\t)\n'
    '\t\t(symbol "Two_2_1"\n\t\t\t(rectangle (start -1 1) (end 1 -1))\n\t\t)\n'
    "\t)\n"
)


def text(size: float, length: float) -> str:
    """A hidden <text> as kicad-cli writes it for a visible text of the size (mm)."""
    return (
        f'<text x="0" y="0" textLength="{length}" font-size="{size * 4 / 3}" '
        'lengthAdjust="spacingAndGlyphs" text-anchor="start" opacity="0" '
        'stroke-opacity="0">T</text>\n'
    )


def svg(width: float = 10, height: float = 5, body: str = "", view: str = "") -> str:
    return (
        '<?xml version="1.0" standalone="no"?>\n'
        ' <!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" \n'
        ' "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"> \n'
        '<svg xmlns:svg="http://www.w3.org/2000/svg" xmlns="http://www.w3.org/2000/svg" '
        f'version="1.1" width="{width}mm" height="{height}mm" '
        f'viewBox="{view or f"0 0 {width} {height}"}">\n'
        f'<g style="stroke-width:0.1">{body}</g></svg>\n'
    )


def pixels(png: bytes) -> list[bytes]:
    """The rows of RGBA pixels of a PNG of resvg (8 bits for each channel)."""
    data, position = b"", 8
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        if png[position + 4 : position + 8] == b"IDAT":
            data += png[position + 8 : position + 8 + length]
        position += 12 + length
    width, height = png_size(png)
    raw, stride, rows, previous = zlib.decompress(data), width * 4, [], bytes(width * 4)
    for index in range(height):
        kind = raw[index * (stride + 1)]
        line = bytearray(raw[index * (stride + 1) + 1 : (index + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - 4] if i >= 4 else 0
            up, corner = previous[i], previous[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[i] = (
                    line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)
                ) & 0xFF
        rows.append(bytes(line))
        previous = line
    return rows


def pixel(png: bytes, x: int, y: int) -> tuple[int, ...]:
    return tuple(pixels(png)[y][x * 4 : x * 4 + 4])


BOARD = (0x00, 0x10, 0x23, 0xFF)
PAPER = (0xF5, 0xF4, 0xEF, 0xFF)
RED = (0xFF, 0x00, 0x00, 0xFF)


class TestReadSvg:
    def test_page_and_texts(self):
        page = read_svg(svg(5.0292, 10.0584, text(1.27, 2.5) + text(0.762, 9) + text(0, 3)))
        assert (page.width, page.height, page.view) == (5.0292, 10.0584, (0, 0, 5.0292, 10.0584))
        assert page.smallest_text == pytest.approx(0.762)
        assert page.longest_text == pytest.approx(9)

    def test_viewbox_units(self):
        # 10 viewBox units in one mm: the text sizes are in mm.
        page = read_svg(svg(10, 5, text(12.7, 30), view="0 0 100 50"))
        assert page.smallest_text == pytest.approx(1.27)
        assert page.longest_text == pytest.approx(3)

    def test_no_text(self):
        page = read_svg(svg())
        assert (page.smallest_text, page.longest_text) == (None, 0.0)
        assert required_scale(page) == MIN_PX_PER_MM

    @pytest.mark.parametrize(
        ("size", "scale"),
        [(1.27, MIN_PX_PER_MM), (2.54, MIN_PX_PER_MM), (0.381, 15 / 0.381)],
    )
    def test_required_scale(self, size, scale):
        assert required_scale(read_svg(svg(body=text(size, 1)))) == pytest.approx(scale)

    @pytest.mark.parametrize(
        ("source", "message"),
        [
            ("<svg", "cannot be read"),
            ('<svg width="1mm" height="1mm"/>', "not svg"),
            (svg().replace('width="10mm"', 'width="10"'), 'the width "10", not a length in mm'),
            (svg().replace('height="5mm"', 'height="5in"'), 'the height "5in"'),
            (svg(view="0 0 10"), 'the viewBox "0 0 10"'),
            (svg(view="0 0 10 0"), 'the viewBox "0 0 10 0"'),
            (svg(0, 5, view="0 0 1 5"), "no area"),
            (svg(view="0 0 10 10"), "different proportions"),
            (svg(body=text(1, 1).replace('textLength="1"', 'textLength="x"')), "not a number"),
        ],
    )
    def test_errors(self, source, message):
        with pytest.raises(SvgImageError, match=message):
            read_svg(source)


class TestPlanImages:
    def test_fit_gives_one_large_image(self):
        page = read_svg(svg(10, 5, text(1.27, 4)))
        plan = plan_images(page, "tiles", 1000, 1000)
        assert plan.tiles == (Tile(0.0, 0.0, 10, 5, 100.0),)
        assert plan.tiles[0].pixels == (1000, 500)

    def test_tiles_at_the_legible_scale(self):
        # 100 × 300 mm at 300 DPI is 1181 × 3543 px: more than 1000 × 1000 px.
        # A tile has 84.67 mm, thus 2 columns and ceil((300 - 15) / (84.67 - 15)) = 5 rows.
        page = read_svg(svg(100, 300, text(1.27, 15)))
        plan = plan_images(page, "tiles", 1000, 1000)
        scale = MIN_PX_PER_MM
        side = 1000 / scale
        assert {tile.scale for tile in plan.tiles} == {scale}
        assert plan.overlap == pytest.approx(15) and plan.split_text is None
        columns = sorted({tile.x for tile in plan.tiles})
        rows = sorted({tile.y for tile in plan.tiles})
        assert len(plan.tiles) == len(columns) * len(rows) == 2 * 5
        for starts, length in ((columns, 100), (rows, 300)):
            assert starts[0] == 0 and starts[-1] + side == pytest.approx(length)
            assert all(b - a <= side - 15 + 1e-9 for a, b in pairwise(starts))
        assert all(max(tile.pixels) <= 1000 for tile in plan.tiles)

    def test_long_text_is_limited_to_half_a_tile(self):
        page = read_svg(svg(100, 300, text(1.27, 80)))
        plan = plan_images(page, "tiles", 1000, 1000)
        assert plan.overlap == pytest.approx(500 / MIN_PX_PER_MM)
        assert plan.split_text == plan.overlap

    def test_small_text_gives_more_tiles(self):
        page = read_svg(svg(20, 10, text(0.381, 2)))
        plan = plan_images(page, "tiles", 400, 400)
        assert all(tile.scale == pytest.approx(15 / 0.381) for tile in plan.tiles)
        assert len(plan.tiles) > 1

    def test_overview(self):
        page = read_svg(svg(100, 300, text(1.27, 15)))
        plan = plan_images(page, "overview", 1000, 1000)
        assert plan.tiles == (Tile(0.0, 0.0, 100, 300, 1000 / 300),)

    def test_region(self):
        page = read_svg(svg(100, 300, text(1.27, 15)))
        plan = plan_images(page, "tiles", 1000, 1000, (-10, 290, 40, 20))
        assert plan.tiles == (Tile(0.0, 290.0, 30, 10, 1000 / 30),)

    def test_region_off_the_page(self):
        page = read_svg(svg())
        with pytest.raises(
            SvgImageError, match=r"the region \(20, 0, 5, 5\) mm is not on the page"
        ):
            plan_images(page, "tiles", 100, 100, (20, 0, 5, 5))
        with pytest.raises(SvgImageError, match="is not on the page"):
            plan_images(page, "tiles", 100, 100, (0, 0, 0, 5))


class TestRenderTile:
    def test_size_and_background(self):
        page = read_svg(svg())
        png = render_tile(page, Tile(0, 0, 10, 5, 33.3), "#001023")
        assert png_size(png) == (333, 166)
        assert pixel(png, 0, 0) == BOARD

    def test_part_of_the_page(self):
        # A red right half: the tile of the right half is red, the left one is not.
        page = read_svg(svg(body='<rect x="5" y="0" width="5" height="5" fill="#FF0000"/>'))
        right = render_tile(page, Tile(5, 0, 5, 5, 10), "#F5F4EF")
        left = render_tile(page, Tile(0, 0, 5, 5, 10), "#F5F4EF")
        assert png_size(right) == png_size(left) == (50, 50)
        assert pixel(right, 0, 0) == pixel(right, 49, 49) == RED
        assert pixel(left, 0, 0) == pixel(left, 49, 49) == PAPER

    def test_small_tile_keeps_its_proportions(self):
        # resvg rounds a size in mm to full pixels: the tile gives its size in pixels.
        page = read_svg(svg(10, 3.3333, view="0 0 10 3.3333"))
        assert png_size(render_tile(page, Tile(0, 0, 10, 3.3333, 100), "#001023")) == (1000, 333)

    def test_no_root_size(self):
        page = read_svg(svg())
        page = type(page)(
            page.text.replace(' viewBox="0 0 10 5"', ""), page.width, page.height, page.view, ()
        )
        with pytest.raises(SvgImageError, match="no single width, height and viewBox"):
            render_tile(page, Tile(0, 0, 10, 5, 10), "#001023")


@pytest.fixture
def render(tmp_path, monkeypatch):
    """Library tables with a symbol and a footprint library, and a fake kicad-cli."""
    tables = make_tables(tmp_path, monkeypatch)
    (tables["libs"] / "Device.kicad_sym").write_text(
        symbol_lib(symbol("R"), UNITS, symbol("UD", extends="U")), encoding="utf-8"
    )
    pretty = tables["libs"] / "Lib.pretty"
    pretty.mkdir()
    (pretty / "Test.kicad_mod").write_text('(footprint "Test"\n)\n', encoding="utf-8")
    (tables["config"] / "10.0" / "fp-lib-table").write_text(
        "(fp_lib_table\n\t(version 7)\n" + tables["lib"]("Lib", pretty) + "\n)\n",
        encoding="utf-8",
    )
    state = {
        "files": {"R_unit1.svg": svg(body=text(1.27, 2))},
        "returncode": 0,
        "stderr": b"",
        "calls": [],
    }

    class Result:
        def __init__(self):
            self.returncode = state["returncode"]
            self.stdout = b""
            self.stderr = state["stderr"]

    def fake(args, timeout=60.0, english=False):
        state["calls"].append(args)
        output = Path(args[args.index("--output") + 1])
        assert output.is_dir()
        for name, content in state["files"].items():
            (output / name).write_text(content, encoding="utf-8")
        return Result()

    monkeypatch.setattr(library_tools, "run_kicad_cli_sync", fake)
    return {**tables, "pretty": pretty, "state": state}


async def render_item(lib_id, item_type, width=1000, height=1000, **options):
    return await library_tools.render_library_item(lib_id, item_type, width, height, **options)


class TestRenderLibraryItem:
    async def test_symbol(self, render):
        render["state"]["files"] = {
            # 8 × 4 mm at 300 DPI is 94 × 47 px: it fits in 100 × 100 px.
            "U_unit10.svg": svg(8, 4),
            "U_unit2.svg": svg(4, 8),
            "U_unit1_demorgan.svg": svg(8, 4),
            "U_unit1.svg": svg(body=text(1.27, 2)),
        }
        out = await render_item("Device:U", "symbol", 100, 100)
        args = render["state"]["calls"][0]
        assert args[:5] == ["sym", "export", "svg", "--symbol", "U"]
        assert args[5:7] == ["--theme", "_builtin_default"]
        assert args[-1] == str(render["libs"] / "Device.kicad_sym")
        assert "--include-hidden-pins" not in args
        summary = out[0]
        assert summary.startswith("# Rendered symbol Device:U")
        assert "**Images**: 5 PNG, layout tiles, at most 100 × 100 px" in summary
        assert (
            "- U_unit1: 10 × 5 mm, smallest text 1.27 mm, legible from 11.8 px/mm (300 DPI)"
            in summary
        )
        assert "- U_unit2: 4 × 8 mm, no text, legible from 11.8 px/mm (300 DPI)" in summary
        assert "_Hidden pins and hidden fields are not shown (include_hidden)._" in summary
        # Four pages: two units with two body styles.
        assert "**Warning**" not in summary
        # 10 × 5 mm at 300 DPI is 118 × 59 px: two tiles. U_unit2 fits: one image.
        assert out[1::2] == [
            "U_unit1: tile 1 of 2, x 0 to 8.47 mm, y 0 to 5 mm, 11.8 px/mm (300 DPI), "
            "100 × 59 px, smallest text 15 px",
            "U_unit1: tile 2 of 2, x 1.53 to 10 mm, y 0 to 5 mm, 11.8 px/mm (300 DPI), "
            "100 × 59 px, smallest text 15 px",
            "U_unit1_demorgan: x 0 to 8 mm, y 0 to 4 mm, 12.5 px/mm (318 DPI), 100 × 50 px",
            "U_unit2: x 0 to 4 mm, y 0 to 8 mm, 12.5 px/mm (318 DPI), 50 × 100 px",
            "U_unit10: x 0 to 8 mm, y 0 to 4 mm, 12.5 px/mm (318 DPI), 100 × 50 px",
        ]
        assert (
            "_Tiles that touch overlap by at least 2 mm (the labels give the region of each tile)._"
        ) in summary
        assert all(isinstance(image, Image) for image in out[2::2])
        assert pixel(out[2].data, 0, 0) == PAPER

    async def test_pages(self, render):
        render["state"]["files"] = {"Two_unit1.svg": svg(), "Two_unit2.svg": svg(5, 10)}
        out = await render_item("Device:Two", "symbol", pages=["Two_unit2"])
        assert out[1::2] == [
            "Two_unit2: x 0 to 5 mm, y 0 to 10 mm, 100.0 px/mm (2540 DPI), 500 × 1000 px"
        ]
        assert "- Two_unit1:" not in out[0]
        out = await render_item("Device:Two", "symbol", pages=["Two_unit3", "Two_2"])
        assert out == (
            "❌ The symbol Device:Two has no page 'Two_unit3', 'Two_2'. "
            "Its pages are: Two_unit1, Two_unit2."
        )

    async def test_page_count(self, render):
        # kicad-cli of KiCad 10.0.0 to 10.0.6 draws only the first body style
        # of a derived symbol.
        render["state"]["files"] = {"UD_unit1.svg": svg(), "UD_unit2.svg": svg()}
        out = await render_item("Device:UD", "symbol")
        assert (
            "**Warning**: kicad-cli wrote 2 pages, but the symbol has 2 units and 2 body "
            "styles (4 pages). kicad-cli of KiCad 10.0.0 to 10.0.6 draws only the first body "
            "style of a derived symbol (KiCad issue 25209, repaired in KiCad 10.0.7)."
        ) in out[0]
        assert len(out) == 5
        # Not a derived symbol: no known cause.
        render["state"]["files"] = {"U_unit1.svg": svg()}
        out = await render_item("Device:U", "symbol")
        assert out[0].splitlines()[-2] == (
            "**Warning**: kicad-cli wrote 1 page, but the symbol has 2 units and 2 body "
            "styles (4 pages)."
        )

    async def test_page_count_of_kicad_10_0_7(self, render, monkeypatch):
        # KiCad 10.0.7 repaired the issue 25209: it is not the cause.
        async def version():
            return "10.0.7"

        monkeypatch.setattr(library_tools, "get_kicad_version", version)
        render["state"]["files"] = {"UD_unit1.svg": svg(), "UD_unit2.svg": svg()}
        out = await render_item("Device:UD", "symbol")
        assert "**Warning**: kicad-cli wrote 2 pages" in out[0]
        assert "issue 25209" not in out[0]

    async def test_region_on_some_pages(self, render):
        render["state"]["files"] = {"Two_unit1.svg": svg(20, 10), "Two_unit2.svg": svg(10, 5)}
        out = await render_item("Device:Two", "symbol", region=[12, 0, 4, 4])
        assert out[1::2] == [
            "Two_unit1: x 12 to 16 mm, y 0 to 4 mm, 250.0 px/mm (6350 DPI), 1000 × 1000 px"
        ]
        assert (
            "**Not on the region [12, 0, 4, 4] mm** (no image):\n"
            "- Two_unit2: 10 × 5 mm, no text, legible from 11.8 px/mm (300 DPI)"
        ) in out[0]
        out = await render_item("Device:Two", "symbol", region=[30, 0, 4, 4])
        assert out == (
            "❌ The region [30, 0, 4, 4] mm is not on a page of the symbol Device:Two:\n"
            "- Two_unit1: 20 × 10 mm, no text, legible from 11.8 px/mm (300 DPI)\n"
            "- Two_unit2: 10 × 5 mm, no text, legible from 11.8 px/mm (300 DPI)"
        )

    async def test_page_count_of_a_symbol_that_the_server_cannot_read(self, render):
        library = render["project"] / "Device.kicad_sym"
        library.write_text(
            symbol_lib(symbol("R")).replace("20241209", "20991231"), encoding="utf-8"
        )
        out = await render_item("Device:R", "symbol", project_path=str(render["project"]))
        assert (
            "**Warning**: the server cannot read the symbol, thus it did not compare the "
            "pages with the units and body styles of the symbol: The symbol library file "
            f"{library} has the file version 20991231"
        ) in out[0]
        assert len(out) == 3

    async def test_overview_and_region(self, render):
        render["state"]["files"] = {"R_unit1.svg": svg(100, 300, text(1.27, 15))}
        out = await render_item("Device:R", "symbol", layout="overview")
        assert out[1] == (
            "R_unit1: x 0 to 100 mm, y 0 to 300 mm, 3.3 px/mm (85 DPI), 333 × 1000 px, "
            "smallest text 4 px (can be not legible: use the layout tiles or a region)"
        )
        out = await render_item("Device:R", "symbol", region=[10, 20, 30, 40])
        assert out[1] == (
            "R_unit1: x 10 to 40 mm, y 20 to 60 mm, 25.0 px/mm (635 DPI), 750 × 1000 px, "
            "smallest text 32 px"
        )

    async def test_max_images(self, render):
        render["state"]["files"] = {"R_unit1.svg": svg(100, 300, text(1.27, 15))}
        out = await render_item("Device:R", "symbol", max_images=9)
        assert out == (
            "❌ The symbol Device:R needs 10 images of at most 1000 × 1000 px for legible text, "
            "more than max_images (9):\n"
            "- R_unit1: 100 × 300 mm, smallest text 1.27 mm, legible from 11.8 px/mm (300 DPI)\n\n"
            "Select some pages, or use the layout overview, a region, a larger max_images, or "
            "larger images if your vision input reads them with no reduction."
        )
        out = await render_item("Device:R", "symbol", max_images=10)
        assert len(out) == 21
        assert "**Warning**" not in out[0]

    async def test_project_library(self, render):
        out = await render_item(
            "Device:R", "symbol", project_path=str(render["project"]), include_hidden=True
        )
        args = render["state"]["calls"][0]
        assert args[-1] == str(render["project"] / "Device.kicad_sym")
        assert "--include-hidden-pins" in args and "--include-hidden-fields" in args
        assert "include_hidden" not in out[0]

    async def test_footprint(self, render):
        render["state"]["files"] = {"Test.svg": svg(body=text(1, 3))}
        out = await render_item("Lib:Test", "footprint", 800, 800)
        args = render["state"]["calls"][0]
        assert args[:5] == ["fp", "export", "svg", "--footprint", "Test"]
        assert args[-1] == str(render["pretty"])
        assert "**Pages** (one for each footprint, the origin is the top left corner):" in out[0]
        assert out[1] == (
            "Test: x 0 to 10 mm, y 0 to 5 mm, 80.0 px/mm (2032 DPI), 800 × 400 px, "
            "smallest text 80 px"
        )
        assert pixel(out[2].data, 0, 0) == BOARD

    async def test_output_dir(self, render, tmp_path):
        target = tmp_path / "out" / "new"
        render["state"]["files"] = {
            "Two_unit1.svg": svg(body=text(1.27, 2)),
            "Two_unit2.svg": svg(8, 4),
        }
        out = await render_item("Device:Two", "symbol", 100, 100, output_dir=str(target))
        assert (
            f"**Files** in {target}: Two_unit1.svg, Two_unit1_tile1.png, Two_unit1_tile2.png, "
            "Two_unit2.svg, Two_unit2.png"
        ) in out[0]
        assert (target / "Two_unit1.svg").read_text(encoding="utf-8") == svg(body=text(1.27, 2))
        assert (target / "Two_unit1_tile1.png").read_bytes() == out[2].data
        assert (target / "Two_unit1_tile2.png").read_bytes() == out[4].data
        assert (target / "Two_unit2.png").read_bytes() == out[6].data

    async def test_missing_footprint(self, render):
        out = await render_item("Lib:Nope", "footprint")
        assert out == (
            "❌ Cannot render the footprint Lib:Nope: the library "
            f'"Lib" ({render["pretty"]}) has no footprint "Nope".'
        )
        assert render["state"]["calls"] == []

    async def test_footprint_with_no_file(self, render):
        # kicad-cli gives the exit code 0 when it cannot write the file.
        render["state"]["files"] = {}
        out = await render_item("Lib:Test", "footprint")
        assert out.startswith(
            "❌ kicad-cli of KiCad 10.0.6 did not render the footprint Lib:Test "
            "(exit code 0, 0 SVG files):"
        )

    async def test_missing_symbol(self, render):
        render["state"].update(
            files={}, returncode=1, stderr=b"There is no symbol selected to save.\n"
        )
        out = await render_item("Device:X", "symbol")
        assert out == (
            '❌ Cannot render the symbol Device:X: the library "Device" '
            f'({render["libs"] / "Device.kicad_sym"}) has no symbol "X".'
        )

    async def test_kicad_cli_error(self, render):
        # The library has the symbol: the output of kicad-cli is the reason.
        render["state"].update(files={}, returncode=1, stderr=b"Unable to load library\n")
        out = await render_item("Device:R", "symbol")
        assert out == (
            "❌ kicad-cli of KiCad 10.0.6 did not render the symbol Device:R "
            "(exit code 1, 0 SVG files):\n\nUnable to load library"
        )

    async def test_timeout(self, render, monkeypatch):
        import subprocess

        def slow(args, timeout=60.0, english=False):
            raise subprocess.TimeoutExpired(args, timeout)

        monkeypatch.setattr(library_tools, "run_kicad_cli_sync", slow)
        out = await render_item("Device:R", "symbol")
        assert out == "❌ kicad-cli of KiCad 10.0.6 did not stop in 120 s."

    async def test_bad_svg(self, render):
        render["state"]["files"] = {"R_unit1.svg": svg().replace('width="10mm"', 'width="10px"')}
        out = await render_item("Device:R", "symbol")
        assert out.startswith(
            "❌ Cannot draw the symbol Device:R: R_unit1.svg: the SVG has the width"
        )

    @pytest.mark.parametrize(
        ("args", "options", "message"),
        [
            (("Device:R", "board"), {}, "❌ The item type 'board' is not 'symbol' or 'footprint'."),
            (("Device:R", "symbol", 63), {}, "❌ max_width_px 63 is not from 64 to 8192 pixels."),
            (("Device:R", "symbol", 64, 8193), {}, "❌ max_height_px 8193 is not from 64 to"),
            (("Device:R", "symbol"), {"layout": "grid"}, "❌ The layout 'grid' is not"),
            (("Device:R", "symbol"), {"region": [1, 2, 3]}, "❌ The region [1, 2, 3] is not"),
            (
                ("Device:R", "symbol"),
                {"region": [1, 2, float("nan"), 3]},
                "❌ The region [1, 2, nan, 3] is not",
            ),
            (
                ("Device:R", "symbol"),
                {"region": [1, 2, 3, float("inf")]},
                "❌ The region [1, 2, 3, inf] is not",
            ),
            (("Device:R", "symbol"), {"max_images": 0}, "❌ max_images 0 is less than 1."),
            (("Lib:Test", "footprint"), {"include_hidden": True}, "❌ include_hidden applies only"),
            (("Device", "symbol"), {}, "❌ The library ID 'Device' is not \"<library>:<symbol>\""),
            (
                ("Lib:", "footprint"),
                {},
                "❌ The library ID 'Lib:' is not \"<library>:<footprint>\"",
            ),
            (
                ("Nope:R", "symbol"),
                {},
                '❌ Cannot render the symbol Nope:R: no symbol library "Nope"',
            ),
            (
                ("Legacy:R", "symbol"),
                {},
                '❌ Cannot render the symbol Legacy:R: the library "Legacy"',
            ),
        ],
    )
    async def test_errors(self, render, args, options, message):
        out = await render_item(*args, **options)
        assert isinstance(out, str) and out.startswith(message), out
        assert render["state"]["calls"] == []

    async def test_no_kicad(self, render, monkeypatch):
        async def version():
            return None

        monkeypatch.setattr(library_tools, "get_kicad_version", version)
        out = await render_item("Device:R", "symbol")
        assert out.startswith("❌ kicad-cli was not found")


@pytest.mark.skipif(find_kicad_cli() is None, reason="no kicad-cli")
class TestWithKiCad:
    """The real kicad-cli of the installed KiCad (8.0 and later)."""

    async def test_symbol_and_footprint(self, tmp_path, monkeypatch):
        # make_tables gives the version 10.0.6 for the directory of the library
        # tables. The real kicad-cli draws the items.
        tables = make_tables(tmp_path, monkeypatch)
        (tables["libs"] / "Device.kicad_sym").write_text(SYMBOL_LIB, encoding="utf-8")
        pretty = tables["libs"] / "Lib.pretty"
        pretty.mkdir()
        (pretty / "Pad.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
        (tables["config"] / "10.0" / "fp-lib-table").write_text(
            "(fp_lib_table\n\t(version 7)\n" + tables["lib"]("Lib", pretty) + "\n)\n",
            encoding="utf-8",
        )

        out = await render_item("Device:Two", "symbol", 200, 200)
        assert isinstance(out, list), out
        assert len(out) == 5
        assert "smallest text 1.27 mm" in out[0]
        assert all(isinstance(image, Image) for image in out[2::2])
        assert max(png_size(out[2].data)) == 200
        assert pixel(out[2].data, 0, 0) == PAPER

        out = await render_item("Lib:Pad", "footprint", 200, 200)
        assert isinstance(out, list), out
        assert out[1].startswith("Pad: x 0 to ")
        assert "smallest text 1 mm" in out[0]
        assert max(png_size(out[2].data)) == 200
        assert pixel(out[2].data, 0, 0) == BOARD


# A symbol with two units, in the format of KiCad 8.0, which KiCad 8, 9 and 10 read.
SYMBOL_LIB = """\
(kicad_symbol_lib
	(version 20231120)
	(generator "kicad_symbol_editor")
	(symbol "Two"
		(in_bom yes)
		(on_board yes)
		(property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
		(property "Value" "Two" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
		(symbol "Two_1_1"
			(rectangle (start -2.54 2.54) (end 2.54 -2.54)
				(stroke (width 0.254) (type default)) (fill (type background)))
			(pin input line (at -5.08 0 0) (length 2.54)
				(name "A" (effects (font (size 1.27 1.27))))
				(number "1" (effects (font (size 1.27 1.27)))))
		)
		(symbol "Two_2_1"
			(circle (center 0 0) (radius 2.54)
				(stroke (width 0.254) (type default)) (fill (type none)))
		)
	)
)
"""

# A footprint with one pad, in the format of KiCad 8.0.
FOOTPRINT = """\
(footprint "Pad"
	(version 20240108)
	(generator "pcbnew")
	(layer "F.Cu")
	(property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
		(effects (font (size 1 1) (thickness 0.15))))
	(property "Value" "Pad" (at 0 2 0) (layer "F.Fab")
		(effects (font (size 1 1) (thickness 0.15))))
	(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""
