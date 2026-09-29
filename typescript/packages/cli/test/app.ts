import { type Host, host } from "@threadsai/host";
import { agent, type ChannelAdapter, scriptedModel, sqlite } from "threadsai";

// A host module as `threadsai dev` loads it: its default export is host({...}). The store path
// comes from the test through THREADS_TEST_STORE.

const bot = agent({
  name: "support",
  model: scriptedModel({ responses: [] }),
});

const slack: ChannelAdapter = {
  agent: "support",
  capabilities: {
    lookup: "none",
    buttons: false,
    edits: false,
    files: false,
    direct_messages: false,
  },
  limits: {},
  secrets: {},
  verify: () => ({ ok: false, error: { code: "unverified", message: "test" } }),
  parse: () => ({ ok: true, value: [] }),
  ack: () => ({ status: 200, headers: {}, body: new Uint8Array() }),
  render: () => [],
  renderText: () => [],
  perform: async () => ({
    status: "delivery_error",
    kind: "permanent",
    sent: "definite_not_sent",
  }),
  lookup: async () => ({ status: "unknown", reason: "test" }),
};

const app: Host = host({
  store: sqlite(process.env["THREADS_TEST_STORE"] ?? ":memory:"),
  agents: { support: bot },
  channels: { slack },
});

export default app;
