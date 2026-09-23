import { credentialCases } from "@threads/adapter-testkit";
import { e2b } from "../src";

// Lane 09: the credential defaults to secret("E2B_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "e2b",
  env: "E2B_API_KEY",
  slot: (apiKey) => ({
    sandbox: e2b({
      fetch: offline,
      ...(apiKey === undefined ? {} : { apiKey }),
    }),
  }),
});
