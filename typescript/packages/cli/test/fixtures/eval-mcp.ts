import type { Agent } from "@threads/core";
import { support } from "./eval-agents";

// An `--agent` module whose agent has an MCP server that refuses connections: keyless CI must
// still compare what it can, with no connection attempt.

const withMcp: Agent = support(true);
export default withMcp;
