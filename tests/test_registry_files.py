"""Tests of the files for the MCP Registry (server.json) and Glama (glama.json): the
keys of their schemas, and the version and the name of the package and the README."""

import json
from pathlib import Path

from kicad_workbench import __version__

ROOT = Path(__file__).resolve().parent.parent
# The keys of ServerDetail and Package of the schema of server.json
# (https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json). The
# registry ignores a different key with no error.
SERVER_KEYS = {
    "$schema", "_meta", "description", "icons", "name", "packages", "remotes",
    "repository", "title", "version", "websiteUrl",
}  # fmt: skip
PACKAGE_KEYS = {
    "environmentVariables", "fileSha256", "identifier", "packageArguments",
    "registryBaseUrl", "registryType", "runtimeArguments", "runtimeHint", "transport",
    "version",
}  # fmt: skip


def _json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_the_server_file_has_the_keys_of_its_schema():
    server = _json("server.json")
    assert set(server) <= SERVER_KEYS
    (package,) = server["packages"]
    assert set(package) <= PACKAGE_KEYS
    assert (package["registryType"], package["identifier"]) == ("pypi", "kicad-workbench")
    assert 1 <= len(server["description"]) <= 100  # minLength and maxLength
    assert 1 <= len(server["title"]) <= 100


def test_the_versions_are_the_version_of_the_package():
    server = _json("server.json")
    assert server["version"] == __version__
    assert [package["version"] for package in server["packages"]] == [__version__]


def test_the_readme_gives_the_name_of_the_registry():
    """The MCP Registry finds the name in the README of the PyPI package."""
    first = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
    assert first == f"<!-- mcp-name: {_json('server.json')['name']} -->"


def test_the_glama_file_has_the_keys_of_its_schema():
    """The schema of Glama (https://glama.ai/mcp/schemas/server.json) has only the
    maintainers."""
    assert _json("glama.json") == {
        "$schema": "https://glama.ai/mcp/schemas/server.json",
        "maintainers": ["turbyho"],
    }
