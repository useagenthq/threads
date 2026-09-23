import { credentialCases } from "@threads/adapter-testkit";
import { openai } from "../src";

// Lane 09: the credential defaults to secret("OPENAI_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "openai",
  env: "OPENAI_API_KEY",
  slot: (apiKey) => ({
    fallback: [
      openai({
        model: "gpt-5.5",
        contextWindow: 400_000,
        maxOutputTokens: 128_000,
        fetch: offline,
        ...(apiKey === undefined ? {} : { apiKey }),
      }),
    ],
  }),
});
