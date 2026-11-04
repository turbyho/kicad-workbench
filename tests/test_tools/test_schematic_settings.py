"""Tests of schematic_settings: the connection grid and the off-grid ERC severity."""

import json

import pytest

from kicad_workbench.utils.schematic_settings import schematic_settings


def project(tmp_path, data) -> object:
    path = tmp_path / "p.kicad_pro"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_no_project_file(tmp_path):
    settings = schematic_settings(tmp_path / "none.kicad_pro")
    assert (settings.connection_grid, settings.off_grid_severity, settings.problem) == (
        12700,
        "warning",
        None,
    )


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
    assert schematic_settings(path).off_grid_severity == severity


@pytest.mark.parametrize("data", [{}, {"schematic": [], "erc": "x"}, []])
def test_values_that_are_not_there(tmp_path, data):
    settings = schematic_settings(project(tmp_path, data))
    assert (settings.connection_grid, settings.off_grid_severity, settings.problem) == (
        12700,
        "warning",
        None,
    )


def test_project_file_that_is_not_json(tmp_path):
    settings = schematic_settings(project(tmp_path, "{ not json"))
    assert (settings.connection_grid, settings.off_grid_severity) == (12700, "warning")
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
    settings = schematic_settings(project(tmp_path, text))
    assert (settings.connection_grid, settings.off_grid_severity, settings.problem) == (
        6350,
        "ignore",
        None,
    )


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
