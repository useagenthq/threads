"""An `--agent` module that reads a required env variable at import time: in keyless CI it
raises, and `threads eval` reports it (exit 2) naming the module."""

import os

if "JIRA_MCP_URL_FOR_THREADS_TESTS" not in os.environ:
    raise RuntimeError("JIRA_MCP_URL_FOR_THREADS_TESTS is not set")

agents = []
