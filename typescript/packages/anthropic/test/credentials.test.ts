import { credentialCases } from "@threads/adapter-testkit";
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
