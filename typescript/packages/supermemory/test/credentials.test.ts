import { credentialCases } from "@threads/adapter-testkit";
import { supermemory } from "../src";

// Lane 09: the credential defaults to secret("SUPERMEMORY_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "supermemory",
  env: "SUPERMEMORY_API_KEY",
  slot: (apiKey) => ({
    memory: supermemory({
      fetch: offline,
      ...(apiKey === undefined ? {} : { apiKey }),
    }),
  }),
});
