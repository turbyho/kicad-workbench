# Test fixtures

The files of the tests of the tools. The tests copy a fixture into `tmp_path` before
they change it: a test does not change a file of this folder.

| Fixture | Content | Tests |
|---|---|---|
| `example_schematic.kicad_sch` | A schematic of KiCad 10 (file version 20260306) with resistors, a capacitor, an ESP32 module, a 4-pin connector, a GND symbol and nets | The schematic tools |
| `example_pcb.kicad_pcb` | A board of KiCad 8 (file version 20240108) with two footprints, tracks, a via and a zone | The board tools, the check of the board file version |
| `hier/` | A hierarchy of KiCad 8 (file version 20231120): `root.kicad_sch` with the sheet "Power" (`child.kicad_sch`) and the sheet "Ghost" of a file that is not there | The hierarchy, the lock files, the check of the schematic file versions |
| `reports/` | ERC reports of kicad-cli 10 (JSON, English and Czech), a DRC report (JSON, English) and a schematic with a dangling label | The readers of the reports |
