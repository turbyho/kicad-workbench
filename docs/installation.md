# Installation

## Requirements

- KiCad 8, 9 or 10, with kicad-cli. The server finds kicad-cli in the standard
  install folder of KiCad and in `PATH`. The environment variable `KICAD_CLI` gives a
  different path.
- Python 3.10 or later.
- A Python that can import the module `pcbnew` of KiCad, for the board tools
  (`list_pcb_footprints`, `get_pcb_statistics`, `find_tracks_by_net`,
  `analyze_pcb_nets`, `analyze_pcb_signal_integrity`, `analyze_pcb_power_integrity`).
  These tools import `pcbnew` in the server process. Without it, they read the board
  file as text: they give no design rules and no board outline.

Other tools also use `pcbnew`: `get_library_footprint`, `check_pin_pad_mapping`,
`check_schematic_pin_pad_mapping`, `check_library_links` and
`setup_pcb_layout`. They run it in a different process, with the Python of KiCad that
the server finds. The environment variable `KICAD_PYTHON` gives a different Python.

Thus the best place for the server is a Python that can import `pcbnew`.

## Linux

The KiCad package of a Linux distribution installs `pcbnew` for the system Python.
Do a check:

```sh
/usr/bin/python3 -c "import pcbnew; print(pcbnew.Version())"
```

Install the server with [pipx](https://pipx.pypa.io/) into a virtual environment of
the system Python. The option `--system-site-packages` lets the environment import
`pcbnew`:

```sh
pipx install --python /usr/bin/python3 --system-site-packages kicad-workbench
```

pipx puts the command `kicad-workbench` into `~/.local/bin`. We tested this procedure
with a wheel of version 0.1.0, KiCad 10.0.6 and Python 3.14 (Manjaro).

Without pipx, make the virtual environment yourself:

```sh
/usr/bin/python3 -m venv --system-site-packages ~/.kicad-workbench/.venv
~/.kicad-workbench/.venv/bin/pip install kicad-workbench
```

The command is then `~/.kicad-workbench/.venv/bin/kicad-workbench`.

## Windows

We did not test this procedure on Windows yet.

KiCad has its own Python with `pcbnew`. It must be Python 3.10 or later:

```bat
"C:\Program Files\KiCad\10.0\bin\python.exe" --version
```

Install the server into that Python, for the user only:

```bat
"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip install --user kicad-workbench
```

Change `10.0` to the version of your KiCad. The MCP client starts the server with
this Python and the arguments `-m kicad_workbench`.

## macOS

We did not test this procedure on macOS yet.

KiCad has its own Python with `pcbnew`. It must be Python 3.10 or later:

```sh
KIPYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
"$KIPYTHON" --version
"$KIPYTHON" -m pip install --user kicad-workbench
```

Use `--user`. Without it, pip writes into the KiCad application, and macOS can then
refuse the signature of the application. The MCP client starts the server with this
Python and the arguments `-m kicad_workbench`.

## A Python with no pcbnew

All tools work, except that the board tools read the board file as text (see
"Requirements"):

```sh
pipx install kicad-workbench
```

## MCP clients

The server is an MCP server on stdio. The examples use the name `kicad`: the tools
are then `mcp__kicad__<tool>` in Claude Code.

### Claude Code

```sh
# Linux (pipx)
claude mcp add --scope user kicad -- kicad-workbench

# Windows (the Python of KiCad)
claude mcp add --scope user kicad -- "C:\Program Files\KiCad\10.0\bin\python.exe" -m kicad_workbench

# macOS (the Python of KiCad)
claude mcp add --scope user kicad -- /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m kicad_workbench
```

### Claude Desktop

The configuration file:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Claude Desktop does not always have the `PATH` of your shell. Give the full path of
the command:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/home/USER/.local/bin/kicad-workbench",
      "args": []
    }
  }
}
```

On Windows and macOS, the command is the Python of KiCad, and `args` is
`["-m", "kicad_workbench"]`.

### Other MCP clients

Set the command `kicad-workbench` with no arguments, or the Python of KiCad with the
arguments `-m kicad_workbench`. Restart the client after a change of its
configuration.

## Environment variables

Set them in the MCP configuration of the server (for example `env` in the JSON file
of Claude Desktop).

| Variable | Effect |
|---|---|
| `KICAD_CLI` | The path of kicad-cli, when the server does not find it |
| `KICAD_PYTHON` | The Python with `pcbnew` for the tools that run it in a different process |
| `KICAD_WORKBENCH_NO_UPDATE_CHECK` | A value other than empty and `0` stops the request to PyPI (see "Release notices") |
| `PARTS_REGISTRY_URL` | The API of the parts registry (default `https://partreel.com/api/v1`) |
| `PARTS_REGISTRY_ASSET_HOSTS` | More hosts (comma list) from which `download_registry_part` can download files |
| `PARTS_REGISTRY_SAVE_ROOTS` | More folders (comma list) into which `download_registry_part` can save files (default: the working folder and the home folder) |

The server reads the KiCad settings (library tables, path variables) from the
settings folder of the installed KiCad. `KICAD_CONFIG_HOME` changes that folder, as
for KiCad.

## Release notices

The server tells the agent about a newer release on PyPI, and about an upgrade while
the server runs. The agent gives each notice to you one time in a session:

- **Newer release.** When the server starts, a background thread gets the newest
  version from PyPI. When the last request that succeeded is younger than 24 hours,
  the server sends no request. The tools read only the stored result, thus no tool
  waits for the network.
- **Restart.** The installed package has a different version than the running
  server. The MCP client starts the server, thus a restart of the client starts the
  new code.

To stop the request to PyPI, set `KICAD_WORKBENCH_NO_UPDATE_CHECK=1`. An editable
install (`pip install -e`) and a source tree in `PYTHONPATH` get no notices.

A defect of the check does not stop a tool or the server. The agent gets the line
"The version check of kicad-workbench failed (...)" one time for each different
defect, and the log gets the details. A cache folder that the server cannot write is
such a defect: the server then sends a request at each start.

## Check the installation

Restart the MCP client, and ask the agent:

> Which KiCad tools do you have?

Then a check of `pcbnew`:

> Analyze the nets of `MyBoard.kicad_pcb`.

When the answer has the warning "pcbnew not available", the server reads the board as
text. Install the server into a Python with `pcbnew`.

## Troubleshooting

### kicad-cli not found

Install KiCad in its standard folder, add the folder of kicad-cli to `PATH`, or set
`KICAD_CLI`. The standard folders:

- Windows: `C:\Program Files\KiCad\<version>\bin\kicad-cli.exe`
- macOS: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`
- Linux: `/usr/bin/kicad-cli`

### pcbnew not found

The board tools import `pcbnew` in the server process: install the server into a
Python with `pcbnew` (see above). For the tools that run `pcbnew` in a different
process (the library footprints, the pin and pad checks, the library links, new
boards), set `KICAD_PYTHON` to the Python of KiCad.

### An edit tool refuses the write

KiCad has the file open. KiCad does not read a change to a file that it has open, and
its next save removes the change. Close the schematic editor or the PCB editor, and
open the file again after the edit. After a crash of KiCad, the lock file
`~<file name>.lck` stays next to the file: open the file in KiCad and close it again,
or delete the lock file. See [editing.md](editing.md).

## Upgrade

```sh
pipx upgrade kicad-workbench
```

With the Python of KiCad, use `-m pip install --user --upgrade kicad-workbench`.
Then restart the MCP client: the client starts the server, and the running server
keeps the old code.

## Uninstall

1. Remove the MCP server from the client. For Claude Code:

   ```sh
   claude mcp remove --scope user kicad
   ```

2. Remove the package:

   ```sh
   pipx uninstall kicad-workbench
   ```

3. Remove the cache folder of the server (the index of the libraries and the state
   of the release check): `~/.cache/kicad-workbench` on Linux,
   `~/Library/Caches/kicad-workbench` on macOS,
   `%LOCALAPPDATA%\kicad-workbench` on Windows.
