"""`threadsai.mcp`: MCP servers as agent tools. Needs the `mcp` extra (`threadsai[mcp]`)."""

from threadsai.adapters.mcp.server import McpOptions, McpServer, mcp

__all__ = ["McpOptions", "McpServer", "mcp"]
