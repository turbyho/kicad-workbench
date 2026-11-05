"""The variant names of a KiCad 10 project (SCHEMATIC::rebuildHierarchyState).

export_bom and set_symbol_fields use them.
"""

from pathlib import Path

from ..parsers.sexpr import Node, parse
from .kicad_text import read_as_kicad, read_kicad_json
from .sch_hierarchy import project_files


def file_variants(root: Node) -> set[str]:
    """Return the variant names of the symbol and sheet instances of a parsed
    schematic file (SCH_SCREEN::GetVariantNames)."""
    names = set()
    for item in [*root.children("symbol"), *root.children("sheet")]:
        for instances in item.children("instances"):
            for variant in instances.walk("variant"):
                name = variant.first("name")
                if name is not None and name.value(0) is not None:
                    names.add(name.value(0))
    return names


def project_variants(project: Path) -> set[str]:
    """Return the names of "schematic.variants" of a project file (SCHEMATIC_SETTINGS)."""
    if not project.is_file():
        return set()
    try:
        settings = read_kicad_json(project)
    except ValueError:
        # KiCad uses the default settings for a project file that is not JSON
        # (JSON_SETTINGS::LoadFromFile): they have no variants.
        return set()
    schematic = settings.get("schematic") if isinstance(settings, dict) else None
    variants = schematic.get("variants") if isinstance(schematic, dict) else None
    return {
        entry["name"]
        for entry in (variants if isinstance(variants, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def variant_names(project: Path) -> set[str]:
    """Return the variant names of a project, as SCHEMATIC::rebuildHierarchyState of KiCad 10.

    The names are in the symbol and sheet instances of each schematic file of
    the project, and in the project file.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
    """
    names = project_variants(project)
    for file in project_files(project):
        names |= file_variants(parse(read_as_kicad(file)))
    return names
