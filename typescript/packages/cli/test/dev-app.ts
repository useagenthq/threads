import { agent, devSandbox, scriptedModel, sqlite } from "@threads/core";
import { type Host, host } from "@threads/host";

// A host module whose agent runs in devSandbox(): what `threads start` refuses and `threads dev`
// serves. The dev root comes from the test through THREADS_TEST_DEV_ROOT.

const bot = agent({
  name: "support",
  model: scriptedModel({ responses: [] }),
  // A trivial program stands in for the confinement: this host only has to ready,
  // so the CLI test runs without bubblewrap.
  sandbox: devSandbox({
    root: process.env["THREADS_TEST_DEV_ROOT"] ?? "",
    tool: "/usr/bin/true",
  }),
});

const app: Host = host({
  store: sqlite(process.env["THREADS_TEST_STORE"] ?? ":memory:"),
  agents: { support: bot },
});

export default app;
