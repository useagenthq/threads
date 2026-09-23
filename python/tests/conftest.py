"""Suite-wide setup: no test can reach a real model provider (AGENTS.md, Tests)."""

from threads.loop.guard import block_model_requests

block_model_requests()
