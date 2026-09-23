import { expect, test } from "bun:test";
import { credentialCases, drain, renderBody } from "@threads/adapter-testkit";
import { memoryContext } from "@threads/core/adapter";
import { anthropic } from "../src";

// Lane 09: the credential defaults to secret("ANTHROPIC_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "anthropic",
  env: "ANTHROPIC_API_KEY",
  slot: (apiKey) => ({
    fallback: [
      anthropic({
        model: "claude-sonnet-5",
        maxTokens: 1024,
        contextWindow: 200_000,
        maxOutputTokens: 64_000,
        fetch: offline,
        ...(apiKey === undefined ? {} : { apiKey }),
      }),
    ],
  }),
});

test("the client uses the key setup resolved, not the env at send time", async () => {
  process.env["ANTHROPIC_API_KEY"] = "sk-lane09-at-setup";
  const seen: (string | null)[] = [];
  const model = anthropic({
    model: "claude-sonnet-5",
    maxTokens: 1024,
    contextWindow: 200_000,
    maxOutputTokens: 64_000,
    fetch: async (input, init) => {
      seen.push(new Headers(init?.headers).get("x-api-key"));
      throw new Error(`no network: ${String(input)}`);
    },
  });
  await model.setup?.();
  delete process.env["ANTHROPIC_API_KEY"];
  const head = {
    adapter: { name: "anthropic", version: "1", settings: {} },
    model: { provider: "anthropic", name: "claude-sonnet-5" },
    params: { max_tokens: 1024 },
    system: "",
    tools: [],
  };
  const body = renderBody([
    head,
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]);
  await drain(model.send({ request_id: "b:e1", body }, memoryContext()));
  expect(seen).toEqual(["sk-lane09-at-setup"]);
});
