"""Main entry point for KiCad Workbench."""


def main() -> None:
    """Run the MCP server. A background thread asks PyPI for a newer release
    (see version_check)."""
    from .server import mcp
    from .version_check import start_update_check

    start_update_check()

    # show_banner=False skips log_server_banner(), which performs a *blocking*
    # httpx.get("https://pypi.org/pypi/fastmcp/json") before stdio_server()
    # opens the transport. That delays the initialize handshake and stalls the
    # client entirely when PyPI is slow or unreachable.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
