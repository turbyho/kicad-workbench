"""Tests of schematic_settings and off_grid_severity: the connection grid and the off-grid
ERC severity."""

import json

import pytest

from kicad_workbench.parsers.rule_severity_keys import severity_keys
from kicad_workbench.utils.schematic_settings import (
    SchematicSettings,
    Severity,
    off_grid_severity,
    rule_severities,
    schematic_settings,
    unknown_severity_reason,
)

KICAD = "10.0.6"  # The installed KiCad: it reads the ERC severities.


def project(tmp_path, data) -> object:
    path = tmp_path / "p.kicad_pro"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_no_project_file(tmp_path):
    path = tmp_path / "none.kicad_pro"
    settings = schematic_settings(path)
    assert (settings.connection_grid, settings.problem) == (12700, None)
    assert off_grid_severity(schematic_settings(path), KICAD) == Severity("warning")
    assert off_grid_severity(settings, "8.0.9") == Severity("warning")


@pytest.mark.parametrize(
    "mils, grid",
    [
        (50.0, 12700),
        (25, 6350),
        (100, 25400),
        (10000, 2540000),
        (50.0001, 12700),  # KiROUND to IU
        (24.9, 12700),  # less than the minimum: the default
        (10001, 12700),  # more than the maximum: the default
        ("100", 12700),  # not a number: the default
        (True, 12700),
        (None, 12700),
    ],
)
def test_connection_grid(tmp_path, mils, grid):
    path = project(tmp_path, {"schematic": {"connection_grid_size": mils}})
    assert schematic_settings(path).connection_grid == grid


@pytest.mark.parametrize(
    "value, severity",
    [
        ("warning", "warning"),
        ("ignore", "ignore"),
        ("error", "error"),
        ("other", "error"),
        (5, "warning"),
    ],
)
def test_off_grid_severity(tmp_path, value, severity):
    path = project(tmp_path, {"erc": {"rule_severities": {"endpoint_off_grid": value}}})
    assert off_grid_severity(schematic_settings(path), KICAD).value == severity
    assert off_grid_severity(schematic_settings(path), "8.0.9").value == severity


@pytest.mark.parametrize("data", [{}, {"schematic": [], "erc": "x"}, []])
def test_values_that_are_not_there(tmp_path, data):
    path = project(tmp_path, data)
    settings = schematic_settings(path)
    assert (settings.connection_grid, settings.problem) == (12700, None)
    assert off_grid_severity(schematic_settings(path), KICAD) == Severity("warning")


def test_project_file_that_is_not_json(tmp_path):
    path = project(tmp_path, "{ not json")
    settings = schematic_settings(path)
    assert settings.connection_grid == 12700
    assert off_grid_severity(schematic_settings(path), KICAD) == Severity("warning")
    assert settings.problem.startswith(
        f"The project file {tmp_path / 'p.kicad_pro'} cannot be read"
    )


def test_project_file_with_comments_and_a_byte_order_mark(tmp_path):
    # KiCad reads the file with nlohmann::json and ignore_comments.
    text = (
        "\ufeff{ // the schematic\n"
        '  "schematic": { /* mils */ "connection_grid_size": 25.0, "name": "a // b /* c */" },\n'
        '  "erc": { "rule_severities": { "endpoint_off_grid": "ignore" } }\n'
        "}\n"
    )
    path = project(tmp_path, text)
    settings = schematic_settings(path)
    assert (settings.connection_grid, settings.problem) == (6350, None)
    assert off_grid_severity(schematic_settings(path), KICAD) == Severity("ignore")


@pytest.mark.parametrize("text", ['{"schematic": {"connection_grid_size": NaN}}', "{ /* open"])
def test_text_that_is_not_json_for_kicad(tmp_path, text):
    settings = schematic_settings(project(tmp_path, text))
    assert settings.connection_grid == 12700
    assert settings.problem is not None


@pytest.mark.parametrize("number", ["1e400", "1" + "0" * 400, "-1e400"])
def test_grid_out_of_the_range_of_a_double(tmp_path, number):
    path = project(tmp_path, '{"schematic": {"connection_grid_size": ' + number + "}}")
    assert schematic_settings(path).connection_grid == 12700


def test_wire_widths_of_the_net_classes(tmp_path):
    classes = [{"name": "Default", "wire_width": 12}, {"name": "Power"}]
    path = project(tmp_path, {"net_settings": {"classes": classes}})
    assert schematic_settings(path).wire_widths == (12 * 254, 12 * 254)
    classes.append({"name": "Thin", "wire_width": 4})
    path = project(tmp_path, {"net_settings": {"classes": classes}})
    assert schematic_settings(path).wire_widths == (4 * 254, 12 * 254)


def keys_around(version: str) -> tuple[str, str]:
    """A key that KiCad reads before endpoint_off_grid, and one that it reads after it.

    A heading has the key "". KiCad reads it first, thus it is not after.
    """
    _pre, keys = severity_keys("erc", version)
    index = keys.index("endpoint_off_grid")
    return keys[index - 1], next(key for key in keys[index + 1 :] if key)


@pytest.mark.parametrize("version", ["8.0.9", "9.0.9", "10.0.6", "10.0.7"])
def test_off_grid_severity_in_the_order_of_kicad(tmp_path, version):
    # ERC_SETTINGS reads the keys in the order of GetItemsWithSeverities. A value
    # that is not a text stops the read: the keys after it keep their defaults.
    before, after = keys_around(version)
    severities = {before: 5, "endpoint_off_grid": "ignore"}
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version) == Severity("warning")
    severities = {after: 5, "endpoint_off_grid": "ignore"}
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version) == Severity("ignore")


@pytest.mark.parametrize("version", [None, "10.99.0"])
def test_off_grid_severity_with_no_known_order(tmp_path, version):
    # With all values texts, the order has no effect.
    severities = {"pin_not_connected": "error", "endpoint_off_grid": "ignore"}
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version) == Severity("ignore")
    # A value that is not a text: the server cannot tell which keys KiCad reads.
    severities["pin_not_connected"] = 5
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    severity = off_grid_severity(schematic_settings(path), version)
    # KiCad gives the text, or the default when it stops the read before it.
    assert severity == Severity(None, unknown_severity_reason(version), ("ignore", "warning"))
    assert ("KiCad 10.99.0" if version else "the version is not known") in severity.reason
    severities["endpoint_off_grid"] = "error"
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version).possible == ("error", "warning")
    # A text that gives the default gives it in each order.
    severities["endpoint_off_grid"] = "warning"
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version) == Severity("warning")
    # The value of the check itself is not a text: KiCad stops the read at it or
    # before it, thus the default applies in each order.
    severities["endpoint_off_grid"] = 5
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), version) == Severity("warning")


def test_kicad8_severity_for_the_no_connect_pins(tmp_path):
    # KiCad 9 and 10 read single_global_label before endpoint_off_grid. KiCad 8 has
    # no such key.
    severities = {"single_global_label": 5, "endpoint_off_grid": "ignore"}
    path = project(tmp_path, {"erc": {"rule_severities": severities}})
    assert off_grid_severity(schematic_settings(path), "10.0.6") == Severity("warning")
    assert off_grid_severity(schematic_settings(path), "8.0.9") == Severity("ignore")


DRC_KEYS = ("board", "design_settings", "rule_severities")


@pytest.mark.parametrize(
    "severities, known, expected",
    [
        # BOARD_DESIGN_SETTINGS of KiCad 9 and 10 reads hole_near_hole into hole_to_hole
        # before the loop. KiCad 8 has no check hole_to_hole (its name is
        # hole_near_hole): the check keeps the default.
        ({"hole_near_hole": "ignore"}, {"8.0.9": "error", "9.0.9": "ignore"}, None),
        # The text gives the default: the same in each version.
        ({"hole_near_hole": "error"}, {"8.0.9": "error", "9.0.9": "error"}, "error"),
        # The loop reads hole_to_hole after it.
        (
            {"hole_near_hole": "ignore", "hole_to_hole": "warning"},
            {"8.0.9": "error", "9.0.9": "warning"},
            "warning",
        ),
    ],
)
def test_drc_key_before_the_loop(tmp_path, severities, known, expected):
    path = project(tmp_path, {"board": {"design_settings": {"rule_severities": severities}}})
    defaults = {"hole_to_hole": "error"}
    for version, severity in known.items():
        assert rule_severities(path, DRC_KEYS, defaults, "drc", version) == {
            "hole_to_hole": severity
        }
    # A nightly build: the server does not know if it reads hole_near_hole.
    assert rule_severities(path, DRC_KEYS, defaults, "drc", "10.99.0") == {"hole_to_hole": expected}


def test_projects_with_severities_that_are_not_known(tmp_path):
    # The same reason, but project b can ignore the check: the result has each
    # severity that one of the projects can give.
    a = {"pin_not_connected": 5, "endpoint_off_grid": "error"}
    b = {"pin_not_connected": 5, "endpoint_off_grid": "ignore"}
    settings = SchematicSettings(12700, erc_severities=(("a", a), ("b", b)))
    severity = off_grid_severity(settings, None)
    assert (severity.value, severity.possible) == (None, ("error", "ignore", "warning"))
    assert severity.reason == (
        "the projects have different severities (a: not known; b: not known); "
        + unknown_severity_reason(None)
    )
    # The same values in the two projects give the result of one project.
    assert off_grid_severity(
        SchematicSettings(12700, erc_severities=(("a", a), ("b", a))), None
    ) == (off_grid_severity(SchematicSettings(12700, erc_severities=(("a", a),)), None))
