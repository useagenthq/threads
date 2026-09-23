import { credentialCases } from "@threads/adapter-testkit";
import { zep } from "../src";

// Lane 09: the credential defaults to secret("ZEP_API_KEY") and is resolved at setup.

/** No network in these tests: a request is a failure the test would see. */
const offline = async (): Promise<Response> => {
  throw new Error("no network in credential tests");
};

credentialCases({
  factory: "zep",
  env: "ZEP_API_KEY",
  slot: (apiKey) => ({
    memory: zep({
      fetch: offline,
      ...(apiKey === undefined ? {} : { apiKey }),
    }),
  }),
});
