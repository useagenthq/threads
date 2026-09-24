// An `--agent` module that reads a required env variable at import time: in keyless CI it
// throws, and `threads eval` reports it (exit 2) naming the module.

const url = process.env["JIRA_MCP_URL_FOR_THREADS_TESTS"];
if (url === undefined)
  throw new Error("JIRA_MCP_URL_FOR_THREADS_TESTS is not set");

const none: readonly never[] = [];
export default none;
