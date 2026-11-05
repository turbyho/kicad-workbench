"""Tests for the references of the placed symbols after the load (parsers/sch_instances.py).

The rules are the rules of KiCad 8, 9 and 10 (parsers/annotation.py):
SCH_SYMBOL::AddHierarchicalReference, the parser of the instances, and
CheckForMissingSymbolInstances. spike/kicad-versions/annotation_check.py compares
the view "cli" with the BOM of kicad-cli.
"""

from pathlib import Path

import pytest

from kicad_workbench.parsers.sch_instances import placed_instances

FILE_VERSION = {"8.0.9": 20231120, "9.0.9": 20250114, "10.0.6": 20260306}


def write_project(directory: Path, instances: str, version: str, reference="R5") -> Path:
    """A root schematic with one resistor: its Reference field and its (instances ...)."""
    (directory / "board.kicad_sch").write_text(
        f'(kicad_sch (version {FILE_VERSION[version]}) (generator "eeschema") (uuid "a0")\n'
        '  (lib_symbols (symbol "Device:R" (symbol "R_1_1")))\n'
        '  (symbol (lib_id "Device:R") (at 0 0 0) (unit 1)\n'
        f'    (property "Reference" "{reference}" (at 0 0 0))\n'
        '    (property "Value" "1k" (at 0 0 0))\n'
        f"    {instances}))\n",
        encoding="utf-8",
    )
    project = directory / "board.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    return project


def references(project: Path, version: str, view: str) -> list[tuple[str, int]]:
    instances, _files, problems = placed_instances(project, version, view)
    assert problems == []
    return [(i.reference, i.unit) for i in instances]


@pytest.mark.parametrize("version", ["8.0.9", "9.0.9", "10.0.6"])
@pytest.mark.parametrize("view", ["gui", "cli"])
def test_the_last_path_of_a_sheet_path_wins(tmp_path, version, view):
    # SCH_SYMBOL::AddHierarchicalReference removes the instance of the same path.
    instances = (
        '(instances (project "board" (path "/a0" (reference "R1") (unit 1)) '
        '(path "/a0" (reference "R2") (unit 1))))'
    )
    project = write_project(tmp_path, instances, version)
    assert references(project, version, view) == [("R2", 1)]


@pytest.mark.parametrize(
    "version, view, expected",
    [
        # KiCad 8 and 9 add the instance after each item of a path: none here. The editor
        # then gives "R?" (CheckForMissingSymbolInstances), kicad-cli the Reference field.
        ("9.0.9", "gui", "R?"),
        ("9.0.9", "cli", "R5"),
        ("8.0.9", "cli", "R5"),
        # KiCad 10 adds an instance with no reference, and it clears the Reference field.
        ("10.0.6", "gui", "R?"),
        ("10.0.6", "cli", "R?"),
    ],
)
def test_a_path_with_no_items(tmp_path, version, view, expected):
    project = write_project(tmp_path, '(instances (project "board" (path "/a0")))', version)
    assert references(project, version, view) == [(expected, 1)]


@pytest.mark.parametrize(
    "version, view, expected",
    [
        # The symbol has only an instance of an other sheet path. Its first instance sets
        # the Reference field (R9).
        ("9.0.9", "gui", ("R?", 1)),
        ("9.0.9", "cli", ("R9", 2)),
        # KiCad 10 copies the instance of the project (CheckForMissingSymbolInstances).
        ("10.0.6", "gui", ("R9", 2)),
        ("10.0.6", "cli", ("R9", 2)),
    ],
)
def test_no_instance_for_the_sheet_path(tmp_path, version, view, expected):
    instances = '(instances (project "board" (path "/ff" (reference "R9") (unit 2))))'
    project = write_project(tmp_path, instances, version)
    assert references(project, version, view) == [expected]


def test_versions_with_no_rules(tmp_path):
    project = write_project(tmp_path, "", "10.0.6")
    with pytest.raises(ValueError, match="no verified rules"):
        placed_instances(project, "10.99.0", "gui")
    with pytest.raises(ValueError, match="view must be"):
        placed_instances(project, "10.0.6", "erc")


def test_kicad10_copies_the_variants_of_an_instance(tmp_path):
    # CheckForMissingSymbolInstances copies the full instance, also its variants, but
    # the copy has no (path ...) of its own in the file.
    instances = (
        '(instances (project "board" (path "/ff" (reference "R9") (unit 1) '
        '(variant (name "Lite") (field (name "MPN") (value "B"))))))'
    )
    project = write_project(tmp_path, instances, "10.0.6")
    (instance,) = placed_instances(project, "10.0.6", "gui")[0]
    assert instance.path is None
    assert instance.variants is not None and instance.variants.value(0) == "/ff"
    # KiCad 9 makes "R?" with no variants.
    project = write_project(
        tmp_path,
        instances.replace('(variant (name "Lite") (field (name "MPN") (value "B")))', ""),
        "9.0.9",
    )
    (instance,) = placed_instances(project, "9.0.9", "gui")[0]
    assert (instance.reference, instance.path, instance.variants) == ("R?", None, None)


@pytest.mark.parametrize("version, value", [("9.0.9", "~"), ("10.0.6", "")])
def test_value_of_an_instance(tmp_path, version, value):
    # The parser sets the Value field from the (value ...) of an instance. KiCad 10
    # reads "~" of a file before EMPTY_TEXT_MARKER as empty, KiCad 8 and 9 keep it.
    instances = '(instances (project "board" (path "/a0" (reference "R5") (unit 1) (value "~"))))'
    project = write_project(tmp_path, instances, "9.0.9")
    (instance,) = placed_instances(project, version, "cli")[0]
    assert instance.value == value
