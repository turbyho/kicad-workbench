# KiCad Workbench

KiCad Workbench is an MCP server that lets an AI agent work with KiCad projects. The
agent reads schematics, boards and libraries, runs the checks of KiCad, makes the
bill of materials (BOM), and writes changes to the schematic. It supports KiCad 8, 9
and 10.

The server gives the results that KiCad gives. Where KiCad has an interface, the
server uses KiCad itself. kicad-cli makes the netlist, the BOM, the ERC and DRC
reports and the exports. The Python module `pcbnew` of KiCad reads boards and
footprints. Where KiCad has no interface, the server uses the rules of the KiCad
source code for the version of KiCad that saved the file. It tells when a result can
be incomplete. We compare these rules with KiCad 8.0.9, 9.0.9 and 10.

- **Design review.** The agent gets the components, nets, pins and sheets of a
  schematic, and the footprints, tracks and zones of a board. It runs ERC and DRC,
  and finds pins with no pad, symbols with no footprint and missing 3D models.
- **Libraries.** The agent searches the symbol and footprint libraries of KiCad and
  of the project, reads a symbol or a footprint as KiCad places it, and gets PNG
  images of it for a visual check.
- **BOM and parts.** The agent makes the BOM as kicad-cli makes it (also for each
  variant of KiCad 10), checks the MPN fields of the symbols, and writes the
  confirmed fields into the schematic.
- **Firmware.** The agent gets the pin functions of a microcontroller, the I2C and
  SPI devices and the power domains, and a device tree source as a start for the
  firmware.

Status: in development (version 0.1). The tools work and have tests. The interfaces
can change.

## Datasheets of the project with datasheet-lens

[datasheet-lens](https://github.com/turbyho/datasheet-lens) is an MCP server for the
datasheets of electronic components. With the two servers, the agent keeps the
datasheets of a design equal to its BOM, and checks the design against the original
datasheets:

- **The datasheets follow the BOM.** `export_bom` gives the parts of the design with
  their MPN and Datasheet fields as a CSV file. `project_check` of datasheet-lens
  compares this file with the project file `datasheet-lens.toml`, and gives the
  parts that are missing, extra or different. The project file and the datasheets go
  into the repository of the design, the same for each person of the team.
- **Confirmed MPNs.** The Value field holds the general value (`2N7002`), the field
  MPN the orderable MPNs (`2N7002LT1G, BSS138LT1G`), and the hidden fields
  Value_Confirmed and MPN_Confirmed the copies that the user confirmed.
  datasheet-lens finds the changes, and `set_symbol_fields` writes the confirmed
  fields into the schematic, as KiCad writes them. Without datasheet-lens,
  `check_symbol_fields` does the same check with the same messages.
- **Checks against the datasheet.** The agent compares the pins of a symbol with the
  pin table of the datasheet, a footprint with the package drawing, and the circuit
  with the absolute maximum ratings and the application circuit. datasheet-lens gives
  the original pages as images, not a text conversion.

See [docs/datasheet-lens.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/datasheet-lens.md) for the procedure.

## Features

- **Schematics.** Components, nets, symbols and fields, the sheet hierarchy, and the
  connections across sheets.
- **Netlist.** The netlist of kicad-cli, and the connections of each pin.
- **Boards.** Footprints, tracks, vias and zones, the statistics and the design rules
  (through `pcbnew`), a 3D view as PNG, and the STEP and Gerber exports.
- **Checks.** ERC (with the annotation check of the ERC dialog, which kicad-cli does
  not run) and DRC through kicad-cli, the pin and pad check of "Update PCB from
  Schematic", and the links to the libraries and the 3D models.
- **Libraries.** The library tables of KiCad and of the project, a search as in the
  choosers of KiCad, the content of symbols and footprints, and PNG images.
- **BOM.** The BOM of kicad-cli with the fields, the groups, the DNP filter and the
  variants of KiCad 10.
- **Edits.** New projects, symbols from the libraries, wires, labels and junctions as
  the schematic editor makes them, and the fields of placed symbols. The server
  writes the format of the file version, and refuses a file that KiCad has open.
- **Firmware.** Pin functions, pin conflicts, I2C and SPI devices, power domains, and
  device tree sources for STM32F4, ESP32 and nRF52.
- **Parts registry.** A search in an open registry of KiCad parts
  ([PartReel](https://partreel.com)), and the download of their symbols and
  footprints (on request also their 3D models).

All tools are in [docs/tools.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/tools.md).

## Requirements

- KiCad 8, 9 or 10, with kicad-cli.
- Python 3.10 or later.
- For exact board data: a Python that can import the module `pcbnew` of KiCad.
  Without it, the board tools (statistics, tracks, nets, zones) read the board file
  as text, with no design rules and no board outline.

## Installation

Install the server into a Python that can import `pcbnew`.

Linux (the KiCad package of the distribution installs `pcbnew` for the system
Python):

```sh
pipx install --python /usr/bin/python3 --system-site-packages kicad-workbench
claude mcp add --scope user kicad -- kicad-workbench
```

Windows and macOS: install the server into the Python of KiCad, and give that Python
to the MCP client. The steps are in [docs/installation.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/installation.md). We
did not test them on Windows and macOS yet.

For a different MCP client, set the command `kicad-workbench` with no arguments. The
server tells the agent about a newer release on PyPI. See
[docs/installation.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/installation.md) for the configuration, the environment
variables and the troubleshooting.

## Quick start

Ask the agent in natural language, and give the path of the schematic or the board:

> Give the components and the nets of `~/boards/sensor/sensor.kicad_sch`.

> Run ERC and DRC on the sensor board, and explain each error.

> Do the pins of U3 agree with the pads of its footprint?

> Make the BOM with the MPN field, and save it as CSV for datasheet-lens.

> Show the footprint `Package_SO:SOIC-8_3.9x4.9mm_P1.27mm` as an image.

> Which pins of the STM32 have I2C and SPI functions?

The agent selects the tools. Close the files in KiCad before the agent edits them:
KiCad does not read a change to a file that it has open.

## Documentation

| Document | Content |
|---|---|
| [docs/installation.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/installation.md) | Requirements, installation on Linux, Windows and macOS, MCP clients, environment variables, release notices, troubleshooting, upgrade |
| [docs/tools.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/tools.md) | All tools, with their arguments and limits |
| [docs/datasheet-lens.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/datasheet-lens.md) | The datasheets of a project with datasheet-lens: BOM, confirmed MPNs, checks against the datasheet |
| [docs/editing.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/editing.md) | The schematic edits: the rules of KiCad that they use, and their limits |
| [docs/firmware.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/firmware.md) | Pin functions, peripherals and device tree sources, and the limits of these tools |
| [docs/development.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/development.md) | Development setup, tests, rules for code and text |

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[test,dev]"
.venv/bin/python -m pytest tests/ -q
```

See [docs/development.md](https://github.com/turbyho/kicad-workbench/blob/main/docs/development.md).

## Acknowledgments

Thanks to all contributors and to the feedback of the community:

- [@raffaeler](https://github.com/raffaeler) for the tests and feedback for KiCad 10 ([kicad-mcp-server#9](https://github.com/Seeed-Studio/kicad-mcp-server/issues/9))
- [@shivam5594](https://github.com/shivam5594) for the report of the install problem with Python 3.14 ([kicad-mcp-server#11](https://github.com/Seeed-Studio/kicad-mcp-server/issues/11))
- [@derekc00](https://github.com/derekc00) for lint fixes ([kicad-mcp-server#10](https://github.com/Seeed-Studio/kicad-mcp-server/issues/10))
- [@befedo](https://github.com/befedo) for the report and the patch of the SchematicComponent bug ([kicad-mcp-server#13](https://github.com/Seeed-Studio/kicad-mcp-server/issues/13))

## License

MIT, see [LICENSE](https://github.com/turbyho/kicad-workbench/blob/main/LICENSE). KiCad Workbench started as a fork of
[Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server)
(MIT, copyright Seeed Studio). The history of this repository starts on 2026-11-04
with the state of that fork.
