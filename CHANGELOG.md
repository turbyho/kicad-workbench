# Changelog

Each release of KiCad Workbench, with the changes for the user.

## 0.1.0 (2026-11-08)

KiCad Workbench is an MCP server that lets an AI agent work with KiCad 8, 9 and 10
projects. The agent reads schematics, boards and libraries, runs the checks of KiCad,
makes the bill of materials (BOM), and writes changes to the schematic. This is the
first release.

KiCad Workbench started as a fork of
[Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server) (MIT).
The notes below give the changes for a user of that server.

### Install

- The package, the command and the Python module have new names:
  `kicad-workbench` (PyPI), the command `kicad-workbench`, the module
  `kicad_workbench`. The server gives the name `kicad-workbench` to the client (before:
  `kicad-mcp-server`). The documentation registers it in the client with the key
  `kicad`: the tools are then `mcp__kicad__<tool>` in Claude Code.
- Requirements: Python 3.10 or later, KiCad 8, 9 or 10 with kicad-cli. For exact board
  data, install the server into a Python that can import the module `pcbnew` of
  KiCad ([docs/installation.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/installation.md)).
- Dependencies: `kicad-skip`, `python-dotenv`, `jinja2` and the extra `cli` of `mcp`
  are not necessary now. New: `packaging` and `resvg-py`. The extras `hardware` and
  `all` are removed. The extra `test` has `hatchling` (new) and no `pytest-mock`. The
  extra `dev` is the extra `test` and ruff (one version): it has no `black` and no
  `mypy`.

### Removed

- The server does not read a file `.env` now (the template `.env.example` and the
  dependency `python-dotenv` are removed), and the resource `kicad://config` is
  removed. No tool read their values (`KICAD_PROJECT_PATHS`, `DEFAULT_SUMMARY_DETAIL_LEVEL`,
  `INCLUDE_NETS_IN_SUMMARY`, `INCLUDE_POWER_IN_SUMMARY`, `DEFAULT_TEST_FRAMEWORK`,
  `DEFAULT_TEST_TYPE`).
- The 8 firmware tools are not in this release: `analyze_pin_functions`,
  `extract_pinmux_config`, `extract_gpio_config`, `extract_i2c_devices`,
  `extract_spi_devices`, `extract_power_domains`, `validate_pin_configuration` and
  `generate_device_tree`. They guessed the data from the net names and the values,
  gave values that come from no datasheet, and `generate_device_tree` gave a device
  tree that `dtc` refuses. A new design for more than one SDK (Zephyr, STM32Cube,
  ESP-IDF, Linux and others) is planned. `detect_pin_conflicts` stays: it reads the
  pin types of the netlist of KiCad.

### New tools (12)

The server has 51 tools: 39 of the 47 tools of the fork, and 12 new tools.

- Libraries: `list_libraries`, `search_library_symbols`, `search_library_footprints`,
  `get_library_symbol`, `get_library_footprint`, `render_library_item` (PNG images of
  a symbol or a footprint at 300 DPI or more).
- Checks: `check_pin_pad_mapping` (a library symbol and a footprint),
  `check_schematic_pin_pad_mapping` (each symbol of a schematic, as "Update PCB from
  Schematic"), `check_library_links` (the links to the symbol and footprint libraries
  and the 3D models).
- BOM and fields: `export_bom` (the BOM of kicad-cli, also for the variants of KiCad
  10), `check_symbol_fields` (the check of the confirmed MPNs), `set_symbol_fields`
  (the fields of placed symbols, as KiCad writes them).

### KiCad is the specification

- The tools use KiCad where KiCad has an interface: kicad-cli for the netlist, the
  BOM, ERC, DRC and the exports, and `pcbnew` for boards and footprints. Where KiCad
  has no interface, the tools use the rules of the KiCad source for the installed
  KiCad version. We compare these rules with KiCad 8.0.9, 9.0.9 and 10.0.7.
- The tools read a file as the installed KiCad loads it. The server does not convert
  files:
  - A schematic, a board or a library of an older version than the installed KiCad
    gives a warning with the step for the user (the conversion in KiCad, or
    `kicad-cli sym upgrade` and `kicad-cli fp upgrade` for a library).
  - A tool that writes refuses an older schematic or board.
  - A file of a newer version than the installed KiCad is an error.
- With no KiCad, each tool that needs KiCad gives an error that tells the user to
  install KiCad 8 or later. The minimum is KiCad 8.
- The installed KiCad version: each tool that uses KiCad refuses a KiCad that the
  server has no rules for (for example 11.0, or a nightly build other than 10.99) and
  a release candidate. The tools of the parts registry and the tools that read a
  netlist file need no KiCad. A newer patch release (for example 10.0.8) and a
  development build of a release branch get the rules of the newest compared patch
  release, and each answer of a tool that uses this KiCad gets a warning. The server reads the full version from `kicad-cli version --format about`.
- The server finds kicad-cli in the environment variable `KICAD_CLI`, then in `PATH`,
  then in the standard install folder of KiCad (on Windows the newest version folder
  that has kicad-cli). The Python of KiCad for `pcbnew` comes from `KICAD_PYTHON`,
  else the Python of the server process when it can import `pcbnew`, else the Python
  of the installation of kicad-cli. A `KICAD_CLI` or `KICAD_PYTHON` with a path that is
  not a file is an error: the server does not use a different KiCad then.
- The text of a result is honest: an error starts with "❌", a warning with "⚠️", and
  a "✅" comes only after a full check. A tool tells when a result can be incomplete,
  for example with an annotation error of the schematic.

### Schematics and edits

- The tools read the files with an own S-expression parser (the lexer rules of
  KiCad), not with `kicad-skip`.
- A placed symbol has the reference and the unit of each sheet path, as KiCad gives
  them: a sheet that the hierarchy uses two times gives two references.
- The edits (`add_component_from_library`, `add_wire`, the label tools,
  `set_symbol_fields`) write the format of the file version. Components, wires and
  labels get the junctions and the connection grid of the schematic editor. A tool
  refuses a file that KiCad has open (the lock file of KiCad), and it saves a file as
  KiCad does: a temporary file, then a rename.
- `run_erc` and `get_erc_violations` add the annotation check of the ERC dialog,
  which kicad-cli does not run, with the severities and the exclusions of the
  project.

### Boards

- The board tools use the `pcbnew` of the installed KiCad. A `pcbnew` of a different
  KiCad version than kicad-cli is an error. With no `pcbnew`, they read the board file
  as text and give a warning with the limits of each tool.
- `list_pcb_footprints` gives the full library ID and the number of pads of each
  footprint.
- `render_pcb` needs KiCad 9 or later, and writes PNG or JPEG images.
- `export_pcb_3d` gives a warning for each 3D model that kicad-cli did not add to the
  STEP file.

### Other

- kicad-cli writes its messages and reports in English, also when the user selected
  a different language in KiCad.
- The server tells the agent about a newer release on PyPI (one check a day). A value
  other than "" and "0" in the environment variable `KICAD_WORKBENCH_NO_UPDATE_CHECK`
  stops this check.
- The descriptions of the tools are in ASD-STE100 Simplified Technical English.

All tools are in [docs/tools.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/tools.md).
