"""An `--agent` module whose agent has an MCP server that refuses connections: keyless CI
still compares what it can, with no connection attempt."""

from cli.eval_modules.eval_agents import support

agents = [support(with_mcp=True)]
