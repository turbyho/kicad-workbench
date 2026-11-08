# Development

## Setup

The board tools import `pcbnew` in the server process. Thus make the virtual
environment from a Python that can import `pcbnew` (on Linux the system Python), with
its site packages:

```sh
git clone https://github.com/turbyho/kicad-workbench.git
cd kicad-workbench
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
```

The extra `test` has the test tools and `hatchling` (a test builds the sdist and the
wheel): CI installs it. The extra `dev` has the extra `test` and ruff.

An MCP client can start this install with `.venv/bin/kicad-workbench`. The server
keeps the modules of its start: after a change of the code, restart the MCP client.
An editable install gets no release notices.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

- The tests that need KiCad (kicad-cli, `pcbnew`) skip when KiCad is not installed.
- `tests/test_build_content.py` builds the sdist and the wheel with hatchling, and
  makes sure that they have only the public files of the repository.
- The character set tests run the tools in a locale that is not UTF-8. When the
  environment variable `KICAD_WORKBENCH_TEST_REQUIRE_LOCALE` is set (as in CI), a missing
  locale is an error, not a skip. On Linux, make the locale with
  `sudo localedef -i zh_CN -f GBK zh_CN.GBK`.

CI runs the tests on Linux and Windows with Python 3.10 and 3.13, and `ruff check` on
Python 3.12.

## Lint and format

Use the version of ruff in `requirements-dev.txt`. A newer ruff can find new rules,
and CI uses this version. The extra `dev` installs it, or run it with uvx:

```sh
uvx --from "$(grep -E '^ruff==' requirements-dev.txt)" ruff check src tests
uvx --from "$(grep -E '^ruff==' requirements-dev.txt)" ruff format --diff <changed files>
```

Format only the new and the changed lines.

## Rules

### KiCad is the specification

The tools must give the result that KiCad gives. A small difference is also a defect.

1. Use KiCad itself where it can do the work: kicad-cli for the netlist, ERC, DRC,
   BOM and the exports, `pcbnew` for boards and footprints.
2. Where KiCad has no interface, get each rule from the KiCad source code, and write
   the KiCad function in the docstring or the comment (for example
   `BOARD_NETLIST_UPDATER::testConnectivity`). Give the limits of the copy in the
   output.
3. Read a file as the installed KiCad loads it: the format follows the file version
   only where the parser of that KiCad branches on it. The installed version also
   applies to kicad-cli, the global library tables and the checks that KiCad runs.
   The server does not convert files (`utils/file_versions.py`:
   `with_schematic_versions`, `with_board_versions` and `with_library_versions` on each
   tool that reads a schematic, a board or a library item, and `check_library_versions`
   in the library search): a schematic, a board or a library of an older version
   gives a warning with the step for the user, and a tool that writes refuses an older
   schematic or board. A file of an unknown version, or of a newer version than the
   installed KiCad, gives an error.
   The minimum is KiCad 8.
4. Compare a new rule with real KiCad 8, 9 and 10. Unit tests alone do not show that a
   rule is correct.

### Clean room

The KiCad source code (GPL) gives facts about the file formats and the behavior
only. Do not translate its code. Do not use or open the code of a GPL library (for
example kiutils). The license of this project is MIT.

### Writes

- The server writes S-expression files only through `parsers/sexpr.py` (the
  round-trip parser and serializer, with the lexer rules of KiCad), in the format of
  the file version. Do not use regular expressions for KiCad files.
- Before each write, `utils/kicad_lock.py` refuses a file that KiCad has open.
- The server saves a KiCad file as KiCad 10.0.7 does (`utils/file_save.py`): a
  temporary file in the same folder, then a rename over the file. After an error,
  the file has its old content, not a part of the new content.
- A write through `pcbnew` or kicad-cli is an edit by KiCad. When it writes a newer
  version than the project has, the tool gives a warning.

### Tools

- A tool is an `async` function with `@mcp.tool()` in `tools/`. Its docstring is the
  description for the model, with `Args:` and `Returns:`.
- A tool returns Markdown. An expected error gives "❌ <message>", an other exception
  also the traceback: `error_answer` of `utils/tool_errors.py` gives the two. An
  expected error is an OSError or a `WorkbenchError` of `errors.py`. For a message to
  the user, raise `InputError` (or a subclass of `WorkbenchError`), not a plain
  `ValueError`: the tools give a plain `ValueError` as a defect. A warning starts with
  "⚠️". Give "✅" only when the tool checked the full item. When a result can be
  incomplete, say so.
- Run file reads and subprocesses off the event loop (`asyncio.to_thread`). Use
  `run_kicad_cli_sync` and `run_kicad_python`, not `subprocess` directly: they set the
  character set and find the KiCad programs.
- Read KiCad text files with `utils/kicad_text.py` (UTF-8, as KiCad reads them).
- The minimum Python is 3.10: no syntax of a later version.

### Text

Write all English text in ASD-STE100 Simplified Technical English: docstrings,
comments, messages, commit messages and documentation.

## Architecture

```
src/kicad_workbench/
├── server.py, __main__.py   the FastMCP server; each tool module registers its tools
├── version_check.py         the release notices
├── tools/                   the MCP tools (one module for each area)
├── parsers/                 readers of the KiCad file formats
└── utils/                   KiCad processes, library tables, shared helpers
```

| Module | Content |
|---|---|
| `parsers/sexpr.py` | Round-trip S-expression parser and serializer |
| `parsers/kicad_versions.py`, `kicad_fields.py` | File versions of each KiCad release, field rules |
| `parsers/schematic_parser.py`, `sch_geometry.py`, `sch_junctions.py`, `bus_names.py` | Schematics: items, pin positions, junction rules, bus names |
| `parsers/lib_symbols.py`, `symbol_info.py` | Library symbols, flattened as KiCad places them |
| `parsers/pcb_parser.py`, `pcb_parser_kicad.py`, `board_models.py`, `board_netlist.py` | Boards (as text and through `pcbnew`), 3D models, the netlist of "Update PCB from Schematic" |
| `parsers/check_report_parser.py`, `netlist_parser.py` | ERC and DRC reports, the XML netlist |
| `parsers/sheet_list.py`, `annotation.py` | The sheet list as KiCad builds it, and the annotation check of KiCad |
| `parsers/confirmed_mpns.py` | The model of the confirmed MPNs (the same as in datasheet-lens) |
| `utils/kicad_cli.py`, `kicad_python.py` | kicad-cli and the Python of KiCad as subprocesses |
| `utils/library_tables.py`, `library_lookup.py`, `library_index.py` | Library tables, path variables, library search |
| `utils/kicad_lock.py`, `kicad_text.py`, `user_files.py` | Lock files, text encoding, the cache folder |
| `utils/file_save.py` | The save of a file as KiCad 10.0.7 saves it (a temporary file, then a rename) |
| `utils/file_versions.py` | The check of the file versions of the schematics, the board and the libraries against the installed KiCad |
