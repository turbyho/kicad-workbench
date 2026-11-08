"""Parts registry tools — search and fetch verified KiCad parts.

Backed by an open, no-login registry (default: PartReel, https://partreel.com;
configurable via PARTS_REGISTRY_URL). Lets the assistant pull real, quality-
gated footprints/symbols/3D models instead of drawing them from scratch.
"""

import asyncio
import json
import os
from pathlib import Path

from ..server import mcp
from ..utils.parts_registry import (
    FORMAT_EXTENSIONS,
    RegistryClient,
    RegistryError,
)


@mcp.tool()
async def search_parts_registry(query: str, limit: int = 20) -> str:
    """Search an open registry of KiCad parts (no login).

    The tool compares the query with the ID, the name, the family, the category, the
    manufacturer and the keywords of each part. Example queries: "usb c connector",
    "0402 capacitor", "nrf9151".

    Args:
        query: The words to find. Each word must match. "*" is a wildcard.
        limit: The maximum number of results (default 20).

    Returns:
        The parts that match (JSON): ID, name, family, category, manufacturer and
        the URL of the page.
    """
    client = RegistryClient()
    try:
        hits = await asyncio.to_thread(client.search, query, limit)
    except RegistryError as exc:
        return f"❌ Registry search failed: {exc}"
    if not hits:
        return f"No registry parts matched '{query}'."
    rows = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "family": p.get("family"),
            "category": p.get("category"),
            "manufacturer": p.get("manufacturer"),
            "page": p.get("page"),
        }
        for p in hits
    ]
    return json.dumps({"count": len(rows), "parts": rows}, indent=2)


@mcp.tool()
async def get_registry_part(part_id: str) -> str:
    """Give the full record of one part of the parts registry.

    The record has the download URLs (footprint, symbol, 3D model), the link to the
    datasheet, the origin of the dimensions, and the reports of real builds.

    Args:
        part_id: The ID of the part (search_parts_registry gives it).

    Returns:
        The record of the part (JSON).
    """
    client = RegistryClient()
    try:
        part = await asyncio.to_thread(client.get_part, part_id)
    except RegistryError as exc:
        return f"❌ Registry lookup failed: {exc}"
    return json.dumps(part, indent=2)


@mcp.tool()
async def download_registry_part(
    part_id: str, save_dir: str, formats: list[str] | None = None
) -> str:
    """Download the files of a part of the parts registry into a local folder.

    The tool downloads only through HTTPS, only from the hosts of the registry, at
    most 50 MB for each file, and only the file extensions of each format.

    Args:
        part_id: The ID of the part (search_parts_registry gives it).
        save_dir: A folder that exists, for example the library folder of the
            project: in the working folder, the home folder or a folder of
            PARTS_REGISTRY_SAVE_ROOTS.
        formats: The files to download: "footprint", "symbol", "step" and
            "preview". Default: the footprint and the symbol.

    Returns:
        The paths of the files, or an error.
    """
    formats = formats or ["footprint", "symbol"]
    unknown = [f for f in formats if f not in FORMAT_EXTENSIONS]
    if unknown:
        return f"❌ Unknown formats: {unknown} (allowed: {sorted(FORMAT_EXTENSIONS)})"

    dest = Path(save_dir)
    if not dest.is_dir():
        return f"❌ Not a directory: {save_dir}"
    # Confine writes to the working dir or the user's home tree
    # (extend via PARTS_REGISTRY_SAVE_ROOTS, comma-separated).
    roots = [Path.cwd(), Path.home()]
    roots += [
        Path(r.strip())
        for r in os.environ.get("PARTS_REGISTRY_SAVE_ROOTS", "").split(",")
        if r.strip()
    ]
    real = dest.resolve()
    if not any(real == r.resolve() or r.resolve() in real.parents for r in roots):
        return f"❌ save_dir outside allowed roots (cwd/home): {save_dir}"

    client = RegistryClient()
    try:
        part = await asyncio.to_thread(client.get_part, part_id)
    except RegistryError as exc:
        return f"❌ Registry lookup failed: {exc}"

    files = (part.get("files") if isinstance(part, dict) else None) or {}
    saved, missing = [], []
    for fmt in formats:
        url = files.get(fmt)
        if not url:
            missing.append(fmt)
            continue
        try:
            path = await asyncio.to_thread(
                client.download_asset, url, str(real), part_id, fmt
            )
            saved.append(path)
        except (RegistryError, OSError) as exc:
            # OSError covers disk-full / permission-denied / Windows reserved
            # names — surface the partial result instead of losing it.
            return f"❌ Download failed for {fmt}: {exc} (saved so far: {saved})"
    out = {
        "saved": saved,
        "not_available": missing,
        "license": part.get("license") if isinstance(part, dict) else None,
    }
    return json.dumps(out, indent=2)
