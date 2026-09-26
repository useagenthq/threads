import { agent, devSandbox, scriptedModel, sqlite } from "@threads/core";
import { type Host, host } from "@threads/host";

// A host module whose agent runs in devSandbox(): what `threads start` refuses and `threads dev`
// serves. The dev root comes from the test through THREADS_TEST_DEV_ROOT.

const bot = agent({
  name: "support",
  model: scriptedModel({ responses: [] }),
  sandbox: devSandbox({ root: process.env["THREADS_TEST_DEV_ROOT"] ?? "" }),
});

const app: Host = host({
  store: sqlite(process.env["THREADS_TEST_STORE"] ?? ":memory:"),
  agents: { support: bot },
});

export default app;
