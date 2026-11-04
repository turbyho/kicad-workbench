"""Draw an SVG file of kicad-cli as PNG images that a model can read.

The multimodal models of an MCP client read PNG, JPEG, GIF and WebP images,
not SVG. PNG keeps thin lines and small text sharp (no loss). resvg (the
package resvg-py) draws the SVG with no system library.

The model knows the largest image that it reads without a reduction, the MCP
server does not: the caller gives that size. This module then selects the
scale and divides the item into tiles.
"""

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import resvg_py

_SVG = "{http://www.w3.org/2000/svg}"
_MM = re.compile(r"(\d+(?:\.\d*)?|\.\d+)mm")
_ROOT_TAG = re.compile(r"<svg\b[^>]*>")
_ROOT_SIZE = re.compile(r'\s(width|height|viewBox)="[^"]*"')

# The text of a library item must be legible: its smallest text gets at least
# 15 pixels, the height of a 1.27 mm text (the KiCad default) at 300 DPI.
# The scale is never less than 300 DPI.
MIN_TEXT_PX = 15.0
MIN_PX_PER_MM = 300 / 25.4


class SvgImageError(Exception):
    """The SVG cannot be read or drawn."""


class RegionError(SvgImageError):
    """The region is not on the page."""


@dataclass(frozen=True)
class SvgPage:
    """An SVG of kicad-cli: its page size, and the size of its texts."""

    text: str
    width: float  # mm
    height: float  # mm
    view: tuple[float, float, float, float]  # the viewBox
    # The height of each visible text in mm, and its longest side (the
    # length or the height, rotated or not) in mm.
    texts: tuple[tuple[float, float], ...]

    @property
    def smallest_text(self) -> float | None:
        """The height of the smallest visible text in mm, or None."""
        return min((height for height, _extent in self.texts), default=None)

    @property
    def longest_text(self) -> float:
        """The longest side of a visible text in mm, or 0."""
        return max((extent for _height, extent in self.texts), default=0.0)


def read_svg(text: str) -> SvgPage:
    """Read the page size and the text sizes of an SVG of kicad-cli.

    kicad-cli writes the page size in mm (for example width="5.0292mm"), and
    a viewBox with the same proportions. For each visible text it writes a
    <text> element with opacity 0 for a search (SVG_PLOTTER::Text in KiCad 8,
    9 and 10): font-size is 4/3 of the text size, and textLength is the length
    of the text.

    Raises:
        SvgImageError: The text is not such an SVG.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SvgImageError(f"the SVG cannot be read: {e}") from e
    if root.tag != f"{_SVG}svg":
        raise SvgImageError(f"the root element is {root.tag}, not svg")
    size = []
    for name in ("width", "height"):
        match = _MM.fullmatch(root.get(name, ""))
        if match is None:
            raise SvgImageError(
                f'the SVG has the {name} "{root.get(name, "")}", not a length in mm'
            )
        size.append(float(match.group(1)))
    width, height = size
    try:
        view = tuple(float(part) for part in root.get("viewBox", "").split())
    except ValueError:
        view = ()
    if len(view) != 4 or view[2] <= 0 or view[3] <= 0:
        raise SvgImageError(f'the SVG has the viewBox "{root.get("viewBox", "")}"')
    if width <= 0 or height <= 0:
        raise SvgImageError(f"the SVG has no area ({width} × {height} mm)")
    units = view[2] / width  # viewBox units in one mm
    if not math.isclose(units, view[3] / height, rel_tol=1e-3):
        raise SvgImageError("the viewBox and the page size have different proportions")
    texts = []
    for element in root.iter(f"{_SVG}text"):
        try:
            font_size = float(element.get("font-size", ""))
            length = float(element.get("textLength", "0"))
        except ValueError as e:
            raise SvgImageError(f"a text has a size that is not a number: {e}") from e
        if font_size <= 0:
            continue  # KiCad draws no text of size 0
        texts.append((font_size * 3 / 4 / units, max(length, font_size) / units))
    return SvgPage(text, width, height, view, tuple(texts))


@dataclass(frozen=True)
class Tile:
    """One image: a part of the page in mm (the origin is the top left corner)."""

    x: float
    y: float
    width: float
    height: float
    scale: float  # pixels per mm

    @property
    def pixels(self) -> tuple[int, int]:
        return max(1, round(self.width * self.scale)), max(1, round(self.height * self.scale))


@dataclass(frozen=True)
class Plan:
    tiles: tuple[Tile, ...]
    # The smallest overlap of two tiles that touch, in mm. The tiles have equal
    # steps, thus they can overlap more.
    overlap: float
    split_text: float | None  # texts longer than this (mm) can be split over two tiles


def required_scale(page: SvgPage) -> float:
    """The smallest scale (pixels per mm) at which the text is legible."""
    smallest = page.smallest_text
    if smallest is None:
        return MIN_PX_PER_MM
    return max(MIN_PX_PER_MM, MIN_TEXT_PX / smallest)


def plan_images(
    page: SvgPage,
    layout: str,
    max_width: int,
    max_height: int,
    region: tuple[float, float, float, float] | None = None,
) -> Plan:
    """Divide the page (or a region of it) into images of at most max_width × max_height px.

    layout "overview" gives one image of the full region, as large as the
    limit permits. layout "tiles" gives one image when the region fits at
    the scale of required_scale, as large as the limit permits. Else it
    divides the region into tiles at that scale. Two tiles that touch overlap
    by the longest text (or by half a tile, if that is less), thus each such
    text is fully in one tile.

    Raises:
        RegionError: The region is not on the page.
    """
    x, y, width, height = region if region is not None else (0.0, 0.0, page.width, page.height)
    left, top = max(x, 0.0), max(y, 0.0)
    right, bottom = min(x + width, page.width), min(y + height, page.height)
    if width <= 0 or height <= 0 or right <= left or bottom <= top:
        raise RegionError(
            f"the region ({x}, {y}, {width}, {height}) mm is not on the page of "
            f"{page.width:g} × {page.height:g} mm"
        )
    width, height = right - left, bottom - top
    fit = min(max_width / width, max_height / height)
    need = required_scale(page)
    if layout == "overview" or fit >= need:
        return Plan((Tile(left, top, width, height, fit),), 0.0, None)

    tile_width, tile_height = max_width / need, max_height / need
    overlap = page.longest_text
    split_text = None
    if overlap > min(tile_width, tile_height) / 2:
        overlap = min(tile_width, tile_height) / 2
        split_text = overlap
    columns = _starts(left, width, tile_width, overlap)
    rows = _starts(top, height, tile_height, overlap)
    tiles = tuple(
        Tile(column, row, min(tile_width, width), min(tile_height, height), need)
        for row in rows
        for column in columns
    )
    return Plan(tiles, overlap, split_text)


def _starts(start: float, length: float, tile: float, overlap: float) -> list[float]:
    """The start of each tile on one axis: equal steps, each overlap at least `overlap`."""
    if length <= tile:
        return [start]
    count = math.ceil((length - overlap) / (tile - overlap))
    step = (length - tile) / (count - 1)
    return [start + index * step for index in range(count)]


def render_tile(page: SvgPage, tile: Tile, background: str) -> bytes:
    """Draw one tile of the page as a PNG image of tile.pixels.

    The root element gets the size of the image in pixels and the viewBox of
    the tile, thus resvg draws only the tile, as vectors. resvg rounds a size
    in mm to full pixels before it scales the image, which would change the
    proportions of a small tile.

    Raises:
        SvgImageError: resvg cannot draw the SVG.
    """
    units = page.view[2] / page.width
    pixel_width, pixel_height = tile.pixels
    view = (
        f"{page.view[0] + tile.x * units:.6f} {page.view[1] + tile.y * units:.6f} "
        f"{tile.width * units:.6f} {tile.height * units:.6f}"
    )
    values = {"width": str(pixel_width), "height": str(pixel_height), "viewBox": view}
    match = _ROOT_TAG.search(page.text)
    if match is None or len(_ROOT_SIZE.findall(match.group(0))) != 3:
        raise SvgImageError("the root element has no single width, height and viewBox")
    tag = _ROOT_SIZE.sub(lambda m: f' {m.group(1)}="{values[m.group(1)]}"', match.group(0))
    text = page.text[: match.start()] + tag + page.text[match.end() :]
    try:
        # resvg-py 0.5.0 sets 0 DPI as the default, and a length in mm in the
        # SVG then has 0 pixels. 96 DPI is the CSS value. kicad-cli draws all
        # the visible text as lines and polygons, thus resvg needs no font.
        png = bytes(
            resvg_py.svg_to_bytes(
                svg_string=text, background=background, skip_system_fonts=True, dpi=96.0
            )
        )
    except ValueError as e:
        raise SvgImageError(f"resvg cannot draw the SVG: {e}") from e
    if png_size(png) != (pixel_width, pixel_height):
        raise SvgImageError(f"resvg drew {png_size(png)} pixels, not {(pixel_width, pixel_height)}")
    return png


def png_size(png: bytes) -> tuple[int, int]:
    """Return the width and the height of a PNG image in pixels (the IHDR chunk)."""
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
