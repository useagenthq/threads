"""`threads.mcp`: MCP servers as agent tools. Needs the `mcp` extra (`threads[mcp]`)."""

from threads.adapters.mcp.server import McpOptions, McpServer, mcp

__all__ = ["McpOptions", "McpServer", "mcp"]
